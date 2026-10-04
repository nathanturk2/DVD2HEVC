from __future__ import annotations

import unittest

from dvd2hevc_app.audio_policy import _ifo_audio_tracks
from dvd2hevc_app.frontend import parse_audio_language_overrides
from dvd2hevc_app.ifo import VTS_AUDIO_ATTRS_OFFSET, VTS_AUDIO_COUNT_OFFSET
from dvd2hevc_app.quality import resolve_disc_quality_policy


class Phase8PolicyTests(unittest.TestCase):
    def test_target_bitrate_multiplier_and_shared_vts_override(self) -> None:
        plan = {
            "source": "movie.iso",
            "summary": {"title_vobus": 1000},
            "vts": [{"vts": 1}, {"vts": 2}],
            "title_tasks": [
                {"title": 1, "vts": 1, "duration_seconds": 6000, "status": "ready"},
                {"title": 2, "vts": 1, "duration_seconds": 300, "status": "ready"},
                {"title": 3, "vts": 2, "duration_seconds": 1200, "status": "ready"},
            ],
        }
        scan = {
            "video_ts": {
                "vobs": [{"domain": "title", "stats": {"video_payload_bytes": 300_000_000}}]
            }
        }
        standard = resolve_disc_quality_policy(plan, scan, quality="target-bitrate")
        retained = resolve_disc_quality_policy(
            plan, scan, quality="target-bitrate", target_bitrate_multiplier=1.5,
            bitrate_mode="cbr",
            main_title_quality="cq:18",
        )
        self.assertEqual(standard["schema"], "dvd2hevc-disc-quality-policy-v2")
        self.assertEqual(standard["general"]["rate_control"], "vbr")
        self.assertEqual(standard["general"]["target_bps"], 1_250_000)
        self.assertEqual(retained["general"]["target_bps"], 1_875_000)
        self.assertEqual(retained["general"]["rate_control"], "cbr")
        self.assertNotIn("estimate", retained["general"])
        self.assertEqual(retained["quality_by_vts"]["1"]["resolved"], "cq:18")
        self.assertEqual(retained["title_override"]["vts_applications"][0]["scope"], "entire-shared-vts")

    def test_ifo_languages_drive_audio_actions(self) -> None:
        data = bytearray(VTS_AUDIO_ATTRS_OFFSET + 16)
        data[:12] = b"DVDVIDEO-VTS"
        data[VTS_AUDIO_COUNT_OFFSET] = 2
        data[VTS_AUDIO_ATTRS_OFFSET + 2 : VTS_AUDIO_ATTRS_OFFSET + 4] = b"en"
        second = VTS_AUDIO_ATTRS_OFFSET + 8
        data[second + 2 : second + 4] = b"fr"
        tracks = _ifo_audio_tracks(bytes(data))
        self.assertEqual([track["language"] for track in tracks], ["eng", "fra"])
        self.assertEqual(parse_audio_language_overrides(["en=passthrough", "other=compact-stereo"]), {
            "eng": "passthrough", "other": "compact-stereo",
        })


if __name__ == "__main__":
    unittest.main()
