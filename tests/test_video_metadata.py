"""Actual encode regression for DVD colour and anamorphic aspect signalling."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from dvd2hevc_app.pipeline import encode_cell_hevc
from dvd2hevc_app.tools import discover_tools


class VideoMetadataTests(unittest.TestCase):
    def test_pal_widescreen_and_colour_survive_encoding(self):
        tools = discover_tools()
        if not tools.get("ffmpeg") or not tools.get("ffprobe"):
            self.skipTest("FFmpeg/FFprobe are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source.mpg", root / "encoded.ts"
            subprocess.run([
                tools["ffmpeg"], "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=720x576:r=25",
                "-t", "0.24", "-vf", "setsar=64/45,setparams=color_primaries=bt470bg:color_trc=bt470bg:colorspace=bt470bg:range=tv",
                "-c:v", "mpeg2video", "-f", "mpeg", str(source),
            ], check=True, capture_output=True)
            encode_cell_hevc(source, output, ffmpeg=tools["ffmpeg"], quality=24, preset="p6", threads=1,
                             log=root / "encode.log", encoder="libx265", cadence_mode="progressive")
            def probe(path):
                result = subprocess.run([tools["ffprobe"], "-v", "error", "-select_streams", "v:0",
                                         "-show_streams", "-of", "json", str(path)], check=True, capture_output=True, text=True)
                return json.loads(result.stdout)["streams"][0]
            original, encoded = probe(source), probe(output)
            self.assertEqual(encoded["codec_name"], "hevc")
            self.assertEqual(encoded["display_aspect_ratio"], "16:9")
            for field in ("width", "height", "sample_aspect_ratio", "display_aspect_ratio",
                          "color_primaries", "color_transfer", "color_space", "color_range"):
                self.assertEqual(encoded[field], original[field], field)


if __name__ == "__main__":
    unittest.main()
