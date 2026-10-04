from __future__ import annotations

import unittest

from dvd2hevc_app.validation import (
    VLC_DVDHEVC_MARKERS,
    compare_vlc_navigation_text,
    extract_dvdnav_trace,
    validate_navigation_only_menu_text,
    validate_vlc_dvdhevc_text,
)


class VlcValidationTests(unittest.TestCase):
    def test_complete_log_passes(self) -> None:
        text = "\n".join(VLC_DVDHEVC_MARKERS.values())
        result = validate_vlc_dvdhevc_text(text)
        self.assertTrue(result["passed"])
        self.assertEqual(result["missing"], [])

    def test_stock_dvdnav_behavior_fails_at_hevc_markers(self) -> None:
        text = '\n'.join([
            'using access_demux module "dvdnav"',
            "DVDNAV_CELL_CHANGE",
            'using packetizer module "mpegvideo"',
        ])
        result = validate_vlc_dvdhevc_text(text)
        self.assertFalse(result["passed"])
        self.assertIn("hevc_psm_accepted", result["missing"])
        self.assertIn("hevc_packetizer", result["missing"])
        self.assertTrue(result["observations"]["mpeg2_packetizer"])

    def test_required_cell_transition_count_is_enforced(self) -> None:
        text = "\n".join(VLC_DVDHEVC_MARKERS.values())
        result = validate_vlc_dvdhevc_text(text, minimum_cell_changes=2)
        self.assertFalse(result["passed"])
        self.assertEqual(result["observations"]["cell_change_count"], 1)

    def test_decoder_corruption_is_a_hard_failure(self) -> None:
        text = "\n".join(VLC_DVDHEVC_MARKERS.values()) + "\nPPS id out of range: 0"
        result = validate_vlc_dvdhevc_text(text)
        self.assertFalse(result["passed"])
        self.assertIn("no_pps_id_errors", result["missing"])
        self.assertEqual(
            result["observations"]["hevc_corruption_counts"]["no_pps_id_errors"], 1
        )

    def test_hevc_decoder_output_frame_is_valid_first_picture_evidence(self) -> None:
        text = "\n".join(
            marker
            for name, marker in VLC_DVDHEVC_MARKERS.items()
            if name != "first_picture"
        )
        text += "\n[hevc @ 000001DEADBEEF00] Output frame with POC 0."
        result = validate_vlc_dvdhevc_text(text)
        self.assertTrue(result["passed"])
        self.assertEqual(result["observations"]["vlc_first_picture_count"], 0)
        self.assertEqual(result["observations"]["hevc_output_frame_count"], 1)

    def test_hevc_decoder_open_without_output_frame_still_fails(self) -> None:
        text = "\n".join(
            marker
            for name, marker in VLC_DVDHEVC_MARKERS.items()
            if name != "first_picture"
        )
        result = validate_vlc_dvdhevc_text(text)
        self.assertFalse(result["passed"])
        self.assertIn("first_picture", result["missing"])

    def test_navigation_trace_ignores_object_addresses_and_codec_logs(self) -> None:
        source = "\n".join([
            "[abc] dvdnav demux debug: DVDNAV_CELL_CHANGE",
            "[abc] dvdnav demux debug:      - cellN=1",
            "[abc] dvdnav demux debug: unrelated message",
        ])
        output = source.replace("[abc]", "[def]") + "\n[def] dvdnav demux debug: DVD-HEVC program stream map accepted"
        self.assertEqual(extract_dvdnav_trace(source), ["DVDNAV_CELL_CHANGE", "- cellN=1"])
        self.assertTrue(compare_vlc_navigation_text(source, output)["passed"])

    def test_compact_trace_ignores_only_relocated_sector_fields(self) -> None:
        source = "\n".join([
            "[a] dvdnav demux debug: DVDNAV_CELL_CHANGE",
            "[a] dvdnav demux debug: - cellN=1",
            "[a] dvdnav demux debug: - cell_length=300",
            "[a] dvdnav demux debug: - pgc_length=90000",
        ])
        output = source.replace("cell_length=300", "cell_length=150")
        self.assertFalse(compare_vlc_navigation_text(source, output)["passed"])
        self.assertTrue(compare_vlc_navigation_text(source, output, compact_relocation=True)["passed"])
        changed_behavior = output.replace("cellN=1", "cellN=2")
        self.assertFalse(compare_vlc_navigation_text(source, changed_behavior, compact_relocation=True)["passed"])

    def test_navigation_only_menu_probe_may_match_source(self) -> None:
        source = "\n".join([
            'using access_demux module "dvdnav"',
            "dvdnav demux debug: DVDNAV_VTS_CHANGE",
            "dvdnav demux debug: - vtsN=1",
            "dvdnav demux debug: - domain=8",
            "dvdnav demux debug: DVDNAV_CELL_CHANGE",
            "dvdnav demux debug: - cellN=2",
            "dvdnav demux debug: - cell_length=45634",
        ])
        output = source.replace("cell_length=45634", "cell_length=16831")
        result = validate_navigation_only_menu_text(source, output)
        self.assertTrue(result["passed"])
        self.assertTrue(result["checks"]["navigation_equivalent"])

    def test_navigation_only_fallback_rejects_source_video(self) -> None:
        source = "\n".join([
            'using access_demux module "dvdnav"',
            "dvdnav demux debug: DVDNAV_CELL_CHANGE",
            'using packetizer module "mpegvideo"',
        ])
        output = source.replace(
            'using packetizer module "mpegvideo"', "navigation only"
        )
        result = validate_navigation_only_menu_text(source, output)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["source_navigation_only"])


if __name__ == "__main__":
    unittest.main()
