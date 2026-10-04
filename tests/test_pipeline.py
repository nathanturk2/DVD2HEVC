from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dvd2hevc_app.pipeline import (
    cell_entry_keyframe_ticks,
    cell_cache_key,
    choose_cell_attempt,
    encode_cell_hevc,
    menu_physical_cells,
    sector_fit_rate_controls,
    select_title_pgc,
    unique_physical_cells,
    format_force_keyframe_times,
    validate_cell_entry_idr,
    validate_vobu_random_access,
    vobu_keyframe_ticks,
    verify_preserved_sectors,
    _probe_video,
)
from dvd2hevc_app.compact import (
    _dvd_audio_inventory,
    _dvd_audio_slot_any,
    _dvd_audio_slot_identity,
    _patch_nav_sector,
    _compact_row_proves_no_audio,
    _match_audio_vobu,
    _retime_expanded_vobu,
    _source_local_vobu_sector_range,
    exact_quality_attempt,
    minimum_video_slots,
)
from dvd2hevc_app.audio import (
    build_compact_audio_command,
    build_compact_audio_copy_command,
    build_dvd_ac3_pes_packets,
    count_ac3_access_units,
    _audio_timestamp_alignment,
    _merge_track_rate_variants,
    _dvd_source_audio_streams,
    _compact_audio_report_matches,
    compact_audio_target,
    dvd_audio_packet_identity,
    dvd_audio_ordinal,
    COMPACT_AUDIO_POLICY,
    parse_audio_bitrate,
    validate_dvd_ac3_pes_packets,
)
from dvd2hevc_app.transport import VideoPes
from dvd2hevc_app.transport import AudioPes, parse_audio_pes
from dvd2hevc_app.repack import build_mpeg2_pack_header, padding_packet, parse_mpeg2_pack_header
from dvd2hevc_app.staging import cell_replacement_segments, replacement_segments
from dvd2hevc_app.cli import (
    parse_compact_quality,
    parse_cq_quality,
    parse_cq_quality_list,
    parse_video_quality_list,
)
from dvd2hevc_app.quality import compact_auto_preset, estimate_cq_for_bitrate, standard_hevc_bitrate
from dvd2hevc_app.ifo import (
    _patch_tmapt,
    _relocated_vmg_extents,
    _relocated_vts_extents,
    _rewrite_compact_audio_attribute,
    _validate_compact_audio_ordinals,
    relocated_title_set_starts,
)
from dvd2hevc_app.vob import DVD_SECTOR_SIZE


class PipelinePlanningTests(unittest.TestCase):
    def test_sector_fit_rate_controls_add_cell_local_bitrate_retries(self) -> None:
        self.assertEqual(
            sector_fit_rate_controls(("vbr:1276629",)),
            (
                "vbr:1276629",
                "vbr:1213000",
                "vbr:1149000",
                "vbr:1021000",
                "vbr:894000",
                "vbr:766000",
            ),
        )
        self.assertEqual(
            sector_fit_rate_controls(("cbr:1500000",)),
            (
                "cbr:1500000",
                "cbr:1425000",
                "cbr:1350000",
                "cbr:1200000",
                "cbr:1050000",
                "cbr:900000",
            ),
        )

    def test_sector_fit_rate_controls_preserve_cq_and_explicit_ladders(self) -> None:
        self.assertEqual(sector_fit_rate_controls((24,)), (24,))
        self.assertEqual(
            sector_fit_rate_controls(("vbr:1500000", "vbr:1200000")),
            ("vbr:1500000", "vbr:1200000"),
        )

    def test_audio_cell_bitrate_variants_are_not_format_changes(self) -> None:
        previous = {
            "source_bitrate": 384_000, "source_bitrates": [384_000],
            "target_bitrate": 256_000, "target_bitrates": [256_000],
        }
        current = {
            "source_bitrate": 448_000, "source_bitrates": [448_000],
            "target_bitrate": 224_000, "target_bitrates": [224_000],
        }
        merged = _merge_track_rate_variants(previous, current)
        self.assertIsNone(merged["source_bitrate"])
        self.assertEqual(merged["source_bitrates"], [384_000, 448_000])
        self.assertEqual(merged["target_bitrate"], 256_000)
        self.assertEqual(merged["target_bitrates"], [224_000, 256_000])

    def test_vts_relocation_validates_source_padding_and_normalizes_stage(self) -> None:
        extents = _relocated_vts_extents(
            ifo_sectors=9, menu_start=32, title_start=64,
            original_menu_sectors=20, compact_menu_sectors=8,
            original_title_sectors=217, compact_title_sectors=113,
            old_last_sector=296,
        )
        self.assertEqual(extents["new_menu_start"], 9)
        self.assertEqual(extents["new_title_start"], 17)
        self.assertEqual(extents["new_last_sector"], 138)
        self.assertEqual(extents["prefix_padding_sectors"], 23)
        self.assertEqual(extents["menu_title_padding_sectors"], 12)
        self.assertEqual(extents["tail_sectors"], 16)

    def test_vts_relocation_rejects_overlapping_authored_extents(self) -> None:
        with self.assertRaisesRegex(Exception, "Unexpected VTS menu/title"):
            _relocated_vts_extents(
                ifo_sectors=9, menu_start=32, title_start=50,
                original_menu_sectors=20, compact_menu_sectors=8,
                original_title_sectors=217, compact_title_sectors=113,
                old_last_sector=296,
            )

    def test_vmg_relocation_removes_old_padding_and_packs_following_vts(self) -> None:
        extents = _relocated_vmg_extents(
            ifo_sectors=12,
            menu_start=32,
            original_menu_sectors=20_644,
            compact_menu_sectors=3_000,
            old_last_sector=20_715,
        )
        self.assertEqual(extents["new_menu_start"], 12)
        self.assertEqual(extents["new_last_sector"], 3_023)
        self.assertEqual(extents["prefix_padding_sectors"], 20)
        self.assertEqual(extents["tail_sectors"], 40)
        self.assertEqual(
            relocated_title_set_starts(
                {1: 20_736, 2: 21_056},
                {1: 139, 2: 167},
                first_start=extents["new_last_sector"] + 1,
            ),
            {1: 3_024, 2: 3_163},
        )

    def test_vmg_relocation_rejects_a_missing_backup_ifo_extent(self) -> None:
        with self.assertRaisesRegex(Exception, "complete backup IFO"):
            _relocated_vmg_extents(
                ifo_sectors=12,
                menu_start=32,
                original_menu_sectors=20_644,
                compact_menu_sectors=3_000,
                old_last_sector=20_680,
            )

    def test_segmented_cell_audio_scan_uses_extracted_local_vobu_offsets(self) -> None:
        cell = {"cell": {"first_sector": 100}}
        vobu = {
            "original_first_sector": 1_000,
            "original_last_sector": 1_002,
            "source_local_first_sector": 40,
            "source_local_last_sector": 42,
        }
        self.assertEqual(list(_source_local_vobu_sector_range(cell, vobu)), [40, 41, 42])

    def test_dvd_audio_ordinals_follow_physical_substream_ids(self) -> None:
        self.assertEqual(dvd_audio_ordinal({"id": "0x80", "codec_name": "ac3"}, 7), 0)
        self.assertEqual(dvd_audio_ordinal({"id": "0x84", "codec_name": "ac3"}, 0), 4)
        self.assertEqual(dvd_audio_ordinal({"id": "0xc2", "codec_name": "mp2"}, 0), 2)
        self.assertEqual(dvd_audio_ordinal({}, 3), 3)

    def test_supported_dvd_audio_packet_identities_cover_private_and_mpeg_audio(self) -> None:
        self.assertEqual(dvd_audio_packet_identity("ac3", 1), (0xBD, 0x81))
        self.assertEqual(dvd_audio_packet_identity("dts", 1), (0xBD, 0x89))
        self.assertEqual(dvd_audio_packet_identity("pcm_dvd", 2), (0xBD, 0xA2))
        self.assertEqual(dvd_audio_packet_identity("mp1", 2), (0xC2, None))
        self.assertEqual(dvd_audio_packet_identity("mp2", 3), (0xC3, None))

    def test_dvd_audio_probe_discards_phantom_non_dvd_stream_ids(self) -> None:
        probe = {"streams": [
            {"codec_type": "audio", "codec_name": "ac3", "id": "0x80", "channels": 6, "sample_rate": "48000"},
            {"codec_type": "audio", "codec_name": "mp2", "id": "0x1da", "channels": 0, "sample_rate": "0"},
        ]}
        self.assertEqual(
            [stream["id"] for stream in _dvd_source_audio_streams(probe)],
            ["0x80"],
        )
        probe["streams"][1]["id"] = "0x1c0"
        with self.assertRaisesRegex(Exception, "no decodable format parameters"):
            _dvd_source_audio_streams(probe)

    def test_compact_audio_finds_private_and_direct_mpeg_audio_packs(self) -> None:
        header = build_mpeg2_pack_header(0, program_mux_rate=25_200)

        def sector(stream_id: int, payload: bytes) -> bytes:
            body = b"\x80\x00\x00" + payload
            pes = b"\x00\x00\x01" + bytes([stream_id]) + len(body).to_bytes(2, "big") + body
            return header + pes + padding_packet(DVD_SECTOR_SIZE - len(header) - len(pes))

        dts = sector(0xBD, b"\x89" + bytes(100))
        mpeg = sector(0xC2, bytes(100))
        self.assertIsNotNone(_dvd_audio_slot_identity(dts, 0, 0xBD, 0x89))
        self.assertIsNotNone(_dvd_audio_slot_identity(mpeg, 0, 0xC2, None))
        self.assertIsNotNone(_dvd_audio_slot_any(dts, 0, {0x89}, set()))
        self.assertIsNotNone(_dvd_audio_slot_any(mpeg, 0, set(), {0xC2}))
        self.assertEqual(_dvd_audio_inventory(dts, 0), ([(0xBD, 0x89)], set()))
        self.assertEqual(_dvd_audio_inventory(mpeg, 0), ([(0xC2, None)], set()))

    def test_compact_audio_cache_identity_includes_rates_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.vob"
            source.touch()
            outputs = [root / name for name in ("audio.ts", "audio.ac3", "audio.dvd-pes")]
            for output in outputs:
                output.touch()
            report = {
                "schema": "dvd2hevc-compact-audio-prototype-v0",
                "status": "passed",
                "audio_mode": "compact-stereo",
                "processing_policy": COMPACT_AUDIO_POLICY,
                "source": str(source),
                "stereo_bitrate": 256000,
                "mono_bitrate": 128000,
                "tracks": [{
                    "source_channels": 6,
                    "source_codec": "ac3",
                    "source_bitrate": 384000,
                    "processing": "downmix",
                    "elementary_identity": "reencoded",
                    "target": {"channels": 2, "bitrate": 256000, "sample_rate": 48000},
                    "timestamped_output": str(outputs[0]),
                    "elementary_output": str(outputs[1]),
                    "dvd_private_stream_output": str(outputs[2]),
                }],
            }
            self.assertTrue(_compact_audio_report_matches(
                report, source, stereo_bitrate=256000, mono_bitrate=128000
            ))
            self.assertFalse(_compact_audio_report_matches(
                report, source, stereo_bitrate=192000, mono_bitrate=128000
            ))
            outputs[2].unlink()
            self.assertFalse(_compact_audio_report_matches(
                report, source, stereo_bitrate=256000, mono_bitrate=128000
            ))

    def test_audio_cell_preroll_is_bounded_and_kept_in_its_physical_cell(self) -> None:
        ranges = [(100, 1_000_000, 1_043_200), (200, 1_043_200, 1_086_400)]
        self.assertEqual(_match_audio_vobu(ranges, 1_010_000), (100, None))
        self.assertEqual(_match_audio_vobu(ranges, 980_000), (100, "preroll"))
        self.assertEqual(_match_audio_vobu(ranges, 940_000), (100, "preroll"))
        self.assertEqual(_match_audio_vobu(ranges, 1_100_000), (200, "postroll"))
        self.assertEqual(_match_audio_vobu(ranges, 900_000), (None, None))

    def test_silent_compact_domain_requires_complete_scan_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text(
                '{"cells":[{"name":"cell-1","validation":{"structure":{"audio_pes_packets":0}}}]}',
                encoding="utf-8",
            )
            row = {"input_report": str(report), "cells": [{"name": "cell-1"}]}
            self.assertTrue(_compact_row_proves_no_audio(row))
            row["cells"].append({"name": "cell-2"})
            self.assertFalse(_compact_row_proves_no_audio(row))

    def test_silent_compact_domain_accepts_compact_first_audio_inventory(self) -> None:
        row = {
            "input_report": "not-needed-when-inventory-is-complete.json",
            "cells": [{
                "name": "cell-1",
                "vobus": [{
                    "source_audio_inventory_policy": "dvd-sector-identity-count-v1",
                    "source_audio_sectors_by_identity": {},
                    "source_audio_unsafe_sectors_by_identity": {},
                    "source_audio_multi_identity_sectors": 0,
                }],
            }],
        }
        self.assertTrue(_compact_row_proves_no_audio(row))
        row["cells"][0]["vobus"][0]["source_audio_sectors_by_identity"] = {
            "0xbd:0x80": 1
        }
        self.assertFalse(_compact_row_proves_no_audio(row))

    def test_silent_compact_domain_accepts_compact_input_extraction_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text(
                '{"cells":[{"name":"cell-1","validation":{"structure":null},'
                '"extraction":{"vob_stats":{"audio_pes_packets":0}}}]}',
                encoding="utf-8",
            )
            row = {
                "input_report": str(report),
                "cells": [{"name": "cell-1", "vobus": [{}]}],
            }
            self.assertTrue(_compact_row_proves_no_audio(row))

    def test_known_short_dvd_ps_can_bypass_format_autodetection(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_name":"hevc","nb_read_frames":"2"}]}',
            stderr="",
        )
        with patch("dvd2hevc_app.pipeline._run", return_value=completed) as run:
            result = _probe_video(Path("short-still.vob"), "ffprobe", force_mpeg_ps=True)

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-f") : command.index("-f") + 2], ["-f", "mpeg"])
        self.assertLess(command.index("-f"), command.index("short-still.vob"))
        self.assertEqual(result["nb_read_frames"], 2)

    def test_known_short_encoded_ts_can_bypass_format_autodetection(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_name":"hevc","nb_read_frames":"2"}]}',
            stderr="",
        )
        with patch("dvd2hevc_app.pipeline._run", return_value=completed) as run:
            result = _probe_video(Path("short-still.ts"), "ffprobe", force_mpeg_ts=True)

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-f") : command.index("-f") + 2], ["-f", "mpegts"])
        self.assertLess(command.index("-f"), command.index("short-still.ts"))
        self.assertEqual(result["nb_read_frames"], 2)

    def test_only_cell_entry_becomes_a_forced_idr_timestamp(self) -> None:
        vobus = [
            {"start_ptm": 90000},
            {"start_ptm": 133200},
            {"start_ptm": 205200},
        ]
        ticks = cell_entry_keyframe_ticks(vobus)
        self.assertEqual(ticks, (0,))
        self.assertEqual(format_force_keyframe_times(ticks), "0")

    def test_every_vobu_can_become_a_random_access_timestamp(self) -> None:
        vobus = [
            {"start_ptm": 90000},
            {"start_ptm": 133200},
            {"start_ptm": 205200},
        ]
        ticks = vobu_keyframe_ticks(vobus)
        self.assertEqual(ticks, (0, 43200, 115200))
        self.assertEqual(format_force_keyframe_times(ticks), "0,0.48,1.28")

    def test_cell_entry_requires_idr_but_later_vobus_may_be_predictive(self) -> None:
        idr = VideoPes(pts=0, dts=0, payload=b"\x00\x00\x01\x26\x01")
        predictive = VideoPes(pts=3600, dts=3600, payload=b"\x00\x00\x01\x02\x01")
        budget = {"vobus": [{"index": 0, "packet_count": 2}, {"index": 1, "packet_count": 1}]}
        with self.assertRaisesRegex(Exception, "cell entry VOBU 0"):
            validate_cell_entry_idr([predictive, predictive, idr], budget)
        result = validate_cell_entry_idr([idr, predictive, predictive], budget)
        self.assertTrue(result["entry_idr"])
        self.assertEqual(result["predictive_vobus_allowed"], 1)

    def test_encoder_rejects_unsorted_forced_keyframes(self) -> None:
        with self.assertRaisesRegex(Exception, "must start at zero and increase"):
            encode_cell_hevc(
                Path("source.vob"), Path("output.ts"), ffmpeg="ffmpeg", quality=24,
                preset="p4", threads=0, log=Path("encode.log"),
                force_keyframe_ticks=(0, 43200, 40000),
            )

    def test_large_forced_keyframe_set_uses_file_backed_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "encoded.ts"
            destination.write_bytes(b"encoded fixture")
            ticks = tuple(range(0, 3_600 * 2_000, 3_600))
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("dvd2hevc_app.pipeline._run", return_value=completed) as run:
                result = encode_cell_hevc(
                    root / "source.vob", destination, ffmpeg="ffmpeg",
                    quality="vbr:1200000", preset="p6", threads=0,
                    log=root / "encode.log", encoder="hevc_nvenc",
                    cadence_mode="progressive",
                    force_keyframe_ticks=ticks,
                )

            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-force_key_frames") + 1], "chapters")
            self.assertEqual(command[command.index("-map_chapters") + 1], "1")
            metadata = Path(command[command.index("ffmetadata") + 2])
            text = metadata.read_text(encoding="utf-8")
            self.assertEqual(text.count("[CHAPTER]"), len(ticks))
            self.assertIn("TIMEBASE=1/90000", text)
            self.assertEqual(result["keyframe_transport"], "chapters-file")

    def test_every_vobu_random_access_requires_headers_and_idr(self) -> None:
        access_unit = (
            b"\x00\x00\x01\x40\x01"  # VPS 32
            b"\x00\x00\x01\x42\x01"  # SPS 33
            b"\x00\x00\x01\x44\x01"  # PPS 34
            b"\x00\x00\x01\x26\x01"  # IDR 19
        )
        packets = [VideoPes(pts=0, dts=0, payload=access_unit) for _ in range(2)]
        budget = {"vobus": [
            {"index": 0, "packet_count": 1},
            {"index": 1, "packet_count": 1},
        ]}
        result = validate_vobu_random_access(packets, budget)
        self.assertEqual(result["random_access_vobus"], 2)
        with self.assertRaisesRegex(Exception, "not independently decodable"):
            validate_vobu_random_access([
                packets[0], VideoPes(pts=1, dts=1, payload=b"\x00\x00\x01\x02\x01")
            ], budget)

    def test_compact_ac3_is_reframed_as_dvd_private_stream_pes(self) -> None:
        frames = [b"\x0b\x77" + bytes([index]) * 1022 for index in range(3)]
        source = [AudioPes(pts=1000 + index * 2880, payload=frame) for index, frame in enumerate(frames)]
        packets = build_dvd_ac3_pes_packets(source)
        self.assertEqual(len(packets), 2)
        result = validate_dvd_ac3_pes_packets(packets, b"".join(frames), expected_access_units=3)
        self.assertEqual(result, {"packets": 2, "access_units": 3, "elementary_bytes": 3072})

    def test_compact_ac3_keeps_cell_leading_frame_continuation(self) -> None:
        continuation = b"\xaa" * 99
        first_complete_frame = b"\x0b\x77" + b"\x11" * 766
        second_complete_frame = b"\x0b\x77" + b"\x22" * 766
        source = [
            AudioPes(pts=90_000, payload=continuation),
            AudioPes(pts=92_880, payload=first_complete_frame),
            AudioPes(pts=95_760, payload=second_complete_frame),
        ]

        packets = build_dvd_ac3_pes_packets(source, substream_id=0x81)
        expected = continuation + first_complete_frame + second_complete_frame
        result = validate_dvd_ac3_pes_packets(
            packets,
            expected,
            expected_access_units=2,
        )

        self.assertEqual(count_ac3_access_units(source), 2)
        self.assertEqual(result["access_units"], 2)
        first = parse_audio_pes(packets[0])
        self.assertEqual(first.pts, 92_880)
        self.assertEqual(first.payload[0], 0x81)
        self.assertEqual(int.from_bytes(first.payload[2:4], "big"), 100)

    def test_compact_ac3_uses_pes_stuffing_for_short_pack_remainder(self) -> None:
        continuation = b"\xaa" * 2_016
        source = [AudioPes(pts=90_000, payload=continuation)]

        packets = build_dvd_ac3_pes_packets(source)
        result = validate_dvd_ac3_pes_packets(
            packets,
            continuation,
            expected_access_units=0,
        )

        self.assertEqual(len(packets), 1)
        self.assertEqual(len(packets[0]), 2_034)
        self.assertEqual(packets[0][8], 5)
        self.assertEqual(result["elementary_bytes"], 2_016)

    def test_compact_ac3_pads_a_one_byte_final_continuation_pes(self) -> None:
        continuation = b"\xaa" * 2_017
        packets = build_dvd_ac3_pes_packets([
            AudioPes(pts=90_000, payload=continuation),
        ])

        result = validate_dvd_ac3_pes_packets(
            packets, continuation, expected_access_units=0,
        )

        self.assertEqual([len(packet) for packet in packets], [2_034, 18])
        self.assertEqual(packets[-1][8], 4)
        self.assertEqual(result["elementary_bytes"], 2_017)

    def test_audio_reencode_uses_decoded_frames_not_pes_packetization(self) -> None:
        source = {
            "first_pts": 25_854, "last_pts": 832_254, "packets": 279,
            "samples": 428_544,
        }
        output = {**source}

        self.assertEqual(
            _audio_timestamp_alignment(source, output, reencoded=True),
            "decoded-ac3-exact-coverage",
        )
        self.assertIsNone(_audio_timestamp_alignment(
            source, {**output, "samples": output["samples"] - 1}, reencoded=True,
        ))

    def test_audio_reencode_accepts_normalized_duplicate_boundary_pts(self) -> None:
        source = {
            "first_pts": 4_694_940, "last_pts": 26_447_580,
            "packets": 7_556, "samples": 11_606_016,
        }
        output = {**source}

        self.assertEqual(
            _audio_timestamp_alignment(source, output, reencoded=True),
            "decoded-ac3-exact-coverage",
        )

    def test_cross_codec_audio_bounds_encoder_priming_and_padding(self) -> None:
        source = {
            "first_pts": 48_349_118, "last_pts": 59_768_318,
            "packets": 11_896, "samples": 6_090_752,
        }
        output = {
            "first_pts": 48_353_918, "last_pts": 59_773_119,
            "packets": 3_967, "samples": 6_093_312,
        }
        self.assertEqual(
            _audio_timestamp_alignment(
                source, output, reencoded=True, source_codec="dts",
            ),
            "decoded-cross-codec-bounded-coverage",
        )
        self.assertIsNone(_audio_timestamp_alignment(
            source, {**output, "samples": source["samples"] - 1},
            reencoded=True, source_codec="dts",
        ))

    def test_audio_reencode_accepts_one_split_cell_entry_frame(self) -> None:
        source = {"first_pts": 30_645_173, "last_pts": 55_024_373, "packets": 8_467}
        output = {"first_pts": 30_648_053, "last_pts": 55_024_373, "packets": 8_466}

        self.assertEqual(
            _audio_timestamp_alignment(source, output, reencoded=True),
            "cell-leading-continuation-trimmed",
        )
        self.assertIsNone(_audio_timestamp_alignment(source, output, reencoded=False))

    def test_audio_reencode_accepts_one_frame_split_across_two_pes_fragments(self) -> None:
        source = {"first_pts": 15_881_816, "last_pts": 17_431_256, "packets": 540}
        output = {"first_pts": 15_884_696, "last_pts": 17_431_256, "packets": 538}

        self.assertEqual(
            _audio_timestamp_alignment(source, output, reencoded=True),
            "cell-leading-continuation-fragments-trimmed",
        )
        self.assertIsNone(
            _audio_timestamp_alignment(
                source, {**output, "packets": 537}, reencoded=True
            )
        )

    def test_audio_reencode_accepts_uniform_one_tick_rounding(self) -> None:
        source = {"first_pts": 4_437, "last_pts": 38_296_917, "packets": 13_297}
        output = {"first_pts": 4_436, "last_pts": 38_296_916, "packets": 13_297}

        self.assertEqual(
            _audio_timestamp_alignment(source, output, reencoded=True),
            "uniform-one-tick-rounding",
        )
        self.assertIsNone(_audio_timestamp_alignment(source, output, reencoded=False))

    def test_cross_codec_audio_accepts_only_one_ac3_frame_of_grid_difference(self) -> None:
        source = {"first_pts": 25_854, "last_pts": 1_752_894, "packets": 1_800}
        output = {"first_pts": 25_854, "last_pts": 1_750_974, "packets": 600}
        self.assertEqual(
            _audio_timestamp_alignment(
                source, output, reencoded=True, source_codec="dts"
            ),
            "cross-codec-frame-grid",
        )
        drifted = {**output, "last_pts": source["last_pts"] - 2_881}
        self.assertIsNone(
            _audio_timestamp_alignment(
                source, drifted, reencoded=True, source_codec="dts"
            )
        )

    def test_ifo_audio_formats_are_rewritten_to_stereo_ac3(self) -> None:
        formats = {"ac3": 0, "mp1": 2, "mp2": 3, "pcm_dvd": 4, "dts": 6}
        for codec, audio_format in formats.items():
            with self.subTest(codec=codec):
                descriptor = bytearray(8)
                descriptor[0] = (audio_format << 5) | 0x1C
                descriptor[1] = 0xF8 | 5  # six channels plus non-AC-3 format bits
                descriptor[2:4] = b"en"
                descriptor[7] = 0xFF
                _rewrite_compact_audio_attribute(
                    descriptor,
                    0,
                    source_codec=codec,
                    source_channels=6,
                    target_channels=2,
                    processing="downmix",
                )
                self.assertEqual(descriptor[0] >> 5, 0)
                self.assertEqual((descriptor[0] >> 4) & 1, 0)
                self.assertEqual((descriptor[1] & 0x07) + 1, 2)
                self.assertEqual(descriptor[1] & 0xF0, 0)
                self.assertEqual(descriptor[2:4], b"en")
                self.assertEqual(descriptor[7], 0)

    def test_ifo_accepts_sparse_observed_audio_tracks(self) -> None:
        _validate_compact_audio_ordinals(8, [0, 1])
        _validate_compact_audio_ordinals(3, [2])
        with self.assertRaisesRegex(Exception, "outside the VTS IFO range"):
            _validate_compact_audio_ordinals(3, [3])
        with self.assertRaisesRegex(Exception, "duplicate track ordinals"):
            _validate_compact_audio_ordinals(3, [2, 2])

    def test_audio_reencode_combines_cell_fragment_and_one_tick_rounding(self) -> None:
        source = {"first_pts": 38_299_797, "last_pts": 68_004_117, "packets": 10_316}
        output = {"first_pts": 38_302_676, "last_pts": 68_004_116, "packets": 10_315}

        self.assertEqual(
            _audio_timestamp_alignment(source, output, reencoded=True),
            "cell-leading-continuation-trimmed-one-tick-rounding",
        )

    def test_time_map_preserves_discontinuity_flag_while_relocating_vobu(self) -> None:
        data = bytearray(64)
        data[0:2] = (1).to_bytes(2, "big")
        data[4:8] = (19).to_bytes(4, "big")
        data[8:12] = (12).to_bytes(4, "big")
        data[12] = 1
        data[14:16] = (1).to_bytes(2, "big")
        data[16:20] = (0x8000002C).to_bytes(4, "big")
        result = _patch_tmapt(data, 0, {44: 30})
        # A zero sector means no table by API contract; use an offset sector in
        # a full-sized synthetic IFO for the real relocation assertion.
        self.assertEqual(result, {"maps": 0, "entries": 0})
        full = bytearray(2048 + 64)
        full[2048:2112] = data
        result = _patch_tmapt(full, 1, {44: 30})
        self.assertEqual(result, {"maps": 1, "entries": 1})
        self.assertEqual(int.from_bytes(full[2064:2068], "big"), 0x8000001E)

    def test_vmgi_title_sets_pack_after_compacted_predecessor(self) -> None:
        self.assertEqual(
            relocated_title_set_starts(
                {1: 2150, 2: 3_359_252},
                {1: 2_237_442, 2: 1_654},
            ),
            {1: 2150, 2: 2_239_592},
        )

    def test_standard_ratio_and_cq_curve(self) -> None:
        self.assertEqual(standard_hevc_bitrate(6_000_000), 1_500_000)
        estimate = estimate_cq_for_bitrate(1_500_000)
        self.assertAlmostEqual(estimate["estimated_cq"], 26.5, delta=0.2)
        self.assertEqual(estimate["nearest_half_cq"], 26.5)

    def test_handbrake_style_cq_spelling(self) -> None:
        self.assertEqual(parse_cq_quality("cq:20"), 20)
        self.assertEqual(parse_cq_quality("24"), 24)
        self.assertEqual(parse_cq_quality("cq:26.5"), 26.5)
        self.assertEqual(parse_cq_quality_list("cq:20,cq:24"), (20, 24))
        self.assertEqual(
            parse_video_quality_list("vbr:1500k,cbr:1250000,cq:24"),
            ("vbr:1500000", "cbr:1250000", 24),
        )
        self.assertEqual(parse_compact_quality("compact-auto"), "compact-auto")

    def test_compact_auto_preset_uses_nearest_half_step(self) -> None:
        preset = compact_auto_preset({
            "schema": "dvd2hevc-equivalent-quality-v0",
            "source": {"average_video_bps": 6_000_000},
            "standard_equivalence": {
                "hevc_to_mpeg2_ratio": 0.25,
                "target_hevc_bps": 1_500_000,
            },
            "cq_estimate": {
                "nearest_half_cq": 26.5,
                "predicted_half_step_bps": 1_495_000,
            },
        }, requested_ratio=0.25)
        self.assertEqual(preset["resolved_quality"], 26.5)
        self.assertEqual(preset["name"], "compact-auto")

    def test_compact_audio_policy_is_dvd_safe(self) -> None:
        self.assertEqual(compact_audio_target(6), {
            "codec": "ac3", "channels": 2, "sample_rate": 48000, "bitrate": 256000,
        })
        self.assertEqual(compact_audio_target(1)["channels"], 1)
        self.assertEqual(compact_audio_target(1)["bitrate"], 128000)
        self.assertEqual(parse_audio_bitrate("256k"), 256000)
        with self.assertRaisesRegex(Exception, "Unsupported DVD AC-3 bitrate"):
            parse_audio_bitrate("250k")

    def test_compact_audio_command_maps_one_stream(self) -> None:
        command = build_compact_audio_command(
            "ffmpeg", Path("source.vob"), Path("audio.ac3"),
            stream_index=3, channels=2, bitrate=256000,
        )
        self.assertIn("0:3", command)
        self.assertIn("256000", command)
        self.assertIn("-copyts", command)
        self.assertIn("-mpegts_copyts", command)
        self.assertEqual(command[-2:], ["mpegts", "audio.ac3"])

        copy = build_compact_audio_copy_command(
            "ffmpeg", Path("source.vob"), Path("audio.ts"), stream_index=4,
        )
        self.assertIn("0:4", copy)
        self.assertEqual(copy[copy.index("-c:a") + 1], "copy")
        self.assertNotIn("-b:a", copy)

    def test_compact_layout_requires_exact_quality(self) -> None:
        cell = {"name": "fixture", "attempts": [{"encode": {"quality": 22}}]}
        with self.assertRaisesRegex(Exception, "exact quality 20"):
            exact_quality_attempt(cell, 20)
        bitrate_cell = {
            "name": "target-fixture",
            "attempts": [{"encode": {"quality": "vbr:1500000"}}],
        }
        self.assertEqual(
            exact_quality_attempt(bitrate_cell, "vbr:1500k")["encode"]["quality"],
            "vbr:1500000",
        )
        with self.assertRaisesRegex(Exception, "exact quality cbr:1500000"):
            exact_quality_attempt(bitrate_cell, "cbr:1500000")

    def test_compact_layout_can_grow_a_tight_vobu_without_quality_fallback(self) -> None:
        packet = VideoPes(pts=1000, dts=1000, payload=b"x" * 3000)
        slots = minimum_video_slots([], [packet], pts_offset=0, start_ptm=0, end_ptm=2000)
        self.assertEqual(slots, 2)

    def test_expanded_vobu_is_retimed_inside_source_scr_interval(self) -> None:
        def sector(scr: int) -> bytes:
            header = build_mpeg2_pack_header(scr, program_mux_rate=25_200)
            return header + padding_packet(DVD_SECTOR_SIZE - len(header))

        source = [sector(1_000_000), sector(1_087_772)]
        expanded = [source[0], source[0], source[0], source[1]]
        rewritten, mux_rate = _retime_expanded_vobu(expanded, source)
        clocks = [parse_mpeg2_pack_header(item).scr_27mhz for item in rewritten]
        self.assertEqual((clocks[0], clocks[-1]), (1_000_000, 1_087_772))
        self.assertEqual(clocks, sorted(set(clocks)))
        self.assertGreater(mux_rate, 25_200)

    def test_interleaved_nav_addresses_follow_compact_physical_layout(self) -> None:
        def private_stream_2(substream: int, size: int = 600) -> bytes:
            payload = bytes([substream]) + bytes(size - 1)
            return b"\x00\x00\x01\xbf" + len(payload).to_bytes(2, "big") + payload

        header = build_mpeg2_pack_header(1_000_000, program_mux_rate=25_200)
        pci = private_stream_2(0)
        dsi = bytearray(private_stream_2(1))
        dsi_data = 7
        dsi[dsi_data + 4 : dsi_data + 8] = (100).to_bytes(4, "big")
        dsi[dsi_data + 8 : dsi_data + 12] = (99).to_bytes(4, "big")
        dsi[dsi_data + 34 : dsi_data + 38] = (99).to_bytes(4, "big")
        dsi[dsi_data + 38 : dsi_data + 42] = (200).to_bytes(4, "big")
        dsi[dsi_data + 42 : dsi_data + 44] = (100).to_bytes(2, "big")
        # Avoid introducing synthetic SRI targets in this focused fixture.
        for offset in [234, *[238 + index * 4 for index in range(19)], 314]:
            dsi[dsi_data + offset : dsi_data + offset + 4] = (0x3FFFFFFF).to_bytes(4, "big")
        for offset in [318, *[322 + index * 4 for index in range(19)], 398]:
            dsi[dsi_data + offset : dsi_data + offset + 4] = (0x3FFFFFFF).to_bytes(4, "big")
        used = len(header) + len(pci) + len(dsi)
        sector = header + pci + bytes(dsi) + padding_packet(DVD_SECTOR_SIZE - used)

        output = _patch_nav_sector(
            sector,
            old_vobu_start=100,
            new_vobu_start=50,
            compact_sectors=20,
            old_vobu_to_new={100: 50, 300: 100},
            old_vobu_end_to_new={199: 69, 399: 129},
            old_relative_to_new={0: 0, 99: 19},
            kept_video_relative=[0, 99],
        )
        dsi_start = len(header) + len(pci) + 7
        self.assertEqual(int.from_bytes(output[dsi_start + 4 : dsi_start + 8], "big"), 50)
        self.assertEqual(int.from_bytes(output[dsi_start + 8 : dsi_start + 12], "big"), 19)
        self.assertEqual(int.from_bytes(output[dsi_start + 34 : dsi_start + 38], "big"), 19)
        self.assertEqual(int.from_bytes(output[dsi_start + 38 : dsi_start + 42], "big"), 50)
        self.assertEqual(int.from_bytes(output[dsi_start + 42 : dsi_start + 44], "big"), 30)

        terminal = bytearray(sector)
        terminal[dsi_start + 38 : dsi_start + 42] = (0xFFFFFFFF).to_bytes(4, "big")
        terminal[dsi_start + 42 : dsi_start + 44] = (0xFFFF).to_bytes(2, "big")
        terminal_output = _patch_nav_sector(
            bytes(terminal),
            old_vobu_start=100,
            new_vobu_start=50,
            compact_sectors=20,
            old_vobu_to_new={100: 50},
            old_vobu_end_to_new={199: 69},
            old_relative_to_new={0: 0, 99: 19},
            kept_video_relative=[0, 99],
        )
        self.assertEqual(
            int.from_bytes(terminal_output[dsi_start + 38 : dsi_start + 42], "big"),
            0xFFFFFFFF,
        )
        self.assertEqual(
            int.from_bytes(terminal_output[dsi_start + 42 : dsi_start + 44], "big"),
            0xFFFF,
        )

    def test_menu_addresses_gain_pgc_duration(self) -> None:
        addresses = [{"vob_id": 2, "cell_id": 3, "start_sector": 10, "last_sector": 20}]
        pgci = [{"pgcs": [{"cells": [{
            "vob_id": 2, "cell_id": 3, "first_sector": 10, "last_sector": 20,
            "duration_ticks": 45000,
        }]}]}]
        self.assertEqual(menu_physical_cells(addresses, pgci), [{
            "vob_id": 2, "cell_id": 3, "first_sector": 10, "last_sector": 20,
            "duration_ticks": 45000,
        }])

    def test_whole_cell_choice_accepts_empty_menu_vobus_without_headroom(self) -> None:
        budget = {
            "vobu_count": 2,
            "unassigned_pes_count": 0,
            "vobus": [
                {"headroom": 8192, "encoded_bytes": 1000, "fits": True},
                {"headroom": 0, "encoded_bytes": 0, "fits": True},
            ],
        }
        self.assertEqual(choose_cell_attempt([budget], minimum_headroom=4096), 0)

    def test_title_maps_to_single_pgc(self) -> None:
        graph = {
            "global_titles": [{"title": 3, "vts": 1, "vts_title_number": 2}],
            "title_sets": [{
                "vts": 1,
                "chapters": [{"vts_title_number": 2, "parts": [{"pgc": 2}, {"pgc": 2}]}],
                "title_pgcs": [{"cells": []}, {"cells": [{"first_sector": 1}]}],
            }],
        }
        title, vts, pgc = select_title_pgc(graph, 3)
        self.assertEqual((title["title"], vts["vts"], pgc), (3, 1, 2))

    def test_duplicate_physical_cells_are_encoded_once(self) -> None:
        cell = {"vob_id": 2, "cell_id": 4, "first_sector": 10, "last_sector": 20}
        self.assertEqual(unique_physical_cells([cell, dict(cell)]), [cell])

    def test_nonvideo_sector_comparison(self) -> None:
        sector = b"\x00\x00\x01\xba" + bytes([0x40]) + bytes(8) + b"\x00" + padding_packet(2034)
        self.assertEqual(len(sector), 2048)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.vob"
            output = Path(temporary) / "output.vob"
            source.write_bytes(sector)
            output.write_bytes(sector)
            result = verify_preserved_sectors(source, output)
        self.assertEqual(result["identical_nonvideo_sectors"], 1)

    def test_replacement_can_cross_vob_file_boundary(self) -> None:
        sectors = DVD_SECTOR_SIZE
        self.assertEqual(
            replacement_segments([2 * sectors, 3 * sectors], 1, 3),
            [(0, sectors, sectors), (1, 0, 2 * sectors)],
        )

    def test_interleaved_replacement_skips_alternate_physical_ranges(self) -> None:
        sectors = DVD_SECTOR_SIZE
        cell = {
            "cell": {"first_sector": 1, "last_sector": 6},
            "physical_segments": [
                {"start_sector": 1, "last_sector": 2},
                {"start_sector": 5, "last_sector": 6},
            ],
        }
        self.assertEqual(
            cell_replacement_segments([4 * sectors, 4 * sectors], cell),
            [
                {
                    "file_index": 0, "file_offset": sectors,
                    "byte_count": 2 * sectors, "logical_offset": 0,
                    "physical_first_sector": 1, "physical_last_sector": 2,
                },
                {
                    "file_index": 1, "file_offset": sectors,
                    "byte_count": 2 * sectors, "logical_offset": 2 * sectors,
                    "physical_first_sector": 5, "physical_last_sector": 6,
                },
            ],
        )

    def test_cache_key_changes_with_encoder_settings(self) -> None:
        cell = {"vob_id": 1, "cell_id": 2, "first_sector": 10, "last_sector": 20}
        identity = {"size": 100, "mtime_ns": 1, "edge_sha256": "abc"}
        first = cell_cache_key(identity, vts=1, cell=cell, vobus=[{"sector": 10}], settings={"crf": 20})
        second = cell_cache_key(identity, vts=1, cell=cell, vobus=[{"sector": 10}], settings={"crf": 22})
        self.assertNotEqual(first, second)

    def test_whole_cell_choice_never_splices_quality_attempts(self) -> None:
        def budget(headrooms: list[int]) -> dict[str, object]:
            return {
                "vobu_count": len(headrooms),
                "unassigned_pes_count": 0,
                "vobus": [{"headroom": value} for value in headrooms],
            }

        choice = choose_cell_attempt([
            budget([5000, -1, -1]),
            budget([6000, 5000, -1]),
            budget([7000, 7000, 5000]),
        ])
        self.assertEqual(choice, 2)


if __name__ == "__main__":
    unittest.main()
