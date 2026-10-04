"""Plan physical extraction units for DVD interleaved branching cells."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .pipeline import PipelineError


def _address_start(row: dict[str, Any]) -> int:
    return int(row.get("start_sector", row.get("first_sector", 0)))


def _matching_segments(
    cell: dict[str, Any], addresses: list[dict[str, Any]]
) -> list[dict[str, int]]:
    first = int(cell["first_sector"])
    last = int(cell["last_sector"])
    vob_id = int(cell["vob_id"])
    cell_id = int(cell["cell_id"])
    seen: set[tuple[int, int]] = set()
    result: list[dict[str, int]] = []
    for address in addresses:
        if int(address["vob_id"]) != vob_id or int(address["cell_id"]) != cell_id:
            continue
        physical_start = _address_start(address)
        physical_end = int(address["last_sector"])
        start = max(first, physical_start)
        end = min(last, physical_end)
        if start > end or (start, end) in seen:
            continue
        seen.add((start, end))
        result.append(
            {
                "start_sector": start,
                "last_sector": end,
                "sector_count": end - start + 1,
                "physical_start_sector": physical_start,
                "physical_last_sector": physical_end,
            }
        )
    return sorted(result, key=lambda row: (row["start_sector"], row["last_sector"]))


def remap_vobus_to_segments(
    vobus: list[dict[str, Any]], segments: list[dict[str, int]]
) -> list[dict[str, Any]]:
    """Map original VOBU sectors into a concatenated segmented extraction."""
    result: list[dict[str, Any]] = []
    output_cursor = 0
    seen_source_sectors: set[int] = set()
    for segment in segments:
        first = int(segment["start_sector"])
        last = int(segment["last_sector"])
        if last < first:
            raise PipelineError("Interleaved segment has an invalid sector range")
        for vobu in vobus:
            source_sector = int(vobu["sector"])
            if first <= source_sector <= last:
                if source_sector in seen_source_sectors:
                    raise PipelineError(f"VOBU sector {source_sector} occurs in overlapping segments")
                seen_source_sectors.add(source_sector)
                mapped = dict(vobu)
                mapped["source_sector"] = source_sector
                mapped["sector"] = output_cursor + source_sector - first
                result.append(mapped)
        output_cursor += last - first + 1
    return sorted(result, key=lambda row: int(row["sector"]))


def build_interleaved_unit_plan(scan: dict[str, Any], vts_number: int) -> dict[str, Any]:
    """Build complete physical conversion tasks for one title VTS.

    A branching cell's PGC first/last range spans both interleaved stories.  C_ADT
    rows identify the extents belonging to the selected VOB/cell identity.  This
    plan makes those discontinuous extents explicit before extraction or encode.
    The same physical-task model also covers an ordinary multi-PGC VTS that the
    title-oriented converter cannot represent: ordinary cells simply have one
    contiguous segment and shared physical ranges are still encoded only once.
    """
    graph = scan.get("physical_graph") or {}
    if not graph.get("title_sets"):
        raise PipelineError("A native physical graph is required for an interleaved-unit plan")
    vts = next(
        (row for row in graph["title_sets"] if int(row.get("vts", 0)) == vts_number),
        None,
    )
    if vts is None:
        raise PipelineError(f"VTS {vts_number} is not present in the physical graph")

    addresses = list(vts.get("title_cell_addresses", []))
    if not addresses:
        raise PipelineError(f"VTS {vts_number} has no title C_ADT rows")

    rows: list[dict[str, Any]] = []
    usage: Counter[tuple[int, int]] = Counter()
    unresolved = 0
    marker_mismatches = 0
    interleaved_count = 0
    selected_sectors = 0
    skipped_sectors = 0
    for pgc_index, pgc in enumerate(vts.get("title_pgcs", []), start=1):
        for pgc_cell_index, cell in enumerate(pgc.get("cells", []), start=1):
            first = int(cell["first_sector"])
            last = int(cell["last_sector"])
            is_interleaved = bool(cell.get("interleaved")) or int(
                cell.get("first_ilvu_end_sector", 0)
            ) != 0
            if is_interleaved:
                interleaved_count += 1
                segments = _matching_segments(cell, addresses)
            else:
                segments = [{
                    "start_sector": first,
                    "last_sector": last,
                    "sector_count": last - first + 1,
                }]
            for segment in segments:
                usage[(segment["start_sector"], segment["last_sector"])] += 1

            resolved = bool(segments) and segments[0]["start_sector"] == first
            resolved = resolved and segments[-1]["last_sector"] == last
            no_segment_overlap = all(
                current["last_sector"] < following["start_sector"]
                for current, following in zip(segments, segments[1:])
            )
            resolved = resolved and no_segment_overlap
            marker = int(cell.get("first_ilvu_end_sector", 0))
            marker_matches = not is_interleaved or (
                bool(segments) and marker == int(segments[0]["physical_last_sector"])
            )
            if not resolved:
                unresolved += 1
            if not marker_matches:
                marker_mismatches += 1

            span_sectors = last - first + 1
            cell_selected = sum(segment["sector_count"] for segment in segments)
            cell_skipped = max(0, span_sectors - cell_selected)
            if is_interleaved:
                selected_sectors += cell_selected
                skipped_sectors += cell_skipped
            rows.append(
                {
                    "pgc": pgc_index,
                    "pgc_cell": pgc_cell_index,
                    "vob_id": int(cell["vob_id"]),
                    "cell_id": int(cell["cell_id"]),
                    "first_sector": first,
                    "last_sector": last,
                    "duration_ticks": int(cell.get("duration_ticks", 0)),
                    "interleaved": is_interleaved,
                    "first_ilvu_end_sector": marker,
                    "first_ilvu_marker_matches": marker_matches,
                    "resolved": resolved,
                    "span_sectors": span_sectors,
                    "selected_sectors": cell_selected,
                    "skipped_alternate_sectors": cell_skipped,
                    "segments": segments,
                }
            )

    pgc_cell_references = len(rows)
    # Some authored discs carry title-domain C_ADT cells that no VMG title or
    # title PGC references.  They may be Easter eggs, command-reachable clips,
    # or simply dormant authored material.  A complete VTS replacement cannot
    # silently leave their MPEG-2 video behind, and the title-oriented runner
    # has no logical title through which to discover them.  Promote wholly
    # unreferenced C_ADT ranges to synthetic physical tasks.  Partially covered
    # ranges remain a hard coverage failure because splitting one C_ADT extent
    # without authoritative navigation boundaries would be unsafe.
    referenced_intervals = [
        (int(segment["start_sector"]), int(segment["last_sector"]))
        for row in rows
        for segment in row["segments"]
    ]
    unreferenced_physical_cells = 0
    for address in sorted(addresses, key=lambda row: _address_start(row)):
        first = _address_start(address)
        last = int(address["last_sector"])
        overlaps = [
            (covered_first, covered_last)
            for covered_first, covered_last in referenced_intervals
            if covered_first <= last and covered_last >= first
        ]
        if overlaps:
            continue
        unreferenced_physical_cells += 1
        segment = {
            "start_sector": first,
            "last_sector": last,
            "sector_count": last - first + 1,
        }
        usage[(first, last)] += 1
        rows.append({
            "pgc": 0,
            "pgc_cell": unreferenced_physical_cells,
            "vob_id": int(address["vob_id"]),
            "cell_id": int(address["cell_id"]),
            "first_sector": first,
            "last_sector": last,
            "duration_ticks": 0,
            "interleaved": False,
            "unreferenced_physical_cell": True,
            "first_ilvu_end_sector": 0,
            "first_ilvu_marker_matches": True,
            "resolved": True,
            "span_sectors": last - first + 1,
            "selected_sectors": last - first + 1,
            "skipped_alternate_sectors": 0,
            "segments": [segment],
        })
        referenced_intervals.append((first, last))

    interleaved_rows = [row for row in rows if row["interleaved"]]
    shared_segments = sum(count - 1 for count in usage.values() if count > 1)
    extraction_ready = bool(rows) and unresolved == 0 and marker_mismatches == 0
    task_groups: dict[tuple[tuple[int, int], ...], list[dict[str, Any]]] = {}
    for row in rows:
        signature = tuple(
            (int(segment["start_sector"]), int(segment["last_sector"]))
            for segment in row["segments"]
        )
        task_groups.setdefault(signature, []).append(row)
    ordered_tasks = sorted(
        task_groups.items(), key=lambda item: (item[0][0][0], item[0][-1][1])
    )
    conversion_tasks: list[dict[str, Any]] = []
    task_intervals: list[tuple[int, int, int]] = []
    for task_number, (signature, references) in enumerate(ordered_tasks, start=1):
        representative = references[0]
        conversion_tasks.append({
            "task": task_number,
            "pgc": int(representative["pgc"]),
            "pgc_cell": int(representative["pgc_cell"]),
            "vob_id": int(representative["vob_id"]),
            "cell_id": int(representative["cell_id"]),
            "interleaved": bool(representative["interleaved"]),
            "duration_ticks": int(representative["duration_ticks"]),
            "duration_seconds": round(int(representative["duration_ticks"]) / 90000.0, 3),
            "selected_sectors": sum(last - first + 1 for first, last in signature),
            "segments": [
                {"start_sector": first, "last_sector": last, "sector_count": last - first + 1}
                for first, last in signature
            ],
            "references": [
                {"pgc": int(row["pgc"]), "pgc_cell": int(row["pgc_cell"])}
                for row in references
            ],
            "workspace": f"tasks/task-{task_number:03d}",
        })
        task_intervals.extend((first, last, task_number) for first, last in signature)
    task_intervals.sort()
    overlap_count = 0
    gap_sectors = 0
    furthest_end = -1
    for first, last, _task in task_intervals:
        if first <= furthest_end:
            overlap_count += 1
        elif first > furthest_end + 1:
            gap_sectors += first - furthest_end - 1
        furthest_end = max(furthest_end, last)
    domain_last_sector = max(int(row["last_sector"]) for row in addresses)
    if furthest_end < domain_last_sector:
        gap_sectors += domain_last_sector - furthest_end
    complete_domain_coverage = (
        bool(task_intervals)
        and task_intervals[0][0] == 0
        and furthest_end == domain_last_sector
        and gap_sectors == 0
    )
    return {
        "schema": "dvd2hevc-interleaved-unit-plan-v0",
        "source": str(Path(scan["source"]).resolve()),
        "label": scan.get("label"),
        "vts": vts_number,
        "summary": {
            "pgcs": len(vts.get("title_pgcs", [])),
            "pgc_cells": pgc_cell_references,
            "unreferenced_physical_cells": unreferenced_physical_cells,
            "interleaved_cells": interleaved_count,
            "resolved_interleaved_cells": interleaved_count - unresolved,
            "unresolved_interleaved_cells": unresolved,
            "first_ilvu_marker_mismatches": marker_mismatches,
            "selected_interleaved_sectors": selected_sectors,
            "skipped_alternate_sectors": skipped_sectors,
            "shared_segment_references": shared_segments,
            "unique_conversion_tasks": len(conversion_tasks),
            "conversion_task_references_reused": len(rows) - len(conversion_tasks),
            "conversion_task_overlaps": overlap_count,
            "uncovered_domain_sectors": gap_sectors,
        },
        "cells": rows,
        "conversion_tasks": conversion_tasks,
        "segmented_extraction_ready": extraction_ready,
        "segmented_repack_ready": True,
        "complete_sector_preserving_coverage": complete_domain_coverage,
        "conversion_ready": extraction_ready and overlap_count == 0 and complete_domain_coverage,
        "next_gate": (
            "Run the resumable unique-task NVENC conversion, stage the complete VTS, and "
            "compare both branching cuts through automated navigation and user visual gates."
        ),
    }
