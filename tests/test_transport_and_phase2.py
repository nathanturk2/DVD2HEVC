from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dvd2hevc_app.transport import (
    HEVC_END_OF_SEQUENCE,
    append_hevc_end_of_sequence,
    has_hevc_idr,
    hevc_nal_types,
    prepare_hevc_dvd_menu,
    decode_pes_timestamp,
    parse_video_pes,
)
from dvd2hevc_app.repack import (
    VideoSlot,
    _pack_packets_into_slots,
    build_mpeg2_pack_header,
    build_hevc_psm,
    build_video_pes,
    mpeg2_crc32,
    parse_mpeg2_pack_header,
    padding_packet,
    repack_cell_hevc,
    rewrite_mpeg2_pack_header,
)
from dvd2hevc_app.transport import VideoPes


def encode_timestamp(value: int, prefix: int = 0x2) -> bytes:
    return bytes(
        [
            (prefix << 4) | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        ]
    )


class TransportTests(unittest.TestCase):
    def test_terminal_hevc_access_unit_gets_one_eos_delimiter(self) -> None:
        packets = [VideoPes(pts=1, dts=1, payload=b"frame")]
        ended = append_hevc_end_of_sequence(packets)
        self.assertEqual(ended[0].payload, b"frame" + HEVC_END_OF_SEQUENCE)
        self.assertEqual(append_hevc_end_of_sequence(ended), ended)
        self.assertEqual(packets[0].payload, b"frame")

    def test_single_picture_dvd_menu_adds_a_one_tick_display_guard_and_eos(self) -> None:
        packets = [VideoPes(pts=90000, dts=89999, payload=b"picture")]
        prepared = prepare_hevc_dvd_menu(packets)
        self.assertEqual(len(prepared), 2)
        self.assertEqual((prepared[0].pts, prepared[0].dts), (90000, 89999))
        self.assertEqual((prepared[1].pts, prepared[1].dts), (90001, 90000))
        self.assertEqual(prepared[0].payload, b"picture")
        self.assertEqual(prepared[1].payload, b"picture" + HEVC_END_OF_SEQUENCE)
        self.assertEqual(prepare_hevc_dvd_menu(prepared), prepared)

    def test_multi_picture_dvd_menu_is_not_duplicated(self) -> None:
        packets = [
            VideoPes(pts=1, dts=1, payload=b"first"),
            VideoPes(pts=2, dts=2, payload=b"second"),
        ]
        prepared = prepare_hevc_dvd_menu(packets)
        self.assertEqual(len(prepared), 2)
        self.assertEqual(prepared[0], packets[0])
        self.assertEqual(prepared[1].payload, b"second" + HEVC_END_OF_SEQUENCE)

    def test_hevc_idr_detection_reads_annex_b_nal_types(self) -> None:
        payload = b"\x00\x00\x00\x01\x46\x01aud\x00\x00\x01\x26\x01idr"
        self.assertEqual(hevc_nal_types(payload), [35, 19])
        self.assertTrue(has_hevc_idr(payload))
        self.assertFalse(has_hevc_idr(b"\x00\x00\x01\x02\x01trail"))

    def test_pes_pts_round_trip(self) -> None:
        value = 123_456_789
        encoded = encode_timestamp(value)
        self.assertEqual(decode_pes_timestamp(encoded), value)
        body = bytes([0x80, 0x80, 0x05]) + encoded + b"payload"
        packet = b"\x00\x00\x01\xe0" + len(body).to_bytes(2, "big") + body
        parsed = parse_video_pes(packet)
        self.assertEqual(parsed.pts, value)
        self.assertEqual(parsed.payload, b"payload")

    def test_repacked_pes_timestamp_round_trip(self) -> None:
        packet = build_video_pes(b"hevc", pts=90000, dts=86400)
        parsed = parse_video_pes(packet)
        self.assertEqual((parsed.pts, parsed.dts, parsed.payload), (90000, 86400, b"hevc"))

    def test_hevc_program_stream_map_has_valid_crc(self) -> None:
        psm = build_hevc_psm()
        self.assertEqual(psm[:4], b"\x00\x00\x01\xbc")
        self.assertEqual(psm[12:14], b"\x24\xe0")
        self.assertEqual(psm[6] & 0x80, 0x80)
        self.assertEqual(psm[6] & 0x30, 0)
        self.assertEqual(mpeg2_crc32(psm), 0)

    def test_hevc_program_stream_map_does_not_trigger_fixed_offset_css_detection(self) -> None:
        pack = build_mpeg2_pack_header(0, program_mux_rate=25_200)
        sector = pack + build_hevc_psm()
        self.assertEqual(len(pack), 14)
        self.assertEqual(sector[0x14] & 0x30, 0)

    def test_vobu_packing_uses_earlier_capacity_for_late_timestamp(self) -> None:
        slots = [VideoSlot(0, bytes(14)), VideoSlot(1, bytes(14))]
        packets = [VideoPes(pts=90, dts=90, payload=bytes(2500))]
        sectors = _pack_packets_into_slots(
            slots,
            packets,
            pts_offset=0,
            start_ptm=0,
            end_ptm=100,
            add_psm=False,
        )
        self.assertEqual(len(sectors), 2)
        self.assertTrue(all(len(sector) == 2048 for sector in sectors))

    def test_cell_repack_announces_hevc_once_across_multiple_vobus(self) -> None:
        def source_sector(pts: int) -> bytes:
            prefix = build_mpeg2_pack_header(pts * 300, program_mux_rate=25_200)
            video = build_video_pes(bytes(1500), pts=pts, dts=pts)
            return prefix + video + padding_packet(2048 - len(prefix) - len(video))

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.vob"
            output = Path(temporary) / "output.vob"
            source.write_bytes(source_sector(0) + source_sector(100))
            result = repack_cell_hevc(
                source,
                output,
                cell_first_sector=0,
                cell_last_sector=1,
                vobus=[
                    {"sector": 0, "start_ptm": 0, "end_ptm": 100},
                    {"sector": 1, "start_ptm": 100, "end_ptm": 200},
                ],
                encoded_pes=[
                    VideoPes(pts=0, dts=0, payload=b"\x00\x00\x01\x26\x01idr"),
                    VideoPes(pts=100, dts=100, payload=b"\x00\x00\x01\x02\x01trail"),
                ],
            )
            packed = output.read_bytes()
            self.assertEqual(result["program_stream_maps"], 1)
            self.assertEqual(packed.count(b"\x00\x00\x01\xbc"), 1)

    def test_random_access_repack_announces_hevc_in_every_video_vobu(self) -> None:
        def source_sector(pts: int) -> bytes:
            prefix = build_mpeg2_pack_header(pts * 300, program_mux_rate=25_200)
            video = build_video_pes(bytes(1500), pts=pts, dts=pts)
            return prefix + video + padding_packet(2048 - len(prefix) - len(video))

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.vob"
            output = Path(temporary) / "output.vob"
            source.write_bytes(source_sector(0) + source_sector(100))
            result = repack_cell_hevc(
                source,
                output,
                cell_first_sector=0,
                cell_last_sector=1,
                vobus=[
                    {"sector": 0, "start_ptm": 0, "end_ptm": 100},
                    {"sector": 1, "start_ptm": 100, "end_ptm": 200},
                ],
                encoded_pes=[
                    VideoPes(pts=0, dts=0, payload=b"\x00\x00\x01\x26\x01idr"),
                    VideoPes(pts=100, dts=100, payload=b"\x00\x00\x01\x26\x01idr"),
                ],
                psm_policy="every-video-vobu",
            )
            self.assertEqual(result["program_stream_maps"], 2)
            self.assertEqual(result["program_stream_map_policy"], "dvd-vobu-psm-v1")
            self.assertEqual(output.read_bytes().count(b"\x00\x00\x01\xbc"), 2)

    def test_mpeg2_pack_header_round_trip_and_rewrite(self) -> None:
        header = build_mpeg2_pack_header(
            1_144_232_228,
            program_mux_rate=25_200,
            stuffing=b"\xff\xff",
        )
        parsed = parse_mpeg2_pack_header(header)
        self.assertEqual(parsed.scr_27mhz, 1_144_232_228)
        self.assertEqual(parsed.program_mux_rate, 25_200)
        self.assertEqual(parsed.stuffing_length, 2)
        sector = header + bytes(2048 - len(header))
        rewritten = rewrite_mpeg2_pack_header(
            sector,
            scr_27mhz=1_144_276_114,
            program_mux_rate=28_200,
        )
        self.assertEqual(len(rewritten), 2048)
        self.assertEqual(rewritten[16:], sector[16:])
        self.assertEqual(
            parse_mpeg2_pack_header(rewritten).scr_27mhz,
            1_144_276_114,
        )
        self.assertEqual(parse_mpeg2_pack_header(rewritten).program_mux_rate, 28_200)


if __name__ == "__main__":
    unittest.main()
