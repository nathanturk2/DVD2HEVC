"""Bounded Phase 7 prototypes for interleaved branching DVD cells."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .atomic import write_json_atomic
from .audio import prefetch_compact_audio_cells
from .cadence import analyze_cadence
from .extract import (
    extract_domain_sector_segment_sets,
    extract_title_sector_range,
    extract_title_sector_segments,
    inspect_vob_file,
)
from .encoders import rate_control_label, rate_control_slug
from .interleaved import remap_vobus_to_segments
from .native import find_dvdinspect, inspect_physical_graph
from .phase2 import analyze_vobu_budgets
from .pipeline import (
    PipelineError,
    choose_cell_attempt,
    encode_cell_hevc,
    source_identity,
    validate_compact_encoded_cell,
    validate_vobu_random_access,
    validate_repacked_cell,
    vobu_keyframe_ticks,
)
from .repack import repack_cell_hevc
from .tools import discover_tools
from .transport import read_video_pes
from .vob import DVD_SECTOR_SIZE


Progress = Callable[[str], None]


def _interleaved_settings(
    *,
    encoder: str,
    preset: str,
    quality_values: tuple[int | float | str, ...],
    cadence: str,
    ambiguous_cadence: str,
    prefer_compact_input: bool = False,
) -> dict[str, Any]:
    return {
        "encoder": encoder,
        "preset": preset,
        "quality_values": list(quality_values),
        "cadence": cadence,
        "ambiguous_cadence": ambiguous_cadence,
        "intermediate_policy": (
            "compact-input-v1" if prefer_compact_input else "sector-preserving-v1"
        ),
        "keyframe_policy": "dvd-vobu-idr-v1",
        "program_stream_map_policy": "dvd-vobu-psm-v1",
    }


def _cached_interleaved_artifact_exists(row: dict[str, Any]) -> bool:
    repacked_value = row.get("repacked_cell")
    if repacked_value:
        return Path(repacked_value).is_file()
    if row.get("layout_mode") != "compact-input":
        return False
    selected = str(row.get("selected_quality")).lower()
    attempt = next(
        (
            value for value in row.get("attempts", [])
            if str((value.get("encode") or {}).get("quality")).lower() == selected
        ),
        None,
    )
    return bool(attempt and Path(attempt["encoded"]).is_file())


def _load_cached_interleaved_report(
    report_path: Path,
    *,
    identity: dict[str, Any],
    settings: dict[str, Any],
    pgc_number: int,
    pgc_cell_number: int,
) -> dict[str, Any] | None:
    if not report_path.is_file():
        return None
    try:
        cached = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not (
        cached.get("status") == "passed"
        and cached.get("source_identity") == identity
        and cached.get("settings") == settings
        and int(cached.get("pgc", 0)) == pgc_number
        and int(cached.get("pgc_cell", 0)) == pgc_cell_number
        and bool(cached.get("cells"))
        and all(
            _cached_interleaved_artifact_exists(row)
            for row in cached.get("cells", [])
        )
        and (
            all(
                row.get("layout_mode") == "compact-input"
                for row in cached.get("cells", [])
            )
            or (
                cached.get("summary", {}).get("selected_readback_matches") is True
                and cached.get("summary", {}).get("alternate_sectors_unchanged") is True
            )
        )
    ):
        return None
    return cached


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    write_json_atomic(path, value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def split_repacked_cell_into_physical_span(
    source_span: Path,
    logical_replacement: Path,
    *,
    span_first_sector: int,
    span_last_sector: int,
    segments: list[dict[str, int]],
    destination: Path,
) -> dict[str, Any]:
    """Patch only selected extents and prove every alternate sector is unchanged."""
    expected_span = (span_last_sector - span_first_sector + 1) * DVD_SECTOR_SIZE
    if source_span.stat().st_size != expected_span:
        raise PipelineError("Source physical span size does not match its sector range")
    expected_logical = sum(int(row["sector_count"]) for row in segments) * DVD_SECTOR_SIZE
    if logical_replacement.stat().st_size != expected_logical:
        raise PipelineError("Logical replacement size does not match its segment plan")
    previous_end = span_first_sector - 1
    for segment in segments:
        first = int(segment["start_sector"])
        last = int(segment["last_sector"])
        if first <= previous_end or first < span_first_sector or last > span_last_sector or last < first:
            raise PipelineError("Physical replacement segments are invalid or overlapping")
        previous_end = last

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        shutil.copyfile(source_span, temporary)
        with logical_replacement.open("rb") as replacement, temporary.open("r+b") as output:
            for segment in segments:
                byte_count = int(segment["sector_count"]) * DVD_SECTOR_SIZE
                data = replacement.read(byte_count)
                if len(data) != byte_count:
                    raise PipelineError("Logical replacement ended inside a physical segment")
                output.seek((int(segment["start_sector"]) - span_first_sector) * DVD_SECTOR_SIZE)
                output.write(data)
            if replacement.read(1):
                raise PipelineError("Logical replacement exceeds its physical segments")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    selected_source = hashlib.sha256()
    selected_output = hashlib.sha256()
    alternate_source = hashlib.sha256()
    alternate_output = hashlib.sha256()
    changed_selected_sectors = 0
    selected_sector_count = 0
    segment_index = 0
    with source_span.open("rb") as source, destination.open("rb") as output:
        for local_sector in range(span_last_sector - span_first_sector + 1):
            absolute_sector = span_first_sector + local_sector
            while (
                segment_index < len(segments)
                and absolute_sector > int(segments[segment_index]["last_sector"])
            ):
                segment_index += 1
            selected = (
                segment_index < len(segments)
                and int(segments[segment_index]["start_sector"]) <= absolute_sector
                <= int(segments[segment_index]["last_sector"])
            )
            source_sector = source.read(DVD_SECTOR_SIZE)
            output_sector = output.read(DVD_SECTOR_SIZE)
            if selected:
                selected_sector_count += 1
                selected_source.update(source_sector)
                selected_output.update(output_sector)
                if source_sector != output_sector:
                    changed_selected_sectors += 1
            else:
                alternate_source.update(source_sector)
                alternate_output.update(output_sector)
                if source_sector != output_sector:
                    raise PipelineError(f"Unselected physical sector {absolute_sector} changed")

    readback = hashlib.sha256()
    with destination.open("rb") as output:
        for segment in segments:
            output.seek((int(segment["start_sector"]) - span_first_sector) * DVD_SECTOR_SIZE)
            remaining = int(segment["sector_count"]) * DVD_SECTOR_SIZE
            while remaining:
                chunk = output.read(min(remaining, 8 * 1024 * 1024))
                if not chunk:
                    raise PipelineError("Patched physical span ended during selected-sector readback")
                readback.update(chunk)
                remaining -= len(chunk)
    logical_hash = _sha256(logical_replacement)
    readback_hash = readback.hexdigest()
    if logical_hash != readback_hash:
        raise PipelineError("Selected physical extents do not hash back to the logical replacement")
    return {
        "source_span": str(source_span.resolve()),
        "logical_replacement": str(logical_replacement.resolve()),
        "destination": str(destination),
        "span_first_sector": span_first_sector,
        "span_last_sector": span_last_sector,
        "span_sectors": span_last_sector - span_first_sector + 1,
        "selected_sectors": selected_sector_count,
        "alternate_sectors": span_last_sector - span_first_sector + 1 - selected_sector_count,
        "changed_selected_sectors": changed_selected_sectors,
        "selected_source_sha256": selected_source.hexdigest(),
        "selected_output_sha256": selected_output.hexdigest(),
        "logical_replacement_sha256": logical_hash,
        "selected_readback_sha256": readback_hash,
        "alternate_source_sha256": alternate_source.hexdigest(),
        "alternate_output_sha256": alternate_output.hexdigest(),
        "alternate_sectors_unchanged": alternate_source.hexdigest() == alternate_output.hexdigest(),
        "selected_readback_matches": logical_hash == readback_hash,
        "vob_stats": inspect_vob_file(destination),
    }


def prototype_interleaved_cell(
    plan_path: Path,
    *,
    pgc_number: int,
    pgc_cell_number: int,
    workspace: Path,
    quality_values: tuple[int | float | str, ...] = (27,),
    preset: str = "p6",
    encoder: str = "hevc_nvenc",
    cadence: str = "auto",
    ambiguous_cadence: str = "deinterlace50",
    original_vobus: list[dict[str, Any]] | None = None,
    identity_value: dict[str, Any] | None = None,
    preextracted_logical: dict[str, Any] | None = None,
    preextracted_span: dict[str, Any] | None = None,
    allow_compact_expansion: bool = False,
    prefer_compact_input: bool = False,
    encode_lock: Any | None = None,
    validation_lock: Any | None = None,
    progress: Progress = print,
) -> dict[str, Any]:
    """Encode and validate one planned interleaved branch cell.

    Managed compact builds use a video-only compact intermediate.  The older
    sector-preserving path remains available to the low-level prototype for
    diagnostics and non-compact experiments.
    """
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "dvd2hevc-interleaved-unit-plan-v0":
        raise PipelineError("Unsupported interleaved-unit plan schema")
    if not plan.get("segmented_extraction_ready"):
        raise PipelineError("Interleaved-unit plan did not pass segmented extraction")
    matches = [
        row for row in plan.get("cells", [])
        if int(row["pgc"]) == pgc_number and int(row["pgc_cell"]) == pgc_cell_number
    ]
    if len(matches) != 1:
        raise PipelineError("Requested PGC cell is not unique in the physical plan")
    cell = matches[0]
    source = Path(plan["source"]).resolve()
    vts_number = int(plan["vts"])
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = workspace / "interleaved-cell-report.json"
    identity = identity_value or source_identity(source)
    settings = _interleaved_settings(
        encoder=encoder,
        preset=preset,
        quality_values=quality_values,
        cadence=cadence,
        ambiguous_cadence=ambiguous_cadence,
        prefer_compact_input=prefer_compact_input,
    )
    tools = discover_tools()
    ffmpeg = tools.get("ffmpeg")
    ffprobe = tools.get("ffprobe")
    dvdinspect = find_dvdinspect()
    if not ffmpeg or not ffprobe or not dvdinspect:
        raise PipelineError("ffmpeg, ffprobe, and dvdinspect are required")

    report: dict[str, Any] = {
        "schema": "dvd2hevc-interleaved-cell-prototype-v0",
        "status": "running",
        "source": str(source),
        "source_identity": identity,
        "domain": "title",
        "plan": str(plan_path),
        "vts": vts_number,
        "pgc": pgc_number,
        "pgc_cell": pgc_cell_number,
        "cell": cell,
        "settings": settings,
    }

    cached = _load_cached_interleaved_report(
        report_path,
        identity=identity,
        settings=settings,
        pgc_number=pgc_number,
        pgc_cell_number=pgc_cell_number,
    )
    if cached is not None:
        progress(f"Reusing validated physical task PGC {pgc_number} cell {pgc_cell_number}")
        return cached
    _atomic_json(report_path, report)
    try:
        if original_vobus is None:
            graph = inspect_physical_graph(source, dvdinspect, timeout=300)
            vts = next(row for row in graph["title_sets"] if int(row["vts"]) == vts_number)
            original_vobus = vts["title_vobu_map"]["vobus"]
        logical_vobus = remap_vobus_to_segments(original_vobus, cell["segments"])
        if not logical_vobus:
            raise PipelineError("No VOBUs map into the selected interleaved extents")

        source_logical = workspace / "source-logical.vob"
        if preextracted_logical is None:
            progress("Extracting discontinuous branch extents")
            extraction = extract_title_sector_segments(
                source,
                vts=vts_number,
                segments=cell["segments"],
                destination=source_logical,
            )
        else:
            if Path(preextracted_logical.get("destination", "")).resolve() != source_logical:
                raise PipelineError("Pre-extracted logical cell has the wrong destination")
            expected_segments = [
                (int(row["start_sector"]), int(row["last_sector"]))
                for row in cell["segments"]
            ]
            actual_segments = [
                (int(row["source_first_sector"]), int(row["source_last_sector"]))
                for row in preextracted_logical.get("segments", [])
            ]
            if actual_segments != expected_segments or not source_logical.is_file():
                raise PipelineError("Pre-extracted logical cell does not match its physical plan")
            progress("Using batch-extracted discontinuous branch extents")
            extraction = preextracted_logical
        if int(extraction["vob_stats"]["video_pes_packets"]) == 0:
            progress("Physical task contains no video PES; preserving it byte-for-byte")
            repacked = workspace / "repacked-logical.vob"
            shutil.copyfile(source_logical, repacked)
            if preextracted_span is None:
                source_span = workspace / "source-physical-span.vob"
                span_extraction = extract_title_sector_range(
                    source,
                    vts=vts_number,
                    first_sector=int(cell["first_sector"]),
                    last_sector=int(cell["last_sector"]),
                    destination=source_span,
                )
            else:
                source_span = Path(preextracted_span["destination"]).resolve()
                span_extraction = preextracted_span
            split_back = split_repacked_cell_into_physical_span(
                source_span,
                repacked,
                span_first_sector=int(cell["first_sector"]),
                span_last_sector=int(cell["last_sector"]),
                segments=cell["segments"],
                destination=workspace / "patched-physical-span.vob",
            )
            structure = extraction["vob_stats"]
            validation = {
                "preserved_without_video": True,
                "structure": structure,
                "source_video": {"nb_read_frames": 0},
                "output_video": {"nb_read_frames": 0},
                "source_audio_hash": None,
                "output_audio_hash": None,
                "complete_video_decode": True,
            }
            cell_report = {
                "name": (
                    f"vts{vts_number:02d}-pgc{pgc_number:02d}-"
                    f"cell{pgc_cell_number:02d}-interleaved"
                ),
                "cell": cell,
                "physical_segments": cell["segments"],
                "source_cell": str(source_logical),
                "repacked_cell": str(repacked),
                "vobu_count": len(logical_vobus),
                "attempts": [],
                "selected_quality": "copy-no-video",
                "layout_mode": "sector-preserving",
                "repack": {"program_stream_maps": 0, "destination": str(repacked)},
                "validation": validation,
                "split_back": split_back,
            }
            report.update({
                "status": "passed",
                "logical_vobus": logical_vobus,
                "extraction": extraction,
                "selected_cadence": "copy-no-video",
                "attempts": [],
                "repack": cell_report["repack"],
                "validation": validation,
                "span_extraction": span_extraction,
                "split_back": split_back,
                "cells": [cell_report],
                "summary": {
                    "segments": len(cell["segments"]),
                    "vobus": len(logical_vobus),
                    "source_frames": 0,
                    "output_frames": 0,
                    "audio_hashes_match": True,
                    "alternate_sectors_unchanged": split_back["alternate_sectors_unchanged"],
                    "selected_readback_matches": split_back["selected_readback_matches"],
                },
            })
            _atomic_json(report_path, report)
            return report
        selected_cadence = cadence
        cadence_analysis: dict[str, Any] | None = None
        if cadence == "auto":
            cadence_analysis = analyze_cadence(
                source_logical,
                ffmpeg=ffmpeg,
                duration_seconds=float(cell.get("duration_ticks", 0)) / 90000.0,
            )
            classification = cadence_analysis["classification"]
            selected_cadence = (
                "progressive" if classification == "progressive"
                else "deinterlace50" if classification == "interlaced"
                else ambiguous_cadence
            )
        if selected_cadence not in {"progressive", "deinterlace50"}:
            raise PipelineError(f"Unsupported {encoder} cadence result: {selected_cadence}")

        attempts: list[dict[str, Any]] = []
        attempt_packets: list[Any] = []
        repack_result: dict[str, Any] | None = None
        selected_budget: dict[str, Any] | None = None
        selected_packets = None
        selected_attempt_index: int | None = None
        compact_expansion = False
        logical_last_sector = int(extraction["sector_count"]) - 1
        for quality in quality_values:
            slug = rate_control_slug(quality)
            label = rate_control_label(quality)
            encoded = workspace / f"encoded-{slug}.ts"
            progress(f"Encoding logical branch cell at {label}")
            with (encode_lock if encode_lock is not None else nullcontext()):
                encode = encode_cell_hevc(
                    source_logical,
                    encoded,
                    ffmpeg=ffmpeg,
                    quality=quality,
                    preset=preset,
                    threads=4,
                    log=workspace / f"encode-{slug}.log",
                    encoder=encoder,
                    cadence_mode=selected_cadence,
                    force_keyframe_ticks=vobu_keyframe_ticks(logical_vobus),
                )
            with (validation_lock if validation_lock is not None else nullcontext()):
                packets = read_video_pes(encoded)
                budget = analyze_vobu_budgets(
                    source_logical,
                    cell_first_sector=0,
                    cell_last_sector=logical_last_sector,
                    vobus=logical_vobus,
                    encoded_pes=packets,
                )
                idr = validate_vobu_random_access(packets, budget)
            attempt = {"encode": encode, "encoded": str(encoded), "budget": budget, "idr_alignment": idr}
            attempts.append(attempt)
            attempt_packets.append(packets)
            if prefer_compact_input:
                if (
                    int(budget.get("unassigned_pes_count") or 0) == 0
                    and int(idr.get("failures") or 0) == 0
                ):
                    selected_packets = packets
                    selected_budget = budget
                    selected_attempt_index = len(attempts) - 1
                    compact_expansion = True
                    break
                progress(f"{label} did not produce a complete random-access HEVC cell")
                continue
            choice = choose_cell_attempt([row["budget"] for row in attempts], minimum_headroom=4096)
            if choice is None:
                progress(f"{label} did not leave safe headroom in every selected VOBU")
                continue
            selected_packets = attempt_packets[choice]
            selected_budget = attempts[choice]["budget"]
            selected_attempt_index = choice
            break
        if selected_budget is None and allow_compact_expansion and attempts:
            candidate = attempts[0]
            if (
                int(candidate["budget"].get("unassigned_pes_count") or 0) == 0
                and int(candidate["idr_alignment"].get("failures") or 0) == 0
            ):
                selected_budget = candidate["budget"]
                selected_packets = attempt_packets[0]
                selected_attempt_index = 0
                compact_expansion = True
                progress(
                    f"Keeping exact {rate_control_label(quality_values[0])}; "
                    "the compact layout will enlarge its overflowing VOBUs"
                )
        if selected_budget is None or selected_packets is None or selected_attempt_index is None:
            raise PipelineError("No configured video rate control produced usable interleaved VOBUs")

        progress("Validating logical HEVC cell")
        with (validation_lock if validation_lock is not None else nullcontext()):
            if compact_expansion:
                validation = validate_compact_encoded_cell(
                    source_logical, Path(attempts[selected_attempt_index]["encoded"]),
                    ffmpeg=ffmpeg, ffprobe=ffprobe, decode_log=workspace / "decode.log",
                    expected_frame_multiplier=2 if selected_cadence == "deinterlace50" else 1,
                )
                span_extraction = None
                split_back = None
            else:
                repacked = workspace / "repacked-logical.vob"
                repack_result = repack_cell_hevc(
                    source_logical,
                    repacked,
                    cell_first_sector=0,
                    cell_last_sector=logical_last_sector,
                    vobus=logical_vobus,
                    encoded_pes=selected_packets,
                    psm_policy="every-video-vobu",
                )
                validation = validate_repacked_cell(
                    source_logical,
                    Path(repack_result["destination"]),
                    expected_vobus=len(logical_vobus),
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    decode_log=workspace / "decode.log",
                    expected_frame_multiplier=2 if selected_cadence == "deinterlace50" else 1,
                    expected_program_stream_maps=sum(
                        int(row["packet_count"]) > 0 for row in selected_budget["vobus"]
                    ),
                )
                if preextracted_span is None:
                    source_span = workspace / "source-physical-span.vob"
                    progress("Extracting original physical span and splitting the replacement back")
                    span_extraction = extract_title_sector_range(
                        source,
                        vts=vts_number,
                        first_sector=int(cell["first_sector"]),
                        last_sector=int(cell["last_sector"]),
                        destination=source_span,
                    )
                else:
                    source_span = Path(preextracted_span["destination"]).resolve()
                    span_extraction = preextracted_span
                    progress("Using batch-extracted physical span and splitting the replacement back")
                split_back = split_repacked_cell_into_physical_span(
                    source_span,
                    Path(repack_result["destination"]),
                    span_first_sector=int(cell["first_sector"]),
                    span_last_sector=int(cell["last_sector"]),
                    segments=cell["segments"],
                    destination=workspace / "patched-physical-span.vob",
                )
        report.update({
            "status": "passed",
            "logical_vobus": logical_vobus,
            "extraction": extraction,
            "cadence_analysis": cadence_analysis,
            "selected_cadence": selected_cadence,
            "attempts": attempts,
            "repack": repack_result,
            "validation": validation,
            "span_extraction": span_extraction,
            "split_back": split_back,
            "cells": [{
                "name": (
                    f"vts{vts_number:02d}-pgc{pgc_number:02d}-"
                    f"cell{pgc_cell_number:02d}-interleaved"
                ),
                "cell": cell,
                "physical_segments": cell["segments"],
                "source_cell": str(source_logical),
                "repacked_cell": None if compact_expansion else repack_result["destination"],
                "layout_mode": "compact-input" if compact_expansion else "sector-preserving",
                "vobu_count": len(logical_vobus),
                "attempts": attempts,
                "selected_quality": attempts[selected_attempt_index]["encode"]["quality"],
                "repack": repack_result,
                "validation": validation,
                "split_back": split_back,
            }],
            "summary": {
                "segments": len(cell["segments"]),
                "vobus": len(logical_vobus),
                "source_frames": int(validation["source_video"]["nb_read_frames"]),
                "output_frames": int(validation["output_video"]["nb_read_frames"]),
                "audio_hashes_match": validation["source_audio_hash"] == validation["output_audio_hash"],
                "alternate_sectors_unchanged": (
                    split_back["alternate_sectors_unchanged"] if split_back else None
                ),
                "selected_readback_matches": (
                    split_back["selected_readback_matches"] if split_back else None
                ),
            },
        })
        _atomic_json(report_path, report)
        progress(f"Interleaved cell round trip passed: {report_path}")
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        _atomic_json(report_path, report)
        raise


def convert_interleaved_vts(
    plan_path: Path,
    *,
    workspace: Path,
    quality_values: tuple[int | float | str, ...] = (27,),
    preset: str = "p6",
    encoder: str = "hevc_nvenc",
    cadence: str = "auto",
    ambiguous_cadence: str = "deinterlace50",
    allow_compact_expansion: bool = False,
    prefer_compact_input: bool = False,
    pipeline_depth: int = 1,
    audio_prefetch_root: Path | None = None,
    audio_policy: Path | None = None,
    stereo_audio_bitrate: int = 256_000,
    mono_audio_bitrate: int = 128_000,
    audio_workers: int = 2,
    progress: Progress = print,
) -> dict[str, Any]:
    """Run every unique physical task in an interleaved VTS, resumably.

    A managed pipeline depth greater than one overlaps one hardware encode with
    one CPU validation.  Separate locks deliberately prevent concurrent NVENC
    sessions and concurrent full-cell validation reads.
    """
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "dvd2hevc-interleaved-unit-plan-v0":
        raise PipelineError("Unsupported interleaved-unit plan schema")
    if not plan.get("conversion_ready"):
        raise PipelineError("Interleaved VTS plan is not ready for complete conversion")
    tasks = list(plan.get("conversion_tasks", []))
    if not tasks:
        raise PipelineError("Interleaved VTS plan has no unique conversion tasks")
    source = Path(plan["source"]).resolve()
    vts_number = int(plan["vts"])
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    status_path = workspace / "status.json"
    combined_path = workspace / f"vts{vts_number:02d}-conversion-report.json"
    started = datetime.now().astimezone().isoformat()

    def write_status(
        state: str, step: str, message: str, completed: int, output: str = ""
    ) -> None:
        _atomic_json(status_path, {
            "schema": "dvd2hevc-phase7-vts-status-v0",
            "state": state,
            "step": step,
            "message": message,
            "source": str(source),
            "plan": str(plan_path),
            "vts": vts_number,
            "encoder": encoder,
            "work_root": str(workspace),
            "completed_tasks": completed,
            "total_tasks": len(tasks),
            "output": output,
            "started": started,
            "updated": datetime.now().astimezone().isoformat(),
            "process_id": os.getpid(),
        })

    write_status("running", "physical-graph", "Reading the source VTS once", 0)
    dvdinspect = find_dvdinspect()
    if not dvdinspect:
        raise PipelineError("dvdinspect is required")
    graph = inspect_physical_graph(source, dvdinspect, timeout=300)
    vts = next(row for row in graph["title_sets"] if int(row["vts"]) == vts_number)
    original_vobus = vts["title_vobu_map"]["vobus"]
    identity = source_identity(source)
    settings = _interleaved_settings(
        encoder=encoder,
        preset=preset,
        quality_values=quality_values,
        cadence=cadence,
        ambiguous_cadence=ambiguous_cadence,
        prefer_compact_input=prefer_compact_input,
    )
    task_reports: list[dict[str, Any]] = []
    task_report_paths: list[str] = []
    audio_prefetch_executor: ThreadPoolExecutor | None = None
    audio_prefetch_future = None
    try:
        cells_by_key = {
            (int(row["pgc"]), int(row["pgc_cell"])): row
            for row in plan.get("cells", [])
        }
        batch_requests: list[tuple[list[tuple[int, int]], Path]] = []
        request_roles: dict[Path, tuple[tuple[int, int], str]] = {}
        span_aliases: set[tuple[int, int]] = set()
        for task in tasks:
            key = (int(task["pgc"]), int(task["pgc_cell"]))
            task_workspace = workspace / str(task["workspace"])
            task_workspace.mkdir(parents=True, exist_ok=True)
            cached = _load_cached_interleaved_report(
                task_workspace / "interleaved-cell-report.json",
                identity=identity,
                settings=settings,
                pgc_number=key[0],
                pgc_cell_number=key[1],
            )
            if cached is not None:
                continue
            cell = cells_by_key.get(key)
            if cell is None:
                raise PipelineError(f"Physical task {key} has no matching planned cell")
            segments = [
                (int(row["start_sector"]), int(row["last_sector"]))
                for row in cell.get("segments", task.get("segments", []))
            ]
            logical_path = (task_workspace / "source-logical.vob").resolve()
            batch_requests.append((segments, logical_path))
            request_roles[logical_path] = (key, "logical")
            first_sector = int(cell["first_sector"])
            last_sector = int(cell["last_sector"])
            if segments == [(first_sector, last_sector)]:
                span_aliases.add(key)
            elif not prefer_compact_input:
                span_path = (task_workspace / "source-physical-span.vob").resolve()
                batch_requests.append(([(first_sector, last_sector)], span_path))
                request_roles[span_path] = (key, "span")

        preextracted_logical: dict[tuple[int, int], dict[str, Any]] = {}
        preextracted_span: dict[tuple[int, int], dict[str, Any]] = {}
        if batch_requests:
            write_status(
                "running",
                "batch-extraction",
                f"Extracting {len(batch_requests)} physical task artifact(s) in one VTS pass",
                0,
            )
            progress(
                f"Batch-extracting {len(batch_requests)} physical task artifact(s) "
                f"from VTS {vts_number:02d} in one ISO pass"
            )
        for extraction in extract_domain_sector_segment_sets(
            source,
            domain="title",
            vts=vts_number,
            requests=batch_requests,
        ):
            destination = Path(extraction["destination"]).resolve()
            key, role = request_roles[destination]
            if role == "logical":
                preextracted_logical[key] = extraction
            else:
                preextracted_span[key] = extraction
        for key in span_aliases:
            preextracted_span[key] = preextracted_logical[key]

        if audio_prefetch_root is not None:
            prefetch_cells = []
            for task in tasks:
                key = (int(task["pgc"]), int(task["pgc_cell"]))
                task_workspace = workspace / str(task["workspace"])
                prefetch_cells.append({
                    "name": (
                        f"vts{vts_number:02d}-pgc{key[0]:02d}-"
                        f"cell{key[1]:02d}-interleaved"
                    ),
                    "source_cell": str((task_workspace / "source-logical.vob").resolve()),
                })
            audio_prefetch_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dvd2hevc-audio-prefetch"
            )
            audio_prefetch_future = audio_prefetch_executor.submit(
                prefetch_compact_audio_cells,
                prefetch_cells,
                audio_prefetch_root,
                vts=vts_number,
                stereo_bitrate=stereo_audio_bitrate,
                mono_bitrate=mono_audio_bitrate,
                workers=audio_workers,
                audio_policy=audio_policy,
            )

        worker_count = min(len(tasks), 2, max(1, int(pipeline_depth)))
        encode_lock = threading.Lock()
        validation_lock = threading.Lock()

        def convert_task(index: int, task: dict[str, Any]) -> tuple[int, dict[str, Any], Path]:
            task_key = (int(task["pgc"]), int(task["pgc_cell"]))
            label = f"task {index}/{len(tasks)} PGC {task['pgc']} cell {task['pgc_cell']}"
            progress(f"[{index}/{len(tasks)}] Converting PGC {task['pgc']} cell {task['pgc_cell']}")
            task_workspace = workspace / str(task["workspace"])
            result = prototype_interleaved_cell(
                plan_path,
                pgc_number=int(task["pgc"]),
                pgc_cell_number=int(task["pgc_cell"]),
                workspace=task_workspace,
                quality_values=quality_values,
                preset=preset,
                encoder=encoder,
                cadence=cadence,
                ambiguous_cadence=ambiguous_cadence,
                original_vobus=original_vobus,
                identity_value=identity,
                preextracted_logical=preextracted_logical.get(task_key),
                preextracted_span=preextracted_span.get(task_key),
                allow_compact_expansion=allow_compact_expansion,
                prefer_compact_input=prefer_compact_input,
                encode_lock=encode_lock if worker_count > 1 else None,
                validation_lock=validation_lock if worker_count > 1 else None,
                progress=progress,
            )
            return index, result, task_workspace / "interleaved-cell-report.json"

        completed_by_index: dict[int, tuple[dict[str, Any], Path]] = {}
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="dvd2hevc-video-pipeline"
        ) as executor:
            futures = {
                executor.submit(convert_task, index, task): index
                for index, task in enumerate(tasks, start=1)
            }
            completed_count = 0
            for future in as_completed(futures):
                try:
                    index, result, report_path = future.result()
                except BaseException:
                    for pending in futures:
                        pending.cancel()
                    raise
                completed_count += 1
                completed_by_index[index] = (result, report_path)
                write_status(
                    "running", f"task-{index:03d}",
                    f"Passed task {index}/{len(tasks)}", completed_count,
                )

        for index in sorted(completed_by_index):
            result, report_path = completed_by_index[index]
            task_reports.append(result)
            task_report_paths.append(str(report_path.resolve()))

        audio_prefetch_result = None
        if audio_prefetch_future is not None:
            audio_prefetch_result = audio_prefetch_future.result()
        if audio_prefetch_executor is not None:
            audio_prefetch_executor.shutdown(wait=True, cancel_futures=True)
            audio_prefetch_executor = None

        cells = [report["cells"][0] for report in task_reports]
        cells.sort(key=lambda row: int(row["physical_segments"][0]["start_sector"]))
        intervals: list[tuple[int, int, str]] = []
        for cell in cells:
            intervals.extend(
                (int(segment["start_sector"]), int(segment["last_sector"]), str(cell["name"]))
                for segment in cell["physical_segments"]
            )
        intervals.sort()
        previous_end = -1
        for first, last, name in intervals:
            if first <= previous_end:
                raise PipelineError(f"Combined physical task overlap at {first}-{last}: {name}")
            if first != previous_end + 1:
                raise PipelineError(f"Combined physical task gap before sector {first}")
            previous_end = last
        expected_last = max(int(row["last_sector"]) for row in vts["title_cell_addresses"])
        if previous_end != expected_last:
            raise PipelineError(
                f"Combined physical coverage ends at {previous_end}; expected {expected_last}"
            )
        actual_vobus = sum(int(cell["vobu_count"]) for cell in cells)
        expected_vobus = int(vts["title_vobu_map"]["count"])
        if actual_vobus != expected_vobus:
            raise PipelineError(
                f"Combined VOBU coverage mismatch: actual={actual_vobus}, expected={expected_vobus}"
            )
        combined = {
            "schema": "dvd2hevc-interleaved-vts-conversion-v0",
            "status": "passed",
            "source": str(source),
            "source_identity": identity,
            "domain": "title",
            "vts": vts_number,
            "plan": str(plan_path),
            "settings": settings,
            "pipeline": {
                "workers": worker_count,
                "encoder_sessions": 1,
                "validation_workers": 1,
                "mode": (
                    "encode-validation-overlap-v1"
                    if worker_count > 1 else "serial-encode-validation-v1"
                ),
                "validation_policy": "random-access-and-full-decode-per-physical-cell",
                "validation_overlap": worker_count > 1,
                "audio_prefetch": bool(audio_prefetch_result),
            },
            "extraction_policy": "single-pass-domain-fanout-v1",
            "input_reports": task_report_paths,
            "cells": cells,
            "summary": {
                "unique_physical_tasks": len(cells),
                "physical_segments": len(intervals),
                "physical_sectors": previous_end + 1,
                "vobus": actual_vobus,
                "expected_vobus": expected_vobus,
                "program_stream_maps": sum(
                    int((cell["validation"].get("structure") or {}).get("program_stream_maps") or 0)
                    for cell in cells
                ),
                "source_frames": sum(
                    int(cell["validation"]["source_video"]["nb_read_frames"])
                    for cell in cells
                ),
                "output_frames": sum(
                    int(cell["validation"]["output_video"]["nb_read_frames"])
                    for cell in cells
                ),
                "audio_hashes_match": all(
                    cell["validation"]["source_audio_hash"]
                    == cell["validation"]["output_audio_hash"]
                    for cell in cells
                ),
                "alternate_sectors_unchanged": all(
                    cell["split_back"]["alternate_sectors_unchanged"]
                    for cell in cells if cell.get("split_back")
                ),
                "selected_readbacks_match": all(
                    cell["split_back"]["selected_readback_matches"]
                    for cell in cells if cell.get("split_back")
                ),
                "compact_input_cells": sum(
                    cell.get("layout_mode") == "compact-input" for cell in cells
                ),
            },
        }
        _atomic_json(combined_path, combined)
        write_status(
            "passed", "complete", "All unique VTS tasks passed", len(tasks), str(combined_path)
        )
        progress(f"Complete interleaved VTS conversion passed: {combined_path}")
        return combined
    except Exception as exc:
        if audio_prefetch_executor is not None:
            audio_prefetch_executor.shutdown(wait=True, cancel_futures=True)
        write_status("failed", "failed", str(exc), len(task_reports))
        raise
