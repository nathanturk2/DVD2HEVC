"""Phase 3 title-level HEVC conversion pipeline."""

from __future__ import annotations

import json
import os
import subprocess
from collections import deque
import time
import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .atomic import write_json_atomic
from .cadence import analyze_cadence
from .encoders import (
    EncoderConfigurationError,
    HEVC_ENCODERS,
    build_hevc_encoder_options,
    parse_rate_control,
    rate_control_label,
    rate_control_slug,
)
from .extract import extract_domain_sector_ranges, inspect_vob_file
from .native import find_dvdinspect, inspect_physical_graph
from .phase2 import analyze_vobu_budgets
from .progress import progress_event
from .repack import repack_cell_hevc, video_slot
from .tools import discover_tools
from .transport import (
    VideoPes,
    has_hevc_idr,
    hevc_nal_types,
    prepare_hevc_dvd_menu,
    read_video_pes,
)
from .vob import DVD_SECTOR_SIZE


class PipelineError(RuntimeError):
    pass


Progress = Callable[[str], None]


def sector_fit_rate_controls(
    quality_values: tuple[int | float | str, ...],
) -> tuple[int | float | str, ...]:
    """Add bounded bitrate retries for fixed-size DVD sector layouts.

    Target-bitrate mode normally supplies one exact VBR or CBR control.  That
    is correct for compact title layouts, which may enlarge a VOBU, but VMG
    menus and diagnostic sector-preserving conversions must still fit the
    source VOBU slots.  Keep the requested control first and add progressively
    smaller, cell-local retries only when the caller supplied one bitrate
    control.  Explicit multi-control and CQ policies remain authoritative.
    """
    if len(quality_values) != 1:
        return quality_values
    control = parse_rate_control(quality_values[0])
    if control["mode"] not in {"vbr", "cbr"}:
        return quality_values
    requested_bps = int(control["target_bps"])
    result: list[int | float | str] = [quality_values[0]]
    seen = {str(control["canonical"])}
    for ratio in (0.95, 0.90, 0.80, 0.70, 0.60):
        fallback_bps = max(16_000, int(round(requested_bps * ratio / 1_000.0)) * 1_000)
        fallback = f"{control['mode']}:{fallback_bps}"
        if fallback not in seen:
            result.append(fallback)
            seen.add(fallback)
    return tuple(result)


def cell_entry_keyframe_ticks(vobus: list[dict[str, Any]]) -> tuple[int, ...]:
    """Force one independently decodable IDR at an addressable cell entry."""
    return (0,) if vobus else ()


def vobu_keyframe_ticks(vobus: list[dict[str, Any]]) -> tuple[int, ...]:
    """Force independently decodable HEVC at every DVD random-access VOBU."""
    if not vobus:
        return ()
    base = int(vobus[0]["start_ptm"])
    ticks = tuple(int(vobu["start_ptm"]) - base for vobu in vobus)
    if ticks[0] != 0 or any(current <= previous for previous, current in zip(ticks, ticks[1:])):
        raise PipelineError("DVD VOBU keyframe timestamps are not strictly increasing")
    return ticks


def format_force_keyframe_times(ticks: tuple[int, ...]) -> str:
    """Format 90 kHz DVD timestamps for FFmpeg's force_key_frames option."""
    return ",".join(
        (f"{tick / 90000.0:.6f}".rstrip("0").rstrip(".") or "0")
        for tick in ticks
    )


FORCE_KEYFRAME_INLINE_LIMIT = 8_000


def write_force_keyframe_chapters(path: Path, ticks: tuple[int, ...]) -> None:
    """Store arbitrary VOBU timestamps as FFmetadata chapters.

    FFmpeg expands ``-force_key_frames chapters`` internally, avoiding the
    Windows command-line limit for long cells with thousands of VOBUs.
    """
    lines = [";FFMETADATA1"]
    for index, start in enumerate(ticks):
        end = ticks[index + 1] if index + 1 < len(ticks) else start + 1
        lines.extend([
            "[CHAPTER]",
            "TIMEBASE=1/90000",
            f"START={start}",
            f"END={max(start + 1, end)}",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_cell_entry_idr(
    packets: list[VideoPes], budget: dict[str, Any]
) -> dict[str, Any]:
    """Require the first video-bearing VOBU in a cell to start with an IDR."""
    cursor = 0
    video_vobus = 0
    entry_vobu: int | None = None
    entry_has_idr = False
    for row in budget["vobus"]:
        count = int(row["packet_count"])
        if count:
            video_vobus += 1
            if entry_vobu is None:
                entry_vobu = int(row["index"])
                entry_has_idr = cursor < len(packets) and has_hevc_idr(packets[cursor].payload)
        cursor += count
    if cursor != len(packets):
        raise PipelineError(
            f"Cell-entry IDR validation consumed {cursor} packets; expected {len(packets)}"
        )
    if entry_vobu is not None and not entry_has_idr:
        raise PipelineError(f"HEVC cell entry VOBU {entry_vobu} does not start on an IDR")
    return {
        "video_vobus": video_vobus,
        "entry_vobu": entry_vobu,
        "entry_idr": entry_has_idr,
        "predictive_vobus_allowed": max(0, video_vobus - 1),
    }


def validate_vobu_random_access(
    packets: list[VideoPes], budget: dict[str, Any]
) -> dict[str, Any]:
    """Require VPS/SPS/PPS and an IDR in every video-bearing DVD VOBU."""
    cursor = 0
    checked = 0
    failures: list[dict[str, Any]] = []
    for row in budget["vobus"]:
        count = int(row["packet_count"])
        selected = packets[cursor : cursor + count]
        cursor += count
        if not selected:
            continue
        checked += 1
        nal_types = hevc_nal_types(b"".join(packet.payload for packet in selected))
        missing_headers = sorted({32, 33, 34} - set(nal_types))
        has_idr = any(nal_type in {19, 20} for nal_type in nal_types)
        if missing_headers or not has_idr:
            failures.append({
                "vobu": int(row["index"]),
                "missing_parameter_sets": missing_headers,
                "has_idr": has_idr,
            })
    if cursor != len(packets):
        raise PipelineError(
            f"VOBU random-access validation consumed {cursor} packets; expected {len(packets)}"
        )
    if failures:
        raise PipelineError(f"HEVC VOBUs are not independently decodable: {failures[:8]}")
    return {
        "policy": "dvd-vobu-random-access-v1",
        "video_vobus": checked,
        "random_access_vobus": checked,
        "failures": 0,
    }


def select_title_pgc(graph: dict[str, Any], title_number: int) -> tuple[dict[str, Any], dict[str, Any], int]:
    title = next((row for row in graph.get("global_titles", []) if int(row.get("title", 0)) == title_number), None)
    if title is None:
        raise PipelineError(f"DVD title {title_number} does not exist")
    vts_number = int(title["vts"])
    vts = next((row for row in graph.get("title_sets", []) if int(row.get("vts", 0)) == vts_number), None)
    if vts is None:
        raise PipelineError(f"Physical graph has no VTS {vts_number}")
    vts_title_number = int(title["vts_title_number"])
    chapter_map = next(
        (row for row in vts.get("chapters", []) if int(row.get("vts_title_number", 0)) == vts_title_number),
        None,
    )
    pgc_numbers = sorted({int(part["pgc"]) for part in (chapter_map or {}).get("parts", [])})
    if len(pgc_numbers) != 1:
        raise PipelineError(
            f"The current pipeline requires one PGC per title; title {title_number} references {pgc_numbers or 'none'}"
        )
    pgc_number = pgc_numbers[0]
    pgcs = vts.get("title_pgcs", [])
    if not 1 <= pgc_number <= len(pgcs):
        raise PipelineError(f"PGC {pgc_number} is outside VTS {vts_number}")
    return title, vts, pgc_number


def unique_physical_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[int, int, int, int]] = set()
    result: list[dict[str, Any]] = []
    for cell in cells:
        key = (
            int(cell["vob_id"]),
            int(cell["cell_id"]),
            int(cell["first_sector"]),
            int(cell["last_sector"]),
        )
        if key not in seen:
            seen.add(key)
            result.append(cell)
    return result


def menu_physical_cells(
    addresses: list[dict[str, Any]], menu_pgci: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Normalize C_ADT menu cells and add the best PGC duration available."""
    durations: dict[tuple[int, int, int, int], int] = {}
    for language in menu_pgci:
        for pgc in language.get("pgcs", []):
            for cell in pgc.get("cells", []):
                key = (
                    int(cell["vob_id"]),
                    int(cell["cell_id"]),
                    int(cell["first_sector"]),
                    int(cell["last_sector"]),
                )
                durations[key] = max(durations.get(key, 0), int(cell.get("duration_ticks", 0)))
    result: list[dict[str, Any]] = []
    for address in addresses:
        cell = dict(address)
        cell["first_sector"] = int(cell.pop("start_sector", cell.get("first_sector", 0)))
        key = (
            int(cell["vob_id"]),
            int(cell["cell_id"]),
            int(cell["first_sector"]),
            int(cell["last_sector"]),
        )
        cell["duration_ticks"] = durations.get(key, 0)
        result.append(cell)
    return sorted(result, key=lambda cell: int(cell["first_sector"]))


def verify_preserved_sectors(source: Path, output: Path) -> dict[str, int]:
    source_size = source.stat().st_size
    output_size = output.stat().st_size
    if source_size != output_size or source_size % DVD_SECTOR_SIZE:
        raise PipelineError("Source/output cell sizes differ or are not sector aligned")
    video_sectors = 0
    identical_nonvideo_sectors = 0
    preserved_video_prefixes = 0
    with source.open("rb") as source_handle, output.open("rb") as output_handle:
        for index in range(source_size // DVD_SECTOR_SIZE):
            source_sector = source_handle.read(DVD_SECTOR_SIZE)
            output_sector = output_handle.read(DVD_SECTOR_SIZE)
            slot = video_slot(source_sector, index)
            if slot is None:
                if source_sector != output_sector:
                    raise PipelineError(f"Non-video sector {index} changed")
                identical_nonvideo_sectors += 1
            else:
                video_sectors += 1
                if output_sector[: len(slot.prefix)] != slot.prefix:
                    raise PipelineError(f"Non-video prefix in video sector {index} changed")
                preserved_video_prefixes += 1
    return {
        "video_sectors": video_sectors,
        "identical_nonvideo_sectors": identical_nonvideo_sectors,
        "preserved_video_prefixes": preserved_video_prefixes,
    }


PIPELINE_REVISION = "phase8-compact-first-pipeline-v3"


def source_identity(path: Path, *, sample_size: int = 1024 * 1024) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(sample_size))
        if stat.st_size > sample_size:
            handle.seek(max(0, stat.st_size - sample_size))
            digest.update(handle.read(sample_size))
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "edge_sha256": digest.hexdigest(),
    }


def cell_cache_key(
    identity: dict[str, Any],
    *,
    vts: int,
    cell: dict[str, Any],
    vobus: list[dict[str, Any]],
    settings: dict[str, Any],
) -> str:
    value = {
        "pipeline_revision": PIPELINE_REVISION,
        "source": identity,
        "vts": vts,
        "cell": {key: cell.get(key) for key in ("vob_id", "cell_id", "first_sector", "last_sector")},
        "vobus": vobus,
        "settings": settings,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _encoded_file_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def load_cached_encode_attempt(path: Path, cache_key: str) -> dict[str, Any] | None:
    """Reuse an interrupted cell's encode only with its original inputs and file."""
    try:
        attempt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(attempt, dict) or attempt.get("cache_key") != cache_key:
            return None
        encoded = Path(attempt["encoded"])
        identity = _encoded_file_identity(encoded)
        if not encoded.is_file() or identity["size"] <= 0 or attempt.get("encoded_identity") != identity:
            return None
        if attempt["idr_alignment"]["failures"] != 0:
            return None
        return attempt
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_cached_cell_report(path: Path, cache_key: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if report.get("status") != "passed" or report.get("cache_key") != cache_key:
        return None
    source_cell = Path(report.get("source_cell", ""))
    expected = (int(report["cell"]["last_sector"]) - int(report["cell"]["first_sector"]) + 1) * DVD_SECTOR_SIZE
    if not source_cell.is_file():
        return None
    if source_cell.stat().st_size != expected:
        return None
    if report.get("layout_mode") == "compact-input":
        try:
            selected_quality = str(report["selected_quality"]).lower()
            selected = next(
                attempt for attempt in report.get("attempts", [])
                if str((attempt.get("encode") or {}).get("quality")).lower() == selected_quality
            )
            encoded = Path(selected["encoded"])
            if selected.get("encoded_identity") is not None and (
                selected["encoded_identity"] != _encoded_file_identity(encoded)
            ):
                return None
        except (OSError, KeyError, StopIteration):
            return None
        return report if encoded.is_file() and encoded.stat().st_size > 0 else None
    repacked_cell = Path(report.get("repacked_cell", ""))
    if not repacked_cell.is_file() or repacked_cell.stat().st_size != expected:
        return None
    return report


def choose_cell_attempt(budgets: list[dict[str, Any]], *, minimum_headroom: int = 4096) -> int | None:
    """Choose one encode that safely fits every VOBU without splicing GOPs."""
    if not budgets:
        return None
    vobu_count = int(budgets[0]["vobu_count"])
    for attempt_index, budget in enumerate(budgets):
        if int(budget["vobu_count"]) != vobu_count or budget["unassigned_pes_count"]:
            continue
        if all(
            int(row["headroom"]) >= minimum_headroom
            or (int(row.get("encoded_bytes", -1)) == 0 and bool(row.get("fits", False)))
            for row in budget["vobus"]
        ):
            return attempt_index
    return None


def _run(command: list[str], *, log: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, errors="replace",
        **hidden_subprocess_kwargs(),
    )
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return completed


def _run_ffmpeg_with_progress(
    command: list[str],
    *,
    log: Path,
    duration_seconds: float,
    callback: Callable[[float], None],
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg while reporting bounded media-time progress."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    output: deque[str] = deque(maxlen=4096)
    record: dict[str, str] = {}
    last_fraction = -1.0
    last_emitted = 0.0
    assert process.stdout is not None
    for line in process.stdout:
        output.append(line)
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        record[key] = value
        if key != "progress":
            continue
        if value == "end":
            fraction = 1.0
        else:
            try:
                # out_time_us is preferred. Older FFmpeg builds exposed the
                # same microsecond value as out_time_ms despite its name.
                microseconds = int(record.get("out_time_us") or record.get("out_time_ms") or 0)
            except ValueError:
                microseconds = 0
            fraction = (
                max(0.0, min(1.0, microseconds / 1_000_000 / duration_seconds))
                if duration_seconds > 0 else 0.0
            )
        now = time.monotonic()
        if value == "end" or fraction - last_fraction >= 0.005 or now - last_emitted >= 2.0:
            try:
                callback(fraction)
            except Exception:
                # Progress reporting must never turn a valid encode into a
                # failed conversion.
                pass
            last_fraction = fraction
            last_emitted = now
        record = {}
    returncode = process.wait()
    rendered = "".join(output)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(rendered, encoding="utf-8")
    return subprocess.CompletedProcess(command, returncode, rendered, "")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    write_json_atomic(path, value)


def encode_cell_hevc(
    source: Path,
    destination: Path,
    *,
    ffmpeg: str,
    quality: int | float | str,
    preset: str,
    threads: int,
    log: Path,
    encoder: str = "libx265",
    cadence_mode: str = "interlaced",
    force_keyframe_ticks: tuple[int, ...] = (),
    duration_seconds: float = 0.0,
    progress_callback: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    if force_keyframe_ticks:
        if force_keyframe_ticks[0] != 0 or any(
            current <= previous
            for previous, current in zip(force_keyframe_ticks, force_keyframe_ticks[1:])
        ):
            raise PipelineError("Forced HEVC keyframe timestamps must start at zero and increase")
    filter_chain = (
        "setfield=prog,setpts=PTS-STARTPTS"
        if cadence_mode == "progressive"
        else "bwdif=mode=send_field:parity=auto:deint=all,setpts=PTS-STARTPTS"
        if cadence_mode == "deinterlace50"
        else "setpts=PTS-STARTPTS"
    )
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"]
    if progress_callback is not None:
        command.extend(["-nostats", "-progress", "pipe:1"])
    command.extend(["-i", str(source)])
    keyframe_transport = "none"
    keyframe_metadata: Path | None = None
    force_keyframe_argument = (
        format_force_keyframe_times(force_keyframe_ticks)
        if force_keyframe_ticks else ""
    )
    if force_keyframe_argument and len(force_keyframe_argument) > FORCE_KEYFRAME_INLINE_LIMIT:
        keyframe_metadata = destination.with_suffix(destination.suffix + ".keyframes.ffmeta")
        write_force_keyframe_chapters(keyframe_metadata, force_keyframe_ticks)
        command.extend(["-f", "ffmetadata", "-i", str(keyframe_metadata)])
        keyframe_transport = "chapters-file"
    elif force_keyframe_argument:
        keyframe_transport = "inline"
    command.extend([
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        filter_chain,
    ])
    if force_keyframe_argument:
        if keyframe_metadata is not None:
            command.extend(["-map_chapters", "1", "-force_key_frames", "chapters"])
        else:
            command.extend(["-force_key_frames", force_keyframe_argument])
    try:
        encoder_options, native_preset = build_hevc_encoder_options(
            encoder,
            quality=quality,
            preset=preset,
            cadence_mode=cadence_mode,
            threads=threads,
        )
    except EncoderConfigurationError as exc:
        raise PipelineError(str(exc)) from exc
    command.extend(encoder_options)
    command.extend([
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-mpegts_flags",
        "+resend_headers",
        "-f",
        "mpegts",
        str(destination),
    ])
    started = time.monotonic()
    completed = (
        _run_ffmpeg_with_progress(
            command, log=log, duration_seconds=duration_seconds,
            callback=progress_callback,
        )
        if progress_callback is not None
        else _run(command, log=log)
    )
    if completed.returncode != 0 or not destination.is_file():
        raise PipelineError(f"HEVC encode failed; see {log}")
    control = parse_rate_control(quality)
    reported_quality: int | float | str
    if control["mode"] == "cq":
        numeric = float(control["quality"])
        reported_quality = int(numeric) if numeric.is_integer() else numeric
    else:
        reported_quality = str(control["canonical"])
    return {
        "quality": reported_quality,
        "rate_control": control,
        "encoder": encoder,
        "cadence_mode": cadence_mode,
        "filter_chain": filter_chain,
        "preset": preset,
        "native_preset": native_preset,
        "keyframe_policy": (
            "dvd-vobu-idr-v1" if len(force_keyframe_ticks) > 1
            else "dvd-cell-entry-idr-v1" if force_keyframe_ticks
            else "fixed-gop"
        ),
        "forced_keyframe_count": len(force_keyframe_ticks),
        "keyframe_transport": keyframe_transport,
        "threads": threads,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output_bytes": destination.stat().st_size,
        "log": str(log.resolve()),
    }


def _probe_video(
    path: Path, ffprobe: str, *, force_mpeg_ps: bool = False,
    force_mpeg_ts: bool = False,
) -> dict[str, Any]:
    if force_mpeg_ps and force_mpeg_ts:
        raise PipelineError("A video probe cannot force both MPEG-PS and MPEG-TS")
    command = [ffprobe, "-v", "error"]
    if force_mpeg_ps:
        # Very small DVD still cells may not contain enough packs for FFprobe's
        # generic format detector to assign an MPEG-PS score, even though the
        # PSM and HEVC payload decode correctly when the known container is
        # supplied. Validation already knows both inputs are DVD PS sectors.
        command += ["-f", "mpeg"]
    elif force_mpeg_ts:
        # One-picture cells can produce a valid transport stream shorter than
        # FFprobe's generic format-detection threshold.  The encoder contract
        # already fixes this artifact as MPEG-TS, so supply that known format.
        command += ["-f", "mpegts"]
    command += [
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=codec_name,width,height,field_order,start_time,duration,nb_read_frames",
        "-of",
        "json",
        str(path),
    ]
    completed = _run(command)
    if completed.returncode != 0:
        raise PipelineError(f"ffprobe failed for {path}: {completed.stderr.strip()}")
    streams = json.loads(completed.stdout).get("streams", [])
    if not streams:
        raise PipelineError(f"No video stream found in {path}")
    stream = streams[0]
    if stream.get("nb_read_frames") is not None:
        stream["nb_read_frames"] = int(stream["nb_read_frames"])
    return stream


def _audio_hash(
    path: Path, ffmpeg: str, *, force_mpeg_ps: bool = False
) -> str | None:
    command = [ffmpeg, "-v", "error"]
    if force_mpeg_ps:
        command += ["-f", "mpeg"]
    command += [
        "-i",
        str(path),
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-f",
        "hash",
        "-hash",
        "md5",
        "-",
    ]
    probe = _run(command)
    if probe.returncode != 0:
        raise PipelineError(f"Audio hash failed for {path}: {probe.stderr.strip()}")
    line = next((line.strip() for line in probe.stdout.splitlines() if line.startswith("MD5=")), None)
    return line


def _has_audio(
    path: Path, ffprobe: str, *, force_mpeg_ps: bool = False
) -> bool:
    command = [ffprobe, "-v", "error"]
    if force_mpeg_ps:
        command += ["-f", "mpeg"]
    command += [
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "json",
        str(path),
    ]
    probe = _run(command)
    if probe.returncode != 0:
        raise PipelineError(f"Audio stream probe failed for {path}: {probe.stderr.strip()}")
    return bool(json.loads(probe.stdout).get("streams", []))


def validate_repacked_cell(
    source: Path,
    output: Path,
    *,
    expected_vobus: int,
    ffmpeg: str,
    ffprobe: str,
    decode_log: Path,
    expected_frame_multiplier: int = 1,
    expected_frame_addition: int = 0,
    expected_program_stream_maps: int | None = None,
) -> dict[str, Any]:
    preservation = verify_preserved_sectors(source, output)
    structure = inspect_vob_file(output)
    if structure["invalid_sectors"] or structure["scrambled_pes_packets"]:
        raise PipelineError(f"Repacked cell failed structural scan: {structure}")
    expected_maps = expected_vobus if expected_program_stream_maps is None else expected_program_stream_maps
    if structure["program_stream_maps"] != expected_maps:
        raise PipelineError(
            f"Expected {expected_maps} HEVC maps, found {structure['program_stream_maps']}"
        )
    source_video = _probe_video(source, ffprobe)
    output_video = _probe_video(output, ffprobe, force_mpeg_ps=True)
    if output_video.get("codec_name") != "hevc":
        raise PipelineError(f"Repacked output is not identified as HEVC: {output_video}")
    expected_frames = (
        int(source_video.get("nb_read_frames") or 0) * expected_frame_multiplier
        + expected_frame_addition
    )
    if expected_frames != output_video.get("nb_read_frames"):
        raise PipelineError(f"Frame-count mismatch: source={source_video}, output={output_video}")
    source_has_audio = _has_audio(source, ffprobe)
    output_has_audio = _has_audio(output, ffprobe, force_mpeg_ps=True)
    if source_has_audio != output_has_audio:
        raise PipelineError(
            f"Audio stream presence changed: source={source_has_audio}, output={output_has_audio}"
        )
    source_audio_hash = _audio_hash(source, ffmpeg) if source_has_audio else None
    output_audio_hash = (
        _audio_hash(output, ffmpeg, force_mpeg_ps=True) if output_has_audio else None
    )
    if source_audio_hash != output_audio_hash:
        raise PipelineError(f"Audio hash mismatch: {source_audio_hash} != {output_audio_hash}")
    decoded = _run([
        ffmpeg,
        "-v",
        "error",
        "-f",
        "mpeg",
        "-i",
        str(output),
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
    ], log=decode_log)
    if decoded.returncode != 0:
        raise PipelineError(f"Complete HEVC decode failed; see {decode_log}")
    return {
        "preservation": preservation,
        "structure": structure,
        "source_video": source_video,
        "output_video": output_video,
        "source_audio_hash": source_audio_hash,
        "output_audio_hash": output_audio_hash,
        "complete_video_decode": True,
        "expected_frame_multiplier": expected_frame_multiplier,
        "expected_frame_addition": expected_frame_addition,
        "decode_log": str(decode_log.resolve()),
    }


def validate_compact_encoded_cell(
    source: Path,
    encoded: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    decode_log: Path,
    expected_frame_multiplier: int = 1,
    expected_frame_addition: int = 0,
) -> dict[str, Any]:
    """Validate an HEVC cell whose final sectors will be assigned by compaction."""
    source_video = _probe_video(source, ffprobe)
    output_video = _probe_video(encoded, ffprobe, force_mpeg_ts=True)
    if output_video.get("codec_name") != "hevc":
        raise PipelineError(f"Compact input is not identified as HEVC: {output_video}")
    expected_frames = (
        int(source_video.get("nb_read_frames") or 0) * expected_frame_multiplier
        + expected_frame_addition
    )
    if expected_frames != output_video.get("nb_read_frames"):
        raise PipelineError(
            f"Compact-input frame-count mismatch: source={source_video}, output={output_video}"
        )
    decoded = _run([
        ffmpeg, "-v", "error", "-f", "mpegts", "-i", str(encoded),
        "-map", "0:v:0", "-f", "null", "-",
    ], log=decode_log)
    if decoded.returncode != 0:
        raise PipelineError(f"Complete compact-input HEVC decode failed; see {decode_log}")
    return {
        "mode": "compact-input",
        "preservation": None,
        "structure": None,
        "source_video": source_video,
        "output_video": output_video,
        # Audio is restored from the source or the compact-audio lane when the
        # final compact VOBU sectors are built; the elementary HEVC TS is video-only.
        "source_audio_hash": None,
        "output_audio_hash": None,
        "complete_video_decode": True,
        "expected_frame_multiplier": expected_frame_multiplier,
        "expected_frame_addition": expected_frame_addition,
        "decode_log": str(decode_log.resolve()),
    }


def convert_title(
    source: Path,
    *,
    title_number: int,
    workspace: Path,
    preset: str = "medium",
    quality_values: tuple[int | float | str, ...] = (20, 22, 24, 26, 28, 30),
    threads: int = 4,
    encoder: str = "libx265",
    cadence: str = "auto",
    ambiguous_cadence: str = "deinterlace50",
    quality_preset: dict[str, Any] | None = None,
    allow_compact_expansion: bool = False,
    prefer_compact_input: bool = False,
    audio_prefetch_root: Path | None = None,
    audio_policy: Path | None = None,
    stereo_audio_bitrate: int = 256_000,
    mono_audio_bitrate: int = 128_000,
    audio_workers: int = 2,
    progress: Progress = print,
) -> dict[str, Any]:
    source = source.resolve()
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = workspace / "title-report.json"
    tools = discover_tools()
    ffmpeg = tools.get("ffmpeg")
    ffprobe = tools.get("ffprobe")
    dvdinspect = find_dvdinspect()
    if not ffmpeg or not ffprobe or not dvdinspect:
        raise PipelineError("ffmpeg, ffprobe, and dvdinspect are required")
    if encoder not in HEVC_ENCODERS:
        raise PipelineError(f"Unsupported HEVC encoder: {encoder}")
    if encoder != "libx265" and cadence == "interlaced":
        raise PipelineError(
            f"{encoder} cannot encode DVD2HEVC interlaced output; use auto, progressive, or deinterlace50"
        )
    graph = inspect_physical_graph(source, dvdinspect, timeout=300)
    title, vts, pgc_number = select_title_pgc(graph, title_number)
    pgc = vts["title_pgcs"][pgc_number - 1]
    cells = unique_physical_cells(pgc.get("cells", []))
    vobus = vts["title_vobu_map"]["vobus"]
    identity = source_identity(source)
    settings = {
        "encoder": encoder,
        "cadence": cadence,
        "ambiguous_cadence": ambiguous_cadence,
        "preset": preset,
        "quality_values": list(quality_values),
        "quality_preset": quality_preset or {"name": "manual"},
        "keyframe_policy": "dvd-vobu-idr-v1",
        "program_stream_map_policy": "dvd-vobu-psm-v1",
        "quality_fallback_scope": "whole-cell",
        "intermediate_policy": (
            "compact-input-v1" if prefer_compact_input else "sector-preserving-v1"
        ),
        "threads": threads,
    }
    encoding_quality_values = (
        quality_values
        if allow_compact_expansion or prefer_compact_input
        else sector_fit_rate_controls(quality_values)
    )
    settings["sector_fit_rate_controls"] = list(encoding_quality_values)
    report: dict[str, Any] = {
        "schema": "dvd2hevc-title-conversion-v0",
        "status": "running",
        "source": str(source),
        "source_identity": identity,
        "pipeline_revision": PIPELINE_REVISION,
        "title": title,
        "vts": int(vts["vts"]),
        "pgc": pgc_number,
        "pgc_duration_ticks": int(pgc.get("duration_ticks", 0)),
        "referenced_cell_count": len(pgc.get("cells", [])),
        "unique_cell_count": len(cells),
        "extraction_policy": "single-pass-domain-fanout-v1",
        "settings": settings,
        "cells": [],
    }
    reused_cells = 0
    last_confident_cadence: str | None = None
    total_duration_ticks = sum(max(1, int(cell.get("duration_ticks") or 0)) for cell in cells)
    completed_duration_ticks = 0
    audio_prefetch_executor: ThreadPoolExecutor | None = None
    audio_prefetch_future = None

    def emit_video_progress(
        *, cell_position: int, cell_fraction: float, phase: str,
    ) -> None:
        cell_ticks = max(1, int(cells[cell_position - 1].get("duration_ticks") or 0))
        current = completed_duration_ticks + round(cell_ticks * max(0.0, min(1.0, cell_fraction)))
        progress_event(
            "video", "progress", f"title-{title_number}",
            scope="video-task-duration", current=current, total=total_duration_ticks,
            unit="ticks", phase=phase, cell=cell_position, cells=len(cells),
        )

    _atomic_json(report_path, report)
    try:
        extraction_requests: list[tuple[int, int, Path]] = []
        for cell in cells:
            first_sector = int(cell["first_sector"])
            last_sector = int(cell["last_sector"])
            name = (
                f"vts{int(vts['vts']):02d}-vob{int(cell['vob_id']):02d}-"
                f"cell{int(cell['cell_id']):02d}-{first_sector}-{last_sector}"
            )
            cell_dir = workspace / "cells" / name
            relevant_vobus = [
                row for row in vobus
                if first_sector <= int(row["sector"]) <= last_sector
            ]
            if not relevant_vobus:
                raise PipelineError(f"No VOBUs found for {name}")
            cache_key = cell_cache_key(
                identity,
                vts=int(vts["vts"]),
                cell=cell,
                vobus=relevant_vobus,
                settings=settings,
            )
            cached = load_cached_cell_report(cell_dir / "cell-report.json", cache_key)
            if (
                cached is not None
                and cached.get("layout_mode") == "compact-input"
                and not (allow_compact_expansion or prefer_compact_input)
            ):
                cached = None
            if cached is None:
                extraction_requests.append(
                    (first_sector, last_sector, cell_dir / "source.vob")
                )
        if extraction_requests:
            progress(
                f"Batch-extracting {len(extraction_requests)} title cell(s) from "
                f"VTS {int(vts['vts']):02d} in one ISO pass"
            )
        extraction_by_destination = {
            Path(row["destination"]).resolve(): row
            for row in extract_domain_sector_ranges(
                source,
                domain="title",
                vts=int(vts["vts"]),
                ranges=extraction_requests,
            )
        }
        if audio_prefetch_root is not None:
            # Import lazily: the audio module reuses PipelineError from this
            # module, so a top-level import would create a circular dependency.
            from .audio import prefetch_compact_audio_cells

            prefetch_cells = []
            for cell in cells:
                first_sector = int(cell["first_sector"])
                last_sector = int(cell["last_sector"])
                name = (
                    f"vts{int(vts['vts']):02d}-vob{int(cell['vob_id']):02d}-"
                    f"cell{int(cell['cell_id']):02d}-{first_sector}-{last_sector}"
                )
                prefetch_cells.append({
                    "name": name,
                    "source_cell": str((workspace / "cells" / name / "source.vob").resolve()),
                })
            audio_prefetch_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="dvd2hevc-title-audio-prefetch"
            )
            audio_prefetch_future = audio_prefetch_executor.submit(
                prefetch_compact_audio_cells,
                prefetch_cells,
                audio_prefetch_root,
                vts=int(vts["vts"]),
                stereo_bitrate=stereo_audio_bitrate,
                mono_bitrate=mono_audio_bitrate,
                workers=audio_workers,
                audio_policy=audio_policy,
            )
        for position, cell in enumerate(cells, start=1):
            cell_duration_ticks = max(1, int(cell.get("duration_ticks") or 0))
            emit_video_progress(cell_position=position, cell_fraction=0.0, phase="preparing")
            first_sector = int(cell["first_sector"])
            last_sector = int(cell["last_sector"])
            name = f"vts{int(vts['vts']):02d}-vob{int(cell['vob_id']):02d}-cell{int(cell['cell_id']):02d}-{first_sector}-{last_sector}"
            cell_dir = workspace / "cells" / name
            cell_dir.mkdir(parents=True, exist_ok=True)
            source_cell = cell_dir / "source.vob"
            repacked = cell_dir / "repacked.vob"
            relevant_vobus = [row for row in vobus if first_sector <= int(row["sector"]) <= last_sector]
            if not relevant_vobus:
                raise PipelineError(f"No VOBUs found for {name}")
            cache_key = cell_cache_key(
                identity,
                vts=int(vts["vts"]),
                cell=cell,
                vobus=relevant_vobus,
                settings=settings,
            )
            cached = load_cached_cell_report(cell_dir / "cell-report.json", cache_key)
            if (
                cached is not None
                and cached.get("layout_mode") == "compact-input"
                and not (allow_compact_expansion or prefer_compact_input)
            ):
                # A compact-input artifact cannot be inserted into a
                # sector-preserving domain.  Re-evaluate its encoded stream;
                # it may prove to fit exactly despite the reserve estimate.
                cached = None
            if cached is not None:
                reused_cells += 1
                cached = dict(cached)
                cached["reused_in_run"] = True
                report["cells"].append(cached)
                if cached.get("cadence_mode") in {"progressive", "deinterlace50"}:
                    last_confident_cadence = str(cached["cadence_mode"])
                _atomic_json(report_path, report)
                progress(f"[{position}/{len(cells)}] Reusing validated {name}")
                completed_duration_ticks += cell_duration_ticks
                emit_video_progress(cell_position=position, cell_fraction=0.0, phase="reused")
                continue
            progress(f"[{position}/{len(cells)}] Using batch-extracted {name}")
            extraction = extraction_by_destination.get(source_cell.resolve())
            if extraction is None:
                raise PipelineError(f"Batch extraction result is missing for {name}")
            cadence_analysis: dict[str, Any] | None = None
            selected_cadence = "interlaced" if cadence == "interlaced" else "progressive"
            cadence_inherited = False
            if cadence == "auto":
                cadence_analysis = analyze_cadence(
                    source_cell,
                    ffmpeg=ffmpeg,
                    duration_seconds=float(cell.get("duration_ticks", 0)) / 90000.0,
                )
                classification = cadence_analysis["classification"]
                if classification == "progressive":
                    selected_cadence = "progressive"
                elif classification == "interlaced":
                    selected_cadence = "deinterlace50"
                else:
                    if last_confident_cadence is not None:
                        selected_cadence = last_confident_cadence
                        cadence_inherited = True
                    elif ambiguous_cadence == "fail":
                        raise PipelineError(
                            f"Cadence is ambiguous for {name}; select progressive or deinterlace50 explicitly"
                        )
                    else:
                        selected_cadence = ambiguous_cadence
                _atomic_json(cell_dir / "cadence-analysis.json", cadence_analysis)
            else:
                selected_cadence = cadence
            if cadence_analysis and cadence_analysis["classification"] != "ambiguous":
                last_confident_cadence = selected_cadence
            attempts: list[dict[str, Any]] = []
            selected_budget: dict[str, Any] | None = None
            repack_result: dict[str, Any] | None = None
            selected_choices: list[int] | None = None
            compact_expansion = False
            required_headroom = 4096
            for quality in encoding_quality_values:
                slug = rate_control_slug(quality)
                label = rate_control_label(quality)
                encoded = cell_dir / f"encoded-{slug}.ts"
                attempt_path = cell_dir / f"attempt-{slug}.json"
                cached_attempt = load_cached_encode_attempt(attempt_path, cache_key)
                expected_control = parse_rate_control(quality)["canonical"]
                cached_encode = (cached_attempt or {}).get("encode") or {}
                cached_encoded = Path(str((cached_attempt or {}).get("encoded") or ""))
                if (
                    cached_attempt
                    and cached_encoded.is_file()
                    and cached_encoded.stat().st_size > 0
                    and str((cached_encode.get("rate_control") or {}).get("canonical")) == expected_control
                    and cached_encode.get("encoder") == encoder
                    and cached_encode.get("preset") == preset
                    and cached_encode.get("cadence_mode") == selected_cadence
                    and int((cached_attempt.get("budget") or {}).get("vobu_count") or 0)
                    == len(relevant_vobus)
                    and int((cached_attempt.get("idr_alignment") or {}).get("failures") or 0) == 0
                ):
                    progress(f"[{position}/{len(cells)}] Reusing validated {label} encode for {name}")
                    attempt = cached_attempt
                else:
                    progress(f"[{position}/{len(cells)}] Encoding {name} at {label}")
                    encode_result = encode_cell_hevc(
                        source_cell,
                        encoded,
                        ffmpeg=ffmpeg,
                        quality=quality,
                        preset=preset,
                        threads=threads,
                        log=cell_dir / f"encode-{slug}.log",
                        encoder=encoder,
                        cadence_mode=selected_cadence,
                        force_keyframe_ticks=vobu_keyframe_ticks(relevant_vobus),
                        duration_seconds=cell_duration_ticks / 90_000.0,
                        progress_callback=lambda fraction, p=position: emit_video_progress(
                            cell_position=p, cell_fraction=0.05 + 0.85 * fraction,
                            phase="encoding",
                        ),
                    )
                    packets = read_video_pes(encoded)
                    budget = analyze_vobu_budgets(
                        source_cell,
                        cell_first_sector=first_sector,
                        cell_last_sector=last_sector,
                        vobus=relevant_vobus,
                        encoded_pes=packets,
                    )
                    idr_alignment = validate_vobu_random_access(packets, budget)
                    attempt = {
                        "cache_key": cache_key,
                        "encode": encode_result,
                        "encoded": str(encoded.resolve()),
                        "encoded_identity": _encoded_file_identity(encoded),
                        "budget": budget,
                        "idr_alignment": idr_alignment,
                    }
                attempts.append(attempt)
                _atomic_json(attempt_path, attempt)
                if prefer_compact_input:
                    candidate_budget = attempt["budget"]
                    candidate_idr = attempt["idr_alignment"]
                    if (
                        int(candidate_budget.get("unassigned_pes_count") or 0) == 0
                        and int(candidate_idr.get("failures") or 0) == 0
                    ):
                        selected_budget = candidate_budget
                        selected_choices = [len(attempts) - 1] * int(
                            candidate_budget["vobu_count"]
                        )
                        adaptive_idr_alignment = candidate_idr
                        compact_expansion = True
                        break
                    progress(
                        f"[{position}/{len(cells)}] {label} did not produce complete "
                        "random-access HEVC VOBUs; retrying"
                    )
                    continue
                choice = choose_cell_attempt(
                    [row["budget"] for row in attempts],
                    minimum_headroom=required_headroom,
                )
                if choice is None:
                    choice = choose_cell_attempt(
                        [row["budget"] for row in attempts], minimum_headroom=0,
                    )
                    if choice is None:
                        progress(f"[{position}/{len(cells)}] {label} left overflowing VOBUs; retrying")
                        continue
                    progress(
                        f"[{position}/{len(cells)}] {label} fits the measured payload; "
                        "proving the exact sector repack"
                    )
                choices = [choice] * int(attempts[choice]["budget"]["vobu_count"])
                packets = read_video_pes(Path(attempts[choice]["encoded"]))
                adaptive_budget = analyze_vobu_budgets(
                    source_cell,
                    cell_first_sector=first_sector,
                    cell_last_sector=last_sector,
                    vobus=relevant_vobus,
                    encoded_pes=packets,
                )
                adaptive_idr_alignment = validate_vobu_random_access(packets, adaptive_budget)
                try:
                    repack_result = repack_cell_hevc(
                        source_cell,
                        repacked,
                        cell_first_sector=first_sector,
                        cell_last_sector=last_sector,
                        vobus=relevant_vobus,
                        encoded_pes=packets,
                        psm_policy="every-video-vobu",
                    )
                except ValueError as exc:
                    selected_attempt = attempts[choice]
                    selected_attempt["repack_error"] = str(exc)
                    selected_slug = rate_control_slug(selected_attempt["encode"]["quality"])
                    _atomic_json(cell_dir / f"attempt-{selected_slug}.json", selected_attempt)
                    required_headroom *= 2
                    progress(
                        f"[{position}/{len(cells)}] {label} did not fit packet overhead; "
                        f"retrying with {required_headroom} bytes of safety"
                    )
                    continue
                selected_budget = adaptive_budget
                selected_choices = choices
                break
            if selected_budget is None and allow_compact_expansion and attempts:
                # Compaction assigns a fresh sector count to every VOBU. Keep
                # the exact requested encode when its random-access structure
                # is valid, even if a few VOBUs exceed their old MPEG-2 slots.
                candidate = attempts[0]
                candidate_budget = candidate["budget"]
                candidate_idr = candidate["idr_alignment"]
                if (
                    int(candidate_budget.get("unassigned_pes_count") or 0) == 0
                    and int(candidate_idr.get("failures") or 0) == 0
                ):
                    selected_budget = candidate_budget
                    selected_choices = [0] * int(candidate_budget["vobu_count"])
                    adaptive_idr_alignment = candidate_idr
                    compact_expansion = True
                    progress(
                        f"[{position}/{len(cells)}] Keeping exact {rate_control_label(quality_values[0])}; "
                        "the compact layout will enlarge its overflowing VOBUs"
                    )
            if selected_budget is None or selected_choices is None or (
                repack_result is None and not compact_expansion
            ):
                raise PipelineError(f"No configured video rate control produced usable VOBUs for {name}")
            progress(f"[{position}/{len(cells)}] Validating {name}")
            emit_video_progress(cell_position=position, cell_fraction=0.92, phase="validating")
            if compact_expansion:
                validation = validate_compact_encoded_cell(
                    source_cell, Path(attempts[selected_choices[0]]["encoded"]),
                    ffmpeg=ffmpeg, ffprobe=ffprobe, decode_log=cell_dir / "decode.log",
                    expected_frame_multiplier=2 if selected_cadence == "deinterlace50" else 1,
                )
            else:
                validation = validate_repacked_cell(
                    source_cell,
                    repacked,
                    expected_vobus=len(relevant_vobus),
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    decode_log=cell_dir / "decode.log",
                    expected_frame_multiplier=2 if selected_cadence == "deinterlace50" else 1,
                    expected_program_stream_maps=sum(
                        int(row["packet_count"]) > 0 for row in selected_budget["vobus"]
                    ),
                )
            cell_report = {
                "status": "passed",
                "cache_key": cache_key,
                "reused_in_run": False,
                "encoder": encoder,
                "cadence_mode": selected_cadence,
                "cadence_inherited": cadence_inherited,
                "cadence_analysis": cadence_analysis,
                "cell": cell,
                "name": name,
                "source_cell": str(source_cell),
                "repacked_cell": None if compact_expansion else str(repacked),
                "layout_mode": "compact-input" if compact_expansion else "sector-preserving",
                "extraction": extraction,
                "vobu_count": len(relevant_vobus),
                "attempts": attempts,
                "selected_quality": attempts[selected_choices[0]]["encode"]["quality"],
                "vobu_quality_counts": dict(
                    Counter(str(attempts[index]["encode"]["quality"]) for index in selected_choices)
                ),
                "repack": repack_result,
                "idr_alignment": adaptive_idr_alignment,
                "validation": validation,
            }
            report["cells"].append(cell_report)
            _atomic_json(cell_dir / "cell-report.json", cell_report)
            _atomic_json(report_path, report)
            completed_duration_ticks += cell_duration_ticks
            emit_video_progress(cell_position=position, cell_fraction=0.0, phase="complete")
        audio_prefetch_result = None
        if audio_prefetch_future is not None:
            audio_prefetch_result = audio_prefetch_future.result()
        if audio_prefetch_executor is not None:
            audio_prefetch_executor.shutdown(wait=True, cancel_futures=True)
            audio_prefetch_executor = None
        report["status"] = "passed"
        report["audio_prefetch"] = (
            {
                "status": audio_prefetch_result.get("status"),
                "vts": audio_prefetch_result.get("vts"),
                "summary": audio_prefetch_result.get("summary"),
            }
            if audio_prefetch_result else None
        )
        report["summary"] = {
            "cells_converted": len(report["cells"]),
            "vobus_converted": sum(int(cell["vobu_count"]) for cell in report["cells"]),
            "source_frames": sum(int(cell["validation"]["source_video"]["nb_read_frames"]) for cell in report["cells"]),
            "output_frames": sum(int(cell["validation"]["output_video"]["nb_read_frames"]) for cell in report["cells"]),
            "all_audio_hashes_match": all(
                cell["validation"]["source_audio_hash"] == cell["validation"]["output_audio_hash"]
                for cell in report["cells"]
            ),
            "reused_cells": reused_cells,
        }
        _atomic_json(report_path, report)
        progress(f"Title {title_number} conversion passed: {report_path}")
        return report
    except Exception as exc:
        if audio_prefetch_executor is not None:
            audio_prefetch_executor.shutdown(wait=True, cancel_futures=True)
        report["status"] = "failed"
        report["error"] = str(exc)
        _atomic_json(report_path, report)
        raise


def convert_menu_domain(
    source: Path,
    *,
    domain: str,
    vts_number: int,
    workspace: Path,
    preset: str = "p6",
    quality_values: tuple[int | float | str, ...] = (20, 22, 24, 26, 28, 30),
    encoder: str = "hevc_nvenc",
    cadence: str = "auto",
    ambiguous_cadence: str = "progressive",
    quality_preset: dict[str, Any] | None = None,
    allow_compact_expansion: bool = False,
    progress: Progress = print,
) -> dict[str, Any]:
    """Convert every physical cell in a VMG or VTS menu domain."""
    if domain not in {"vmg_menu", "vts_menu"}:
        raise PipelineError("Menu domain must be vmg_menu or vts_menu")
    if domain == "vmg_menu" and vts_number != 0:
        raise PipelineError("The VMG menu uses VTS number 0")
    if domain == "vts_menu" and vts_number < 1:
        raise PipelineError("A VTS menu requires a positive VTS number")
    source = source.resolve()
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    report_path = workspace / "menu-report.json"
    tools = discover_tools()
    ffmpeg = tools.get("ffmpeg")
    ffprobe = tools.get("ffprobe")
    dvdinspect = find_dvdinspect()
    if not ffmpeg or not ffprobe or not dvdinspect:
        raise PipelineError("ffmpeg, ffprobe, and dvdinspect are required")
    if encoder not in HEVC_ENCODERS:
        raise PipelineError(f"Unsupported HEVC encoder: {encoder}")
    if cadence == "interlaced":
        raise PipelineError("Menu video must be progressive or deinterlaced")

    graph = inspect_physical_graph(source, dvdinspect, timeout=300)
    if domain == "vmg_menu":
        addresses = graph.get("vmg_menu_cell_addresses", [])
        menu_pgci = graph.get("vmg_menu_pgci", [])
        vobus = (graph.get("vmg_menu_vobu_map") or {}).get("vobus", [])
    else:
        vts = next(
            (row for row in graph.get("title_sets", []) if int(row.get("vts", 0)) == vts_number),
            None,
        )
        if vts is None:
            raise PipelineError(f"Physical graph has no VTS {vts_number}")
        addresses = vts.get("menu_cell_addresses", [])
        menu_pgci = vts.get("menu_pgci", [])
        vobus = (vts.get("menu_vobu_map") or {}).get("vobus", [])
    cells = menu_physical_cells(addresses, menu_pgci)
    if not cells or not vobus:
        raise PipelineError(f"{domain} VTS {vts_number} has no physical menu video")

    identity = source_identity(source)
    settings = {
        "domain": domain,
        "encoder": encoder,
        "cadence": cadence,
        "ambiguous_cadence": ambiguous_cadence,
        "preset": preset,
        "quality_values": list(quality_values),
        "quality_preset": quality_preset or {"name": "manual"},
        "keyframe_policy": "dvd-vobu-idr-v1",
        "program_stream_map_policy": "dvd-vobu-psm-v1",
        "quality_fallback_scope": "whole-cell",
        "terminal_hevc_still_policy": "repeat-single-picture-one-tick-plus-eos-v1",
        "threads": 0,
    }
    encoding_quality_values = (
        quality_values
        if allow_compact_expansion
        else sector_fit_rate_controls(quality_values)
    )
    settings["sector_fit_rate_controls"] = list(encoding_quality_values)
    report: dict[str, Any] = {
        "schema": "dvd2hevc-menu-conversion-v0",
        "status": "running",
        "source": str(source),
        "source_identity": identity,
        "pipeline_revision": PIPELINE_REVISION,
        "domain": domain,
        "vts": vts_number,
        "physical_cell_count": len(cells),
        "menu_language_units": len(menu_pgci),
        "extraction_policy": "single-pass-domain-fanout-v1",
        "settings": settings,
        "cells": [],
    }
    reused_cells = 0
    last_confident_cadence: str | None = None
    total_duration_ticks = sum(max(1, int(cell.get("duration_ticks") or 0)) for cell in cells)
    completed_duration_ticks = 0

    def emit_menu_progress(
        *, cell_position: int, cell_fraction: float, phase: str,
    ) -> None:
        cell_ticks = max(1, int(cells[cell_position - 1].get("duration_ticks") or 0))
        current = completed_duration_ticks + round(
            cell_ticks * max(0.0, min(1.0, cell_fraction))
        )
        progress_event(
            "video", "progress", f"{domain}-{vts_number}",
            scope="video-task-duration", current=current, total=total_duration_ticks,
            unit="ticks", phase=phase, cell=cell_position, cells=len(cells),
        )
    _atomic_json(report_path, report)
    try:
        extraction_requests: list[tuple[int, int, Path]] = []
        for cell in cells:
            first_sector = int(cell["first_sector"])
            last_sector = int(cell["last_sector"])
            name = (
                f"{domain}-vts{vts_number:02d}-vob{int(cell['vob_id']):02d}-"
                f"cell{int(cell['cell_id']):02d}-{first_sector}-{last_sector}"
            )
            cell_dir = workspace / "cells" / name
            relevant_vobus = [
                row for row in vobus
                if first_sector <= int(row["sector"]) <= last_sector
            ]
            if not relevant_vobus:
                raise PipelineError(f"No VOBUs found for {name}")
            cache_key = cell_cache_key(
                identity,
                vts=vts_number,
                cell=cell,
                vobus=relevant_vobus,
                settings=settings,
            )
            cached = load_cached_cell_report(cell_dir / "cell-report.json", cache_key)
            if (
                cached is not None
                and cached.get("layout_mode") == "compact-input"
                and not allow_compact_expansion
            ):
                cached = None
            if cached is None:
                extraction_requests.append(
                    (first_sector, last_sector, cell_dir / "source.vob")
                )
        if extraction_requests:
            progress(
                f"Batch-extracting {len(extraction_requests)} {domain} cell(s) from "
                f"VTS {vts_number:02d} in one ISO pass"
            )
        extraction_by_destination = {
            Path(row["destination"]).resolve(): row
            for row in extract_domain_sector_ranges(
                source,
                domain=domain,
                vts=vts_number,
                ranges=extraction_requests,
            )
        }
        for position, cell in enumerate(cells, start=1):
            cell_duration_ticks = max(1, int(cell.get("duration_ticks") or 0))
            emit_menu_progress(cell_position=position, cell_fraction=0.0, phase="preparing")
            first_sector = int(cell["first_sector"])
            last_sector = int(cell["last_sector"])
            name = (
                f"{domain}-vts{vts_number:02d}-vob{int(cell['vob_id']):02d}-"
                f"cell{int(cell['cell_id']):02d}-{first_sector}-{last_sector}"
            )
            cell_dir = workspace / "cells" / name
            cell_dir.mkdir(parents=True, exist_ok=True)
            source_cell = cell_dir / "source.vob"
            repacked = cell_dir / "repacked.vob"
            relevant_vobus = [row for row in vobus if first_sector <= int(row["sector"]) <= last_sector]
            if not relevant_vobus:
                raise PipelineError(f"No VOBUs found for {name}")
            cache_key = cell_cache_key(
                identity,
                vts=vts_number,
                cell=cell,
                vobus=relevant_vobus,
                settings=settings,
            )
            cached = load_cached_cell_report(cell_dir / "cell-report.json", cache_key)
            if (
                cached is not None
                and cached.get("layout_mode") == "compact-input"
                and not allow_compact_expansion
            ):
                cached = None
            if cached is not None:
                reused_cells += 1
                cached = dict(cached)
                cached["reused_in_run"] = True
                report["cells"].append(cached)
                if cached.get("cadence_mode") in {"progressive", "deinterlace50"}:
                    last_confident_cadence = str(cached["cadence_mode"])
                _atomic_json(report_path, report)
                progress(f"[{position}/{len(cells)}] Reusing validated {name}")
                completed_duration_ticks += cell_duration_ticks
                emit_menu_progress(cell_position=position, cell_fraction=0.0, phase="reused")
                continue

            progress(f"[{position}/{len(cells)}] Using batch-extracted {name}")
            extraction = extraction_by_destination.get(source_cell.resolve())
            if extraction is None:
                raise PipelineError(f"Batch extraction result is missing for {name}")
            cadence_analysis: dict[str, Any] | None = None
            selected_cadence = "progressive"
            cadence_inherited = False
            if cadence == "auto":
                cadence_analysis = analyze_cadence(
                    source_cell,
                    ffmpeg=ffmpeg,
                    duration_seconds=float(cell.get("duration_ticks", 0)) / 90000.0,
                )
                classification = cadence_analysis["classification"]
                if classification == "progressive":
                    selected_cadence = "progressive"
                elif classification == "interlaced":
                    selected_cadence = "deinterlace50"
                elif last_confident_cadence is not None:
                    selected_cadence = last_confident_cadence
                    cadence_inherited = True
                elif ambiguous_cadence == "fail":
                    raise PipelineError(f"Cadence is ambiguous for {name}")
                else:
                    selected_cadence = ambiguous_cadence
                _atomic_json(cell_dir / "cadence-analysis.json", cadence_analysis)
            else:
                selected_cadence = cadence
            if cadence_analysis and cadence_analysis["classification"] != "ambiguous":
                last_confident_cadence = selected_cadence

            attempts: list[dict[str, Any]] = []
            selected_budget: dict[str, Any] | None = None
            repack_result: dict[str, Any] | None = None
            selected_choices: list[int] | None = None
            required_headroom = 4096
            for quality in encoding_quality_values:
                slug = rate_control_slug(quality)
                label = rate_control_label(quality)
                encoded = cell_dir / f"encoded-{slug}.ts"
                progress(f"[{position}/{len(cells)}] Encoding {name} at {label}")
                encode_result = encode_cell_hevc(
                    source_cell,
                    encoded,
                    ffmpeg=ffmpeg,
                    quality=quality,
                    preset=preset,
                    threads=0,
                    log=cell_dir / f"encode-{slug}.log",
                    encoder=encoder,
                    cadence_mode=selected_cadence,
                    force_keyframe_ticks=vobu_keyframe_ticks(relevant_vobus),
                    duration_seconds=cell_duration_ticks / 90_000.0,
                    progress_callback=lambda fraction, p=position: emit_menu_progress(
                        cell_position=p, cell_fraction=0.05 + 0.85 * fraction,
                        phase="encoding",
                    ),
                )
                encoded_packets = read_video_pes(encoded)
                packets = prepare_hevc_dvd_menu(encoded_packets)
                budget = analyze_vobu_budgets(
                    source_cell,
                    cell_first_sector=first_sector,
                    cell_last_sector=last_sector,
                    vobus=relevant_vobus,
                    encoded_pes=packets,
                )
                idr_alignment = validate_vobu_random_access(packets, budget)
                attempt = {
                    "encode": encode_result,
                    "encoded": str(encoded.resolve()),
                    "budget": budget,
                    "idr_alignment": idr_alignment,
                    "terminal_picture_guard_addition": len(packets) - len(encoded_packets),
                }
                attempts.append(attempt)
                _atomic_json(cell_dir / f"attempt-{slug}.json", attempt)
                choice = choose_cell_attempt(
                    [row["budget"] for row in attempts], minimum_headroom=required_headroom
                )
                if choice is None:
                    choice = choose_cell_attempt(
                        [row["budget"] for row in attempts], minimum_headroom=0,
                    )
                    if choice is None:
                        progress(f"[{position}/{len(cells)}] {label} overflowed a VOBU; retrying")
                        continue
                    progress(
                        f"[{position}/{len(cells)}] {label} fits the measured payload; "
                        "proving the exact sector repack"
                    )
                choices = [choice] * int(attempts[choice]["budget"]["vobu_count"])
                packets = prepare_hevc_dvd_menu(
                    read_video_pes(Path(attempts[choice]["encoded"]))
                )
                adaptive_budget = analyze_vobu_budgets(
                    source_cell,
                    cell_first_sector=first_sector,
                    cell_last_sector=last_sector,
                    vobus=relevant_vobus,
                    encoded_pes=packets,
                )
                adaptive_idr_alignment = validate_vobu_random_access(packets, adaptive_budget)
                try:
                    repack_result = repack_cell_hevc(
                        source_cell,
                        repacked,
                        cell_first_sector=first_sector,
                        cell_last_sector=last_sector,
                        vobus=relevant_vobus,
                        encoded_pes=packets,
                        psm_policy="every-video-vobu",
                    )
                except ValueError as exc:
                    selected_attempt = attempts[choice]
                    selected_attempt["repack_error"] = str(exc)
                    selected_slug = rate_control_slug(selected_attempt["encode"]["quality"])
                    _atomic_json(cell_dir / f"attempt-{selected_slug}.json", selected_attempt)
                    required_headroom *= 2
                    progress(f"[{position}/{len(cells)}] Retrying with {required_headroom} bytes of safety")
                    continue
                selected_budget = adaptive_budget
                selected_choices = choices
                break
            compact_expansion = False
            if selected_budget is None and allow_compact_expansion and attempts:
                candidate = attempts[0]
                candidate_budget = candidate["budget"]
                candidate_idr = candidate["idr_alignment"]
                if (
                    int(candidate_budget.get("unassigned_pes_count") or 0) == 0
                    and int(candidate_idr.get("failures") or 0) == 0
                ):
                    selected_budget = candidate_budget
                    selected_choices = [0] * int(candidate_budget["vobu_count"])
                    adaptive_idr_alignment = candidate_idr
                    compact_expansion = True
                    progress(
                        f"[{position}/{len(cells)}] Keeping exact "
                        f"{rate_control_label(quality_values[0])}; compact menu layout will "
                        "rebalance its VOBU sectors"
                    )
            if selected_budget is None or selected_choices is None or (
                repack_result is None and not compact_expansion
            ):
                raise PipelineError(f"No configured video rate control produced usable VOBUs for {name}")

            progress(f"[{position}/{len(cells)}] Validating {name}")
            emit_menu_progress(cell_position=position, cell_fraction=0.92, phase="validating")
            if compact_expansion:
                validation = validate_compact_encoded_cell(
                    source_cell, Path(attempts[selected_choices[0]]["encoded"]),
                    ffmpeg=ffmpeg, ffprobe=ffprobe, decode_log=cell_dir / "decode.log",
                    expected_frame_multiplier=2 if selected_cadence == "deinterlace50" else 1,
                )
            else:
                validation = validate_repacked_cell(
                    source_cell,
                    repacked,
                    expected_vobus=len(relevant_vobus),
                    ffmpeg=ffmpeg,
                    ffprobe=ffprobe,
                    decode_log=cell_dir / "decode.log",
                    expected_frame_multiplier=2 if selected_cadence == "deinterlace50" else 1,
                    expected_frame_addition=int(
                        attempts[selected_choices[0]].get(
                            "terminal_picture_guard_addition", 0
                        )
                    ),
                    expected_program_stream_maps=sum(
                        int(row["packet_count"]) > 0 for row in selected_budget["vobus"]
                    ),
                )
            cell_report = {
                "status": "passed",
                "cache_key": cache_key,
                "reused_in_run": False,
                "domain": domain,
                "encoder": encoder,
                "cadence_mode": selected_cadence,
                "cadence_inherited": cadence_inherited,
                "cadence_analysis": cadence_analysis,
                "cell": cell,
                "name": name,
                "source_cell": str(source_cell),
                "repacked_cell": None if compact_expansion else str(repacked),
                "layout_mode": "compact-input" if compact_expansion else "sector-preserving",
                "extraction": extraction,
                "vobu_count": len(relevant_vobus),
                "attempts": attempts,
                "selected_quality": attempts[selected_choices[0]]["encode"]["quality"],
                "vobu_quality_counts": dict(
                    Counter(str(attempts[index]["encode"]["quality"]) for index in selected_choices)
                ),
                "repack": repack_result,
                "idr_alignment": adaptive_idr_alignment,
                "validation": validation,
            }
            report["cells"].append(cell_report)
            _atomic_json(cell_dir / "cell-report.json", cell_report)
            _atomic_json(report_path, report)
            completed_duration_ticks += cell_duration_ticks
            emit_menu_progress(cell_position=position, cell_fraction=0.0, phase="complete")

        report["status"] = "passed"
        report["summary"] = {
            "cells_converted": len(report["cells"]),
            "expected_cells": len(cells),
            "vobus_converted": sum(int(cell["vobu_count"]) for cell in report["cells"]),
            "expected_vobus": len(vobus),
            "source_frames": sum(
                int(cell["validation"]["source_video"]["nb_read_frames"]) for cell in report["cells"]
            ),
            "output_frames": sum(
                int(cell["validation"]["output_video"]["nb_read_frames"]) for cell in report["cells"]
            ),
            "all_audio_hashes_match": all(
                cell["validation"]["source_audio_hash"] == cell["validation"]["output_audio_hash"]
                for cell in report["cells"]
            ),
            "reused_cells": reused_cells,
        }
        if report["summary"]["vobus_converted"] != len(vobus):
            raise PipelineError(f"Menu VOBU coverage mismatch: {report['summary']}")
        _atomic_json(report_path, report)
        progress(f"{domain} VTS {vts_number} conversion passed: {report_path}")
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        _atomic_json(report_path, report)
        raise
from .subprocess_utils import hidden_subprocess_kwargs
