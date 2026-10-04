import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dvd2hevc_app.branching import (
    _atomic_json,
    convert_interleaved_vts,
    split_repacked_cell_into_physical_span,
)
from dvd2hevc_app.extract import SectorRangesSink
from dvd2hevc_app.interleaved import build_interleaved_unit_plan, remap_vobus_to_segments
from dvd2hevc_app.vob import DVD_SECTOR_SIZE


class InterleavedUnitPlanTests(unittest.TestCase):
    def test_status_publication_retries_transient_windows_reader_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "status.json"
            destination.write_text('{"state":"running"}', encoding="utf-8")
            original_replace = os.replace
            attempts = 0

            def transient_replace(source: object, target: object) -> None:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise PermissionError(13, "Access is denied", str(target))
                original_replace(source, target)

            with (
                patch("dvd2hevc_app.atomic.os.replace", side_effect=transient_replace),
                patch("dvd2hevc_app.atomic.time.sleep"),
            ):
                _atomic_json(destination, {"state": "passed", "completed_tasks": 13})

            self.assertEqual(attempts, 3)
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"state": "passed", "completed_tasks": 13},
            )
            self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_complete_physical_vts_batch_extracts_uncached_tasks_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.iso"
            source.write_bytes(b"iso")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "schema": "dvd2hevc-interleaved-unit-plan-v0",
                "source": str(source),
                "vts": 1,
                "conversion_ready": True,
                "segmented_extraction_ready": True,
                "conversion_tasks": [
                    {
                        "pgc": 1,
                        "pgc_cell": cell,
                        "segments": [{
                            "start_sector": cell - 1,
                            "last_sector": cell - 1,
                            "sector_count": 1,
                        }],
                        "workspace": f"tasks/task-{cell:03d}",
                    }
                    for cell in (1, 2)
                ],
                "cells": [
                    {
                        "pgc": 1,
                        "pgc_cell": cell,
                        "vob_id": 1,
                        "cell_id": cell,
                        "first_sector": cell - 1,
                        "last_sector": cell - 1,
                        "segments": [{
                            "start_sector": cell - 1,
                            "last_sector": cell - 1,
                            "sector_count": 1,
                        }],
                    }
                    for cell in (1, 2)
                ],
            }), encoding="utf-8")
            captured_requests: list[tuple[list[tuple[int, int]], Path]] = []

            def fake_batch(_source: Path, **kwargs: object) -> list[dict[str, object]]:
                requests = kwargs["requests"]
                assert isinstance(requests, list)
                captured_requests.extend(requests)
                reports = []
                for segments, destination in requests:
                    reports.append({
                        "destination": str(destination),
                        "segments": [{
                            "source_first_sector": first,
                            "source_last_sector": last,
                        } for first, last in segments],
                    })
                return reports

            prototype_calls: list[dict[str, object]] = []

            def fake_prototype(*_args: object, **kwargs: object) -> dict[str, object]:
                prototype_calls.append(kwargs)
                sector = int(kwargs["pgc_cell_number"]) - 1
                return {"cells": [{
                    "name": f"physical-cell-{sector}",
                    "physical_segments": [{
                        "start_sector": sector, "last_sector": sector,
                    }],
                    "vobu_count": 1,
                    "layout_mode": "compact-input",
                    "validation": {
                        "structure": None,
                        "source_video": {"nb_read_frames": 1},
                        "output_video": {"nb_read_frames": 1},
                        "source_audio_hash": None,
                        "output_audio_hash": None,
                    },
                    "split_back": None,
                }]}

            graph = {"title_sets": [{
                "vts": 1,
                "title_vobu_map": {
                    "count": 2, "vobus": [{"sector": 0}, {"sector": 1}],
                },
                "title_cell_addresses": [{"last_sector": 0}, {"last_sector": 1}],
            }]}
            with (
                patch("dvd2hevc_app.branching.find_dvdinspect", return_value="dvdinspect"),
                patch("dvd2hevc_app.branching.inspect_physical_graph", return_value=graph),
                patch("dvd2hevc_app.branching.source_identity", return_value={"size": 3}),
                patch(
                    "dvd2hevc_app.branching.extract_domain_sector_segment_sets",
                    side_effect=fake_batch,
                ),
                patch(
                    "dvd2hevc_app.branching.prototype_interleaved_cell",
                    side_effect=fake_prototype,
                ),
                patch(
                    "dvd2hevc_app.branching.prefetch_compact_audio_cells",
                    return_value={"status": "passed"},
                ) as audio_prefetch,
            ):
                result = convert_interleaved_vts(
                    plan_path,
                    workspace=root / "work",
                    prefer_compact_input=True,
                    pipeline_depth=2,
                    audio_prefetch_root=root / "audio",
                )

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["extraction_policy"], "single-pass-domain-fanout-v1")
            self.assertEqual(len(captured_requests), 2)
            self.assertEqual([row[0] for row in captured_requests], [[(0, 0)], [(1, 1)]])
            self.assertEqual(len(prototype_calls), 2)
            for call in prototype_calls:
                self.assertIs(
                    call["preextracted_logical"], call["preextracted_span"]
                )
                self.assertTrue(call["prefer_compact_input"])
                self.assertIsNotNone(call["encode_lock"])
                self.assertIsNotNone(call["validation_lock"])
            self.assertEqual(result["settings"]["intermediate_policy"], "compact-input-v1")
            self.assertEqual(result["pipeline"]["workers"], 2)
            self.assertEqual(
                result["pipeline"]["mode"], "encode-validation-overlap-v1"
            )
            self.assertTrue(result["pipeline"]["validation_overlap"])
            self.assertEqual(
                result["pipeline"]["validation_policy"],
                "random-access-and-full-decode-per-physical-cell",
            )
            self.assertTrue(result["pipeline"]["audio_prefetch"])
            audio_prefetch.assert_called_once()
            prefetched_cells = audio_prefetch.call_args.args[0]
            self.assertEqual(
                [row["name"] for row in prefetched_cells],
                [
                    "vts01-pgc01-cell01-interleaved",
                    "vts01-pgc01-cell02-interleaved",
                ],
            )

    def test_sector_ranges_sink_concatenates_selected_ranges(self) -> None:
        sectors = [bytes([index]) * DVD_SECTOR_SIZE for index in range(5)]
        output = io.BytesIO()
        sink = SectorRangesSink(output, [(1, 1), (3, 4)])
        sink.write(b"".join(sectors[:2]))
        sink.write(b"".join(sectors[2:]))
        self.assertEqual(output.getvalue(), sectors[1] + sectors[3] + sectors[4])
        self.assertEqual(sink.written, 3 * DVD_SECTOR_SIZE)

    def test_vobu_map_is_rebased_across_removed_alternate_extents(self) -> None:
        vobus = [{"sector": value, "start_ptm": value * 90} for value in (100, 105, 115, 120, 125)]
        mapped = remap_vobus_to_segments(
            vobus,
            [
                {"start_sector": 100, "last_sector": 109},
                {"start_sector": 120, "last_sector": 129},
            ],
        )
        self.assertEqual([row["source_sector"] for row in mapped], [100, 105, 120, 125])
        self.assertEqual([row["sector"] for row in mapped], [0, 5, 10, 15])

    def test_split_back_changes_only_selected_physical_sectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_sectors = [bytes([index]) * DVD_SECTOR_SIZE for index in range(6)]
            replacement_sectors = [bytes([20 + index]) * DVD_SECTOR_SIZE for index in range(3)]
            source = root / "source.vob"
            replacement = root / "replacement.vob"
            destination = root / "patched.vob"
            source.write_bytes(b"".join(source_sectors))
            replacement.write_bytes(b"".join(replacement_sectors))
            result = split_repacked_cell_into_physical_span(
                source,
                replacement,
                span_first_sector=100,
                span_last_sector=105,
                segments=[
                    {"start_sector": 101, "last_sector": 101, "sector_count": 1},
                    {"start_sector": 104, "last_sector": 105, "sector_count": 2},
                ],
                destination=destination,
            )
            output = destination.read_bytes()
            expected = b"".join([
                source_sectors[0], replacement_sectors[0], source_sectors[2],
                source_sectors[3], replacement_sectors[1], replacement_sectors[2],
            ])
            self.assertEqual(output, expected)
            self.assertTrue(result["alternate_sectors_unchanged"])
            self.assertTrue(result["selected_readback_matches"])
            self.assertEqual(result["changed_selected_sectors"], 3)

    def test_alternate_extents_are_excluded_from_branch_cell(self) -> None:
        scan = {
            "source": "fixture.iso",
            "label": "FIXTURE",
            "physical_graph": {
                "title_sets": [{
                    "vts": 1,
                    "title_pgcs": [{"cells": [{
                        "vob_id": 2,
                        "cell_id": 1,
                        "first_sector": 100,
                        "first_ilvu_end_sector": 109,
                        "last_sector": 129,
                        "duration_ticks": 90000,
                        "interleaved": True,
                    }]}],
                    "title_cell_addresses": [
                        {"vob_id": 2, "cell_id": 1, "start_sector": 100, "last_sector": 109},
                        {"vob_id": 3, "cell_id": 1, "start_sector": 110, "last_sector": 119},
                        {"vob_id": 2, "cell_id": 1, "start_sector": 120, "last_sector": 129},
                        {"vob_id": 3, "cell_id": 1, "start_sector": 130, "last_sector": 139},
                    ],
                }],
            },
        }
        result = build_interleaved_unit_plan(scan, 1)
        cell = result["cells"][0]
        self.assertEqual(
            [(row["start_sector"], row["last_sector"]) for row in cell["segments"]],
            [(100, 109), (120, 129)],
        )
        self.assertEqual(cell["selected_sectors"], 20)
        self.assertEqual(cell["skipped_alternate_sectors"], 10)
        self.assertTrue(result["segmented_extraction_ready"])
        self.assertTrue(result["segmented_repack_ready"])
        self.assertEqual(result["summary"]["unique_conversion_tasks"], 3)
        self.assertEqual(result["summary"]["unreferenced_physical_cells"], 2)

    def test_bad_first_ilvu_marker_blocks_extraction_gate(self) -> None:
        scan = {
            "source": "fixture.iso",
            "label": "FIXTURE",
            "physical_graph": {
                "title_sets": [{
                    "vts": 1,
                    "title_pgcs": [{"cells": [{
                        "vob_id": 2, "cell_id": 1, "first_sector": 10,
                        "first_ilvu_end_sector": 18, "last_sector": 29,
                        "interleaved": True,
                    }]}],
                    "title_cell_addresses": [
                        {"vob_id": 2, "cell_id": 1, "start_sector": 10, "last_sector": 19},
                        {"vob_id": 2, "cell_id": 1, "start_sector": 25, "last_sector": 29},
                    ],
                }],
            },
        }
        result = build_interleaved_unit_plan(scan, 1)
        self.assertEqual(result["summary"]["first_ilvu_marker_mismatches"], 1)
        self.assertFalse(result["segmented_extraction_ready"])

    def test_ordinary_multi_pgc_vts_becomes_complete_physical_tasks(self) -> None:
        scan = {
            "source": "fixture.iso",
            "label": "MULTI_PGC",
            "physical_graph": {
                "title_sets": [{
                    "vts": 3,
                    "title_pgcs": [
                        {"cells": [
                            {"vob_id": 1, "cell_id": 1, "first_sector": 0,
                             "last_sector": 9, "duration_ticks": 90000},
                            {"vob_id": 1, "cell_id": 2, "first_sector": 10,
                             "last_sector": 19, "duration_ticks": 90000},
                        ]},
                        {"cells": [
                            {"vob_id": 1, "cell_id": 1, "first_sector": 0,
                             "last_sector": 9, "duration_ticks": 90000},
                        ]},
                    ],
                    "title_cell_addresses": [
                        {"vob_id": 1, "cell_id": 1, "start_sector": 0,
                         "last_sector": 9},
                        {"vob_id": 1, "cell_id": 2, "start_sector": 10,
                         "last_sector": 19},
                    ],
                }],
            },
        }

        result = build_interleaved_unit_plan(scan, 3)

        self.assertEqual(result["summary"]["interleaved_cells"], 0)
        self.assertEqual(result["summary"]["unique_conversion_tasks"], 2)
        self.assertEqual(result["summary"]["conversion_task_references_reused"], 1)
        self.assertEqual(result["summary"]["uncovered_domain_sectors"], 0)
        self.assertTrue(result["segmented_extraction_ready"])
        self.assertTrue(result["complete_sector_preserving_coverage"])
        self.assertTrue(result["conversion_ready"])

    def test_unreferenced_cadt_cell_becomes_a_physical_task(self) -> None:
        scan = {
            "source": "fixture.iso",
            "label": "ORPHAN_CELL",
            "physical_graph": {
                "title_sets": [{
                    "vts": 10,
                    "title_pgcs": [{"cells": [{
                        "vob_id": 1, "cell_id": 1, "first_sector": 0,
                        "last_sector": 9, "duration_ticks": 90000,
                    }]}],
                    "title_cell_addresses": [
                        {"vob_id": 1, "cell_id": 1, "start_sector": 0,
                         "last_sector": 9},
                        {"vob_id": 4, "cell_id": 1, "start_sector": 10,
                         "last_sector": 19},
                    ],
                }],
            },
        }

        result = build_interleaved_unit_plan(scan, 10)

        self.assertEqual(result["summary"]["pgc_cells"], 1)
        self.assertEqual(result["summary"]["unreferenced_physical_cells"], 1)
        self.assertEqual(result["summary"]["unique_conversion_tasks"], 2)
        orphan = next(row for row in result["cells"] if row.get("unreferenced_physical_cell"))
        self.assertEqual((orphan["first_sector"], orphan["last_sector"]), (10, 19))
        self.assertEqual((orphan["pgc"], orphan["pgc_cell"]), (0, 1))
        self.assertTrue(result["complete_sector_preserving_coverage"])
        self.assertTrue(result["conversion_ready"])


if __name__ == "__main__":
    unittest.main()
