"""Assemble validated cell replacements into a DVD folder staging transaction."""

from __future__ import annotations

import json
import os
import re
import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

from .extract import inspect_vob_file
from .iso import DiscScanError, _entry_name, close_iso_image, open_iso_image
from .pipeline import PipelineError
from .native import find_dvdinspect, inspect_physical_graph
from .ifo import VMG_LAST_SECTOR_OFFSET, rewrite_vmgi_title_set_sectors
from .vob import DVD_SECTOR_SIZE


Progress = Callable[[str], None]
MAX_DVD_VOB_SECTORS = 524_287


def stage_compact_vmg(
    base_stage: Path,
    layout_path: Path,
    *,
    compact_vob: Path,
    compact_ifo: Path,
    compact_bup: Path,
    destination: Path,
) -> dict[str, Any]:
    """Create a hardlinked stage with a compact VIDEO_TS.VOB and VMGI/BUP."""
    base_stage = base_stage.resolve()
    layout_path = layout_path.resolve()
    destination = destination.resolve()
    base_report_path = base_stage / "dvd2hevc-stage-report.json"
    if not base_report_path.is_file():
        raise PipelineError(f"Base staged DVD report is missing: {base_report_path}")
    base_report = json.loads(base_report_path.read_text(encoding="utf-8"))
    if base_report.get("status") != "passed":
        raise PipelineError("Compact VMG staging requires a passed base stage")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    if str(Path(layout.get("source") or "").resolve()) != str(
        Path(base_report.get("source") or "").resolve()
    ):
        raise PipelineError("Compact VMG layout and base stage use different source discs")
    row = next(
        (
            item for item in layout.get("domains", [])
            if item.get("domain") == "vmg_menu" and int(item.get("vts", -1)) == 0
        ),
        None,
    )
    if row is None:
        raise PipelineError("Compact layout has no VMG menu domain")
    compact_vob = compact_vob.resolve()
    compact_ifo = compact_ifo.resolve()
    compact_bup = compact_bup.resolve()
    replacements = {
        "VIDEO_TS.IFO": compact_ifo,
        "VIDEO_TS.BUP": compact_bup,
        "VIDEO_TS.VOB": compact_vob,
    }
    missing = [str(path) for path in replacements.values() if not path.is_file()]
    if missing:
        raise PipelineError(f"Compact VMG stage replacements are missing: {missing}")
    expected_vob_bytes = int(row["compact_sectors"]) * DVD_SECTOR_SIZE
    if compact_vob.stat().st_size != expected_vob_bytes:
        raise PipelineError("Compact VMG VOB size does not match its layout")
    if compact_ifo.read_bytes() != compact_bup.read_bytes():
        raise PipelineError("Compact VMGI IFO and BUP differ")
    if destination.exists():
        raise PipelineError(f"Compact VMG staging destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        raise PipelineError(f"Previous incomplete compact VMG stage exists: {temporary}")
    video_ts = temporary / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    base_video_ts = base_stage / "VIDEO_TS"
    link_count = 0
    copy_count = 0
    for source in base_video_ts.iterdir():
        if not source.is_file() or source.name.upper() in replacements:
            continue
        target = video_ts / source.name
        try:
            os.link(source, target)
            link_count += 1
        except OSError:
            shutil.copy2(source, target)
            copy_count += 1
    for name, source in replacements.items():
        shutil.copy2(source, video_ts / name)
        copy_count += 1

    scan = inspect_vob_file(video_ts / "VIDEO_TS.VOB")
    if scan["invalid_sectors"] or scan["scrambled_pes_packets"]:
        raise PipelineError(
            "Compact staged VMG VOB scan failed: "
            f"invalid={scan['invalid_sectors']}, scrambled={scan['scrambled_pes_packets']}"
        )
    vmgi_bytes = (video_ts / "VIDEO_TS.IFO").read_bytes()
    first_title_set_sector = (
        int.from_bytes(
            vmgi_bytes[
                VMG_LAST_SECTOR_OFFSET : VMG_LAST_SECTOR_OFFSET + 4
            ],
            "big",
        )
        + 1
    )
    vmgi_relocation = rewrite_vmgi_title_set_sectors(
        video_ts,
        first_title_set_sector=first_title_set_sector,
    )
    # The copied compact VMGI/BUP are atomically rewritten with final title-set
    # starts. Other normalized VTS control files break hardlinks as needed.
    normalized_title_sets = len(vmgi_relocation.get("normalized_title_sets", []))
    link_count -= normalized_title_sets * 2
    copy_count += 2 + normalized_title_sets * 2

    dvdinspect = find_dvdinspect()
    if not dvdinspect:
        raise PipelineError("dvdinspect is required for compact VMG staging")
    graph = inspect_physical_graph(temporary, dvdinspect, timeout=300)
    expected_vobus = sum(len(cell["vobus"]) for cell in row["cells"])
    actual_vobus = int((graph.get("vmg_menu_vobu_map") or {}).get("count") or 0)
    if actual_vobus != expected_vobus:
        raise PipelineError(
            f"Compact staged VMG graph has {actual_vobus} VOBUs; expected {expected_vobus}"
        )
    expected_cells = len(row["cells"])
    actual_cells = len(graph.get("vmg_menu_cell_addresses") or [])
    if actual_cells != expected_cells:
        raise PipelineError(
            f"Compact staged VMG graph has {actual_cells} cells; expected {expected_cells}"
        )
    result = {
        "schema": "dvd2hevc-compact-stage-v0",
        "status": "passed",
        "source": base_report["source"],
        "base_stage": str(base_stage),
        "base_stage_report": str(base_report_path),
        "layout": str(layout_path),
        "layout_sha256": hashlib.sha256(layout_path.read_bytes()).hexdigest(),
        "destination": str(destination),
        "vts": 0,
        "vmg_compacted": True,
        "replacements": {name: str(path) for name, path in replacements.items()},
        "files": sorted(
            ({"name": path.name, "size": path.stat().st_size} for path in video_ts.iterdir()),
            key=lambda item: item["name"].upper(),
        ),
        "summary": {
            "hardlinked_files": link_count,
            "copied_files": copy_count,
            "compact_vmg_vob_bytes": expected_vob_bytes,
            "compact_vmg_vob_sectors": int(row["compact_sectors"]),
            "vmg_vobus": actual_vobus,
            "vmg_cells": actual_cells,
            "invalid_sectors": int(scan["invalid_sectors"]),
            "vmgi_title_set_relocation": vmgi_relocation,
        },
    }
    (temporary / "dvd2hevc-stage-report.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return result


def stage_compact_vts(
    base_stage: Path,
    layout_path: Path,
    *,
    vts: int,
    compact_vob: Path,
    compact_ifo: Path,
    compact_bup: Path,
    compact_menu_vob: Path | None = None,
    destination: Path,
) -> dict[str, Any]:
    """Create a hardlinked validation stage with one rewritten compact VTS."""
    base_stage = base_stage.resolve()
    layout_path = layout_path.resolve()
    destination = destination.resolve()
    base_report_path = base_stage / "dvd2hevc-stage-report.json"
    if not base_report_path.is_file():
        raise PipelineError(f"Base staged DVD report is missing: {base_report_path}")
    base_report = json.loads(base_report_path.read_text(encoding="utf-8"))
    if base_report.get("status") != "passed":
        raise PipelineError("Compact staging requires a passed base stage")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    row = next(
        (item for item in layout.get("domains", []) if item.get("domain") == "title" and int(item["vts"]) == vts),
        None,
    )
    if row is None:
        raise PipelineError(f"Compact layout has no title-domain VTS {vts}")
    menu_row = next(
        (
            item for item in layout.get("domains", [])
            if item.get("domain") == "vts_menu" and int(item["vts"]) == vts
        ),
        None,
    )
    if menu_row is not None and compact_menu_vob is None:
        raise PipelineError(f"Compact layout requires a VTS {vts} menu VOB replacement")
    if menu_row is None and compact_menu_vob is not None:
        raise PipelineError(f"Compact menu VOB was supplied but layout has no VTS {vts} menu")
    control_replacements = {
        f"VTS_{vts:02d}_0.IFO": compact_ifo.resolve(),
        f"VTS_{vts:02d}_0.BUP": compact_bup.resolve(),
    }
    compact_vob = compact_vob.resolve()
    compact_menu_vob = compact_menu_vob.resolve() if compact_menu_vob else None
    replacement_inputs = [*control_replacements.values(), compact_vob]
    if compact_menu_vob is not None:
        replacement_inputs.append(compact_menu_vob)
    if any(not path.is_file() for path in replacement_inputs):
        missing = [str(path) for path in replacement_inputs if not path.is_file()]
        raise PipelineError(f"Compact stage replacements are missing: {missing}")
    expected_vob_bytes = int(row["compact_sectors"]) * DVD_SECTOR_SIZE
    if compact_vob.stat().st_size != expected_vob_bytes:
        raise PipelineError("Compact VOB size does not match its layout")
    expected_menu_bytes = int(menu_row["compact_sectors"]) * DVD_SECTOR_SIZE if menu_row else 0
    if compact_menu_vob is not None and compact_menu_vob.stat().st_size != expected_menu_bytes:
        raise PipelineError("Compact menu VOB size does not match its layout")
    if control_replacements[f"VTS_{vts:02d}_0.IFO"].read_bytes() != control_replacements[f"VTS_{vts:02d}_0.BUP"].read_bytes():
        raise PipelineError("Compact VTS IFO and BUP differ")
    if destination.exists():
        raise PipelineError(f"Compact staging destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        raise PipelineError(f"Previous incomplete compact stage exists: {temporary}")
    video_ts = temporary / "VIDEO_TS"
    video_ts.mkdir(parents=True)
    base_video_ts = base_stage / "VIDEO_TS"
    title_pattern = re.compile(rf"^VTS_{vts:02d}_[1-9][0-9]*\.VOB$", re.IGNORECASE)
    link_count = 0
    copy_count = 0
    for source in base_video_ts.iterdir():
        if not source.is_file():
            continue
        if title_pattern.match(source.name):
            continue
        if compact_menu_vob is not None and source.name.upper() == f"VTS_{vts:02d}_0.VOB":
            continue
        target = video_ts / source.name
        replacement = control_replacements.get(source.name.upper())
        if replacement is not None:
            shutil.copy2(replacement, target)
            copy_count += 1
        else:
            try:
                os.link(source, target)
                link_count += 1
            except OSError:
                shutil.copy2(source, target)
                copy_count += 1
    compact_menu_file: Path | None = None
    if compact_menu_vob is not None:
        compact_menu_file = video_ts / f"VTS_{vts:02d}_0.VOB"
        shutil.copy2(compact_menu_vob, compact_menu_file)
        copy_count += 1
    compact_title_files: list[Path] = []
    remaining = expected_vob_bytes
    with compact_vob.open("rb") as source:
        index = 1
        while remaining:
            byte_count = min(remaining, MAX_DVD_VOB_SECTORS * DVD_SECTOR_SIZE)
            target = video_ts / f"VTS_{vts:02d}_{index}.VOB"
            with target.open("wb") as output:
                left = byte_count
                while left:
                    chunk = source.read(min(left, 8 * 1024 * 1024))
                    if not chunk:
                        raise PipelineError("Compact title domain ended during VOB splitting")
                    output.write(chunk)
                    left -= len(chunk)
            compact_title_files.append(target)
            remaining -= byte_count
            index += 1
        if source.read(1):
            raise PipelineError("Compact title domain exceeds its planned size")
    copy_count += len(compact_title_files)
    scanned_vobs = [*([compact_menu_file] if compact_menu_file else []), *compact_title_files]
    scans = [{"name": path.name, "stats": inspect_vob_file(path)} for path in scanned_vobs]
    invalid = sum(item["stats"]["invalid_sectors"] for item in scans)
    scrambled = sum(item["stats"]["scrambled_pes_packets"] for item in scans)
    if invalid or scrambled:
        raise PipelineError(f"Compact staged VOB scan failed: invalid={invalid}, scrambled={scrambled}")
    vmg_compacted = bool(base_report.get("vmg_compacted"))
    first_title_set_sector: int | None = None
    if vmg_compacted:
        vmgi = (video_ts / "VIDEO_TS.IFO").read_bytes()
        first_title_set_sector = (
            int.from_bytes(
                vmgi[VMG_LAST_SECTOR_OFFSET : VMG_LAST_SECTOR_OFFSET + 4],
                "big",
            )
            + 1
        )
    vmgi_relocation = rewrite_vmgi_title_set_sectors(
        video_ts,
        first_title_set_sector=first_title_set_sector,
    )
    # VIDEO_TS.IFO/BUP were initially linked from the base stage; atomic
    # replacement above deliberately breaks those links for this transaction.
    rewritten_controls = int(vmgi_relocation.get("rewritten_control_files", 2))
    # The current VTS IFO/BUP were copied replacements, not hardlinks.  Only
    # normalized control files belonging to other VTSes break base-stage links.
    normalized_others = sum(
        1 for item in vmgi_relocation.get("normalized_title_sets", [])
        if int(item) != vts
    )
    link_count -= 2 + normalized_others * 2
    copy_count += rewritten_controls
    dvdinspect = find_dvdinspect()
    if not dvdinspect:
        raise PipelineError("dvdinspect is required for compact staging")
    graph = inspect_physical_graph(temporary, dvdinspect, timeout=300)
    compact_vts = next((item for item in graph["title_sets"] if int(item["vts"]) == vts), None)
    if compact_vts is None or int(compact_vts["title_vobu_map"]["count"]) != sum(
        len(cell["vobus"]) for cell in row["cells"]
    ):
        raise PipelineError("Compact staged physical graph does not match the layout")
    if menu_row is not None and int(compact_vts["menu_vobu_map"]["count"]) != sum(
        len(cell["vobus"]) for cell in menu_row["cells"]
    ):
        raise PipelineError("Compact staged menu graph does not match the layout")
    result = {
        "schema": "dvd2hevc-compact-stage-v0",
        "status": "passed",
        "source": base_report["source"],
        "base_stage": str(base_stage),
        "base_stage_report": str(base_report_path),
        "layout": str(layout_path),
        "layout_sha256": hashlib.sha256(layout_path.read_bytes()).hexdigest(),
        "destination": str(destination),
        "vts": vts,
        "vmg_compacted": vmg_compacted,
        "replacements": {
            **{name: str(path) for name, path in control_replacements.items()},
            "title_domain": str(compact_vob),
            "menu_domain": str(compact_menu_vob) if compact_menu_vob else None,
        },
        "files": sorted(
            ({"name": path.name, "size": path.stat().st_size} for path in video_ts.iterdir()),
            key=lambda item: item["name"].upper(),
        ),
        "summary": {
            "hardlinked_files": link_count,
            "copied_files": copy_count,
            "compact_vob_bytes": expected_vob_bytes,
            "compact_vob_sectors": int(row["compact_sectors"]),
            "compact_vob_files": len(compact_title_files),
            "vobus": int(compact_vts["title_vobu_map"]["count"]),
            "compact_menu_vob_bytes": expected_menu_bytes,
            "menu_vobus": int(compact_vts["menu_vobu_map"]["count"]) if menu_row else 0,
            "invalid_sectors": invalid,
            "vmgi_title_set_relocation": vmgi_relocation,
        },
    }
    (temporary / "dvd2hevc-stage-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    return result


def _cell_key(cell: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(cell["vob_id"]),
        int(cell["cell_id"]),
        int(cell.get("first_sector", cell.get("start_sector"))),
        int(cell["last_sector"]),
    )


def combine_title_reports(report_paths: list[Path], destination: Path) -> dict[str, Any]:
    if not report_paths:
        raise PipelineError("At least one title report is required")
    reports = [json.loads(path.resolve().read_text(encoding="utf-8")) for path in report_paths]
    if any(report.get("status") != "passed" for report in reports):
        raise PipelineError("Every title report must have passed before VTS combination")
    sources = {str(Path(report["source"]).resolve()) for report in reports}
    title_sets = {int(report["vts"]) for report in reports}
    if len(sources) != 1 or len(title_sets) != 1:
        raise PipelineError("Combined title reports must use the same source ISO and VTS")
    source = Path(next(iter(sources)))
    vts_number = next(iter(title_sets))
    unique: dict[tuple[int, int, int, int], dict[str, Any]] = {}

    def replacement_artifact(cell: dict[str, Any]) -> Path:
        selected = str(cell.get("selected_quality")).lower()
        attempt = next(
            (
                row for row in cell.get("attempts", [])
                if str((row.get("encode") or {}).get("quality")).lower() == selected
            ),
            None,
        )
        if attempt:
            return Path(attempt["encoded"])
        if cell.get("repacked_cell"):
            return Path(cell["repacked_cell"])
        raise PipelineError(f"Cell has no selected HEVC artifact: {cell.get('name')}")

    for report in reports:
        for cell in report["cells"]:
            key = _cell_key(cell["cell"])
            previous = unique.get(key)
            if previous and replacement_artifact(previous).read_bytes() != replacement_artifact(cell).read_bytes():
                raise PipelineError(f"Conflicting replacements for physical cell {key}")
            unique[key] = cell
    executable = find_dvdinspect()
    if not executable:
        raise PipelineError("dvdinspect is required to verify complete VTS coverage")
    graph = inspect_physical_graph(source, executable, timeout=300)
    vts = next((row for row in graph["title_sets"] if int(row["vts"]) == vts_number), None)
    if not vts:
        raise PipelineError(f"VTS {vts_number} is absent from the physical graph")
    expected = {_cell_key(cell) for cell in vts.get("title_cell_addresses", [])}
    actual = set(unique)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise PipelineError(f"Incomplete VTS cell coverage: missing={missing}, extra={extra}")
    cells = [unique[key] for key in sorted(unique, key=lambda row: row[2])]
    result = {
        "schema": "dvd2hevc-vts-conversion-v0",
        "status": "passed",
        "source": str(source),
        "source_identity": reports[0].get("source_identity"),
        "vts": vts_number,
        "title": reports[0]["title"],
        "titles": sorted(int(report["title"]["title"]) for report in reports),
        "input_reports": [str(path.resolve()) for path in report_paths],
        "settings": reports[0].get("settings"),
        "cells": cells,
        "summary": {
            "physical_cells": len(cells),
            "expected_physical_cells": len(expected),
            "vobus": sum(int(cell["vobu_count"]) for cell in cells),
            "expected_vobus": int(vts["title_vobu_map"]["count"]),
            "complete_physical_coverage": True,
        },
    }
    if result["summary"]["vobus"] != result["summary"]["expected_vobus"]:
        raise PipelineError(f"VTS VOBU coverage mismatch: {result['summary']}")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def replacement_segments(
    file_sizes: list[int], first_sector: int, last_sector: int
) -> list[tuple[int, int, int]]:
    """Map a title-domain sector range to (file index, file offset, byte count)."""
    start = first_sector * DVD_SECTOR_SIZE
    end = (last_sector + 1) * DVD_SECTOR_SIZE
    segments: list[tuple[int, int, int]] = []
    domain_offset = 0
    for index, size in enumerate(file_sizes):
        file_end = domain_offset + size
        overlap_start = max(start, domain_offset)
        overlap_end = min(end, file_end)
        if overlap_start < overlap_end:
            segments.append((index, overlap_start - domain_offset, overlap_end - overlap_start))
        domain_offset = file_end
    if sum(segment[2] for segment in segments) != end - start:
        raise PipelineError(f"Replacement range {first_sector}-{last_sector} is outside the title VOB domain")
    return segments


def cell_replacement_segments(
    file_sizes: list[int], cell: dict[str, Any]
) -> list[dict[str, int]]:
    """Map a contiguous or interleaved logical replacement into domain files."""
    physical = cell.get("physical_segments")
    if physical:
        ranges = [
            (int(row["start_sector"]), int(row["last_sector"]))
            for row in physical
        ]
    else:
        ranges = [(
            int(cell["cell"]["first_sector"]),
            int(cell["cell"]["last_sector"]),
        )]
    previous_end = -1
    logical_offset = 0
    result: list[dict[str, int]] = []
    for first_sector, last_sector in ranges:
        if first_sector <= previous_end or last_sector < first_sector:
            raise PipelineError("Cell physical segments are invalid or overlapping")
        previous_end = last_sector
        for file_index, file_offset, byte_count in replacement_segments(
            file_sizes, first_sector, last_sector
        ):
            result.append({
                "file_index": file_index,
                "file_offset": file_offset,
                "byte_count": byte_count,
                "logical_offset": logical_offset,
                "physical_first_sector": first_sector,
                "physical_last_sector": last_sector,
            })
            logical_offset += byte_count
    return result


def _selected_names(entries: list[Any], vts: int) -> set[str]:
    title_pattern = re.compile(rf"^VTS_{vts:02d}_[1-9][0-9]*\.VOB$", re.IGNORECASE)
    names = {_entry_name(entry) for entry in entries if entry is not None and entry.is_file()}
    return {
        name
        for name in names
        if name.upper().endswith((".IFO", ".BUP"))
        or name.upper() == "VIDEO_TS.VOB"
        or name.upper().endswith("_0.VOB")
        or title_pattern.match(name)
    }


def _domain_files(video_ts: Path, domain: str, vts: int) -> list[Path]:
    if domain == "vmg_menu":
        files = [video_ts / "VIDEO_TS.VOB"]
    elif domain == "vts_menu":
        files = [video_ts / f"VTS_{vts:02d}_0.VOB"]
    elif domain == "title":
        pattern = re.compile(rf"^VTS_{vts:02d}_([1-9][0-9]*)\.VOB$", re.IGNORECASE)
        files = sorted(
            (path for path in video_ts.iterdir() if pattern.match(path.name)),
            key=lambda path: int(pattern.match(path.name).group(1)),  # type: ignore[union-attr]
        )
    else:
        raise PipelineError(f"Unknown conversion domain: {domain}")
    if not files or any(not path.is_file() for path in files):
        raise PipelineError(f"Staged DVD is missing files for {domain} VTS {vts}")
    return files


def _apply_conversion(
    conversion: dict[str, Any], video_ts: Path
) -> list[dict[str, Any]]:
    domain = str(conversion.get("domain") or "title")
    vts = int(conversion["vts"])
    files = _domain_files(video_ts, domain, vts)
    sizes = [path.stat().st_size for path in files]
    rows: list[dict[str, Any]] = []
    for cell in conversion["cells"]:
        replacement_value = cell.get("repacked_cell")
        if cell.get("layout_mode") == "compact-input" or not replacement_value:
            raise PipelineError(
                f"{domain} VTS {vts} cell {cell.get('name')} has only a compact-layout "
                "replacement and cannot be applied to a sector-preserving base stage"
            )
        replacement = Path(replacement_value)
        data = replacement.read_bytes()
        file_segments = cell_replacement_segments(sizes, cell)
        expected = sum(row["byte_count"] for row in file_segments)
        if len(data) != expected:
            raise PipelineError(f"Replacement size mismatch for {cell['name']}")
        for segment in file_segments:
            offset = segment["logical_offset"]
            count = segment["byte_count"]
            with files[segment["file_index"]].open("r+b") as handle:
                handle.seek(segment["file_offset"])
                handle.write(data[offset : offset + count])
        ranges = cell.get("physical_segments") or [cell["cell"]]
        rows.append({
            "name": cell["name"],
            "domain": domain,
            "vts": vts,
            "first_sector": int(ranges[0].get("start_sector", ranges[0].get("first_sector"))),
            "last_sector": int(ranges[-1]["last_sector"]),
            "physical_ranges": len(ranges),
            "bytes": expected,
            "segments": [
                {
                    "file": files[row["file_index"]].name,
                    "offset": row["file_offset"],
                    "bytes": row["byte_count"],
                    "logical_offset": row["logical_offset"],
                }
                for row in file_segments
            ],
        })
    return rows


def stage_complete_dvd(
    conversion_reports: list[Path],
    destination: Path,
    *,
    source: Path | None = None,
    progress: Progress = print,
) -> dict[str, Any]:
    """Apply validated title and menu reports to a complete VIDEO_TS folder."""
    partial_base = source is not None
    report_paths = [path.resolve() for path in conversion_reports]
    conversions = [json.loads(path.read_text(encoding="utf-8")) for path in report_paths]
    if any(conversion.get("status") != "passed" for conversion in conversions):
        raise PipelineError("Only passed conversion reports can be staged")
    sources = {str(Path(conversion["source"]).resolve()) for conversion in conversions}
    if source is not None:
        sources.add(str(source.resolve()))
    if len(sources) > 1:
        raise PipelineError("All complete-disc conversion reports must use one source ISO")
    if not sources:
        raise PipelineError("A source ISO or at least one conversion report is required")
    source = Path(next(iter(sources)))
    destinations = {
        (str(conversion.get("domain") or "title"), int(conversion["vts"]))
        for conversion in conversions
    }
    if len(destinations) != len(conversions):
        raise PipelineError("Complete-disc reports contain a duplicate domain/VTS destination")

    destination = destination.resolve()
    if destination.exists():
        raise PipelineError(f"Staging destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        raise PipelineError(f"Previous incomplete staging directory exists: {temporary}")
    video_ts = temporary / "VIDEO_TS"
    video_ts.mkdir(parents=True)

    image = open_iso_image(source)
    try:
        entries = [
            entry for entry in image.list_children(udf_path="/VIDEO_TS")
            if entry is not None and not entry.is_dot() and not entry.is_dotdot() and entry.is_file()
        ]
        for entry in entries:
            name = _entry_name(entry)
            progress(f"Extracting {name}")
            image.get_file_from_iso(udf_path=f"/VIDEO_TS/{name}", local_path=str(video_ts / name))
    except Exception as exc:
        raise DiscScanError(f"Could not assemble complete staged DVD folder: {exc}") from exc
    finally:
        close_iso_image(image)

    replacement_rows: list[dict[str, Any]] = []
    for report_path, conversion in zip(report_paths, conversions):
        domain = str(conversion.get("domain") or "title")
        progress(f"Applying {domain} VTS {int(conversion['vts'])}: {report_path.name}")
        replacement_rows.extend(_apply_conversion(conversion, video_ts))

    progress("Scanning every staged VOB")
    vob_scans = [
        {"name": path.name, "size": path.stat().st_size, "stats": inspect_vob_file(path)}
        for path in sorted(video_ts.glob("*.VOB"), key=lambda path: path.name.upper())
    ]
    invalid = sum(row["stats"]["invalid_sectors"] for row in vob_scans)
    scrambled = sum(row["stats"]["scrambled_pes_packets"] for row in vob_scans)
    if invalid or scrambled:
        raise PipelineError(f"Complete staged DVD scan failed: invalid={invalid}, scrambled={scrambled}")
    expected_maps = sum(
        int(cell["validation"]["structure"]["program_stream_maps"])
        for conversion in conversions for cell in conversion["cells"]
    )
    actual_maps = sum(row["stats"]["program_stream_maps"] for row in vob_scans)
    if (partial_base and actual_maps < expected_maps) or (not partial_base and actual_maps != expected_maps):
        raise PipelineError(f"Complete staged PSM count mismatch: expected={expected_maps}, actual={actual_maps}")

    result = {
        "schema": "dvd2hevc-complete-staged-dvd-v0",
        "status": "passed",
        "source": str(source),
        "conversion_reports": [str(path) for path in report_paths],
        "destination": str(destination),
        "partial_compact_base": partial_base,
        "files": sorted(
            ({"name": path.name, "size": path.stat().st_size} for path in video_ts.iterdir()),
            key=lambda row: row["name"].upper(),
        ),
        "replacements": replacement_rows,
        "vob_scans": vob_scans,
        "summary": {
            "conversion_domains": len(conversions),
            "replacement_cells": len(replacement_rows),
            "replacement_bytes": sum(row["bytes"] for row in replacement_rows),
            "vob_bytes": sum(row["size"] for row in vob_scans),
            "invalid_sectors": invalid,
            "scrambled_pes_packets": scrambled,
            "program_stream_maps": actual_maps,
        },
    }
    (temporary / "dvd2hevc-stage-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    progress(f"Complete staged DVD folder passed: {destination}")
    return result


def extend_complete_stage(
    base_stage: Path,
    conversion_reports: list[Path],
    destination: Path,
    *,
    progress: Progress = print,
) -> dict[str, Any]:
    """Add disjoint conversions to a passed stage using hardlink copy-on-write."""
    if not conversion_reports:
        raise PipelineError("At least one additional conversion report is required")
    base_stage = base_stage.resolve()
    base_report_path = base_stage / "dvd2hevc-stage-report.json"
    if not base_report_path.is_file():
        raise PipelineError(f"Base stage report is missing: {base_report_path}")
    base = json.loads(base_report_path.read_text(encoding="utf-8"))
    if base.get("schema") != "dvd2hevc-complete-staged-dvd-v0" or base.get("status") != "passed":
        raise PipelineError("Stage extension requires a passed complete sector-preserving stage")

    report_paths = [path.resolve() for path in conversion_reports]
    additions = [json.loads(path.read_text(encoding="utf-8")) for path in report_paths]
    if any(item.get("status") != "passed" for item in additions):
        raise PipelineError("Only passed conversion reports can extend a stage")
    source = str(Path(base["source"]).resolve())
    if any(str(Path(item["source"]).resolve()) != source for item in additions):
        raise PipelineError("Stage extension reports use a different source ISO")
    base_report_paths = [Path(path).resolve() for path in base.get("conversion_reports", [])]
    base_conversions = [json.loads(path.read_text(encoding="utf-8")) for path in base_report_paths]
    existing_destinations = {
        (str(item.get("domain") or "title"), int(item["vts"])) for item in base_conversions
    }
    new_destinations = {
        (str(item.get("domain") or "title"), int(item["vts"])) for item in additions
    }
    if len(new_destinations) != len(additions) or existing_destinations & new_destinations:
        raise PipelineError("Stage extension contains a duplicate domain/VTS destination")

    destination = destination.resolve()
    if destination.exists():
        raise PipelineError(f"Staging destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        raise PipelineError(f"Previous incomplete staging directory exists: {temporary}")
    video_ts = temporary / "VIDEO_TS"
    video_ts.mkdir(parents=True)

    base_video_ts = base_stage / "VIDEO_TS"
    hardlinked = 0
    copied = 0
    for source_file in base_video_ts.iterdir():
        if not source_file.is_file():
            continue
        target = video_ts / source_file.name
        try:
            os.link(source_file, target)
            hardlinked += 1
        except OSError:
            shutil.copy2(source_file, target)
            copied += 1

    # Break links only for the title/menu VOB files that will be changed.
    cow_files: set[Path] = set()
    for conversion in additions:
        cow_files.update(_domain_files(
            video_ts,
            str(conversion.get("domain") or "title"),
            int(conversion["vts"]),
        ))
    for target in sorted(cow_files, key=lambda path: path.name.upper()):
        replacement = target.with_name(f".{target.name}.{os.getpid()}.cow")
        shutil.copy2(target, replacement)
        os.replace(replacement, target)
        hardlinked -= 1
        copied += 1

    replacement_rows = list(base.get("replacements", []))
    for report_path, conversion in zip(report_paths, additions):
        domain = str(conversion.get("domain") or "title")
        progress(f"Applying additional {domain} VTS {int(conversion['vts'])}: {report_path.name}")
        replacement_rows.extend(_apply_conversion(conversion, video_ts))

    progress("Scanning every extended-stage VOB")
    vob_scans = [
        {"name": path.name, "size": path.stat().st_size, "stats": inspect_vob_file(path)}
        for path in sorted(video_ts.glob("*.VOB"), key=lambda path: path.name.upper())
    ]
    invalid = sum(row["stats"]["invalid_sectors"] for row in vob_scans)
    scrambled = sum(row["stats"]["scrambled_pes_packets"] for row in vob_scans)
    if invalid or scrambled:
        raise PipelineError(f"Extended staged DVD scan failed: invalid={invalid}, scrambled={scrambled}")
    all_conversions = [*base_conversions, *additions]
    expected_maps = sum(
        int(cell["validation"]["structure"]["program_stream_maps"])
        for conversion in all_conversions for cell in conversion["cells"]
    )
    actual_maps = sum(row["stats"]["program_stream_maps"] for row in vob_scans)
    if actual_maps != expected_maps:
        raise PipelineError(
            f"Extended staged PSM count mismatch: expected={expected_maps}, actual={actual_maps}"
        )

    result = {
        "schema": "dvd2hevc-complete-staged-dvd-v0",
        "status": "passed",
        "source": source,
        "base_stage": str(base_stage),
        "base_stage_report": str(base_report_path),
        "conversion_reports": [str(path) for path in [*base_report_paths, *report_paths]],
        "destination": str(destination),
        "files": sorted(
            ({"name": path.name, "size": path.stat().st_size} for path in video_ts.iterdir()),
            key=lambda row: row["name"].upper(),
        ),
        "replacements": replacement_rows,
        "vob_scans": vob_scans,
        "summary": {
            "conversion_domains": len(all_conversions),
            "additional_domains": len(additions),
            "replacement_cells": len(replacement_rows),
            "replacement_bytes": sum(int(row["bytes"]) for row in replacement_rows),
            "vob_bytes": sum(row["size"] for row in vob_scans),
            "invalid_sectors": invalid,
            "scrambled_pes_packets": scrambled,
            "program_stream_maps": actual_maps,
            "hardlinked_files": hardlinked,
            "copy_on_write_files": len(cow_files),
            "copied_files": copied,
        },
    }
    (temporary / "dvd2hevc-stage-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    progress(f"Extended complete staged DVD passed: {destination}")
    return result


def verify_complete_stage(stage_report: Path) -> dict[str, Any]:
    """Hash every replacement back from a complete staged VIDEO_TS folder."""
    stage_report = stage_report.resolve()
    stage = json.loads(stage_report.read_text(encoding="utf-8"))
    destination = Path(stage["destination"])
    video_ts = destination / "VIDEO_TS"
    rows: list[dict[str, Any]] = []
    for conversion_path in stage["conversion_reports"]:
        conversion = json.loads(Path(conversion_path).read_text(encoding="utf-8"))
        domain = str(conversion.get("domain") or "title")
        vts = int(conversion["vts"])
        files = _domain_files(video_ts, domain, vts)
        sizes = [path.stat().st_size for path in files]
        for cell in conversion["cells"]:
            replacement = Path(cell["repacked_cell"])
            expected_hash = hashlib.sha256(replacement.read_bytes()).hexdigest()
            actual = hashlib.sha256()
            for segment in cell_replacement_segments(sizes, cell):
                with files[segment["file_index"]].open("rb") as handle:
                    handle.seek(segment["file_offset"])
                    remaining = segment["byte_count"]
                    while remaining:
                        chunk = handle.read(min(4 * 1024 * 1024, remaining))
                        if not chunk:
                            raise PipelineError(f"Short staged read for {cell['name']}")
                        actual.update(chunk)
                        remaining -= len(chunk)
            actual_hash = actual.hexdigest()
            rows.append({
                "name": cell["name"],
                "domain": domain,
                "vts": vts,
                "expected_sha256": expected_hash,
                "staged_sha256": actual_hash,
                "matches": expected_hash == actual_hash,
            })
    result = {
        "schema": "dvd2hevc-complete-stage-verification-v0",
        "passed": all(row["matches"] for row in rows),
        "stage_report": str(stage_report),
        "cells": rows,
    }
    output = destination / "dvd2hevc-stage-verification.json"
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result["passed"]:
        raise PipelineError(f"A staged cell differs from its validated replacement; see {output}")
    return result


def stage_title_dvd(
    conversion_report: Path,
    destination: Path,
    *,
    progress: Progress = print,
) -> dict[str, Any]:
    conversion_report = conversion_report.resolve()
    conversion = json.loads(conversion_report.read_text(encoding="utf-8"))
    if conversion.get("status") != "passed":
        raise PipelineError("Only a passed title conversion report can be staged")
    source = Path(conversion["source"]).resolve()
    vts = int(conversion["vts"])
    destination = destination.resolve()
    if destination.exists():
        raise PipelineError(f"Staging destination already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        raise PipelineError(f"Previous incomplete staging directory exists: {temporary}")
    video_ts = temporary / "VIDEO_TS"
    video_ts.mkdir(parents=True)

    image = open_iso_image(source)
    try:
        entries = [
            entry for entry in image.list_children(udf_path="/VIDEO_TS")
            if entry is not None and not entry.is_dot() and not entry.is_dotdot() and entry.is_file()
        ]
        names = _selected_names(entries, vts)
        for entry in entries:
            name = _entry_name(entry)
            if name not in names:
                continue
            progress(f"Extracting {name}")
            image.get_file_from_iso(udf_path=f"/VIDEO_TS/{name}", local_path=str(video_ts / name))
    except Exception as exc:
        raise DiscScanError(f"Could not assemble staged DVD folder: {exc}") from exc
    finally:
        close_iso_image(image)

    title_pattern = re.compile(rf"^VTS_{vts:02d}_([1-9][0-9]*)\.VOB$", re.IGNORECASE)
    title_files = sorted(
        (path for path in video_ts.iterdir() if title_pattern.match(path.name)),
        key=lambda path: int(title_pattern.match(path.name).group(1)),  # type: ignore[union-attr]
    )
    file_sizes = [path.stat().st_size for path in title_files]
    replacement_rows: list[dict[str, Any]] = []
    for cell in conversion["cells"]:
        replacement = Path(cell["repacked_cell"])
        data = replacement.read_bytes()
        file_segments = cell_replacement_segments(file_sizes, cell)
        expected = sum(row["byte_count"] for row in file_segments)
        if len(data) != expected:
            raise PipelineError(f"Replacement size mismatch for {cell['name']}")
        for segment in file_segments:
            offset = segment["logical_offset"]
            count = segment["byte_count"]
            with title_files[segment["file_index"]].open("r+b") as handle:
                handle.seek(segment["file_offset"])
                handle.write(data[offset : offset + count])
        ranges = cell.get("physical_segments") or [cell["cell"]]
        replacement_rows.append({
            "name": cell["name"],
            "first_sector": int(ranges[0].get("start_sector", ranges[0].get("first_sector"))),
            "last_sector": int(ranges[-1]["last_sector"]),
            "physical_ranges": len(ranges),
            "bytes": expected,
            "segments": [
                {
                    "file": title_files[row["file_index"]].name,
                    "offset": row["file_offset"],
                    "bytes": row["byte_count"],
                    "logical_offset": row["logical_offset"],
                }
                for row in file_segments
            ],
        })

    progress("Scanning staged title VOB structure")
    scans = [{"name": path.name, "size": path.stat().st_size, "stats": inspect_vob_file(path)} for path in title_files]
    invalid = sum(row["stats"]["invalid_sectors"] for row in scans)
    scrambled = sum(row["stats"]["scrambled_pes_packets"] for row in scans)
    if invalid or scrambled:
        raise PipelineError(f"Staged title-domain scan failed: invalid={invalid}, scrambled={scrambled}")
    result = {
        "schema": "dvd2hevc-staged-dvd-v0",
        "status": "passed",
        "source": str(source),
        "conversion_report": str(conversion_report),
        "destination": str(destination),
        "vts": vts,
        "title": int(conversion["title"]["title"]),
        "files": sorted(
            ({"name": path.name, "size": path.stat().st_size} for path in video_ts.iterdir()),
            key=lambda row: row["name"].upper(),
        ),
        "replacements": replacement_rows,
        "title_vob_scans": scans,
        "summary": {
            "replacement_cells": len(replacement_rows),
            "replacement_bytes": sum(row["bytes"] for row in replacement_rows),
            "title_vob_bytes": sum(file_sizes),
            "invalid_sectors": invalid,
            "scrambled_pes_packets": scrambled,
            "program_stream_maps": sum(row["stats"]["program_stream_maps"] for row in scans),
        },
    }
    report_path = temporary / "dvd2hevc-stage-report.json"
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    progress(f"Staged DVD folder passed: {destination}")
    return result


def verify_staged_title(stage_report: Path) -> dict[str, Any]:
    """Read back every replaced range and compare it with the validated cell."""
    stage_report = stage_report.resolve()
    stage = json.loads(stage_report.read_text(encoding="utf-8"))
    destination = Path(stage["destination"])
    conversion = json.loads(Path(stage["conversion_report"]).read_text(encoding="utf-8"))
    vts = int(stage["vts"])
    video_ts = destination / "VIDEO_TS"
    pattern = re.compile(rf"^VTS_{vts:02d}_([1-9][0-9]*)\.VOB$", re.IGNORECASE)
    title_files = sorted(
        (path for path in video_ts.iterdir() if pattern.match(path.name)),
        key=lambda path: int(pattern.match(path.name).group(1)),  # type: ignore[union-attr]
    )
    sizes = [path.stat().st_size for path in title_files]
    rows: list[dict[str, Any]] = []
    for cell in conversion["cells"]:
        replacement = Path(cell["repacked_cell"])
        expected_hash = hashlib.sha256(replacement.read_bytes()).hexdigest()
        actual = hashlib.sha256()
        for segment in cell_replacement_segments(sizes, cell):
            with title_files[segment["file_index"]].open("rb") as handle:
                handle.seek(segment["file_offset"])
                remaining = segment["byte_count"]
                while remaining:
                    chunk = handle.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        raise PipelineError(f"Short staged read for {cell['name']}")
                    actual.update(chunk)
                    remaining -= len(chunk)
        actual_hash = actual.hexdigest()
        rows.append({
            "name": cell["name"],
            "expected_sha256": expected_hash,
            "staged_sha256": actual_hash,
            "matches": expected_hash == actual_hash,
        })
    result = {
        "schema": "dvd2hevc-stage-verification-v0",
        "passed": all(row["matches"] for row in rows),
        "stage_report": str(stage_report),
        "cells": rows,
    }
    output = destination / "dvd2hevc-stage-verification.json"
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result["passed"]:
        raise PipelineError(f"A staged cell differs from its validated replacement; see {output}")
    return result
