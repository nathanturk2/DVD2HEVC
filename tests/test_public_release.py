from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dvd2hevc_app import diagnostics, quality, paths
from dvd2hevc_app.pipeline import PipelineError


class PublicReleaseTests(unittest.TestCase):
    def test_live_legacy_worker_or_watcher_defers_state_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "reports"; legacy.mkdir()
            destination = root / "new-state"
            for key, status, filename in [("runner_pid", "running", "job.json"), ("watcher_pid", "active", "watch.json"), ("pid", None, "dispatcher.lock")]:
                with self.subTest(key=key):
                    record = legacy / filename
                    record.write_text(json.dumps({key: 123, "status": status}))
                    with patch.dict(paths.os.environ, {}, clear=True), patch.object(paths, "PROJECT_ROOT", root), patch.object(paths, "ROOT", root), patch.object(paths, "state_root", return_value=destination), patch.object(paths.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '"python.exe","123"')), patch.object(paths.os, "kill"):
                        self.assertEqual(paths.reports_root(), legacy)
                    record.unlink()
                    self.assertFalse(destination.exists())

    def test_diagnostic_output_cannot_replace_source_or_converted_iso(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "source.iso", root / "converted.iso"
            source.write_bytes(b"original"); destination.write_bytes(b"converted")
            job = {"id":"demo", "source":str(source), "output":str(destination), "work_root":str(root / "work")}
            for output in [source, destination]:
                with patch.object(diagnostics, "resolve_job", return_value=(root / "job.json", job)):
                    with self.assertRaises(PipelineError): diagnostics.create_diagnostic_bundle("demo", output=output, force=True)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(destination.read_bytes(), b"converted")

    def test_json_escaped_windows_path_is_redacted(self):
        source = Path(r"C:\PrivateCollection\Movie.iso")
        serialized = json.dumps({"source":str(source)})
        self.assertNotIn("PrivateCollection", diagnostics.redact_text(serialized, diagnostics.redaction_map([source])))

    def test_physical_duration_deduplicates_vobus_and_handles_ptm_wrap(self):
        plan = {"summary":{"title_vobus":2}}
        row = {"sector":1, "start_ptm":2**32 - 45000, "end_ptm":45000}
        scan = {"video_ts":{"vobs":[{"domain":"title", "stats":{"video_payload_bytes":1_000_000}}]},
                "physical_graph":{"title_sets":[{"vts":1,"title_vobu_map":{"vobus":[row,row,{"sector":2,"start_ptm":90000,"end_ptm":180000}]}}]}}
        result = quality.estimate_disc_source_bitrate(plan, scan)
        self.assertTrue(result["duration_measured"])
        self.assertEqual(result["duration_seconds"], 2)
        self.assertEqual(result["average_video_bps"], 4_000_000)

    def test_incomplete_native_duration_fails_confidently(self):
        plan = {"summary":{"title_vobus":1}}
        scan = {"video_ts":{"vobs":[{"domain":"title","stats":{"video_payload_bytes":1000}}]},
                "physical_graph":{"title_sets":[{"vts":1,"title_vobu_map":{"vobus":[{"sector":1,"error":"unreadable PCI"}]}}]}}
        with self.assertRaises(PipelineError): quality.estimate_disc_source_bitrate(plan, scan)

    def test_measurement_drains_large_stderr(self):
        real_popen = subprocess.Popen
        def launch(_command, **kwargs):
            return real_popen([sys.executable,"-c","import sys; sys.stderr.write('x'*524288); sys.stderr.flush(); sys.stdout.buffer.write(b'video')"], **kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/"source.vob"; source.touch()
            with patch.object(quality,"_probe_video",return_value={"duration":1}), patch.object(quality.subprocess,"Popen",side_effect=launch):
                result=quality.measure_mpeg2_video(source,"ffmpeg","ffprobe",timeout=5)
            self.assertEqual(result["elementary_video_bytes"],5)

    def test_measurement_timeout_reaps_child(self):
        real_popen=subprocess.Popen
        children=[]
        def launch(_command,**kwargs):
            child=real_popen([sys.executable,"-c","import time; time.sleep(20)"],**kwargs);children.append(child);return child
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/"source.vob";source.touch()
            with patch.object(quality,"_probe_video",return_value={"duration":1}),patch.object(quality.subprocess,"Popen",side_effect=launch):
                with self.assertRaisesRegex(PipelineError,"timed out"): quality.measure_mpeg2_video(source,"ffmpeg","ffprobe",timeout=0.1)
            self.assertIsNotNone(children[0].poll())
