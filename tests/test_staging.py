from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dvd2hevc_app.staging import extend_complete_stage, verify_complete_stage
from dvd2hevc_app.vob import DVD_SECTOR_SIZE


class CompleteStageExtensionTests(unittest.TestCase):
    @staticmethod
    def _conversion(source: Path, vts: int, replacement: Path) -> dict[str, object]:
        return {
            "schema": "dvd2hevc-vts-conversion-v0",
            "status": "passed",
            "source": str(source),
            "domain": "title",
            "vts": vts,
            "cells": [{
                "name": f"vts{vts:02d}-cell",
                "cell": {"first_sector": 0, "last_sector": 0},
                "repacked_cell": str(replacement),
                "validation": {"structure": {"program_stream_maps": 1}},
            }],
        }

    def test_extension_breaks_only_modified_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.iso"
            source.write_bytes(b"fixture")
            base_stage = root / "base-stage"
            video_ts = base_stage / "VIDEO_TS"
            video_ts.mkdir(parents=True)
            base_vts1 = video_ts / "VTS_01_1.VOB"
            base_vts2 = video_ts / "VTS_02_1.VOB"
            base_vts1.write_bytes(b"A" * DVD_SECTOR_SIZE)
            base_vts2.write_bytes(b"B" * DVD_SECTOR_SIZE)

            old_replacement = root / "old.vob"
            new_replacement = root / "new.vob"
            old_replacement.write_bytes(base_vts1.read_bytes())
            new_replacement.write_bytes(b"C" * DVD_SECTOR_SIZE)
            old_report = root / "vts01.json"
            new_report = root / "vts02.json"
            old_report.write_text(
                json.dumps(self._conversion(source, 1, old_replacement)), encoding="utf-8"
            )
            new_report.write_text(
                json.dumps(self._conversion(source, 2, new_replacement)), encoding="utf-8"
            )
            (base_stage / "dvd2hevc-stage-report.json").write_text(json.dumps({
                "schema": "dvd2hevc-complete-staged-dvd-v0",
                "status": "passed",
                "source": str(source),
                "conversion_reports": [str(old_report)],
                "destination": str(base_stage),
                "replacements": [{"name": "vts01-cell", "bytes": DVD_SECTOR_SIZE}],
            }), encoding="utf-8")

            scan = {
                "invalid_sectors": 0,
                "scrambled_pes_packets": 0,
                "program_stream_maps": 1,
            }
            destination = root / "extended-stage"
            with patch("dvd2hevc_app.staging.inspect_vob_file", return_value=scan):
                result = extend_complete_stage(base_stage, [new_report], destination)

            extended_vts1 = destination / "VIDEO_TS" / base_vts1.name
            extended_vts2 = destination / "VIDEO_TS" / base_vts2.name
            self.assertEqual(result["summary"]["copy_on_write_files"], 1)
            self.assertTrue(os.path.samefile(base_vts1, extended_vts1))
            self.assertFalse(os.path.samefile(base_vts2, extended_vts2))
            self.assertEqual(base_vts2.read_bytes(), b"B" * DVD_SECTOR_SIZE)
            self.assertEqual(extended_vts2.read_bytes(), new_replacement.read_bytes())
            self.assertTrue(verify_complete_stage(
                destination / "dvd2hevc-stage-report.json"
            )["passed"])


if __name__ == "__main__":
    unittest.main()
