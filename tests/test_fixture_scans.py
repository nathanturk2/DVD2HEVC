from __future__ import annotations

import os
import unittest
from pathlib import Path

from dvd2hevc_app.iso import scan_iso


FIXTURE_DIR = os.environ.get("DVD2HEVC_FIXTURE_DIR")


@unittest.skipUnless(FIXTURE_DIR, "set DVD2HEVC_FIXTURE_DIR to run multi-gigabyte ISO integration tests")
class FixtureScanTests(unittest.TestCase):
    EXPECTED = {
        "TAKEN_2.iso": {"vts": 5, "vobs": 13, "sectors": 3_470_934},
        "TAKEN_3.iso": {"vts": 6, "vobs": 14, "sectors": 3_629_200},
        "THE_BOURNE_IDENTITY.iso": {"vts": 10, "vobs": 26, "sectors": 3_933_289},
        "THE_INTERN.iso": {"vts": 2, "vobs": 11, "sectors": 3_362_805},
    }

    def test_all_known_images_are_clear_and_structurally_sound(self) -> None:
        root = Path(FIXTURE_DIR or "")
        for name, expected in self.EXPECTED.items():
            with self.subTest(iso=name):
                report = scan_iso(root / name, inspect_vobs=True)
                video_ts = report["video_ts"]
                totals = video_ts["totals"]
                self.assertEqual(video_ts["vts_count"], expected["vts"])
                self.assertEqual(len(video_ts["vobs"]), expected["vobs"])
                self.assertEqual(totals["vob_sectors"], expected["sectors"])
                self.assertEqual(totals["scrambled_pes_packets"], 0)
                self.assertEqual(totals["invalid_sectors"], 0)


if __name__ == "__main__":
    unittest.main()
