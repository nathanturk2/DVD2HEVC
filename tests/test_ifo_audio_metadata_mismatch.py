import unittest

from dvd2hevc_app.ifo import _rewrite_compact_audio_attribute


class IfoAudioMetadataMismatchTests(unittest.TestCase):
    def test_dts_authored_channel_mismatch_is_recorded_and_normalized(self) -> None:
        descriptor = bytearray.fromhex("c4 c4 65 6e 00 00 00 00")

        status = _rewrite_compact_audio_attribute(
            descriptor,
            0,
            source_codec="dts",
            source_channels=6,
            target_channels=2,
            processing="downmix",
        )

        self.assertEqual(status, "authored-mismatch-normalized")
        self.assertEqual(descriptor[0] >> 5, 0)
        self.assertEqual((descriptor[1] & 0x07) + 1, 2)
        self.assertEqual(descriptor[2:4], b"en")

    def test_passthrough_preserves_an_authored_channel_mismatch(self) -> None:
        descriptor = bytearray.fromhex("04 c4 65 6e 00 00 00 00")
        original = bytes(descriptor)

        status = _rewrite_compact_audio_attribute(
            descriptor,
            0,
            source_codec="ac3",
            source_channels=6,
            target_channels=6,
            processing="passthrough",
        )

        self.assertEqual(status, "authored-mismatch-preserved")
        self.assertEqual(bytes(descriptor), original)

    def test_invalid_decoded_channel_count_still_fails(self) -> None:
        descriptor = bytearray.fromhex("c4 c4 65 6e 00 00 00 00")
        with self.assertRaisesRegex(Exception, "decoded source audio channel count"):
            _rewrite_compact_audio_attribute(
                descriptor,
                0,
                source_codec="dts",
                source_channels=9,
                target_channels=2,
                processing="downmix",
            )


if __name__ == "__main__":
    unittest.main()
