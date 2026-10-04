"""Pixel-based progressive/interlaced classification for hardware encoding."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


IDET_MULTI = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)"
)
IDET_REPEAT = re.compile(r"Repeated Fields:\s*Neither:\s*(\d+)\s+Top:\s*(\d+)\s+Bottom:\s*(\d+)")


def parse_idet_output(text: str) -> dict[str, int]:
    multi = IDET_MULTI.findall(text)
    repeat = IDET_REPEAT.findall(text)
    if not multi:
        raise ValueError("ffmpeg idet output did not contain multi-frame results")
    tff, bff, progressive, undetermined = (int(value) for value in multi[-1])
    neither, repeated_top, repeated_bottom = (int(value) for value in repeat[-1]) if repeat else (0, 0, 0)
    return {
        "tff": tff,
        "bff": bff,
        "progressive": progressive,
        "undetermined": undetermined,
        "neither": neither,
        "repeated_top": repeated_top,
        "repeated_bottom": repeated_bottom,
    }


def classify_idet_totals(totals: dict[str, int]) -> dict[str, Any]:
    interlaced = totals["tff"] + totals["bff"]
    determinate = interlaced + totals["progressive"]
    if determinate == 0:
        classification = "ambiguous"
        progressive_ratio = 0.0
        interlaced_ratio = 0.0
    else:
        progressive_ratio = totals["progressive"] / determinate
        interlaced_ratio = interlaced / determinate
        if progressive_ratio >= 0.98:
            classification = "progressive"
        elif interlaced_ratio >= 0.10:
            classification = "interlaced"
        else:
            classification = "ambiguous"
    return {
        "classification": classification,
        "progressive_ratio": round(progressive_ratio, 6),
        "interlaced_ratio": round(interlaced_ratio, 6),
        "determinate_frames": determinate,
    }


def analyze_cadence(
    path: Path,
    *,
    ffmpeg: str,
    duration_seconds: float,
    sample_frames: int = 750,
) -> dict[str, Any]:
    sample_seconds = sample_frames / 25.0
    starts = sorted({
        0.0,
        max(0.0, duration_seconds / 2.0 - sample_seconds / 2.0),
        max(0.0, duration_seconds - sample_seconds),
    })
    samples: list[dict[str, Any]] = []
    for start in starts:
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{start:.6f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            "idet",
            "-frames:v",
            str(sample_frames),
            "-an",
            "-f",
            "null",
            "-",
        ]
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, errors="replace",
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"idet failed at {start:.3f}s: {completed.stderr.strip()}")
        sample = parse_idet_output(completed.stderr)
        sample["start_seconds"] = round(start, 6)
        # Retain a decision for each sampled region as well as the aggregate.
        # A title cell can contain progressive credits, telecined film, and
        # genuinely interlaced video at different points; the raw totals alone
        # made that useful mixed-content evidence needlessly hard to see.
        sample.update(classify_idet_totals(sample))
        samples.append(sample)
    keys = ("tff", "bff", "progressive", "undetermined", "neither", "repeated_top", "repeated_bottom")
    totals = {key: sum(int(sample[key]) for sample in samples) for key in keys}
    aggregate = classify_idet_totals(totals)
    confident_sample_classes = {
        str(sample["classification"])
        for sample in samples
        if sample["classification"] != "ambiguous"
    }
    repeated_fields = totals["repeated_top"] + totals["repeated_bottom"]
    repeated_observations = totals["neither"] + repeated_fields
    detected_pattern = (
        "mixed"
        if len(confident_sample_classes) > 1
        else str(aggregate["classification"])
    )
    return {
        "method": "ffmpeg-idet-three-sample-v0",
        "duration_seconds": duration_seconds,
        "sample_frames": sample_frames,
        "samples": samples,
        "totals": totals,
        "sample_classifications": [str(sample["classification"]) for sample in samples],
        "detected_pattern": detected_pattern,
        "mixed_sample_evidence": detected_pattern == "mixed",
        "repeated_field_ratio": round(
            repeated_fields / repeated_observations if repeated_observations else 0.0,
            6,
        ),
        "repeated_fields_detected": repeated_fields,
        "suggested_cadence": (
            "progressive"
            if aggregate["classification"] == "progressive"
            else "deinterlace50"
            if aggregate["classification"] == "interlaced"
            else "fallback-required"
        ),
        **aggregate,
    }
from .subprocess_utils import hidden_subprocess_kwargs
