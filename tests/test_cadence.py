from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dvd2hevc_app.cadence import analyze_cadence, classify_idet_totals, parse_idet_output


class CadenceTests(unittest.TestCase):
    def test_parse_and_classify_progressive_idet_output(self) -> None:
        text = """
        Repeated Fields: Neither: 1496 Top: 4 Bottom: 1
        Multi frame detection: TFF: 10 BFF: 0 Progressive: 1435 Undetermined: 56
        """
        totals = parse_idet_output(text)
        result = classify_idet_totals(totals)
        self.assertEqual(result["classification"], "progressive")
        self.assertGreater(result["progressive_ratio"], 0.99)

    def test_classification_exposes_mixed_and_repeated_field_evidence(self) -> None:
        outputs = [
            "Repeated Fields: Neither: 90 Top: 5 Bottom: 5\n"
            "Multi frame detection: TFF: 0 BFF: 0 Progressive: 100 Undetermined: 0",
            "Repeated Fields: Neither: 100 Top: 0 Bottom: 0\n"
            "Multi frame detection: TFF: 60 BFF: 40 Progressive: 0 Undetermined: 0",
            "Repeated Fields: Neither: 100 Top: 0 Bottom: 0\n"
            "Multi frame detection: TFF: 0 BFF: 0 Progressive: 0 Undetermined: 100",
        ]
        completed = [
            SimpleNamespace(returncode=0, stderr=output)
            for output in outputs
        ]
        with patch("dvd2hevc_app.cadence.subprocess.run", side_effect=completed):
            result = analyze_cadence(
                Path("source.vob"), ffmpeg="ffmpeg", duration_seconds=120,
                sample_frames=100,
            )
        self.assertEqual(
            result["sample_classifications"],
            ["progressive", "interlaced", "ambiguous"],
        )
        self.assertEqual(result["detected_pattern"], "mixed")
        self.assertTrue(result["mixed_sample_evidence"])
        self.assertEqual(result["repeated_fields_detected"], 10)
        self.assertAlmostEqual(result["repeated_field_ratio"], 1 / 30, places=6)
        self.assertEqual(result["suggested_cadence"], "deinterlace50")

    def test_true_interlace_is_not_normalized_as_progressive(self) -> None:
        totals = {"tff": 800, "bff": 0, "progressive": 200, "undetermined": 0}
        self.assertEqual(classify_idet_totals(totals)["classification"], "interlaced")


if __name__ == "__main__":
    unittest.main()
