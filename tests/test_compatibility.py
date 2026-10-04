import unittest

from dvd2hevc_app.compatibility import build_compatibility_plan


class CompatibilityPlanTests(unittest.TestCase):
    def test_interleaved_branching_is_blocked_before_encode(self) -> None:
        cell = {
            "vob_id": 1,
            "cell_id": 1,
            "first_sector": 0,
            "last_sector": 99,
            "first_ilvu_end_sector": 10,
            "interleaved": True,
        }
        scan = {
            "source": "fixture.iso",
            "label": "FIXTURE",
            "scan_depth": "quick",
            "content_decrypted": None,
            "physical_graph": {
                "summary": {
                    "global_titles": 1,
                    "title_sets": 1,
                    "title_pgcs": 1,
                    "referenced_cells": 1,
                    "unique_physical_cells": 1,
                    "shared_cell_references": 0,
                    "title_vobus": 2,
                    "menu_vobus": 0,
                },
                "global_titles": [{
                    "title": 1,
                    "vts": 1,
                    "vts_title_number": 1,
                    "chapter_count": 1,
                    "angle_count": 1,
                }],
                "vmg_menu_vobu_map": {"count": 0},
                "title_sets": [{
                    "vts": 1,
                    "audio_stream_count": 2,
                    "subpicture_stream_count": 3,
                    "chapters": [{"vts_title_number": 1, "parts": [{"pgc": 1}]}],
                    "title_pgcs": [{"duration_ticks": 90000, "cells": [cell]}],
                    "title_cell_addresses": [cell],
                    "title_vobu_map": {"count": 2},
                    "menu_vobu_map": {"count": 0},
                }],
            },
        }
        result = build_compatibility_plan(scan)
        self.assertEqual(result["summary"]["blocked_titles"], 1)
        self.assertEqual(result["title_tasks"][0]["blockers"], ["interleaved-branching-cells"])
        self.assertFalse(result["vts"][0]["compact_ready"])

    def test_shortest_safe_title_is_recommended(self) -> None:
        base = {
            "vob_id": 1,
            "cell_id": 1,
            "first_sector": 0,
            "last_sector": 9,
            "interleaved": False,
        }
        scan = {
            "source": "fixture.iso",
            "label": "FIXTURE",
            "scan_depth": "quick",
            "content_decrypted": True,
            "physical_graph": {
                "summary": {
                    "global_titles": 2, "title_sets": 1, "title_pgcs": 2,
                    "referenced_cells": 2, "unique_physical_cells": 1,
                    "shared_cell_references": 1, "title_vobus": 1, "menu_vobus": 0,
                },
                "global_titles": [
                    {"title": 1, "vts": 1, "vts_title_number": 1, "chapter_count": 1, "angle_count": 1},
                    {"title": 2, "vts": 1, "vts_title_number": 2, "chapter_count": 1, "angle_count": 1},
                ],
                "vmg_menu_vobu_map": {"count": 0},
                "title_sets": [{
                    "vts": 1, "audio_stream_count": 1, "subpicture_stream_count": 0,
                    "chapters": [
                        {"vts_title_number": 1, "parts": [{"pgc": 1}]},
                        {"vts_title_number": 2, "parts": [{"pgc": 2}]},
                    ],
                    "title_pgcs": [
                        {"duration_ticks": 180000, "cells": [base]},
                        {"duration_ticks": 90000, "cells": [base]},
                    ],
                    "title_cell_addresses": [base],
                    "title_vobu_map": {"count": 1}, "menu_vobu_map": {"count": 0},
                }],
            },
        }
        result = build_compatibility_plan(scan)
        self.assertEqual(result["recommended_first_gate"]["title"], 2)
        self.assertEqual(result["summary"]["ready_titles"], 2)


if __name__ == "__main__":
    unittest.main()
