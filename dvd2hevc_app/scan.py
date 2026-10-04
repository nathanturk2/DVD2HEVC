"""High-level scan orchestration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .handbrake import scan_titles
from .iso import scan_iso
from .native import inspect_physical_graph
from .tools import discover_tools


def scan_disc(
    source: Path,
    *,
    inspect_vobs: bool = True,
    use_handbrake: bool = True,
    use_native: bool = True,
    min_title_duration: int = 1,
) -> dict[str, Any]:
    result = scan_iso(source, inspect_vobs=inspect_vobs)
    tools = discover_tools()
    if use_native and tools.get("dvdinspect"):
        try:
            result["physical_graph"] = inspect_physical_graph(source.resolve(), str(tools["dvdinspect"]))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            result["physical_graph"] = {"available": True, "ok": False, "error": str(exc)}
    else:
        result["physical_graph"] = {"available": bool(tools.get("dvdinspect")), "skipped": True}
    if use_handbrake and tools.get("handbrake"):
        try:
            result["title_scan"] = scan_titles(source.resolve(), str(tools["handbrake"]), min_duration=min_title_duration)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result["title_scan"] = {"available": True, "ok": False, "error": str(exc), "titles": []}
    else:
        result["title_scan"] = {
            "available": bool(tools.get("handbrake")),
            "skipped": True,
            "titles": [],
        }
    graph_titles = ((result.get("physical_graph") or {}).get("summary") or {}).get("global_titles")
    handbrake_titles = (result.get("title_scan") or {}).get("title_count")
    result["cross_checks"] = {
        "native_and_handbrake_title_counts_match": graph_titles == handbrake_titles if graph_titles is not None and handbrake_titles is not None else None,
        "native_global_title_count": graph_titles,
        "handbrake_title_count": handbrake_titles,
        "note": "Different counts can be legitimate when HandBrake filters duplicate, tiny, or non-user-facing VMG titles.",
    }
    return result
