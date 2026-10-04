"""Source-derived target bitrate policy and historical CQ diagnostics."""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from .pipeline import PipelineError
from .subprocess_utils import hidden_subprocess_kwargs
from .tools import discover_tools


# P6 results from three normalized DVD feature samples. These are a seed curve,
# not a promise that CQ has a content-independent bitrate.
BASELINE_CQ_KBPS = {
    20.0: 3481.8,
    22.0: 2684.9,
    24.0: 2061.4,
    26.0: 1599.2,
}

TARGET_BITRATE_BASE_RATIO = 0.25


def _quality_number(value: str | int | float) -> float:
    text = str(value).strip().lower()
    if text.startswith("cq:"):
        text = text[3:]
    try:
        result = float(text)
    except ValueError as exc:
        raise PipelineError(f"Invalid CQ quality: {value!r}") from exc
    if not 0 <= result <= 51:
        raise PipelineError("CQ quality must be between 0 and 51")
    return result


def _render_quality(value: float) -> str:
    return f"cq:{int(value) if value.is_integer() else f'{value:g}'}"


def estimate_disc_source_bitrate(
    compatibility_plan: dict[str, Any], full_scan: dict[str, Any]
) -> dict[str, Any]:
    """Estimate physical title-video bitrate from the mandatory sector scan.

    A DVD VOBU normally represents about 0.48 seconds.  Using unique physical
    title-domain VOBUs avoids counting shared/branched titles more than once,
    while the full scan supplies exact MPEG-2 PES payload bytes.
    """
    title_vobs = [
        row for row in (full_scan.get("video_ts") or full_scan.get("vob_scan") or {}).get("vobs", [])
        if row.get("domain") == "title"
    ]
    payload_bytes = sum(int((row.get("stats") or {}).get("video_payload_bytes") or 0) for row in title_vobs)
    vobus = int((compatibility_plan.get("summary") or {}).get("title_vobus") or 0)
    if payload_bytes <= 0 or vobus <= 0:
        raise PipelineError("The source scan has insufficient title-video data for target bitrate mode")
    graph = full_scan.get("physical_graph") or {}
    physical = {}
    invalid = 0
    for title_set in graph.get("title_sets") or []:
        for row in (title_set.get("title_vobu_map") or {}).get("vobus") or []:
            key = (int(title_set["vts"]), int(row["sector"]))
            start, end = row.get("start_ptm"), row.get("end_ptm")
            if start is None or end is None:
                invalid += 1
                continue
            ticks = (int(end) - int(start)) & 0xffffffff
            if ticks <= 0 or ticks > 90_000 * 60:
                invalid += 1
                continue
            physical[key] = ticks / 90_000
    measured = len(physical) == vobus and invalid == 0
    if graph.get("title_sets") and not measured:
        raise PipelineError(f"Cannot measure physical video duration: {len(physical)}/{vobus} VOBUs have valid timestamps")
    duration = sum(physical.values()) if measured else vobus * 0.48
    return {
        "video_payload_bytes": payload_bytes,
        "physical_title_vobus": vobus,
        "duration_seconds": duration,
        "estimated_duration_seconds": duration,
        "duration_measured": measured,
        "confidence": "measured" if measured else "estimated-no-native-timestamps",
        "seconds_per_vobu": None if measured else 0.48,
        "average_video_bps": payload_bytes * 8 / duration,
        "method": "unique-physical-vobu-ptm-and-scanned-mpeg2-payload-v2" if measured else "estimated-vobu-duration-and-scanned-mpeg2-payload-v1",
        "warning": None if measured else "Duration assumes 0.48 seconds per VOBU; bitrate is an estimate.",
    }



def resolve_disc_quality_policy(
    compatibility_plan: dict[str, Any],
    full_scan: dict[str, Any],
    *,
    quality: str = "target-bitrate",
    target_bitrate_multiplier: float = 1.0,
    bitrate_mode: str = "vbr",
    auto_cq_multiplier: float | None = None,
    main_title_quality: str | None = None,
    top_n_quality: str | None = None,
    top_n_count: int = 0,
    destination: Path | None = None,
) -> dict[str, Any]:
    """Resolve source-derived bitrate or manual CQ into per-VTS controls.

    ``auto_cq_multiplier`` is accepted only to migrate an old queued job or
    saved preset.  New callers use ``target_bitrate_multiplier`` and no CQ
    estimate is involved in target-bitrate mode.
    """
    multiplier = float(
        auto_cq_multiplier if auto_cq_multiplier is not None else target_bitrate_multiplier
    )
    if not 0.25 <= multiplier <= 4.0:
        raise PipelineError("Target bitrate multiplier must be between 0.25 and 4.0")
    bitrate_mode = str(bitrate_mode).strip().lower()
    if bitrate_mode not in {"vbr", "cbr"}:
        raise PipelineError("Bitrate mode must be VBR or CBR")
    if main_title_quality and (top_n_quality or top_n_count):
        raise PipelineError("Main-title quality and top-N quality are mutually exclusive")
    if bool(top_n_quality) != bool(top_n_count):
        raise PipelineError("Top-N quality requires both a positive count and a quality")

    source = estimate_disc_source_bitrate(compatibility_plan, full_scan)
    ratio = TARGET_BITRATE_BASE_RATIO * multiplier
    target_bps = standard_hevc_bitrate(float(source["average_video_bps"]), ratio)

    def resolve(value: str) -> dict[str, Any]:
        normalized = str(value).strip().lower()
        if normalized in {
            "auto", "auto-cq", "compact-auto", "target", "target-bitrate", "bitrate"
        }:
            exact_bps = int(round(target_bps))
            return {
                "requested": "target-bitrate",
                "resolved": f"{bitrate_mode}:{exact_bps}",
                "rate_control": bitrate_mode,
                "target_bps": exact_bps,
            }
        cq = _quality_number(normalized)
        return {
            "requested": normalized,
            "resolved": _render_quality(cq),
            "rate_control": "cq",
            "cq": cq,
        }

    general = resolve(quality)
    titles = sorted(
        [dict(row) for row in compatibility_plan.get("title_tasks") or [] if row.get("status") == "ready"],
        key=lambda row: (-float(row.get("duration_seconds") or 0), int(row.get("title") or 0)),
    )
    selected: list[dict[str, Any]] = []
    override: dict[str, Any] | None = None
    override_mode = "none"
    if main_title_quality:
        override = resolve(main_title_quality)
        selected = titles[:1]
        override_mode = "main-title"
    elif top_n_quality and top_n_count:
        override = resolve(top_n_quality)
        selected = titles[: int(top_n_count)]
        override_mode = "top-n"

    all_vts = sorted(int(row["vts"]) for row in compatibility_plan.get("vts") or [])
    quality_by_vts = {str(vts): dict(general) for vts in all_vts}
    vts_applications: list[dict[str, Any]] = []
    if override:
        for row in selected:
            vts = str(int(row["vts"]))
            # A whole VTS receives the explicit override because alternate
            # titles may reference the same physical cells. The user's
            # override is authoritative even when it changes rate-control mode.
            quality_by_vts[vts] = dict(override)
            vts_applications.append({
                "title": int(row["title"]),
                "vts": int(row["vts"]),
                "duration_seconds": float(row.get("duration_seconds") or 0),
                "resolved": quality_by_vts[vts]["resolved"],
                "scope": "entire-shared-vts",
            })

    result = {
        "schema": "dvd2hevc-disc-quality-policy-v2",
        "status": "resolved",
        "source": str(compatibility_plan.get("source") or full_scan.get("source") or ""),
        "target_bitrate": {
            "mode": bitrate_mode,
            "base_hevc_to_mpeg2_ratio": TARGET_BITRATE_BASE_RATIO,
            "bitrate_multiplier": multiplier,
            "effective_hevc_to_mpeg2_ratio": ratio,
            "source_measurement": source,
            "target_bps": int(round(target_bps)),
            "policy": "measured-mpeg2-average-times-ratio; no CQ estimation",
        },
        "general": general,
        "title_override": {
            "mode": override_mode,
            "count": len(selected),
            "quality": override,
            "vts_applications": vts_applications,
        },
        "quality_by_vts": quality_by_vts,
        "menu_quality": general,
        "physical_reuse_policy": "one-encode-per-physical-cell; title overrides apply to shared VTS",
    }
    if destination is not None:
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def standard_hevc_bitrate(source_mpeg2_bps: float, ratio: float = 0.25) -> float:
    if source_mpeg2_bps <= 0:
        raise PipelineError("Source MPEG-2 bitrate must be positive")
    if not 0 < ratio <= 1:
        raise PipelineError("HEVC source ratio must be greater than 0 and at most 1")
    return source_mpeg2_bps * ratio


def fit_cq_curve(points_kbps: dict[float, float]) -> tuple[float, float]:
    """Fit ln(kbit/s) = intercept + slope * CQ."""
    if len(points_kbps) < 2 or any(cq < 0 or kbps <= 0 for cq, kbps in points_kbps.items()):
        raise PipelineError("At least two positive CQ/bitrate calibration points are required")
    xs = list(points_kbps)
    ys = [math.log(points_kbps[x]) for x in xs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        raise PipelineError("CQ calibration points must use different quality values")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    if slope >= 0:
        raise PipelineError("CQ calibration must decrease bitrate as CQ increases")
    return mean_y - slope * mean_x, slope


def estimate_cq_for_bitrate(
    target_bps: float,
    points_kbps: dict[float, float] | None = None,
) -> dict[str, float]:
    if target_bps <= 0:
        raise PipelineError("Target HEVC bitrate must be positive")
    intercept, slope = fit_cq_curve(points_kbps or BASELINE_CQ_KBPS)
    estimated = (math.log(target_bps / 1000) - intercept) / slope
    half_step = min(51.0, max(0.0, round(estimated * 2) / 2))
    integer = min(51, max(0, round(estimated)))
    predicted_half_bps = math.exp(intercept + slope * half_step) * 1000
    predicted_integer_bps = math.exp(intercept + slope * integer) * 1000
    return {
        "estimated_cq": estimated,
        "nearest_half_cq": half_step,
        "nearest_integer_cq": float(integer),
        "predicted_half_step_bps": predicted_half_bps,
        "predicted_integer_bps": predicted_integer_bps,
        "curve_intercept": intercept,
        "curve_slope": slope,
    }


def _probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,duration:format=duration", "-of", "json", str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode:
        raise PipelineError(f"Could not inspect MPEG-2 video in {path}: {completed.stderr.strip()}")
    probe = json.loads(completed.stdout)
    streams = probe.get("streams") or []
    if not streams or streams[0].get("codec_name") != "mpeg2video":
        raise PipelineError(f"Expected an MPEG-2 video stream in {path}")
    duration_value = streams[0].get("duration") or (probe.get("format") or {}).get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"Could not determine video duration for {path}") from exc
    if duration <= 0:
        raise PipelineError(f"Invalid video duration for {path}: {duration}")
    return {"duration": duration, "codec": "mpeg2video"}


def measure_mpeg2_video(path: Path, ffmpeg: str, ffprobe: str, *, timeout: float = 1800) -> dict[str, Any]:
    """Count demuxed MPEG-2 elementary bytes, excluding audio and VOB overhead."""
    path = path.resolve()
    if not path.is_file():
        raise PipelineError(f"Source VOB is missing: {path}")
    video = _probe_video(path, ffprobe)
    with tempfile.TemporaryFile() as error_file:
        process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy", "-f", "mpeg2video", "-"],
            stdout=subprocess.PIPE, stderr=error_file, **hidden_subprocess_kwargs(),
        )
        timed_out = threading.Event()
        def expire():
            if process.poll() is None:
                timed_out.set()
                process.kill()
        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        payload_bytes = 0
        try:
            if process.stdout is None:
                raise PipelineError("Could not open FFmpeg measurement pipe")
            while chunk := process.stdout.read(1024 * 1024):
                payload_bytes += len(chunk)
            returncode = process.wait()
            error_file.seek(0, 2)
            error_file.seek(max(0, error_file.tell() - 65536))
            stderr = error_file.read().decode("utf-8", errors="replace")
            if timed_out.is_set():
                raise PipelineError(f"MPEG-2 measurement timed out after {timeout:g} seconds")
            if returncode:
                raise PipelineError(f"Could not demux MPEG-2 video from {path}: {stderr.strip()}")
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
    duration = float(video["duration"])
    return {
        "path": str(path),
        "duration_seconds": duration,
        "elementary_video_bytes": payload_bytes,
        "average_video_bps": payload_bytes * 8 / duration,
    }


def _collect_vobs(paths: list[Path]) -> list[Path]:
    collected: list[Path] = []
    for input_path in paths:
        path = input_path.resolve()
        if path.suffix.lower() != ".json":
            collected.append(path)
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        cells: list[dict[str, Any]] = list(report.get("cells") or [])
        for domain in report.get("domains") or []:
            cells.extend(domain.get("cells") or [])
        for cell in cells:
            source = cell.get("source_cell")
            if source:
                collected.append(Path(source).resolve())
    unique: list[Path] = []
    seen: set[str] = set()
    for path in collected:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        raise PipelineError("No source VOB cells were found for quality estimation")
    return unique


def estimate_equivalent_quality(
    inputs: list[Path],
    *,
    ratio: float = 0.25,
    destination: Path | None = None,
) -> dict[str, Any]:
    tools = discover_tools()
    ffmpeg = tools.get("ffmpeg")
    ffprobe = tools.get("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PipelineError("FFmpeg and FFprobe are required for quality estimation")
    measurements = [measure_mpeg2_video(path, ffmpeg, ffprobe) for path in _collect_vobs(inputs)]
    source_bytes = sum(int(row["elementary_video_bytes"]) for row in measurements)
    duration = sum(float(row["duration_seconds"]) for row in measurements)
    source_bps = source_bytes * 8 / duration
    target_bps = standard_hevc_bitrate(source_bps, ratio)
    cq = estimate_cq_for_bitrate(target_bps)
    result = {
        "schema": "dvd2hevc-equivalent-quality-v0",
        "status": "estimated",
        "inputs": [str(path.resolve()) for path in inputs],
        "measurements": measurements,
        "source": {
            "codec": "mpeg2video",
            "elementary_video_bytes": source_bytes,
            "duration_seconds": duration,
            "average_video_bps": source_bps,
        },
        "standard_equivalence": {
            "hevc_to_mpeg2_ratio": ratio,
            "target_hevc_bps": target_bps,
            "basis": "HEVC 50% of AVC and AVC 50% of MPEG-2 at equal perceptual quality",
        },
        "cq_estimate": {
            **cq,
            "encoder": "hevc_nvenc",
            "preset": "p6",
            "calibration_points_kbps": BASELINE_CQ_KBPS,
            "warning": "CQ bitrate is content-dependent; verify with representative samples",
        },
    }
    if destination is not None:
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def compact_auto_preset(estimate: dict[str, Any], *, requested_ratio: float | None = None) -> dict[str, Any]:
    """Resolve a saved standards-equivalent estimate into an encoding preset."""
    if estimate.get("schema") != "dvd2hevc-equivalent-quality-v0":
        raise PipelineError("Unsupported compact-auto quality estimate")
    standard = estimate.get("standard_equivalence") or {}
    cq = estimate.get("cq_estimate") or {}
    try:
        ratio = float(standard["hevc_to_mpeg2_ratio"])
        source_bps = float(estimate["source"]["average_video_bps"])
        target_bps = float(standard["target_hevc_bps"])
        quality = float(cq["nearest_half_cq"])
        predicted_bps = float(cq["predicted_half_step_bps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError("Incomplete compact-auto quality estimate") from exc
    if requested_ratio is not None and not math.isclose(ratio, requested_ratio, rel_tol=0, abs_tol=1e-9):
        raise PipelineError(
            f"Saved quality estimate uses ratio {ratio:g}, not requested ratio {requested_ratio:g}"
        )
    return {
        "name": "compact-auto",
        "resolved_quality": int(quality) if quality.is_integer() else quality,
        "source_mpeg2_bps": source_bps,
        "hevc_to_mpeg2_ratio": ratio,
        "target_hevc_bps": target_bps,
        "predicted_hevc_bps": predicted_bps,
        "selection": "nearest-half-step",
        "encoder": "hevc_nvenc",
        "encoder_preset": "p6",
        "feedback": (
            "Measure the completed HEVC average and regenerate at the adjacent half-step "
            "when it falls outside the target tolerance"
        ),
    }
