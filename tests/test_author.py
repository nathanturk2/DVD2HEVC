from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from dvd2hevc_app.author import _wsl_path, author_dvd_iso, verify_authored_iso
from dvd2hevc_app.pipeline import PipelineError
from dvd2hevc_app.udf_validation import validate_udf, UdfValidationError


class _IsoEntry:
    name = "VIDEO_TS.IFO"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def is_dot(self) -> bool:
        return False

    def is_dotdot(self) -> bool:
        return False

    def is_file(self) -> bool:
        return True

    def get_data_length(self) -> int:
        return len(self.payload)


class _IsoImage:
    def __init__(self, payload: bytes) -> None:
        self.entry = _IsoEntry(payload)

    def list_children(self, **_kwargs: object) -> list[_IsoEntry]:
        return [self.entry]

    def get_file_from_iso_fp(self, sink: object, **_kwargs: object) -> None:
        sink.write(self.entry.payload)


class AuthorReportLocationTests(unittest.TestCase):
    def test_invalid_udf_is_rejected_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            (stage / "VIDEO_TS").mkdir(parents=True)
            (stage / "VIDEO_TS/VIDEO_TS.IFO").write_bytes(b"ifo")
            (stage / "dvd2hevc-stage-report.json").write_text(json.dumps({"status": "passed"}))
            destination = root / "Movie.iso"
            def run_author(command, **kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"not a valid image")
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("dvd2hevc_app.author.shutil.which", return_value="genisoimage"), patch(
                "dvd2hevc_app.author.subprocess.run", side_effect=run_author
            ), self.assertRaisesRegex(PipelineError, "filesystem validation failed"):
                author_dvd_iso(stage, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(list(root.glob("*.part")))

    def test_independent_udf_reader_accepts_other_author(self) -> None:
        import pycdlib
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "valid.iso"
            image = pycdlib.PyCdlib()
            try:
                image.new(udf="2.60")
                image.write(str(path))
            finally:
                image.close()
            self.assertTrue(validate_udf(path)["ok"])
            data = bytearray(path.read_bytes())
            data[256 * 2048 + 4] ^= 1
            path.write_bytes(data)
            with self.assertRaises(UdfValidationError):
                validate_udf(path)

    def test_wsl_path_uses_direct_exec_and_preserves_backslashes(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, "/mnt/c/Users/test/AppData/Local/DVD2HEVC/work\n", ""
        )
        source = Path(r"C:\Users\test\AppData\Local\DVD2HEVC\work")
        with patch("dvd2hevc_app.author.subprocess.run", return_value=completed) as run:
            translated = _wsl_path(source, "Ubuntu-24.04")

        command = run.call_args.args[0]
        self.assertEqual(command[0:6], [
            "wsl.exe", "-d", "Ubuntu-24.04", "-e", "wslpath", "-a",
        ])
        self.assertIn("\\", command[6])
        self.assertEqual(translated, "/mnt/c/Users/test/AppData/Local/DVD2HEVC/work")

    def test_authoring_accepts_workspace_report_and_log_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            (stage / "VIDEO_TS").mkdir(parents=True)
            (stage / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"ifo")
            (stage / "dvd2hevc-stage-report.json").write_text(
                json.dumps({"status": "passed"}), encoding="utf-8"
            )
            destination = root / "library" / "Movie.iso"
            report = root / "job" / "final-output-reports" / "author.json"
            log = root / "job" / "final-output-reports" / "author.log"

            def run_author(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(command[command.index("-o") + 1]).write_bytes(b"iso")
                return subprocess.CompletedProcess(command, 0, "authored", "")

            with patch("dvd2hevc_app.author.validate_udf", return_value={"ok": True}), patch("dvd2hevc_app.author.shutil.which", return_value="genisoimage"), patch(
                "dvd2hevc_app.author.subprocess.run", side_effect=run_author
            ):
                result = author_dvd_iso(
                    stage, destination, report_path=report, log_path=log
                )

            self.assertTrue(destination.is_file())
            self.assertTrue(report.is_file())
            self.assertTrue(log.is_file())
            self.assertEqual(result["log"], str(log.resolve()))
            self.assertFalse(Path(f"{destination}.json").exists())
            self.assertFalse(Path(f"{destination}.author.log").exists())

    def test_verification_accepts_workspace_report_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"ifo"
            stage = root / "stage"
            video_ts = stage / "VIDEO_TS"
            video_ts.mkdir(parents=True)
            (video_ts / "VIDEO_TS.IFO").write_bytes(payload)
            stage_report = stage / "dvd2hevc-stage-report.json"
            stage_report.write_text(
                json.dumps({"schema": "dvd2hevc-compact-stage-v0"}), encoding="utf-8"
            )
            destination = root / "library" / "Movie.iso"
            destination.parent.mkdir()
            destination.write_bytes(b"iso")
            author_report = root / "job" / "author.json"
            author_report.parent.mkdir()
            author_report.write_text(
                json.dumps({
                    "schema": "dvd2hevc-authored-iso-v0",
                    "destination": str(destination),
                    "stage": str(stage),
                    "stage_report": str(stage_report),
                }),
                encoding="utf-8",
            )
            verification = root / "job" / "final-output-reports" / "verification.json"
            graph = {"domains": [], "digest": hashlib.sha256(payload).hexdigest()}

            with patch("dvd2hevc_app.author.validate_udf", return_value={"ok": True}), patch("dvd2hevc_app.author.open_iso_image", return_value=_IsoImage(payload)), patch(
                "dvd2hevc_app.author.close_iso_image"
            ), patch("dvd2hevc_app.author._entry_name", side_effect=lambda entry: entry.name), patch(
                "dvd2hevc_app.author.find_dvdinspect", return_value=Path("dvdinspect")
            ), patch("dvd2hevc_app.author.inspect_physical_graph", return_value=graph):
                result = verify_authored_iso(author_report, report_path=verification)

            self.assertTrue(result["passed"])
            self.assertTrue(verification.is_file())
            self.assertFalse(Path(f"{destination}.verification.json").exists())


if __name__ == "__main__":
    unittest.main()
