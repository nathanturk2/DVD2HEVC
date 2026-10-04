from __future__ import annotations

import json
import unittest

from dvd2hevc_app.handbrake import normalize_titles, parse_title_set
from dvd2hevc_app.planning import build_plan
from dvd2hevc_app.vob import DVD_SECTOR_SIZE, VobSectorScanner


def make_sector(*, scrambled: bool = False, stream_id: int = 0xE0) -> bytes:
    pack = bytearray(b"\x00\x00\x01\xba\x44\x00\x04\x00\x04\x01\x01\x89\xc3\xf8")
    payload = b"hevc-payload"
    header_flags = 0xB0 if scrambled else 0x80
    pes_body = bytes([header_flags, 0x00, 0x00]) + payload
    pack.extend(b"\x00\x00\x01" + bytes([stream_id]) + len(pes_body).to_bytes(2, "big") + pes_body)
    remaining = DVD_SECTOR_SIZE - len(pack)
    pack.extend(b"\x00\x00\x01\xbe" + (remaining - 6).to_bytes(2, "big") + bytes(remaining - 6))
    return bytes(pack)


class VobScannerTests(unittest.TestCase):
    def test_clear_video_pes_and_capacity(self) -> None:
        scanner = VobSectorScanner()
        scanner.write(make_sector())
        stats = scanner.finish()
        self.assertEqual(stats.sectors, 1)
        self.assertEqual(stats.invalid_sectors, 0)
        self.assertEqual(stats.video_pes_packets, 1)
        self.assertEqual(stats.video_payload_bytes, len(b"hevc-payload"))
        self.assertEqual(stats.scrambled_pes_packets, 0)

    def test_scrambled_video_is_reported(self) -> None:
        scanner = VobSectorScanner()
        scanner.write(make_sector(scrambled=True))
        stats = scanner.finish()
        self.assertEqual(stats.scrambled_pes_packets, 1)
        self.assertEqual(stats.scrambled_video_packets, 1)


class HandBrakeParserTests(unittest.TestCase):
    def test_json_title_set_is_extracted_from_logs(self) -> None:
        payload = {
            "TitleList": [
                {
                    "Index": 2,
                    "Name": "TEST",
                    "Duration": {"Hours": 1, "Minutes": 2, "Seconds": 3},
                    "ChapterList": [{}, {}],
                    "AngleCount": 1,
                    "AudioList": [{}],
                    "SubtitleList": [{}, {}],
                }
            ]
        }
        parsed = parse_title_set("noise\nJSON Title Set: " + json.dumps(payload) + "\nmore noise")
        titles = normalize_titles(parsed)
        self.assertEqual(titles[0]["duration_seconds"], 3723)
        self.assertEqual(titles[0]["chapter_count"], 2)


class PlanningTests(unittest.TestCase):
    def test_clear_full_scan_is_ready(self) -> None:
        scan = {
            "source": "disc.iso",
            "scan_depth": "full",
            "video_ts": {
                "totals": {"scrambled_pes_packets": 0, "invalid_sectors": 0},
                "vobs": [
                    {
                        "name": "VTS_01_1.VOB",
                        "domain": "title",
                        "stats": {"video_pes_packets": 10, "video_payload_bytes": 1000, "nav_packs": 2},
                    }
                ],
            },
        }
        plan = build_plan(scan)
        self.assertTrue(plan["ready_for_rewriter_prototype"])
        self.assertEqual(plan["summary"]["reencode_candidates"], 1)


if __name__ == "__main__":
    unittest.main()
