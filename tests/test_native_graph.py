from __future__ import annotations

import unittest

from dvd2hevc_app.native import physical_graph_summary
from dvd2hevc_app.author import normalized_physical_graph


class NativeGraphSummaryTests(unittest.TestCase):
    def test_graph_normalization_ignores_only_acquisition_diagnostics(self) -> None:
        first = {"source": "folder", "tool": "a", "stderr_tail": "warning", "title_sets": [1]}
        second = {"source": "iso", "tool": "b", "stderr_tail": "", "title_sets": [1]}
        self.assertEqual(normalized_physical_graph(first), normalized_physical_graph(second))
        second["title_sets"] = [2]
        self.assertNotEqual(normalized_physical_graph(first), normalized_physical_graph(second))

    def test_shared_physical_cells_are_deduplicated(self) -> None:
        cell = {"vob_id": 1, "cell_id": 2, "first_sector": 10, "last_sector": 20}
        graph = {
            "global_titles": [{}, {}],
            "vmg_menu_vobu_map": {"count": 3},
            "title_sets": [
                {
                    "vts": 1,
                    "title_pgcs": [{"cells": [cell]}, {"cells": [dict(cell)]}],
                    "title_vobu_map": {"count": 4},
                    "menu_vobu_map": {"count": 2},
                }
            ],
        }
        summary = physical_graph_summary(graph)
        self.assertEqual(summary["referenced_cells"], 2)
        self.assertEqual(summary["unique_physical_cells"], 1)
        self.assertEqual(summary["shared_cell_references"], 1)
        self.assertEqual(summary["title_vobus"], 4)
        self.assertEqual(summary["menu_vobus"], 5)


if __name__ == "__main__":
    unittest.main()
