from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from dvd2hevc_app.cli import build_parser
from dvd2hevc_app.config import (
    all_presets,
    remove_named_preset,
    resolve_preset,
    save_named_preset,
)
from dvd2hevc_app.frontend import (
    WorkSlotBusy,
    WorkspaceSpaceLow,
    acquire_active_work_lock,
    canonical_quality,
    cmd_cancel,
    cmd_cancel_all,
    cmd_pause_queue,
    cmd_resume_queue,
    cmd_resume_job,
    collect_iso_sources,
    create_watched_batch,
    default_output_for,
    ensure_workspace_capacity,
    _existing_output_is_authored_job_artifact,
    find_authoring_backend,
    lane_progress_snapshot,
    normalize_volume_label,
    pipeline_percent,
    process_alive,
    progress_bar,
    prune_completed_work,
    handle_output_storage_failure,
    queue_pause_reason,
    queue_prepared_job,
    recover_jobs_interrupted_by_restart,
    refresh_job,
    release_active_work_lock,
    resolve_conversion_settings,
    reset_watched_batch,
    resume_watched_batch,
    run_job,
    runner_command,
    scan_watched_batch,
    stage_percent,
    task_lane_lines,
    stop_watched_batch,
    watched_batch_summary,
    write_json_atomic,
)
from dvd2hevc_app.pipeline import PipelineError
from dvd2hevc_app.progress import progress_event


class Phase8FrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._queue_control_root = tempfile.TemporaryDirectory()
        self._queue_control_patch = patch(
            "dvd2hevc_app.frontend.QUEUE_CONTROL",
            Path(self._queue_control_root.name) / "queue-control.json",
        )
        self._queue_control_patch.start()

    def tearDown(self) -> None:
        self._queue_control_patch.stop()
        self._queue_control_root.cleanup()

    def test_handbrake_quality_and_friendly_aliases_are_canonical(self) -> None:
        self.assertEqual(canonical_quality("target-bitrate"), "target-bitrate")
        self.assertEqual(canonical_quality("auto-cq"), "target-bitrate")
        self.assertEqual(canonical_quality("cq:24"), "cq:24")
        self.assertEqual(canonical_quality("24.5"), "cq:24.5")
        self.assertEqual(canonical_quality("compact"), "cq:27")
        self.assertEqual(canonical_quality("high-quality"), "cq:20")
        with self.assertRaises(PipelineError):
            canonical_quality("cq:52")

    def test_workspace_capacity_fails_before_encoding_with_a_clear_wait_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "disc.iso"
            source.write_bytes(b"dvd")
            usage = SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=1024**3)
            with patch("dvd2hevc_app.frontend.shutil.disk_usage", return_value=usage):
                with self.assertRaisesRegex(WorkspaceSpaceLow, "low on space"):
                    ensure_workspace_capacity(source, root / "job" / "work")

    def test_atomic_json_retries_transient_windows_replace_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "watch.json"
            destination.write_text('{"old":true}', encoding="utf-8")
            original_replace = os.replace
            attempts = 0

            def transient_replace(source: object, target: object) -> None:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise PermissionError(13, "Access is denied", str(target))
                original_replace(source, target)

            with (
                patch("dvd2hevc_app.frontend.os.replace", side_effect=transient_replace),
                patch("dvd2hevc_app.frontend.time.sleep") as sleep,
            ):
                write_json_atomic(destination, {"status": "active"})
            self.assertEqual(json.loads(destination.read_text()), {"status": "active"})
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_passed_managed_job_prunes_only_reproducible_binary_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_root = Path(temporary) / "job"
            work = job_root / "work"
            work.mkdir(parents=True)
            output = Path(temporary) / "disc.iso"
            output.write_bytes(b"verified")
            for name in ("source.vob", "encoded.ts", "audio.ac3", "stream.dvd-pes"):
                (work / name).write_bytes(b"large-artifact")
            (work / "status.json").write_text('{"state":"passed"}', encoding="utf-8")
            (work / "run.log").write_text("evidence", encoding="utf-8")
            job_path = job_root / "job.json"
            job = {
                "status": "passed",
                "output": str(output),
                "work_root": str(work),
                "managed_work_root": True,
            }

            report = prune_completed_work(job_path, job)

            self.assertTrue(report["performed"])
            self.assertEqual(report["files_removed"], 4)
            self.assertTrue((work / "status.json").is_file())
            self.assertTrue((work / "run.log").is_file())
            self.assertFalse((work / "source.vob").exists())

    def test_workspace_waiting_counts_as_pending_not_attention(self) -> None:
        watch = {"ledger": {"disc": {"status": "waiting-for-space"}}}
        summary = watched_batch_summary(watch)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["attention"], 0)

    def test_balanced_defaults_match_phase8_user_workflow(self) -> None:
        settings = resolve_conversion_settings(
            argparse.Namespace(
                preset=None,
                quality=None,
                encoder_preset=None,
                deinterlace=None,
            )
        )
        self.assertEqual(settings["quality"], "target-bitrate")
        self.assertEqual(settings["target_bitrate_multiplier"], 1.0)
        self.assertEqual(settings["bitrate_mode"], "vbr")
        self.assertEqual(settings["encoder"], "hevc_nvenc")
        self.assertEqual(settings["encoder_preset"], "p6")
        self.assertEqual(settings["cadence"], "auto")
        self.assertEqual(settings["ambiguous_cadence"], "deinterlace50")
        self.assertEqual(settings["audio_mode"], "passthrough")
        self.assertEqual(settings["stereo_audio_bitrate"], 256000)

    def test_compact_stereo_preset_exposes_bounded_parallel_audio(self) -> None:
        settings = resolve_conversion_settings(
            argparse.Namespace(
                preset="compact-stereo", quality=None, encoder_preset=None,
                deinterlace=None, audio_mode=None, stereo_audio_bitrate=None,
                mono_audio_bitrate=None, audio_workers=None, pipeline_depth=None,
            )
        )
        self.assertEqual(settings["audio_mode"], "compact-stereo")
        self.assertEqual(settings["stereo_audio_bitrate"], 256000)
        self.assertEqual(settings["mono_audio_bitrate"], 128000)
        self.assertEqual(settings["audio_workers"], 2)
        self.assertEqual(settings["pipeline_depth"], 2)

    def test_output_and_volume_label_are_safe(self) -> None:
        source = Path("Movie Name.iso")
        self.assertEqual(default_output_for(source), Path("Movie Name (DVD) (UHD-BD).iso"))
        self.assertEqual(
            default_output_for(Path("Movie Name (DVD) (HEVC).iso")),
            Path("Movie Name (DVD) (UHD-BD).iso"),
        )
        self.assertEqual(
            default_output_for(source, add_filename_tags=False),
            Path("Movie Name - converted.iso"),
        )
        self.assertEqual(
            default_output_for(Path("Movie Name - converted.iso"), add_filename_tags=False),
            Path("Movie Name - converted.iso"),
        )
        self.assertEqual(normalize_volume_label("Movie: Name (2024)"), "MOVIE__NAME__2024")
        self.assertLessEqual(len(normalize_volume_label("x" * 100)), 32)

    def test_queue_source_discovery_is_deduplicated_and_optionally_recursive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "A.iso"
            second = root / "nested" / "B.ISO"
            first.touch()
            second.parent.mkdir()
            second.touch()
            shallow = collect_iso_sources([str(root), str(first)], recursive=False)
            self.assertEqual(shallow, [first.resolve()])
            recursive = collect_iso_sources([str(root)], recursive=True)
            self.assertEqual(recursive, [first.resolve(), second.resolve()])

    def test_named_presets_are_saved_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DVD2HEVC_CONFIG_DIR": temporary}):
                save_named_preset(
                    "my-dvd",
                    quality="cq:23.5",
                    encoder="hevc_qsv",
                    encoder_preset="p7",
                    deinterlace="always",
                    audio_mode="compact-stereo",
                    stereo_audio_bitrate="224k",
                    mono_audio_bitrate="96k",
                    audio_workers=3,
                    pipeline_depth=2,
                )
                value = resolve_preset("my-dvd")
                self.assertEqual(value["quality"], "cq:23.5")
                self.assertEqual(value["encoder"], "hevc_qsv")
                self.assertEqual(value["audio_mode"], "compact-stereo")
                self.assertEqual(value["stereo_audio_bitrate"], "224k")
                self.assertFalse(value["builtin"])
                self.assertIn("balanced", all_presets())
                remove_named_preset("my-dvd")
                with self.assertRaises(PipelineError):
                    resolve_preset("my-dvd")

    def test_runner_uses_one_exact_quality_for_encode_and_compaction(self) -> None:
        command = runner_command(
            {
                "plan": "plan.json",
                "work_root": "work",
                "output": "out.iso",
                "label": "MOVIE_HEVC",
                "vlc_root": "vlc",
                "settings": {
                    "quality": "cq:24",
                    "encoder": "hevc_qsv",
                    "encoder_preset": "p6",
                    "cadence": "auto",
                    "ambiguous_cadence": "deinterlace50",
                },
            }
        )
        self.assertEqual(command[command.index("-QualityValues") + 1], "cq:24")
        self.assertEqual(command[command.index("-QualityMode") + 1], "cq:24")
        self.assertEqual(command[command.index("-CompactQuality") + 1], "cq:24")
        self.assertIn("-Cadence", command)
        self.assertEqual(command[command.index("-Encoder") + 1], "hevc_qsv")
        self.assertIn("-AmbiguousCadence", command)
        self.assertEqual(command[command.index("-AudioMode") + 1], "passthrough")
        self.assertIn("-PipelineDepth", command)

    def test_runner_passes_target_bitrate_controls_without_cq_translation(self) -> None:
        command = runner_command({
            "plan": "plan.json", "work_root": "work", "output": "out.iso",
            "label": "MOVIE_HEVC", "vlc_root": "vlc",
            "settings": {
                "quality": "target-bitrate", "target_bitrate_multiplier": 1.25,
                "bitrate_mode": "cbr", "encoder": "hevc_nvenc",
                "encoder_preset": "p6", "cadence": "auto",
                "ambiguous_cadence": "deinterlace50",
            },
        })
        self.assertEqual(command[command.index("-QualityMode") + 1], "target-bitrate")
        self.assertEqual(command[command.index("-TargetBitrateMultiplier") + 1], "1.25")
        self.assertEqual(command[command.index("-BitrateMode") + 1], "cbr")
        self.assertNotIn("-AutoCqMultiplier", command)

    def test_stage_progress_never_claims_completion_early(self) -> None:
        self.assertEqual(stage_percent("complete", "passed"), 100.0)
        self.assertEqual(stage_percent("author-final-iso", "running"), 92.7)
        self.assertEqual(stage_percent("plan-compact-audio-vts02", "running"), 85.9)
        self.assertLess(stage_percent("vlc-menu-hardware", "running"), 100.0)
        self.assertEqual(stage_percent("stage-all-compact-base", "failed"), 84.8)
        self.assertEqual(stage_percent("anything", "failed"), 0.0)
        self.assertEqual(progress_bar(100.0, 8), "[########]")
        self.assertEqual(progress_bar(99.9, 8), "[#######-]")

    def test_public_parser_exposes_bd2hevc_style_commands(self) -> None:
        parser = build_parser()
        auto = parser.parse_args(["auto", "movie.iso", "--quality", "cq:24", "--dry-run"])
        self.assertEqual(auto.command, "auto")
        self.assertEqual(auto.quality, "cq:24")
        qsv = parser.parse_args(["auto", "movie.iso", "--encoder", "hevc_qsv", "--dry-run"])
        self.assertEqual(qsv.encoder, "hevc_qsv")
        target = parser.parse_args([
            "auto", "movie.iso", "--quality", "target-bitrate",
            "--target-bitrate-multiplier", "1.3", "--bitrate-mode", "cbr", "--dry-run",
        ])
        self.assertEqual(target.target_bitrate_multiplier, 1.3)
        self.assertEqual(target.bitrate_mode, "cbr")
        status = parser.parse_args(["status", "--watch", "10"])
        self.assertEqual(status.watch, 10.0)
        queue = parser.parse_args(["queue", "backups", "--output-dir", "converted"])
        self.assertEqual(queue.command, "queue")
        self.assertTrue(queue.add_filename_tags)
        untagged = parser.parse_args(["auto", "movie.iso", "--no-filename-tags", "--dry-run"])
        self.assertFalse(untagged.add_filename_tags)
        untagged_alias = parser.parse_args([
            "queue", "backups", "--output-dir", "converted", "--no-output-tags",
        ])
        self.assertFalse(untagged_alias.add_filename_tags)
        watch = parser.parse_args([
            "watch-folder", "incoming", "--output-dir", "converted",
            "--recursive", "--settle-seconds", "45",
        ])
        self.assertEqual(watch.command, "watch-folder")
        self.assertTrue(watch.recursive)
        self.assertEqual(watch.settle_seconds, 45.0)
        self.assertEqual(parser.parse_args(["watches"]).command, "watches")
        self.assertEqual(parser.parse_args(["stop-watch", "my-watch"]).watch, "my-watch")
        self.assertEqual(parser.parse_args(["reset-watch", "my-watch"]).watch, "my-watch")
        self.assertEqual(parser.parse_args(["resume-watch", "my-watch"]).watch, "my-watch")
        cancel = parser.parse_args(["cancel", "movie-job"])
        self.assertEqual(cancel.command, "cancel")
        resume = parser.parse_args(["resume", "movie-job"])
        self.assertEqual(resume.command, "resume")
        compact = parser.parse_args([
            "auto", "movie.iso", "--audio-mode", "compact-stereo",
            "--stereo-audio-bitrate", "256k", "--dry-run",
        ])
        self.assertEqual(compact.audio_mode, "compact-stereo")
        self.assertEqual(parser.parse_args(["pause-queue"]).command, "pause-queue")
        self.assertEqual(parser.parse_args(["cancel-all"]).command, "cancel-all")

    def test_progress_renderer_has_independent_video_audio_and_mux_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "run.log"
            log.write_text(
                "\n".join([
                    'DVD2HEVC_PROGRESS {"timestamp":1,"lane":"video","event":"done","task":"encode"}',
                    'DVD2HEVC_PROGRESS {"timestamp":2,"lane":"audio","event":"batch-progress","task":"vts02","current":2,"total":4}',
                    'DVD2HEVC_PROGRESS {"timestamp":3,"lane":"mux","event":"start","task":"stage"}',
                ]),
                encoding="utf-8",
            )
            lines = task_lane_lines({"work_root": str(root), "log": str(log)})
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("Video"))
            self.assertIn("2/4", lines[1])
            self.assertIn("stage", lines[2])
            with log.open("a", encoding="utf-8") as handle:
                handle.write('\nDVD2HEVC_PROGRESS {"timestamp":3.5,"lane":"audio","event":"start","task":"plan-audio-layout"}')
            refreshed = task_lane_lines({"work_root": str(root), "log": str(log)})
            self.assertIn("start: plan-audio-layout", refreshed[1])
            self.assertNotIn("2/4", refreshed[1])
            job = {"work_root": str(root), "log": str(log), "status": "running"}
            with log.open("a", encoding="utf-8") as handle:
                handle.write('\nDVD2HEVC_PROGRESS {"timestamp":4,"lane":"mux","event":"progress","task":"compact-domains","current":2,"total":4}')
            self.assertEqual(pipeline_percent(job, {"stage": "compact-vts03"}), 89.5)

    def test_pipeline_progress_interpolates_the_active_title_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "ordinary-and-menus"
            nested.mkdir()
            (nested / "status.json").write_text(json.dumps({
                "state": "running", "step": "convert-title-4",
                "completed_video_tasks": 3, "total_video_tasks": 10,
            }), encoding="utf-8")
            log = root / "run.log"
            log.write_text(
                'DVD2HEVC_PROGRESS {"timestamp":1,"lane":"video","event":"progress",'
                '"task":"title-4","scope":"video-task-duration","current":45000,'
                '"total":90000,"unit":"ticks","phase":"encoding"}',
                encoding="utf-8",
            )
            job = {"work_root": str(root), "log": str(log), "status": "running"}
            self.assertEqual(
                pipeline_percent(job, {"stage": "ordinary-and-menu-domains"}),
                28.5,
            )

    def test_fresh_queue_starts_at_zero_but_resumed_work_keeps_its_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "run.log"
            log.write_text("", encoding="utf-8")
            job = {
                "work_root": str(root), "log": str(log), "status": "queued",
                "settings": {"audio_mode": "compact-stereo"},
            }
            self.assertEqual(pipeline_percent(job, {"stage": "waiting for queue"}), 0.0)
            log.write_text(
                'DVD2HEVC_PROGRESS {"timestamp":1,"lane":"video","event":"done",'
                '"task":"ordinary-and-menu-domains"}',
                encoding="utf-8",
            )
            self.assertEqual(pipeline_percent(job, {"stage": "waiting for queue"}), 80.5)

    def test_overlapping_audio_does_not_advance_overall_wall_clock_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "run.log"
            log.write_text(
                'DVD2HEVC_PROGRESS {"timestamp":1,"lane":"audio","event":"progress",'
                '"task":"compact-stereo","scope":"audio-batch-cells",'
                '"current":75,"total":100}',
                encoding="utf-8",
            )
            job = {
                "work_root": str(root), "log": str(log), "status": "running",
                "settings": {"audio_mode": "compact-stereo"},
            }
            self.assertEqual(
                pipeline_percent(job, {"stage": "ordinary-and-menu-domains"}),
                0.5,
            )

    def test_lane_progress_is_cumulative_across_subtask_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "ordinary-and-menus"
            nested.mkdir()
            log = root / "run.log"
            log.write_text(
                '\n'.join([
                    'DVD2HEVC_PROGRESS {"timestamp":1,"lane":"mux","event":"done","task":"resolve-audio-policy"}',
                    'DVD2HEVC_PROGRESS {"timestamp":2,"lane":"video","event":"progress","task":"title-1",'
                    '"scope":"video-task-duration","current":50,"total":100}',
                    'DVD2HEVC_PROGRESS {"timestamp":3,"lane":"audio","event":"progress","task":"compact-stereo",'
                    '"scope":"audio-batch-cells","current":1,"total":10}',
                    'DVD2HEVC_PROGRESS {"timestamp":4,"lane":"audio","event":"start","task":"stream-2"}',
                ]),
                encoding="utf-8",
            )
            (nested / "status.json").write_text(json.dumps({
                "state": "running", "step": "convert-title-1",
                "completed_video_tasks": 0, "total_video_tasks": 2,
            }), encoding="utf-8")
            job = {
                "work_root": str(root), "log": str(log), "status": "running",
                "settings": {"audio_mode": "compact-stereo"},
            }
            first = lane_progress_snapshot(job, {"stage": "ordinary-and-menu-domains"})
            self.assertEqual(first["video"][0], 25.0)
            self.assertEqual(first["audio"][0], 10.0)
            self.assertEqual(first["mux"][0], 20.0)

            (nested / "status.json").write_text(json.dumps({
                "state": "running", "step": "convert-title-2",
                "completed_video_tasks": 1, "total_video_tasks": 2,
            }), encoding="utf-8")
            with log.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\nDVD2HEVC_PROGRESS {"timestamp":5,"lane":"video","event":"progress","task":"title-2",'
                    '"scope":"video-task-duration","current":0,"total":100}'
                    '\nDVD2HEVC_PROGRESS {"timestamp":6,"lane":"audio","event":"progress","task":"compact-stereo",'
                    '"scope":"audio-batch-cells","current":2,"total":10}'
                    '\nDVD2HEVC_PROGRESS {"timestamp":7,"lane":"mux","event":"start","task":"plan-full-disc-compaction"}'
                )
            second = lane_progress_snapshot(job, {"stage": "plan-full-disc-compaction"})
            self.assertGreaterEqual(second["video"][0], first["video"][0])
            self.assertGreaterEqual(second["audio"][0], first["audio"][0])
            self.assertGreaterEqual(second["mux"][0], first["mux"][0])

            with log.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\nDVD2HEVC_PROGRESS {"timestamp":8,"lane":"mux","event":"progress",'
                    '"task":"compact-domains","current":1,"total":4}'
                )
            third = lane_progress_snapshot(job, {"stage": "compact-vts01"})
            self.assertGreaterEqual(third["mux"][0], second["mux"][0])

    def test_video_lane_does_not_retreat_between_physical_and_ordinary_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "ordinary-and-menus"
            nested.mkdir()
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "vts": [{"vts": 4, "title_vobus": 100}],
                "title_tasks": [{
                    "status": "ready", "vts": 1, "duration_seconds": 48,
                }],
                "menu_tasks": [],
            }), encoding="utf-8")
            log = root / "run.log"
            log.write_text(
                'DVD2HEVC_PROGRESS {"timestamp":1,"lane":"video","event":"done",'
                '"task":"convert-vts04-physical","scope":"video-task-duration",'
                '"current":100,"total":100}',
                encoding="utf-8",
            )
            job = {
                "work_root": str(root), "log": str(log), "plan": str(plan),
                "status": "running", "settings": {"audio_mode": "compact-stereo"},
            }
            status = {"stage": "convert-vts04-physical", "physical_vts": [4]}
            physical = lane_progress_snapshot(job, status)

            (nested / "status.json").write_text(json.dumps({
                "state": "running", "step": "convert-title-1",
                "completed_video_tasks": 0, "total_video_tasks": 1,
            }), encoding="utf-8")
            with log.open("a", encoding="utf-8") as handle:
                handle.write(
                    '\nDVD2HEVC_PROGRESS {"timestamp":2,"lane":"video","event":"progress",'
                    '"task":"title-1","scope":"video-task-duration","current":0,"total":100}'
                )
            ordinary = lane_progress_snapshot(
                job, {"stage": "ordinary-and-menu-domains", "physical_vts": [4]},
            )
            self.assertEqual(physical["video"][0], 50.0)
            self.assertEqual(ordinary["video"][0], 50.0)
            self.assertGreaterEqual(ordinary["video"][0], physical["video"][0])

    def test_physical_cell_counter_interpolates_overall_and_video_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            physical = root / "physical" / "vts05"
            tasks = physical / "tasks"
            tasks.mkdir(parents=True)
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "vts": [{"vts": 5, "title_vobus": 100}],
                "title_tasks": [{
                    "status": "ready", "vts": 1, "duration_seconds": 48,
                }],
                "menu_tasks": [],
            }), encoding="utf-8")
            log = root / "run.log"
            log.write_text("", encoding="utf-8")
            nested_status = {
                "schema": "dvd2hevc-phase7-vts-status-v0", "state": "running",
                "vts": 5, "completed_tasks": 36, "total_tasks": 72,
            }
            (physical / "status.json").write_text(json.dumps(nested_status), encoding="utf-8")
            for index in range(1, 37):
                report = tasks / f"task-{index:03d}" / "interleaved-cell-report.json"
                report.parent.mkdir()
                report.write_text("{}", encoding="utf-8")
            job = {
                "work_root": str(root), "log": str(log), "plan": str(plan),
                "status": "running", "settings": {"audio_mode": "compact-stereo"},
            }
            status = {"stage": "convert-vts05-physical", "physical_vts": [5]}
            first_pipeline = pipeline_percent(job, status)
            first_video = lane_progress_snapshot(job, status)["video"]
            self.assertEqual(first_pipeline, 20.5)
            self.assertEqual(first_video, (25.0, "36/72 physical cells"))

            # A worker restart may briefly reset the live counter, but durable
            # completed reports must keep both bars at their previous values.
            nested_status["completed_tasks"] = 0
            (physical / "status.json").write_text(json.dumps(nested_status), encoding="utf-8")
            self.assertEqual(pipeline_percent(job, status), first_pipeline)
            self.assertEqual(lane_progress_snapshot(job, status)["video"], first_video)

            for index in range(37, 55):
                report = tasks / f"task-{index:03d}" / "interleaved-cell-report.json"
                report.parent.mkdir()
                report.write_text("{}", encoding="utf-8")
            nested_status["completed_tasks"] = 54
            (physical / "status.json").write_text(json.dumps(nested_status), encoding="utf-8")
            self.assertGreater(pipeline_percent(job, status), first_pipeline)
            self.assertEqual(
                lane_progress_snapshot(job, status)["video"],
                (37.5, "54/72 physical cells"),
            )

    def test_managed_runner_can_route_progress_away_from_powershell_stderr(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"DVD2HEVC_PROGRESS_STDOUT": "1"}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            progress_event("mux", "progress", "cell", current=1, total=2)
        self.assertIn("DVD2HEVC_PROGRESS", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_queue_pause_and_resume_state_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary) / "queue-control.json"
            with (
                patch("dvd2hevc_app.frontend.QUEUE_CONTROL", control),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=42),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_pause_queue(argparse.Namespace()), 0)
                self.assertTrue(json.loads(control.read_text())["paused"])
                self.assertEqual(cmd_resume_queue(argparse.Namespace()), 0)
                self.assertFalse(json.loads(control.read_text())["paused"])

    def test_disk_full_failure_removes_new_iso_and_authoring_partial(self) -> None:
        root = Path(self._queue_control_root.name)
        output = root / "Converted (DVD) (HEVC).iso"
        output.write_bytes(b"incomplete")
        author_partial = root / f".{output.name}.1234.part"
        author_partial.write_bytes(b"incomplete")
        unrelated = root / ".different.iso.1234.part"
        unrelated.write_bytes(b"keep")
        work = root / "work"
        reports = work / "final-output-reports"
        reports.mkdir(parents=True)
        log = work / "run.log"
        log.write_text("DVD ISO authoring failed; see author.log\n", encoding="utf-8")
        (reports / "author.log").write_text(
            "genisoimage: No space left on device\n", encoding="utf-8"
        )
        job = {
            "id": "disc-1",
            "source": str(root / "source.iso"),
            "output": str(output),
            "output_existed_at_plan": False,
            "output_created_by_job": True,
            "work_root": str(work),
            "log": str(log),
        }

        result = handle_output_storage_failure(job)

        self.assertFalse(output.exists())
        self.assertFalse(author_partial.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(result["failure_reason"], "output-disk-full")
        self.assertTrue(result["partial_output_removed"])
        self.assertTrue(result["queue_paused_on_failure"])
        self.assertIn("Output disk full", queue_pause_reason() or "")

    def test_disk_full_failure_never_removes_preexisting_iso(self) -> None:
        root = Path(self._queue_control_root.name)
        output = root / "Existing.iso"
        output.write_bytes(b"keep")
        work = root / "work-existing"
        work.mkdir()
        log = work / "run.log"
        log.write_text(
            "OSError: [WinError 112] There is not enough space on the disk\n",
            encoding="utf-8",
        )
        job = {
            "id": "disc-2",
            "source": str(root / "source.iso"),
            "output": str(output),
            "output_existed_at_plan": True,
            "output_created_by_job": False,
            "work_root": str(work),
            "log": str(log),
        }

        result = handle_output_storage_failure(job)

        self.assertEqual(output.read_bytes(), b"keep")
        self.assertFalse(result["partial_output_removed"])
        self.assertIn("existed before", result["partial_output_cleanup_error"])
        self.assertIn("Output disk full", queue_pause_reason() or "")

    def test_unrelated_failure_does_not_remove_output_or_pause_queue(self) -> None:
        root = Path(self._queue_control_root.name)
        output = root / "Failed.iso"
        output.write_bytes(b"keep for diagnosis")
        work = root / "work-unrelated"
        work.mkdir()
        log = work / "run.log"
        log.write_text("Decoder rejected an invalid packet\n", encoding="utf-8")
        job = {
            "id": "disc-3",
            "source": str(root / "source.iso"),
            "output": str(output),
            "output_created_by_job": True,
            "work_root": str(work),
            "log": str(log),
        }

        result = handle_output_storage_failure(job)

        self.assertTrue(output.exists())
        self.assertNotIn("failure_reason", result)
        self.assertIsNone(queue_pause_reason())

    def test_stale_disk_full_log_from_prior_attempt_is_ignored(self) -> None:
        root = Path(self._queue_control_root.name)
        output = root / "Current attempt.iso"
        output.write_bytes(b"keep for current diagnosis")
        work = root / "work-current"
        reports = work / "final-output-reports"
        reports.mkdir(parents=True)
        log = work / "run.log"
        log.write_text("Current attempt failed during decoding\n", encoding="utf-8")
        stale_author = reports / "author.log"
        stale_author.write_text("No space left on device\n", encoding="utf-8")
        old_time = time.time() - 120
        os.utime(stale_author, (old_time, old_time))
        job = {
            "id": "disc-4",
            "source": str(root / "source.iso"),
            "output": str(output),
            "output_created_by_job": True,
            "work_root": str(work),
            "log": str(log),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

        result = handle_output_storage_failure(job)

        self.assertTrue(output.exists())
        self.assertNotIn("failure_reason", result)
        self.assertIsNone(queue_pause_reason())

    def test_pause_all_prevents_watched_folder_planning(self) -> None:
        root = Path(self._queue_control_root.name)
        control = root / "queue-control.json"
        control.write_text(json.dumps({
            "schema": "dvd2hevc-queue-control-v1", "paused": True,
            "cancel_generation": 0,
        }), encoding="utf-8")
        source_dir = root / "incoming"
        output_dir = root / "out"
        source_dir.mkdir()
        (source_dir / "Paused.iso").write_bytes(b"dvd")
        watch_path = root / "watch.json"
        watch_path.write_text(json.dumps({
            "schema": "dvd2hevc-watched-batch-v1", "id": "paused-watch",
            "status": "active", "source_dir": str(source_dir),
            "output_dir": str(output_dir), "recursive": False,
            "settle_seconds": 0, "settings": {}, "ledger": {},
        }), encoding="utf-8")
        with patch("dvd2hevc_app.frontend.prepare_job") as prepare:
            watch = scan_watched_batch(watch_path, now=100)
        self.assertTrue(watch["paused_by_queue"])
        self.assertEqual(watch["ledger"], {})
        prepare.assert_not_called()

    def test_cancel_generation_invalidates_a_job_that_finishes_planning_late(self) -> None:
        root = Path(self._queue_control_root.name)
        control = root / "queue-control.json"
        control.write_text(json.dumps({
            "schema": "dvd2hevc-queue-control-v1", "paused": True,
            "cancel_generation": 2,
        }), encoding="utf-8")
        job_path = root / "late" / "job.json"
        job_path.parent.mkdir()
        job = {"id": "late", "status": "planned", "cancel_generation": 1}
        job_path.write_text(json.dumps(job), encoding="utf-8")
        self.assertFalse(queue_prepared_job(job_path, job))
        self.assertEqual(json.loads(job_path.read_text())["status"], "canceled")

    def test_cancel_all_stops_watchers_cancels_waiting_and_requests_safe_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "jobs"
            watches = jobs / "watched-batches"
            control = jobs / "queue-control.json"
            watches.mkdir(parents=True)
            watch_path = watches / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1", "id": "watch-all",
                "status": "active", "source_dir": str(root / "incoming"),
                "output_dir": str(root / "out"), "ledger": {},
            }), encoding="utf-8")
            queued_path = jobs / "queued" / "job.json"
            queued_path.parent.mkdir()
            queued_path.write_text(json.dumps({
                "id": "queued", "status": "queued", "work_root": str(root / "queued-work"),
            }), encoding="utf-8")
            running_path = jobs / "running" / "job.json"
            running_path.parent.mkdir()
            running_work = root / "running-work"
            running_path.write_text(json.dumps({
                "id": "running", "status": "running", "runner_pid": 123,
                "work_root": str(running_work),
            }), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", jobs),
                patch("dvd2hevc_app.frontend.WATCH_ROOT", watches),
                patch("dvd2hevc_app.frontend.QUEUE_CONTROL", control),
                patch("dvd2hevc_app.frontend.process_alive", side_effect=lambda pid: pid == 123),
                patch("dvd2hevc_app.frontend.terminate_process_tree", return_value=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_cancel_all(argparse.Namespace()), 0)
            self.assertTrue(json.loads(control.read_text())["paused"])
            self.assertGreater(json.loads(control.read_text())["cancel_generation"], 0)
            self.assertEqual(json.loads(watch_path.read_text())["status"], "stopped")
            self.assertEqual(json.loads(queued_path.read_text())["status"], "canceled")
            running = json.loads(running_path.read_text())
            self.assertEqual(running["status"], "canceled")
            self.assertEqual(running["termination_mode"], "immediate-process-tree")
            self.assertIn("cancel_requested_at", running)
            self.assertTrue((running_work / "cancel.requested").is_file())

    def test_authoring_backend_prefers_native_then_wsl(self) -> None:
        with patch("dvd2hevc_app.frontend.shutil.which") as which:
            which.side_effect = lambda name: "C:/tools/genisoimage.exe" if name == "genisoimage" else None
            self.assertEqual(find_authoring_backend(), "C:/tools/genisoimage.exe")
        with (
            patch("dvd2hevc_app.frontend.shutil.which") as which,
            patch("dvd2hevc_app.frontend.subprocess.run") as run,
        ):
            which.side_effect = lambda name: "C:/Windows/System32/wsl.exe" if name == "wsl.exe" else None
            run.return_value = SimpleNamespace(returncode=0, stdout="/usr/bin/genisoimage\n")
            self.assertEqual(
                find_authoring_backend(),
                "WSL Ubuntu-24.04: /usr/bin/genisoimage",
            )
            self.assertEqual(run.call_args.args[0][3], "-e")

    def test_planned_job_can_be_canceled_and_resumed_without_losing_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_path = root / "job.json"
            job = {
                "id": "movie-job",
                "status": "planned",
                "source": str(root / "movie.iso"),
                "output": str(root / "movie-hevc.iso"),
                "work_root": str(root / "work"),
            }
            with (
                patch("dvd2hevc_app.frontend.resolve_job", return_value=(job_path, job)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_cancel(argparse.Namespace(job="movie-job")), 0)
            canceled = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(canceled["status"], "canceled")
            with (
                patch("dvd2hevc_app.frontend.resolve_job", return_value=(job_path, canceled)),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_resume_job(argparse.Namespace(job="movie-job")), 0)
            resumed = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["work_root"], str(root / "work"))

    def test_resume_accepts_only_this_jobs_recorded_authored_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "Movie.iso"
            output.write_bytes(b"authored iso")
            work = root / "work"
            reports = work / "final-output-reports"
            reports.mkdir(parents=True)
            stage_report = work / "stage-report.json"
            stage_report.write_text('{"status":"passed"}', encoding="utf-8")
            (reports / "author.json").write_text(json.dumps({
                "schema": "dvd2hevc-authored-iso-v0",
                "status": "authored",
                "destination": str(output),
                "stage_report": str(stage_report),
                "size": output.stat().st_size,
            }), encoding="utf-8")
            job_path = root / "job.json"
            job = {
                "id": "late-failure", "status": "failed",
                "output": str(output), "work_root": str(work),
            }
            self.assertTrue(_existing_output_is_authored_job_artifact(job))
            with (
                patch("dvd2hevc_app.frontend.resolve_job", return_value=(job_path, job)),
                patch("dvd2hevc_app.frontend.refresh_job", return_value=job),
                patch("dvd2hevc_app.frontend._reset_job_for_resume") as reset,
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_resume_job(argparse.Namespace(job="late-failure")), 0)
            reset.assert_called_once()
            (reports / "author.json").write_text(json.dumps({
                "schema": "dvd2hevc-authored-iso-v0", "status": "authored",
                "destination": str(root / "Someone-Elses.iso"),
                "stage_report": str(stage_report), "size": output.stat().st_size,
            }), encoding="utf-8")
            self.assertFalse(_existing_output_is_authored_job_artifact(job))

    def test_running_job_cancel_terminates_its_process_tree_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            job_path = root / "job.json"
            job = {
                "id": "running-job", "status": "running", "runner_pid": 456,
                "work_root": str(work),
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.resolve_job", return_value=(job_path, job)),
                patch("dvd2hevc_app.frontend.process_alive", return_value=True),
                patch("dvd2hevc_app.frontend.terminate_process_tree", return_value=True) as stop,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_cancel(argparse.Namespace(job="running-job")), 0)
            stop.assert_called_once_with(456)
            canceled = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(canceled["status"], "canceled")
            self.assertEqual(canceled["returncode"], 3)
            self.assertTrue((work / "cancel.requested").is_file())

    def test_refresh_preserves_cancel_after_an_old_runner_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_path = root / "job.json"
            job = {
                "id": "canceled-race", "status": "failed", "runner_pid": 987,
                "work_root": str(root / "work"),
                "cancel_requested_at": "2026-07-16T00:00:00+1200",
                "error": "old dispatcher reported a nonzero process code",
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with patch("dvd2hevc_app.frontend.process_alive", return_value=False):
                refreshed = refresh_job(job_path, job)
            self.assertEqual(refreshed["status"], "canceled")
            self.assertEqual(refreshed["returncode"], 3)
            self.assertNotIn("error", refreshed)

    def test_restart_recovery_requeues_old_boot_job_and_retains_cached_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_path = root / "interrupted" / "job.json"
            work = job_path.parent / "work"
            work.mkdir(parents=True)
            (work / "run.log").write_text("old run", encoding="utf-8")
            (work / "status.json").write_text('{"state":"running"}', encoding="utf-8")
            job = {
                "id": "interrupted",
                "status": "running",
                "queue_order": 10,
                "source": str(root / "source.iso"),
                "output": str(root / "output.iso"),
                "work_root": str(work),
                "started_at": "2026-07-17T03:00:00+12:00",
                "runner_boot_session": "100",
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.known_job_files", return_value=[job_path]),
                patch("dvd2hevc_app.frontend.current_boot_epoch", return_value=200 * 60.0),
                patch("dvd2hevc_app.frontend.current_cancel_generation", return_value=7),
            ):
                recovered = recover_jobs_interrupted_by_restart()
            self.assertEqual(recovered, ["interrupted"])
            resumed = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["queue_order"], 10)
            self.assertEqual(resumed["cancel_generation"], 7)
            self.assertEqual(resumed["restart_recovery_count"], 1)
            self.assertFalse((work / "run.log").exists())
            self.assertFalse((work / "status.json").exists())
            self.assertEqual(len(list(work.glob("run.previous-*.log"))), 1)
            self.assertEqual(len(list(work.glob("status.previous-*.json"))), 1)

    def test_restart_recovery_does_not_retry_current_boot_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_path = root / "failed" / "job.json"
            job_path.parent.mkdir(parents=True)
            job = {
                "id": "same-boot-failure",
                "status": "failed",
                "source": str(root / "source.iso"),
                "output": str(root / "output.iso"),
                "work_root": str(job_path.parent / "work"),
                "runner_boot_session": "200",
                "error": "Conversion runner exited before recording a terminal result",
            }
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.known_job_files", return_value=[job_path]),
                patch("dvd2hevc_app.frontend.current_boot_epoch", return_value=200 * 60.0),
            ):
                recovered = recover_jobs_interrupted_by_restart()
            self.assertEqual(recovered, [])
            self.assertEqual(
                json.loads(job_path.read_text(encoding="utf-8"))["status"],
                "failed",
            )

    def test_runner_rechecks_canceled_state_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            active_lock = Path(temporary) / "active-work.lock"
            path.write_text(
                json.dumps({"id": "race-job", "status": "canceled"}),
                encoding="utf-8",
            )
            with (
                patch("dvd2hevc_app.frontend.ACTIVE_WORK_LOCK", active_lock),
                patch("dvd2hevc_app.frontend.subprocess.Popen") as popen,
            ):
                self.assertEqual(run_job(path, quiet=True), 0)
            popen.assert_not_called()

    def test_global_work_lock_allows_only_one_owner_and_recovers_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "active-work.lock"
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root),
                patch("dvd2hevc_app.frontend.ACTIVE_WORK_LOCK", lock),
            ):
                first = acquire_active_work_lock("first-disc")
                acquired = threading.Event()
                finished = threading.Event()

                def take_second_slot() -> None:
                    token = acquire_active_work_lock("second-disc")
                    acquired.set()
                    release_active_work_lock(token)
                    finished.set()

                thread = threading.Thread(target=take_second_slot)
                thread.start()
                time.sleep(0.15)
                self.assertFalse(acquired.is_set())
                release_active_work_lock(first)
                self.assertTrue(finished.wait(2.0))
                thread.join(2.0)
                self.assertFalse(lock.exists())

                lock.write_text(json.dumps({"pid": 99999999, "token": "stale"}), encoding="utf-8")
                stale_replacement = acquire_active_work_lock("replacement")
                self.assertNotEqual(stale_replacement, "stale")
                release_active_work_lock(stale_replacement)

    def test_process_liveness_probe_is_non_destructive_on_windows(self) -> None:
        self.assertTrue(process_alive(os.getpid()))

    def test_dispatcher_check_and_spawn_is_serialized_across_callers(self) -> None:
        from dvd2hevc_app.frontend import ensure_dispatcher

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "dispatcher.json"
            dispatcher_lock = root / "dispatcher.lock"
            start_lock = root / "dispatcher-start.lock"
            process = SimpleNamespace(pid=4242)
            results: list[int] = []
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root),
                patch("dvd2hevc_app.frontend.DISPATCHER_STATE", state),
                patch("dvd2hevc_app.frontend.DISPATCHER_LOCK", dispatcher_lock),
                patch("dvd2hevc_app.frontend.DISPATCHER_START_LOCK", start_lock),
                patch("dvd2hevc_app.frontend.process_alive", side_effect=lambda pid: pid == 4242),
                patch("dvd2hevc_app.frontend.subprocess.Popen", return_value=process) as popen,
            ):
                threads = [threading.Thread(target=lambda: results.append(ensure_dispatcher())) for _ in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2.0)
            self.assertEqual(results, [4242] * 6)
            self.assertEqual(popen.call_count, 1)
            self.assertFalse(start_lock.exists())

    def test_watched_batch_waits_for_stability_and_queues_each_iso_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            iso = source_dir / "Movie.iso"
            iso.write_bytes(b"dvd")
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "watch-test", "status": "active",
                "source_dir": str(source_dir), "output_dir": str(output_dir),
                "recursive": False, "settle_seconds": 10, "poll_seconds": 5,
                "add_filename_tags": True, "settings": {}, "ledger": {},
            }), encoding="utf-8")
            job_path = root / "jobs" / "movie-job" / "job.json"
            job = {"id": "movie-job", "status": "planned"}
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch("dvd2hevc_app.frontend.prepare_job", return_value=(job_path, job)) as prepare,
                patch("dvd2hevc_app.frontend.queue_prepared_job") as queue,
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
            ):
                first = scan_watched_batch(watch_path, now=100)
                self.assertEqual(next(iter(first["ledger"].values()))["status"], "settling")
                scan_watched_batch(watch_path, now=109)
                prepare.assert_not_called()
                queued = scan_watched_batch(watch_path, now=111)
                entry = next(iter(queued["ledger"].values()))
                self.assertEqual(entry["status"], "queued")
                self.assertEqual(entry["job_id"], "movie-job")
                self.assertTrue(str(entry["output"]).endswith("Movie (DVD) (HEVC).iso"))
                scan_watched_batch(watch_path, now=120)
                self.assertEqual(prepare.call_count, 1)
                self.assertEqual(queue.call_count, 1)

    def test_canceling_a_watched_job_does_not_add_its_iso_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "jobs"
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            iso = source_dir / "Canceled.iso"
            iso.write_bytes(b"dvd")
            stat = iso.stat()
            job_path = jobs / "canceled-job" / "job.json"
            job_path.parent.mkdir(parents=True)
            job_path.write_text(json.dumps({
                "id": "canceled-job", "status": "canceled",
                "source": str(iso), "output": str(output_dir / "Canceled (DVD) (HEVC).iso"),
            }), encoding="utf-8")
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1", "id": "watch-canceled",
                "status": "active", "source_dir": str(source_dir),
                "output_dir": str(output_dir), "recursive": False, "settle_seconds": 0,
                "add_filename_tags": True, "settings": {},
                "ledger": {str(iso).casefold(): {
                    "source": str(iso), "fingerprint": f"{stat.st_size}:{stat.st_mtime_ns}",
                    "status": "queued", "job_id": "canceled-job", "present": True,
                }},
            }), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", jobs),
                patch("dvd2hevc_app.frontend.prepare_job") as prepare,
            ):
                watched = scan_watched_batch(watch_path, now=100)
            entry = next(iter(watched["ledger"].values()))
            self.assertEqual(entry["status"], "canceled")
            prepare.assert_not_called()

    def test_watched_batch_queues_at_most_one_new_disc_per_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            for name in ("First.iso", "Second.iso", "Third.iso"):
                (source_dir / name).write_bytes(name.encode("ascii"))
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "watch-serial", "status": "active",
                "source_dir": str(source_dir), "output_dir": str(output_dir),
                "recursive": False, "settle_seconds": 0,
                "add_filename_tags": True, "settings": {}, "ledger": {},
            }), encoding="utf-8")
            created = 0

            def prepare(*_args: object, **_kwargs: object) -> tuple[Path, dict[str, object]]:
                nonlocal created
                created += 1
                job = {"id": f"job-{created}", "status": "planned"}
                return root / "jobs" / f"job-{created}" / "job.json", job

            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch("dvd2hevc_app.frontend.prepare_job", side_effect=prepare),
                patch("dvd2hevc_app.frontend.queue_prepared_job"),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
            ):
                first = scan_watched_batch(watch_path, now=100)
                self.assertEqual(watched_batch_summary(first)["queued"], 1)
                second = scan_watched_batch(watch_path, now=101)
                self.assertEqual(watched_batch_summary(second)["queued"], 2)
                third = scan_watched_batch(watch_path, now=102)
                self.assertEqual(watched_batch_summary(third)["queued"], 3)
            self.assertEqual(created, 3)

    def test_watched_batch_queues_oldest_iso_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            newest = source_dir / "A Newest.iso"
            oldest = source_dir / "Z Oldest.iso"
            middle = source_dir / "M Middle.iso"
            for path in (newest, oldest, middle):
                path.write_bytes(path.name.encode("ascii"))
            os.utime(oldest, ns=(1_000_000_000, 1_000_000_000))
            os.utime(middle, ns=(2_000_000_000, 2_000_000_000))
            os.utime(newest, ns=(3_000_000_000, 3_000_000_000))
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1", "id": "watch-oldest",
                "status": "active", "source_dir": str(source_dir),
                "output_dir": str(output_dir), "recursive": False,
                "settle_seconds": 0, "add_filename_tags": True,
                "settings": {}, "ledger": {},
            }), encoding="utf-8")
            order: list[str] = []

            def prepare(*_args: object, **kwargs: object) -> tuple[Path, dict[str, object]]:
                source = Path(str(kwargs["source"]))
                order.append(source.name)
                job = {"id": f"job-{len(order)}", "status": "planned"}
                return root / "jobs" / str(job["id"]) / "job.json", job

            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch("dvd2hevc_app.frontend.prepare_job", side_effect=prepare),
                patch("dvd2hevc_app.frontend.queue_prepared_job", return_value=True),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
            ):
                scan_watched_batch(watch_path, now=100)
                scan_watched_batch(watch_path, now=101)
                scan_watched_batch(watch_path, now=102)
            self.assertEqual(order, [oldest.name, middle.name, newest.name])

    def test_renamed_queued_iso_replaces_old_job_using_new_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "jobs"
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            old_source = source_dir / "Draft Name.iso"
            old_source.write_bytes(b"same disc")
            stat = old_source.stat()
            old_key = str(old_source.resolve()).casefold()
            old_job_path = jobs / "old-job" / "job.json"
            old_job_path.parent.mkdir(parents=True)
            old_job_path.write_text(json.dumps({
                "id": "old-job", "status": "queued", "source": str(old_source),
                "output": str(output_dir / "Draft Name (DVD) (HEVC).iso"),
            }), encoding="utf-8")
            new_source = source_dir / "Final Name.iso"
            old_source.rename(new_source)
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1", "id": "watch-rename",
                "status": "active", "source_dir": str(source_dir),
                "output_dir": str(output_dir), "recursive": False,
                "settle_seconds": 0, "add_filename_tags": True, "settings": {},
                "ledger": {old_key: {
                    "source": str(old_source),
                    "fingerprint": f"{stat.st_size}:{stat.st_mtime_ns}",
                    "status": "queued", "job_id": "old-job", "present": True,
                }},
            }), encoding="utf-8")
            new_job = {"id": "new-job", "status": "planned"}
            new_job_path = jobs / "new-job" / "job.json"
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", jobs),
                patch("dvd2hevc_app.frontend.prepare_job", return_value=(new_job_path, new_job)) as prepare,
                patch("dvd2hevc_app.frontend.queue_prepared_job", return_value=True),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
            ):
                watched = scan_watched_batch(watch_path, now=100)
            self.assertEqual(json.loads(old_job_path.read_text())["status"], "canceled")
            entry = watched["ledger"][str(new_source.resolve()).casefold()]
            self.assertEqual(entry["status"], "queued")
            self.assertEqual(entry["job_id"], "new-job")
            self.assertEqual(entry["renamed_from"], str(old_source))
            self.assertTrue(str(entry["output"]).endswith("Final Name (DVD) (HEVC).iso"))
            self.assertEqual(Path(str(prepare.call_args.kwargs["source"])), new_source.resolve())

    def test_busy_converter_leaves_watched_disc_visibly_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            (source_dir / "Waiting.iso").write_bytes(b"dvd")
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1", "id": "watch-busy",
                "status": "active", "source_dir": str(source_dir),
                "output_dir": str(output_dir), "recursive": False,
                "settle_seconds": 0, "add_filename_tags": True,
                "settings": {}, "ledger": {},
            }), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch(
                    "dvd2hevc_app.frontend.prepare_job",
                    side_effect=WorkSlotBusy("another disc is active"),
                ) as prepare,
            ):
                watched = scan_watched_batch(watch_path, now=100)
            entry = next(iter(watched["ledger"].values()))
            self.assertEqual(entry["status"], "ready")
            self.assertEqual(entry["waiting_reason"], "another-disc-active")
            self.assertEqual(watched_batch_summary(watched)["pending"], 1)
            self.assertEqual(watched_batch_summary(watched)["attention"], 0)
            self.assertFalse(prepare.call_args.kwargs["wait_for_slot"])

    def test_watched_batch_marks_existing_outputs_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            output_dir.mkdir()
            (source_dir / "Movie.iso").write_bytes(b"dvd")
            (output_dir / "Movie - converted.iso").write_bytes(b"existing")
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "watch-existing", "status": "active",
                "source_dir": str(source_dir), "output_dir": str(output_dir),
                "recursive": False, "settle_seconds": 0,
                "add_filename_tags": False, "settings": {}, "ledger": {},
            }), encoding="utf-8")
            with patch("dvd2hevc_app.frontend.prepare_job") as prepare:
                watch = scan_watched_batch(watch_path, now=100)
            entry = next(iter(watch["ledger"].values()))
            self.assertEqual(entry["status"], "output-exists")
            self.assertEqual(watched_batch_summary(watch)["existing"], 1)
            prepare.assert_not_called()

    def test_watched_batch_create_stop_and_reset_use_durable_fresh_ledgers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            watch_root = root / "watches"
            source_dir.mkdir()
            args = argparse.Namespace(
                source_dir=str(source_dir), output_dir=str(output_dir), recursive=True,
                poll_seconds=5, settle_seconds=0, add_filename_tags=False,
                preset=None, quality=None, target_bitrate_multiplier=None,
                bitrate_mode=None, main_title_quality=None, top_n_quality=None,
                top_n_count=None, encoder=None, encoder_preset=None,
                deinterlace=None, audio_mode=None, audio_language=None,
                stereo_audio_bitrate=None, mono_audio_bitrate=None,
                audio_workers=None, pipeline_depth=None, vlc_root=None,
                label=None, name_prefix=None,
            )
            with (
                patch("dvd2hevc_app.frontend.WATCH_ROOT", watch_root),
                patch("dvd2hevc_app.frontend.find_patched_vlc_root", return_value=root / "vlc"),
                patch("dvd2hevc_app.frontend._spawn_watched_batch", return_value=4321),
            ):
                first_path, first = create_watched_batch(args)
                self.assertEqual(first["status"], "active")
                self.assertEqual(first["ledger"], {})
                self.assertFalse(first["add_filename_tags"])
                with self.assertRaises(PipelineError):
                    create_watched_batch(args)
                stopped = stop_watched_batch(first["id"])
                self.assertEqual(stopped["status"], "stopped")
                second_path, second = reset_watched_batch(first["id"])
                self.assertNotEqual(first_path, second_path)
                self.assertEqual(second["status"], "active")
                self.assertEqual(second["ledger"], {})
                self.assertFalse(second["add_filename_tags"])

    def test_failed_watched_batch_resumes_without_clearing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_root = root / "watches"
            watch_root.mkdir()
            watch_path = watch_root / "failed-watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "failed-watch", "status": "failed",
                "source_dir": str(root / "incoming"),
                "output_dir": str(root / "output"),
                "watcher_pid": 123, "error": "access denied",
                "stopped_at": "yesterday",
                "ledger": {"disc": {"status": "passed"}},
            }), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.WATCH_ROOT", watch_root),
                patch("dvd2hevc_app.frontend.process_alive", return_value=False),
                patch("dvd2hevc_app.frontend._spawn_watched_batch", return_value=4321),
            ):
                resumed = resume_watched_batch("failed-watch")
            self.assertEqual(resumed["status"], "active")
            self.assertEqual(resumed["ledger"], {"disc": {"status": "passed"}})
            self.assertNotIn("error", resumed)
            self.assertNotIn("stopped_at", resumed)

    def test_resume_recovers_watcher_wide_dependency_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watch_root = root / "watches"
            watch_root.mkdir()
            watch_path = watch_root / "failed-watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "failed-watch", "status": "failed",
                "source_dir": str(root / "incoming"),
                "output_dir": str(root / "output"),
                "ledger": {
                    "retry": {
                        "status": "planning-failed",
                        "error": "Missing conversion requirements: ffmpeg, pycdlib",
                        "failed_at": "yesterday",
                    },
                    "disc-failure": {
                        "status": "planning-failed",
                        "error": "The DVD navigation data is malformed",
                    },
                },
            }), encoding="utf-8")
            with (
                patch("dvd2hevc_app.frontend.WATCH_ROOT", watch_root),
                patch("dvd2hevc_app.frontend.process_alive", return_value=False),
                patch("dvd2hevc_app.frontend._spawn_watched_batch", return_value=4321),
            ):
                resumed = resume_watched_batch("failed-watch")
            self.assertEqual(resumed["ledger"]["retry"]["status"], "ready")
            self.assertNotIn("failed_at", resumed["ledger"]["retry"])
            self.assertEqual(
                resumed["ledger"]["disc-failure"]["status"], "planning-failed"
            )
            self.assertEqual(resumed["recovered_runtime_entries"], 1)

    def test_watched_batch_retries_watcher_wide_planning_failure_once_per_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            for name in ("First.iso", "Second.iso"):
                (source_dir / name).write_bytes(b"dvd")
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "watch-runtime", "status": "active",
                "source_dir": str(source_dir), "output_dir": str(output_dir),
                "recursive": False, "settle_seconds": 0,
                "add_filename_tags": True, "settings": {}, "ledger": {},
            }), encoding="utf-8")
            with patch(
                "dvd2hevc_app.frontend.prepare_job",
                side_effect=PipelineError(
                    "Missing conversion requirements: ffmpeg, ffprobe, Python package pycdlib"
                ),
            ) as prepare:
                watched = scan_watched_batch(watch_path, now=100)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(watched["runtime_error"], prepare.side_effect.args[0])
            states = sorted(entry["status"] for entry in watched["ledger"].values())
            self.assertEqual(states, ["ready", "ready"])
            self.assertEqual(watched_batch_summary(watched)["attention"], 0)

    def test_starting_watched_batch_resumes_a_paused_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            watch_root = root / "watches"
            source_dir.mkdir()
            control = Path(self._queue_control_root.name) / "queue-control.json"
            control.write_text(json.dumps({
                "schema": "dvd2hevc-queue-control-v1", "paused": True,
                "cancel_generation": 77,
            }), encoding="utf-8")
            args = argparse.Namespace(
                source_dir=str(source_dir), output_dir=str(output_dir), recursive=False,
                poll_seconds=5, settle_seconds=0, add_filename_tags=True,
                preset=None, quality=None, target_bitrate_multiplier=None,
                bitrate_mode=None, main_title_quality=None, top_n_quality=None,
                top_n_count=None, encoder=None, encoder_preset=None,
                deinterlace=None, audio_mode="compact-stereo", audio_language=None,
                stereo_audio_bitrate=None, mono_audio_bitrate=None,
                audio_workers=None, pipeline_depth=None, vlc_root=None,
                label=None, name_prefix=None,
            )
            with (
                patch("dvd2hevc_app.frontend.WATCH_ROOT", watch_root),
                patch("dvd2hevc_app.frontend.find_patched_vlc_root", return_value=root / "vlc"),
                patch("dvd2hevc_app.frontend._spawn_watched_batch", return_value=4321),
            ):
                _path, watch = create_watched_batch(args)
            control_value = json.loads(control.read_text())
            self.assertFalse(control_value["paused"])
            self.assertEqual(control_value["cancel_generation"], 77)
            self.assertEqual(control_value["reason"], f"start-watch:{watch['id']}")
            self.assertTrue(watch["queue_resumed_at_start"])
            self.assertEqual(watch["settings"]["quality"], "target-bitrate")
            self.assertEqual(watch["settings"]["bitrate_mode"], "vbr")
            self.assertEqual(watch["settings"]["audio_mode"], "compact-stereo")

    def test_watched_batch_blocks_recursive_output_name_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            for branch in ("disc-a", "disc-b"):
                path = source_dir / branch
                path.mkdir(parents=True)
                (path / "Same Name.iso").write_bytes(branch.encode("ascii"))
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "watch-collision", "status": "active",
                "source_dir": str(source_dir), "output_dir": str(output_dir),
                "recursive": True, "settle_seconds": 0,
                "add_filename_tags": True, "settings": {}, "ledger": {},
            }), encoding="utf-8")
            job_path = root / "jobs" / "same-name-job" / "job.json"
            job = {"id": "same-name-job", "status": "planned"}
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch("dvd2hevc_app.frontend.prepare_job", return_value=(job_path, job)) as prepare,
                patch("dvd2hevc_app.frontend.queue_prepared_job"),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
            ):
                watch = scan_watched_batch(watch_path, now=100)
            states = sorted(entry["status"] for entry in watch["ledger"].values())
            self.assertEqual(states, ["output-name-conflict", "queued"])
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(watched_batch_summary(watch)["attention"], 1)

    def test_watched_batch_waits_while_backup_process_has_a_writer_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "incoming"
            output_dir = root / "converted"
            source_dir.mkdir()
            (source_dir / "Still Backing Up.iso").write_bytes(b"partial")
            watch_path = root / "watch.json"
            watch_path.write_text(json.dumps({
                "schema": "dvd2hevc-watched-batch-v1",
                "id": "watch-busy", "status": "active",
                "source_dir": str(source_dir), "output_dir": str(output_dir),
                "recursive": False, "settle_seconds": 0,
                "add_filename_tags": True, "settings": {}, "ledger": {},
            }), encoding="utf-8")
            job_path = root / "jobs" / "backup-job" / "job.json"
            job = {"id": "backup-job", "status": "planned"}
            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch("dvd2hevc_app.frontend.iso_is_write_quiet", return_value=False),
                patch("dvd2hevc_app.frontend.prepare_job") as prepare,
            ):
                waiting = scan_watched_batch(watch_path, now=100)
            entry = next(iter(waiting["ledger"].values()))
            self.assertEqual(entry["status"], "waiting-for-backup")
            self.assertEqual(watched_batch_summary(waiting)["waiting"], 1)
            prepare.assert_not_called()

            with (
                patch("dvd2hevc_app.frontend.JOB_ROOT", root / "jobs"),
                patch("dvd2hevc_app.frontend.iso_is_write_quiet", return_value=True),
                patch("dvd2hevc_app.frontend.prepare_job", return_value=(job_path, job)) as prepare,
                patch("dvd2hevc_app.frontend.queue_prepared_job"),
                patch("dvd2hevc_app.frontend.ensure_dispatcher", return_value=1234),
            ):
                queued = scan_watched_batch(watch_path, now=101)
            self.assertEqual(next(iter(queued["ledger"].values()))["status"], "queued")
            self.assertEqual(prepare.call_count, 1)


if __name__ == "__main__":
    unittest.main()
