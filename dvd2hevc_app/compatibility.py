"""Phase 7 compatibility planning from the authoritative physical DVD graph."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .pipeline import PipelineError


def _first_sector(cell: dict[str, Any]) -> int:
    return int(cell.get("first_sector", cell.get("start_sector", 0)))


def _cell_key(cell: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(cell.get("vob_id", 0)),
        int(cell.get("cell_id", 0)),
        _first_sector(cell),
        int(cell.get("last_sector", 0)),
    )


def _pgcs_for_vts_title(vts: dict[str, Any], vts_title_number: int) -> list[int]:
    chapter = next(
        (
            row
            for row in vts.get("chapters", [])
            if int(row.get("vts_title_number", 0)) == vts_title_number
        ),
        None,
    )
    if chapter is None:
        return []
    return sorted({int(part.get("pgc", 0)) for part in chapter.get("parts", []) if part.get("pgc")})


def _overlapping_ranges(cells: list[dict[str, Any]]) -> int:
    unique = {(_cell_key(cell)): cell for cell in cells}
    rows = sorted(unique.values(), key=lambda cell: (_first_sector(cell), int(cell.get("last_sector", 0))))
    overlaps = 0
    furthest_end = -1
    for cell in rows:
        start = _first_sector(cell)
        end = int(cell.get("last_sector", 0))
        if start <= furthest_end:
            overlaps += 1
        furthest_end = max(furthest_end, end)
    return overlaps


def build_compatibility_plan(scan: dict[str, Any]) -> dict[str, Any]:
    """Describe safe work, sharing, and blockers without starting any encode."""
    graph = scan.get("physical_graph") or {}
    if not graph.get("summary") or not graph.get("title_sets"):
        raise PipelineError("A native physical graph is required for a compatibility plan")

    global_titles = graph.get("global_titles", [])
    titles_by_vts: dict[int, list[dict[str, Any]]] = {}
    for title in global_titles:
        titles_by_vts.setdefault(int(title["vts"]), []).append(title)

    title_tasks: list[dict[str, Any]] = []
    vts_rows: list[dict[str, Any]] = []
    menu_tasks: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()

    vmg_vobus = int((graph.get("vmg_menu_vobu_map") or {}).get("count", 0))
    if vmg_vobus:
        menu_tasks.append(
            {
                "domain": "vmg_menu",
                "vts": 0,
                "physical_cells": len(graph.get("vmg_menu_cell_addresses", [])),
                "vobus": vmg_vobus,
                "workspace": "menus/vmg",
            }
        )

    for vts in sorted(graph["title_sets"], key=lambda row: int(row["vts"])):
        number = int(vts["vts"])
        vts_titles = sorted(titles_by_vts.get(number, []), key=lambda row: int(row["title"]))
        pgcs = vts.get("title_pgcs", [])
        all_references = [cell for pgc in pgcs for cell in pgc.get("cells", [])]
        unique_references = {_cell_key(cell): cell for cell in all_references}
        physical_cells = vts.get("title_cell_addresses", [])
        interleaved_cells = {
            _cell_key(cell)
            for cell in all_references
            if bool(cell.get("interleaved"))
            or int(cell.get("block_type", 0)) != 0
            or int(cell.get("first_ilvu_end_sector", 0)) != 0
        }
        angle_cells = {
            _cell_key(cell)
            for cell in all_references
            if bool(cell.get("seamless_angle")) or int(cell.get("block_type", 0)) != 0
        }
        range_overlaps = _overlapping_ranges(all_references)

        for title in vts_titles:
            global_number = int(title["title"])
            pgc_numbers = _pgcs_for_vts_title(vts, int(title["vts_title_number"]))
            reasons: list[str] = []
            if len(pgc_numbers) != 1:
                reasons.append("title-does-not-map-to-one-pgc")
            if int(title.get("angle_count", 1)) > 1:
                reasons.append("multi-angle-title")
            referenced_for_title: list[dict[str, Any]] = []
            for pgc_number in pgc_numbers:
                if 1 <= pgc_number <= len(pgcs):
                    referenced_for_title.extend(pgcs[pgc_number - 1].get("cells", []))
            if any(
                bool(cell.get("interleaved"))
                or int(cell.get("block_type", 0)) != 0
                or int(cell.get("first_ilvu_end_sector", 0)) != 0
                for cell in referenced_for_title
            ):
                reasons.append("interleaved-branching-cells")
            if reasons:
                blocker_counts.update(set(reasons))
            duration_ticks = sum(
                int(pgcs[pgc_number - 1].get("duration_ticks", 0))
                for pgc_number in pgc_numbers
                if 1 <= pgc_number <= len(pgcs)
            )
            title_tasks.append(
                {
                    "title": global_number,
                    "vts": number,
                    "vts_title_number": int(title["vts_title_number"]),
                    "chapters": int(title.get("chapter_count", 0)),
                    "angles": int(title.get("angle_count", 1)),
                    "pgcs": pgc_numbers,
                    "duration_seconds": round(duration_ticks / 90000.0, 3),
                    "unique_cells": len({_cell_key(cell) for cell in referenced_for_title}),
                    "status": "ready" if not reasons else "blocked",
                    "blockers": reasons,
                    "workspace": f"titles/vts{number:02d}-shared",
                    "report_copy": f"title-reports/title{global_number}-report.json",
                }
            )

        menu_vobus = int((vts.get("menu_vobu_map") or {}).get("count", 0))
        if menu_vobus:
            menu_tasks.append(
                {
                    "domain": "vts_menu",
                    "vts": number,
                    "physical_cells": len(vts.get("menu_cell_addresses", [])),
                    "vobus": menu_vobus,
                    "workspace": f"menus/vts{number:02d}",
                }
            )

        vts_rows.append(
            {
                "vts": number,
                "global_titles": [int(title["title"]) for title in vts_titles],
                "title_pgcs": len(pgcs),
                "referenced_cells": len(all_references),
                "unique_referenced_cells": len(unique_references),
                "physical_cells": len(physical_cells),
                "shared_references": max(0, len(all_references) - len(unique_references)),
                "interleaved_cells": len(interleaved_cells),
                "angle_cells": len(angle_cells),
                "overlapping_reference_ranges": range_overlaps,
                "title_vobus": int((vts.get("title_vobu_map") or {}).get("count", 0)),
                "menu_vobus": menu_vobus,
                "audio_streams": int(vts.get("audio_stream_count", 0)),
                "subpicture_streams": int(vts.get("subpicture_stream_count", 0)),
                "combine_report": f"combined/vts{number:02d}-report.json" if vts_titles else None,
                "sector_preserving_ready": not interleaved_cells and range_overlaps == 0,
                "compact_ready": not interleaved_cells and not angle_cells and range_overlaps == 0,
            }
        )

    ready_titles = [row for row in title_tasks if row["status"] == "ready"]
    meaningful_ready_titles = [
        row for row in ready_titles if float(row["duration_seconds"]) >= 5.0
    ]
    recommended = min(
        meaningful_ready_titles or ready_titles,
        key=lambda row: (float(row["duration_seconds"] or 10**12), int(row["unique_cells"]), int(row["title"])),
        default=None,
    )
    summary = graph["summary"]
    return {
        "schema": "dvd2hevc-compatibility-plan-v0",
        "source": str(Path(scan["source"]).resolve()),
        "label": scan.get("label"),
        "scan_depth": scan.get("scan_depth"),
        "decryption": (
            "passed" if scan.get("content_decrypted") is True
            else "failed" if scan.get("content_decrypted") is False
            else "not-checked-by-quick-scan"
        ),
        "summary": {
            "global_titles": int(summary["global_titles"]),
            "title_sets": int(summary["title_sets"]),
            "title_pgcs": int(summary["title_pgcs"]),
            "referenced_cells": int(summary["referenced_cells"]),
            "unique_physical_cells": int(summary["unique_physical_cells"]),
            "shared_cell_references": int(summary.get("shared_cell_references", 0)),
            "title_vobus": int(summary["title_vobus"]),
            "menu_vobus": int(summary["menu_vobus"]),
            "ready_titles": len(ready_titles),
            "blocked_titles": len(title_tasks) - len(ready_titles),
            "interleaved_cells": sum(int(row["interleaved_cells"]) for row in vts_rows),
            "overlapping_reference_ranges": sum(
                int(row["overlapping_reference_ranges"]) for row in vts_rows
            ),
        },
        "vts": vts_rows,
        "title_tasks": title_tasks,
        "menu_tasks": menu_tasks,
        "blockers": [
            {"code": code, "affected_titles": count}
            for code, count in sorted(blocker_counts.items())
        ],
        "recommended_first_gate": recommended,
        "policy": {
            "shared_workspace_per_vts": True,
            "encode_shared_physical_cells_once": True,
            "large_jobs_run_in_background": True,
            "interleaved_units_fail_before_encode": True,
        },
    }
