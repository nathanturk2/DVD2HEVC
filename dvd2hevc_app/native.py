"""Native libdvdread physical-graph integration."""

from __future__ import annotations

import json
import os
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .subprocess_utils import hidden_subprocess_kwargs


from .paths import ROOT, REPORT_ROOT, STATE_ROOT, TOOL_ROOT
LOCAL_DVDINSPECT = ROOT / "tools" / "dvdinspect.exe"


@contextmanager
def _dvdinspect_source_path(source: Path) -> Iterator[Path]:
    """Give legacy libdvdread a short Windows path for deep stage folders."""
    resolved = source.resolve()
    if os.name != "nt" or not resolved.is_dir() or len(str(resolved)) < 220:
        yield resolved
        return
    root = Path(tempfile.mkdtemp(prefix="dvd2hevc-inspect-"))
    alias = root / "dvd"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(resolved)],
        check=False,
        capture_output=True,
        text=True,
        **hidden_subprocess_kwargs(),
    )
    if created.returncode:
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(
            "Could not create a short Windows junction for dvdinspect: "
            f"{created.stderr.strip() or created.stdout.strip()}"
        )
    try:
        yield alias
    finally:
        # Remove the junction itself, never traverse it with recursive cleanup.
        try:
            os.rmdir(alias)
        finally:
            shutil.rmtree(root, ignore_errors=True)


def find_dvdinspect() -> str | None:
    candidates = [os.environ.get("DVD2HEVC_DVDINSPECT"), str(TOOL_ROOT / "dvdinspect.exe"), str(LOCAL_DVDINSPECT), shutil.which("dvdinspect")]
    return next((str(Path(item).resolve()) for item in candidates if item and Path(item).is_file()), None)


def inspect_physical_graph(source: Path, executable: str, *, timeout: float = 120.0) -> dict[str, Any]:
    with _dvdinspect_source_path(source) as native_source:
        completed = subprocess.run(
            [executable, str(native_source)],
            check=False,
            capture_output=True,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise RuntimeError(f"dvdinspect failed with exit code {completed.returncode}: {stderr.strip()}")
    try:
        graph = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dvdinspect returned invalid JSON: {exc}") from exc
    if graph.get("schema") != "dvdinspect-physical-graph-v0":
        raise RuntimeError(f"Unsupported dvdinspect schema: {graph.get('schema')}")
    graph["tool"] = executable
    graph["stderr_tail"] = "\n".join(stderr.splitlines()[-20:])
    graph["summary"] = physical_graph_summary(graph)
    return graph


def physical_graph_summary(graph: dict[str, Any]) -> dict[str, int]:
    title_sets = graph.get("title_sets") or []
    pgcs = [pgc for vts in title_sets for pgc in (vts.get("title_pgcs") or [])]
    cells = [cell for pgc in pgcs for cell in (pgc.get("cells") or [])]
    unique_cells = {
        (vts.get("vts"), cell.get("vob_id"), cell.get("cell_id"), cell.get("first_sector"), cell.get("last_sector"))
        for vts in title_sets
        for pgc in (vts.get("title_pgcs") or [])
        for cell in (pgc.get("cells") or [])
    }
    return {
        "global_titles": len(graph.get("global_titles") or []),
        "title_sets": len(title_sets),
        "title_pgcs": len(pgcs),
        "referenced_cells": len(cells),
        "unique_physical_cells": len(unique_cells),
        "shared_cell_references": len(cells) - len(unique_cells),
        "title_vobus": sum(int((vts.get("title_vobu_map") or {}).get("count") or 0) for vts in title_sets),
        "menu_vobus": (
            int((graph.get("vmg_menu_vobu_map") or {}).get("count") or 0)
            + sum(int((vts.get("menu_vobu_map") or {}).get("count") or 0) for vts in title_sets)
        ),
    }
