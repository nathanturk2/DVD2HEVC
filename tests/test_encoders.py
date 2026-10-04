from __future__ import annotations

import unittest

from dvd2hevc_app.encoders import (
    EncoderConfigurationError,
    HEVC_ENCODERS,
    build_hevc_encoder_options,
    resolve_encoder_preset,
)


class SelectableEncoderTests(unittest.TestCase):
    def test_bd2hevc_encoder_set_is_hevc_only(self) -> None:
        self.assertEqual(
            HEVC_ENCODERS,
            ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"),
        )

    def test_common_preset_scale_maps_to_backend_native_names(self) -> None:
        self.assertEqual(resolve_encoder_preset("hevc_nvenc", "p6"), "p6")
        self.assertEqual(resolve_encoder_preset("hevc_qsv", "p6"), "slower")
        self.assertEqual(resolve_encoder_preset("hevc_amf", "p6"), "quality")
        self.assertEqual(resolve_encoder_preset("libx265", "p6"), "medium")

    def test_every_backend_builds_only_an_hevc_main_profile(self) -> None:
        for encoder in HEVC_ENCODERS:
            with self.subTest(encoder=encoder):
                options, _native = build_hevc_encoder_options(
                    encoder,
                    quality=24,
                    preset="p6",
                    cadence_mode="progressive",
                    threads=4,
                )
                self.assertEqual(options[options.index("-c:v") + 1], encoder)
                self.assertEqual(options[options.index("-profile:v") + 1], "main")
                self.assertIn("-bf", options)
                self.assertEqual(options[options.index("-bf") + 1], "0")
                self.assertNotIn("libx264", options)
                self.assertNotIn("h264_nvenc", options)

    def test_target_vbr_is_not_implemented_as_cq(self) -> None:
        for encoder in HEVC_ENCODERS:
            with self.subTest(encoder=encoder):
                options, _ = build_hevc_encoder_options(
                    encoder, quality="vbr:1500000", preset="p6",
                    cadence_mode="progressive", threads=4,
                )
                self.assertEqual(options[options.index("-b:v") + 1], "1500000")
                self.assertNotIn("-cq", options)
                self.assertNotIn("-global_quality", options)
                self.assertNotIn("-crf", options)
                if encoder == "hevc_nvenc":
                    self.assertEqual(options[options.index("-rc") + 1], "vbr")
                if encoder == "hevc_amf":
                    self.assertEqual(options[options.index("-rc") + 1], "vbr_peak")

    def test_target_cbr_constrains_minimum_and_maximum_rate(self) -> None:
        for encoder in HEVC_ENCODERS:
            with self.subTest(encoder=encoder):
                options, _ = build_hevc_encoder_options(
                    encoder, quality="cbr:1250000", preset="p6",
                    cadence_mode="progressive", threads=4,
                )
                self.assertEqual(options[options.index("-b:v") + 1], "1250000")
                self.assertEqual(options[options.index("-minrate") + 1], "1250000")
                self.assertEqual(options[options.index("-maxrate") + 1], "1250000")
                self.assertNotIn("-cq", options)
                self.assertNotIn("-crf", options)

    def test_backends_request_forced_idr_and_access_unit_delimiters(self) -> None:
        for encoder in ("hevc_nvenc", "hevc_qsv", "hevc_amf"):
            options, _ = build_hevc_encoder_options(
                encoder, quality=27, preset="p6", cadence_mode="progressive"
            )
            forced = "-forced-idr" if encoder == "hevc_nvenc" else "-forced_idr"
            self.assertEqual(options[options.index(forced) + 1], "1")
            self.assertEqual(options[options.index("-aud") + 1], "1")
        x265, _ = build_hevc_encoder_options(
            "libx265", quality=27, preset="p6", cadence_mode="progressive"
        )
        parameters = x265[x265.index("-x265-params") + 1]
        self.assertIn("repeat-headers=1", parameters)
        self.assertIn("aud=1", parameters)
        self.assertIn("open-gop=0", parameters)

    def test_only_x265_may_retain_interlaced_output(self) -> None:
        with self.assertRaises(EncoderConfigurationError):
            build_hevc_encoder_options(
                "hevc_qsv", quality=24, preset="p6", cadence_mode="interlaced"
            )
        options, _ = build_hevc_encoder_options(
            "libx265", quality=24, preset="p6", cadence_mode="interlaced"
        )
        self.assertIn("interlace=tff", options[options.index("-x265-params") + 1])
        progressive, _ = build_hevc_encoder_options(
            "libx265", quality=24, preset="p6", cadence_mode="progressive"
        )
        self.assertNotIn("interlace=tff", progressive[progressive.index("-x265-params") + 1])


if __name__ == "__main__":
    unittest.main()
