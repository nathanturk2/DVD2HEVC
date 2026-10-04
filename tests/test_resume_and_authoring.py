from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from dvd2hevc_app import author, pipeline
from dvd2hevc_app.encoders import parse_rate_control


class TitleResumeTests(unittest.TestCase):
    def test_attempt_reuse_requires_same_source_timing_settings_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as mocks:
            root = Path(temporary)
            source = root / "source.iso"
            source.write_bytes(b"original disc")
            workspace = root / "work"
            cell = {"vob_id": 1, "cell_id": 1, "first_sector": 0, "last_sector": 1,
                    "duration_ticks": 90_000}
            vobus = [{"sector": 0, "start_ptm": 0}, {"sector": 1, "start_ptm": 45_000}]
            graph = {
                "global_titles": [{"title": 1, "vts": 1, "vts_title_number": 1}],
                "title_sets": [{"vts": 1, "chapters": [{"vts_title_number": 1, "parts": [{"pgc": 1}]}],
                                "title_pgcs": [{"cells": [cell]}], "title_vobu_map": {"vobus": vobus}}],
            }

            def extract(_source: Path, *, ranges: list, **_kwargs: object) -> list:
                rows = []
                for _first, _last, destination in ranges:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(bytes(4096))
                    rows.append({"destination": str(destination)})
                return rows

            def encode(_source: Path, destination: Path, **kwargs: object) -> dict:
                destination.write_bytes(b"validated video")
                return {"quality": kwargs["quality"], "rate_control": parse_rate_control(kwargs["quality"]),
                        "encoder": kwargs["encoder"], "preset": kwargs["preset"],
                        "cadence_mode": kwargs["cadence_mode"], "output_bytes": destination.stat().st_size}

            mocks.enter_context(patch.object(pipeline, "discover_tools", return_value={"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}))
            mocks.enter_context(patch.object(pipeline, "find_dvdinspect", return_value=Path("dvdinspect")))
            mocks.enter_context(patch.object(pipeline, "inspect_physical_graph", return_value=graph))
            mocks.enter_context(patch.object(pipeline, "extract_domain_sector_ranges", side_effect=extract))
            encoder = mocks.enter_context(patch.object(pipeline, "encode_cell_hevc", side_effect=encode))
            mocks.enter_context(patch.object(pipeline, "read_video_pes", return_value=[]))
            mocks.enter_context(patch.object(pipeline, "analyze_vobu_budgets", return_value={
                "vobu_count": 2, "unassigned_pes_count": 0, "vobus": [],
            }))
            mocks.enter_context(patch.object(pipeline, "validate_vobu_random_access", return_value={"failures": 0}))
            mocks.enter_context(patch.object(pipeline, "validate_compact_encoded_cell", return_value={
                "source_video": {"nb_read_frames": 25}, "output_video": {"nb_read_frames": 25},
                "source_audio_hash": None, "output_audio_hash": None,
            }))
            mocks.enter_context(patch.object(pipeline, "progress_event"))

            def convert(*, threads: int = 4) -> None:
                pipeline.convert_title(
                    source, title_number=1, workspace=workspace, quality_values=(24,),
                    cadence="progressive", prefer_compact_input=True, threads=threads,
                    progress=lambda _message: None,
                )

            def discard_cell_report() -> None:
                next(workspace.glob("cells/*/cell-report.json")).unlink()

            convert()
            self.assertEqual(encoder.call_count, 1)
            discard_cell_report()  # Interrupted after a validated encode: resume it.
            convert()
            self.assertEqual(encoder.call_count, 1)
            source.write_bytes(b"different disc with the same sector layout")
            convert()
            self.assertEqual(encoder.call_count, 2, "An old attempt must not survive a source change")
            vobus[1]["start_ptm"] = 36_000
            convert()
            self.assertEqual(encoder.call_count, 3, "Equal VOBU counts do not prove equal timestamps")
            convert(threads=2)
            self.assertEqual(encoder.call_count, 4, "Encode settings must participate in attempt identity")
            discard_cell_report()
            next(workspace.glob("cells/*/encoded-*.ts")).write_bytes(b"partial")
            convert(threads=2)
            self.assertEqual(encoder.call_count, 5, "A changed intermediate must be reencoded")
            next(workspace.glob("cells/*/encoded-*.ts")).write_bytes(b"changed after validation")
            convert(threads=2)
            self.assertEqual(encoder.call_count, 6, "A passed cell report must also check the intermediate")


class AuthorFailureTests(unittest.TestCase):
    def make_stage(self, root: Path) -> Path:
        stage = root / "stage"
        (stage / "VIDEO_TS").mkdir(parents=True)
        (stage / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"ifo")
        (stage / "dvd2hevc-stage-report.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        return stage

    def test_log_write_failure_removes_partial_iso(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = self.make_stage(root)
            destination = root / "movie.iso"
            log = root / "author.log"
            log.mkdir()  # Force a log write failure after the author has written its image.

            def create(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                Path(command[command.index("-o") + 1]).write_bytes(b"partial image")
                return subprocess.CompletedProcess(command, 0, "authored", "")

            with patch.object(author.shutil, "which", return_value="genisoimage"), patch.object(
                author.subprocess, "run", side_effect=create
            ):
                with self.assertRaises(OSError):
                    author.author_dvd_iso(stage, destination, log_path=log)
            self.assertEqual(list(root.glob(".*.part")), [])
            self.assertFalse(destination.exists())

    def test_author_rejects_output_inside_staged_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = self.make_stage(root)
            with patch.object(author.shutil, "which", return_value="genisoimage"), patch.object(
                author.subprocess, "run", side_effect=AssertionError("Author must not run")
            ):
                with self.assertRaisesRegex(pipeline.PipelineError, "inside"):
                    author.author_dvd_iso(stage, stage / "movie.iso")

    def test_author_rejects_empty_image_and_preserves_newly_appearing_destination(self) -> None:
        for scenario in ("empty", "destination-created"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = self.make_stage(root)
                destination = root / "movie.iso"

                def create(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                    partial = Path(command[command.index("-o") + 1])
                    partial.write_bytes(b"" if scenario == "empty" else b"new image")
                    if scenario == "destination-created":
                        destination.write_bytes(b"other conversion")
                    return subprocess.CompletedProcess(command, 0, "authored", "")

                with patch.object(author.shutil, "which", return_value="genisoimage"), patch.object(
                    author.subprocess, "run", side_effect=create
                ):
                    with self.assertRaises(pipeline.PipelineError):
                        author.author_dvd_iso(stage, destination)
                self.assertEqual(list(root.glob(".*.part")), [])
                if scenario == "destination-created":
                    self.assertEqual(destination.read_bytes(), b"other conversion")
                else:
                    self.assertFalse(destination.exists())

    def test_subprocess_exception_removes_partial_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = self.make_stage(root)
            destination = root / "movie.iso"

            def fail(command: list[str], **_kwargs: object) -> None:
                Path(command[command.index("-o") + 1]).write_bytes(b"partial image")
                raise OSError("author interrupted")

            with patch.object(author.shutil, "which", return_value="genisoimage"), patch.object(
                author.subprocess, "run", side_effect=fail
            ):
                with self.assertRaisesRegex(OSError, "author interrupted"):
                    author.author_dvd_iso(stage, destination)
            self.assertEqual(list(root.glob(".*.part")), [])
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
