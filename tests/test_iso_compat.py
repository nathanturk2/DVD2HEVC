from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dvd2hevc_app.iso import _IsoPaddingOverlay


class IsoCompatibilityTests(unittest.TestCase):
    def test_short_directory_read_is_virtually_sector_padded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "short.iso"
            source.write_bytes(b"abc")
            view = _IsoPaddingOverlay(source, [], {(0, 3): 2048})
            try:
                data = view.read(3)
                self.assertEqual(len(data), 2048)
                self.assertEqual(data[:3], b"abc")
                self.assertEqual(data[3:], bytes(2045))
                # Virtual padding must not change the underlying file offset.
                self.assertEqual(view.tell(), 3)
            finally:
                view.close()

    def test_nonzero_directory_padding_is_hidden_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "padding.iso"
            source.write_bytes(b"dataJUNKtail")
            view = _IsoPaddingOverlay(source, [(4, 8)], {})
            try:
                self.assertEqual(view.read(), b"data\x00\x00\x00\x00tail")
            finally:
                view.close()
            self.assertEqual(source.read_bytes(), b"dataJUNKtail")


if __name__ == "__main__":
    unittest.main()
