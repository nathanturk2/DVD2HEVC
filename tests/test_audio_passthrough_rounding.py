import unittest

from dvd2hevc_app.audio import _audio_timestamp_alignment


class AudioPassthroughRoundingTests(unittest.TestCase):
    def test_accepts_one_tick_at_one_mux_endpoint_only(self) -> None:
        source = {"first_pts": 46_073_520, "last_pts": 46_082_160, "packets": 5}
        rounded = {**source, "last_pts": source["last_pts"] + 1}
        self.assertEqual(
            _audio_timestamp_alignment(source, rounded, reencoded=False),
            "passthrough-one-tick-mux-rounding",
        )

    def test_rejects_packet_loss_or_more_than_one_tick(self) -> None:
        source = {"first_pts": 46_073_520, "last_pts": 46_082_160, "packets": 5}
        self.assertIsNone(_audio_timestamp_alignment(
            source, {**source, "last_pts": source["last_pts"] + 2},
            reencoded=False,
        ))
        self.assertIsNone(_audio_timestamp_alignment(
            source,
            {**source, "last_pts": source["last_pts"] + 1, "packets": 4},
            reencoded=False,
        ))


if __name__ == "__main__":
    unittest.main()
