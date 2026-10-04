import tempfile
import unittest
from pathlib import Path

from dvd2hevc_app.repack import (
    build_hevc_psm,
    build_mpeg2_pack_header,
    build_video_pes,
    padding_packet,
)
from dvd2hevc_app.repair import (
    audit_css_safe_hevc_psms,
    repair_css_safe_hevc_psms,
    verify_css_safe_hevc_psms,
)


class RepairTests(unittest.TestCase):
    def test_repair_changes_only_legacy_psm_bytes_and_preserves_size(self) -> None:
        pack = build_mpeg2_pack_header(0, program_mux_rate=25_200)
        legacy = bytearray(build_hevc_psm())
        legacy[6] = 0xE0
        # The CRC is intentionally stale here: the repair identifies the
        # fixed DVD2HEVC map fields and writes a fresh valid packet.
        video = build_video_pes(b"\x00\x00\x01\x26\x01idr", pts=0, dts=0)
        sector = pack + bytes(legacy) + video
        sector += padding_packet(2048 - len(sector))

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "old.iso"
            destination = Path(temporary) / "fixed.iso"
            source.write_bytes(sector)
            result = repair_css_safe_hevc_psms(source, destination)
            repaired = destination.read_bytes()

        self.assertEqual(result["maps_rewritten"], 1)
        self.assertTrue(result["verification"]["only_program_stream_maps_changed"])
        self.assertEqual(len(repaired), len(sector))
        self.assertEqual(repaired[0x14] & 0x30, 0)
        self.assertEqual(repaired[:14], sector[:14])
        self.assertEqual(repaired[34:], sector[34:])

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "safe.iso"
            image.write_bytes(repaired)
            self.assertTrue(audit_css_safe_hevc_psms(image)["passed"])

    def test_verifier_rejects_non_psm_changes(self) -> None:
        pack = build_mpeg2_pack_header(0, program_mux_rate=25_200)
        video = build_video_pes(b"\x00\x00\x01\x26\x01idr", pts=0, dts=0)
        sector = pack + build_hevc_psm() + video
        sector += padding_packet(2048 - len(sector))
        changed = bytearray(sector)
        changed[-1] ^= 1
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.iso"
            destination = Path(temporary) / "bad.iso"
            source.write_bytes(sector)
            destination.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "non-PSM difference"):
                verify_css_safe_hevc_psms(source, destination)


if __name__ == "__main__":
    unittest.main()
