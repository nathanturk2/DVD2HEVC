import tempfile
import unittest
from pathlib import Path
from dvd2hevc_app.runtime_support import read_progress_log


class ProgressHistoryTests(unittest.TestCase):
    def test_finished_task_survives_large_raw_output_and_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.log"
            marker = 'DVD2HEVC_PROGRESS {"lane":"video","task":"one","event":"done"}\n'
            path.write_text(marker + "tool output\n" * 300000, encoding="utf-16")
            text = read_progress_log(path, "DVD2HEVC_PROGRESS ")
            self.assertIn('"event":"done"', text)
            self.assertLess(len(text), 2 * 1024 * 1024 + 100)
            path.write_text("new run\n",encoding="utf-16")
            self.assertNotIn('"event":"done"', read_progress_log(path,"DVD2HEVC_PROGRESS "))
