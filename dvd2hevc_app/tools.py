"""External tool and Python dependency discovery."""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any

from .native import find_dvdinspect


HANDBRAKE_CANDIDATES = [
    Path.home() / "Downloads" / "HandBrakeCLI-1.10.2-win-x86_64" / "HandBrakeCLI.exe",
    Path(r"C:\Program Files\HandBrake\HandBrakeCLI.exe"),
    Path(r"C:\Program Files (x86)\HandBrake\HandBrakeCLI.exe"),
]
FFMPEG_ROOT_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "tools" / "ffmpeg" / "bin",
    Path(__file__).resolve().parents[2] / "bd2hevc" / "tools" / "ffmpeg" / "bin",
    Path.home() / "Downloads" / "BD to UHD-BD" / "tools" / "ffmpeg"
    / "ffmpeg-8.0.1-essentials_build" / "bin",
]
FFMPEG_CANDIDATES = [root / "ffmpeg.exe" for root in FFMPEG_ROOT_CANDIDATES]
FFPROBE_CANDIDATES = [root / "ffprobe.exe" for root in FFMPEG_ROOT_CANDIDATES]
VLC_CANDIDATES = [
    Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
]


def find_executable(name: str, candidates: list[Path] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates or []:
        if candidate.is_file():
            return str(candidate)
    return None


def discover_tools() -> dict[str, Any]:
    return {
        "python_pycdlib": importlib.util.find_spec("pycdlib") is not None,
        "ffmpeg": find_executable("ffmpeg", FFMPEG_CANDIDATES),
        "ffprobe": find_executable("ffprobe", FFPROBE_CANDIDATES),
        "handbrake": find_executable("HandBrakeCLI", HANDBRAKE_CANDIDATES),
        "git": find_executable("git"),
        "cmake": find_executable("cmake"),
        "xorriso": find_executable("xorriso"),
        "vlc": find_executable("vlc", VLC_CANDIDATES),
        "dvdinspect": find_dvdinspect(),
        "platform": os.name,
    }


def required_tools_ok(tools: dict[str, Any], *, for_scan: bool = True) -> bool:
    if for_scan:
        return bool(tools.get("python_pycdlib"))
    return bool(tools.get("python_pycdlib") and tools.get("ffmpeg") and tools.get("ffprobe"))
