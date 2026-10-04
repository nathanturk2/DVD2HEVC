"""HandBrake title scanning and JSON normalization."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


TITLE_MARKER = "JSON Title Set:"


def parse_title_set(text: str) -> dict[str, Any]:
    marker = text.find(TITLE_MARKER)
    if marker < 0:
        raise ValueError("HandBrake output did not contain a JSON title set")
    payload = text[marker + len(TITLE_MARKER) :].lstrip()
    value, _end = json.JSONDecoder().raw_decode(payload)
    if not isinstance(value, dict):
        raise ValueError("HandBrake title set was not a JSON object")
    return value


def duration_seconds(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    if value.get("Ticks") is not None:
        try:
            return float(value["Ticks"]) / 90_000.0
        except (TypeError, ValueError):
            pass
    try:
        return float(value.get("Hours", 0)) * 3600 + float(value.get("Minutes", 0)) * 60 + float(value.get("Seconds", 0))
    except (TypeError, ValueError):
        return None


def normalize_titles(title_set: dict[str, Any]) -> list[dict[str, Any]]:
    titles: list[dict[str, Any]] = []
    for raw in title_set.get("TitleList") or []:
        duration = duration_seconds(raw.get("Duration"))
        titles.append(
            {
                "index": raw.get("Index"),
                "name": raw.get("Name"),
                "duration_seconds": duration,
                "chapter_count": len(raw.get("ChapterList") or []),
                "angle_count": raw.get("AngleCount"),
                "audio_track_count": len(raw.get("AudioList") or []),
                "subtitle_track_count": len(raw.get("SubtitleList") or []),
                "geometry": raw.get("Geometry"),
                "frame_rate": raw.get("FrameRate"),
            }
        )
    return titles


def scan_titles(source: Path, handbrake: str, *, min_duration: int = 1, timeout: float = 300.0) -> dict[str, Any]:
    command = [
        handbrake,
        "--json",
        "-i",
        str(source),
        "-t",
        "0",
        "--scan",
        "--min-duration",
        str(min_duration),
    ]
    completed = subprocess.run(
        command, check=False, capture_output=True, timeout=timeout,
        **hidden_subprocess_kwargs(),
    )
    output = (completed.stdout + completed.stderr).decode("utf-8", "replace")
    title_set = parse_title_set(output)
    titles = normalize_titles(title_set)
    return {
        "available": True,
        "returncode": completed.returncode,
        "titles": titles,
        "title_count": len(titles),
        "longest_title": max(titles, key=lambda item: item.get("duration_seconds") or 0, default=None),
        "encrypted_support_unavailable": "Encrypted DVD support unavailable" in output,
    }
from .subprocess_utils import hidden_subprocess_kwargs
