"""Watched batches: explicit services preserve the public facade and test seams."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def watched_batch_files(services) -> list[Path]:
    if not services.WATCH_ROOT.is_dir():
        return []
    return sorted(
        services.WATCH_ROOT.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def make_watched_batch_id(source_dir: Path, *, services) -> str:
    timestamp = services.time.strftime("%Y%m%d-%H%M%S")
    base = services.safe_slug(f"watch-{source_dir.name}", fallback="watch-dvds")
    candidate = f"{timestamp}-{base}"
    suffix = 2
    while (services.WATCH_ROOT / f"{candidate}.json").exists():
        candidate = f"{timestamp}-{base}-{suffix}"
        suffix += 1
    return candidate


def try_read_watched_batch(path: Path, *, services) -> dict[str, Any] | None:
    try:
        value = services.read_json(path)
    except services.PipelineError:
        return None
    value["watch_file"] = str(path.resolve())
    return value


def resolve_watched_batch(identifier: str, *, services) -> tuple[Path, dict[str, Any]]:
    direct = services.Path(identifier).expanduser()
    if direct.is_file():
        return direct.resolve(), services.read_json(direct.resolve())
    watches = [
        (path, watch)
        for path in services.watched_batch_files()
        if (watch := services.try_read_watched_batch(path))
    ]
    exact = [(path, watch) for path, watch in watches if watch.get("id") == identifier]
    if exact:
        return exact[0]
    matches = [
        (path, watch)
        for path, watch in watches
        if identifier.casefold() in str(watch.get("id", "")).casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise services.PipelineError(f"Watched batch not found: {identifier}")
    raise services.PipelineError(f"Watched batch name is ambiguous: {identifier}")


def watched_batch_summary(watch: dict[str, Any], *, services) -> dict[str, int]:
    entries = list((watch.get("ledger") or {}).values())
    states = [str(entry.get("status") or "unknown") for entry in entries]
    active = sum(state in {"planned", "queued", "running"} for state in states)
    pending = sum(
        state in {"settling", "ready", "waiting-for-backup", "waiting-for-space", "source-missing"}
        for state in states
    )
    passed = states.count("passed")
    existing = states.count("output-exists")
    return {
        "detected": len(entries),
        "waiting": pending,
        "pending": pending,
        "active": active,
        # Retain this key for old reports and third-party callers. In the UI it
        # is now labelled Active because it includes a running conversion.
        "queued": active,
        "passed": passed,
        "done": passed + existing,
        "attention": sum(
            state in {"failed", "canceled", "planning-failed", "output-name-conflict", "source-changed"}
            for state in states
        ),
        "existing": existing,
    }


def _watch_namespace(watch: dict[str, Any], source: Path, output: Path, *, services) -> argparse.Namespace:
    settings = dict(watch.get("settings") or {})
    language_rules = settings.get("audio_language_overrides") or {}
    return services.argparse.Namespace(
        source=str(source), output=str(output), preset=None,
        settings_are_explicit=True, gui_preset_name=settings.get("named_preset"),
        quality=settings.get("quality"),
        output_format=settings.get("output_format", "dvd-hevc"),
        tsmuxer=settings.get("tsmuxer"), udf_tool=settings.get("udf_tool"), java_home=settings.get("java_home"),
        target_bitrate_multiplier=settings.get("target_bitrate_multiplier"),
        bitrate_mode=settings.get("bitrate_mode"),
        main_title_quality=settings.get("main_title_quality"),
        top_n_quality=settings.get("top_n_quality"),
        top_n_count=settings.get("top_n_count"),
        encoder=settings.get("encoder"), encoder_preset=settings.get("encoder_preset"),
        deinterlace=settings.get("deinterlace"), audio_mode=settings.get("audio_mode"),
        audio_language=[f"{key}={value}" for key, value in language_rules.items()],
        stereo_audio_bitrate=settings.get("stereo_audio_bitrate"),
        mono_audio_bitrate=settings.get("mono_audio_bitrate"),
        audio_workers=settings.get("audio_workers"), pipeline_depth=settings.get("pipeline_depth"),
        label=watch.get("label"), work_dir=None, vlc_root=watch.get("vlc_root") or None,
        name=None, resume=False,
        add_filename_tags=bool(watch.get("add_filename_tags", True)),
    )


def _watched_iso_sources(watch: dict[str, Any], *, services) -> list[Path]:
    source_dir = services.Path(str(watch["source_dir"]))
    if not source_dir.is_dir():
        return []
    iterator = source_dir.rglob("*.iso") if watch.get("recursive") else source_dir.glob("*.iso")
    sources = [path.resolve() for path in iterator if path.is_file()]

    def oldest_first(path: services.Path) -> tuple[int, str]:
        try:
            modified_ns = path.stat().st_mtime_ns
        except OSError:
            modified_ns = 2**63 - 1
        return modified_ns, str(path).casefold()

    # A backup's modification time normally records when MakeMKV finished it.
    # Handling the oldest backup first also leaves the newest arrival alone
    # longest, giving the user time to settle on its final filename.
    return sorted(sources, key=oldest_first)


def _reconcile_renamed_watch_entry(
    ledger: dict[str, dict[str, Any]],
    *,
    key: str,
    source: Path,
    fingerprint: str,
    source_keys: set[str],
    now: float, services) -> dict[str, Any] | None:
    """Move a durable ledger entry with its ISO instead of duplicating it."""
    entry = ledger.get(key)
    old_key = key if entry is not None else None
    if entry is None:
        matches = [
            (candidate_key, candidate)
            for candidate_key, candidate in ledger.items()
            if candidate_key not in source_keys
            and candidate.get("fingerprint") == fingerprint
        ]
        if len(matches) != 1:
            return None
        old_key, entry = matches[0]
        ledger.pop(old_key, None)
        ledger[key] = entry

    old_source = str(entry.get("source") or "")
    new_source = str(source)
    if old_source == new_source:
        return entry

    entry["source"] = new_source
    entry["renamed_from"] = old_source
    entry["renamed_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry["last_seen_epoch"] = now

    state = str(entry.get("status") or "")
    if state not in {"planned", "queued", "running"}:
        # Terminal states deliberately remain terminal: renaming a canceled or
        # failed disc must not silently defeat the user's cancellation/reset
        # model, and a passed disc must not be encoded twice.
        return entry

    job_id = str(entry.get("job_id") or "")
    job_path = services.JOB_ROOT / job_id / "job.json"
    job = services.try_read_job(job_path) if job_id and job_path.is_file() else None
    if not job:
        entry["status"] = "ready"
    elif str(job.get("status") or "") in {"planned", "queued"}:
        now_text = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
        job["status"] = "canceled"
        job["canceled_at"] = now_text
        job["ended_at"] = now_text
        job["cancel_reason"] = "Source ISO was renamed before conversion started"
        services.save_job(job_path, job)
        entry["status"] = "ready"
    elif str(job.get("status") or "") == "running":
        canceled = services.cancel_running_job(
            job_path,
            job,
            reason="Source ISO was renamed while conversion was running",
        )
        if canceled.get("status") == "canceled":
            entry["status"] = "ready"
        else:
            entry["status"] = "source-changed"
            entry["error"] = "Source was renamed, but its old conversion could not be stopped"
            return entry
    else:
        entry["status"] = str(job.get("status") or state)
        return entry

    entry["replaced_job_id"] = job_id or None
    entry["stable_since_epoch"] = now
    for field in ("job_id", "output", "queued_at", "error", "waiting_reason"):
        entry.pop(field, None)
    return entry


def scan_watched_batch(watch_path: Path, *, now: float | None = None, services) -> dict[str, Any]:
    """Discover stable ISOs, queue each fingerprint once, and persist the ledger."""
    now = services.time.time() if now is None else float(now)
    watch = services.read_json(watch_path)
    if watch.get("status") != "active":
        return watch
    if services.queue_is_paused():
        watch["paused_by_queue"] = True
        watch["updated_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
        latest = services.read_json(watch_path)
        if latest.get("status") != "active":
            return latest
        services.write_json_atomic(watch_path, watch)
        return watch
    watch.pop("paused_by_queue", None)
    ledger = dict(watch.get("ledger") or {})
    present: set[str] = set()
    settle_value = watch.get("settle_seconds")
    settle_seconds = max(0.0, float(60.0 if settle_value is None else settle_value))
    output_dir = services.Path(str(watch["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    reserved_outputs: dict[str, dict[str, Any]] = {}
    queued_one_this_scan = False
    runtime_blocked_error: str | None = None

    # A reset must not duplicate a job that the previous watcher already put
    # into the shared queue, even though its output ISO does not exist yet.
    for job_file in services.known_job_files():
        job = services.try_read_job(job_file)
        if not job or str(job.get("status")) not in {"planned", "queued", "running"}:
            continue
        output_value = job.get("output")
        if output_value:
            reserved_outputs[str(services.Path(str(output_value)).resolve()).casefold()] = job

    # Reflect the real queue state before deciding whether anything is new.
    for entry in ledger.values():
        job_id = entry.get("job_id")
        if not job_id:
            continue
        job_file = services.JOB_ROOT / str(job_id) / "job.json"
        job = services.try_read_job(job_file) if job_file.is_file() else None
        if job and str(job.get("status")) in {"queued", "running", "passed", "failed", "canceled"}:
            entry["status"] = str(job["status"])
            entry["job_status_updated_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")

    sources = services._watched_iso_sources(watch)
    source_keys = {str(source).casefold() for source in sources}
    for source in sources:
        key = str(source).casefold()
        present.add(key)
        try:
            stat = source.stat()
        except OSError:
            continue
        fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
        entry = services._reconcile_renamed_watch_entry(
            ledger,
            key=key,
            source=source,
            fingerprint=fingerprint,
            source_keys=source_keys,
            now=now,
        )
        if entry is None or entry.get("fingerprint") != fingerprint:
            if entry is not None and entry.get("status") in {"planned", "queued", "running"}:
                entry["status"] = "source-changed"
                entry["observed_fingerprint"] = fingerprint
                entry["error"] = "Source ISO changed after its conversion job was created"
                entry["present"] = True
                entry["last_seen_epoch"] = now
                continue
            entry = {
                "source": str(source),
                "fingerprint": fingerprint,
                "bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "status": "settling",
                "first_seen_epoch": now,
                "stable_since_epoch": now,
            }
            ledger[key] = entry
        entry["present"] = True
        entry["last_seen_epoch"] = now

        if entry.get("status") == "source-missing":
            entry["status"] = "settling"
            entry["stable_since_epoch"] = now
        elif entry.get("status") == "waiting-for-backup":
            entry["status"] = "settling"
        elif entry.get("status") == "waiting-for-space":
            # Capacity is checked again on each scan. This is a waiting state,
            # not a failed disc and not something the user must reset.
            entry["status"] = "ready"

        if entry.get("status") == "output-exists" and not services.Path(str(entry.get("output"))).exists():
            entry["status"] = "settling"
            entry["stable_since_epoch"] = now

        if entry.get("status") == "settling":
            if now - float(entry.get("stable_since_epoch") or now) < settle_seconds:
                continue
            if not services.iso_is_write_quiet(source):
                entry["status"] = "waiting-for-backup"
                entry["last_busy_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
                continue
            entry.pop("last_busy_at", None)
            entry["status"] = "ready"
            entry["ready_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if entry.get("status") != "ready":
            continue

        generated = services.default_output_for(
            source,
            add_filename_tags=bool(watch.get("add_filename_tags", True)),
            output_format=watch.get("settings",{}).get("output_format", "dvd-hevc"),
        )
        output = (output_dir / generated.name).resolve()
        entry["output"] = str(output)
        if output.exists():
            entry["status"] = "output-exists"
            entry["handled_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
            continue
        output_key = str(output).casefold()
        reservation = reserved_outputs.get(output_key)
        if reservation:
            if services.Path(str(reservation.get("source"))).resolve() == source:
                entry["status"] = str(reservation.get("status") or "queued")
                entry["job_id"] = reservation.get("id")
            else:
                entry["status"] = "output-name-conflict"
                entry["error"] = (
                    f"Another queued source already targets {output}; rename one source or use separate output folders"
                )
            continue
        if runtime_blocked_error is not None:
            entry["status"] = "ready"
            entry["waiting_reason"] = "converter-temporarily-unavailable"
            entry["error"] = runtime_blocked_error
            entry.pop("failed_at", None)
            continue
        if queued_one_this_scan:
            # Keep walking so every ledger entry remains present, but leave
            # later stable discs for the next poll.  This prevents one watcher
            # scan from looking like several discs started together.
            continue
        latest_watch = services.read_json(watch_path)
        if latest_watch.get("status") != "active":
            return latest_watch
        args = services._watch_namespace(watch, source, output)
        prefix = str(watch.get("name_prefix") or "").strip()
        requested = f"{prefix}-{source.stem}" if prefix else source.stem
        try:
            job_path, job = services.prepare_job(
                args, source=source, output=output, requested_name=requested,
                wait_for_slot=False,
            )
            latest_watch = services.read_json(watch_path)
            if latest_watch.get("status") != "active" or services.queue_is_paused():
                job["status"] = "canceled"
                job["canceled_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
                job["ended_at"] = job["canceled_at"]
                job["error"] = "Watched-batch planning completed after discovery was paused or stopped"
                services.save_job(job_path, job)
                if latest_watch.get("status") != "active":
                    return latest_watch
                entry["status"] = "settling"
                entry["stable_since_epoch"] = now
                watch["ledger"] = ledger
                watch["paused_by_queue"] = True
                watch["updated_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
                watch["summary"] = services.watched_batch_summary(watch)
                services.write_json_atomic(watch_path, watch)
                return watch
            if not services.queue_prepared_job(job_path, job):
                entry["status"] = "canceled"
                entry["job_id"] = job["id"]
                entry["error"] = job.get("error")
                continue
            entry["status"] = "queued"
            entry["job_id"] = job["id"]
            entry["queued_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
            entry.pop("error", None)
            entry.pop("waiting_reason", None)
            reserved_outputs[output_key] = job
            services.ensure_dispatcher()
            queued_one_this_scan = True
            watch.pop("runtime_error", None)
            watch.pop("runtime_error_at", None)
        except services.WorkSlotBusy:
            # A watched folder is discovery, not another conversion worker.
            # Leave the disc visibly waiting and retry on the next poll instead
            # of blocking this watcher (and making Stop appear unresponsive).
            entry["status"] = "ready"
            entry["waiting_reason"] = "another-disc-active"
        except services.WorkspaceSpaceLow as exc:
            entry["status"] = "waiting-for-space"
            entry["waiting_reason"] = "workspace-space-low"
            entry["error"] = str(exc)
        except Exception as exc:
            if services.retryable_watcher_planning_error(exc):
                entry["status"] = "ready"
                entry["waiting_reason"] = "converter-temporarily-unavailable"
                entry["error"] = str(exc)
                entry.pop("failed_at", None)
                watch["runtime_error"] = str(exc)
                watch["runtime_error_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
                # This is a watcher-wide problem. Keep discovering/output-
                # checking later ISOs, but do not repeat the same expensive
                # failing preflight for each one in this poll.
                runtime_blocked_error = str(exc)
                continue
            entry["status"] = "planning-failed"
            entry["error"] = str(exc)
            entry["failed_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")

    for key, entry in ledger.items():
        if key not in present:
            entry["present"] = False
            if entry.get("status") == "settling":
                entry["status"] = "source-missing"

    watch["ledger"] = ledger
    watch["last_scan_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
    watch["last_scan_epoch"] = now
    watch["summary"] = services.watched_batch_summary(watch)
    watch["updated_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Do not resurrect a watch stopped while an expensive disc plan was running.
    latest = services.read_json(watch_path)
    if latest.get("status") != "active":
        return latest
    services.write_json_atomic(watch_path, watch)
    return watch


def _spawn_watched_batch(watch_path: Path, *, services) -> int:
    command = [services.sys.executable, "-m", "dvd2hevc_app.frontend", "_watch", str(watch_path)]
    kwargs: dict[str, Any] = {
        "cwd": str(services.ROOT), "stdin": services.subprocess.DEVNULL,
        "stdout": services.subprocess.DEVNULL, "stderr": services.subprocess.DEVNULL,
    }
    kwargs.update(services.hidden_subprocess_kwargs())
    process = services.subprocess.Popen(command, **kwargs)
    watch = services.read_json(watch_path)
    watch["watcher_pid"] = process.pid
    services.write_json_atomic(watch_path, watch)
    return process.pid


def ensure_active_watchers(services) -> list[int]:
    started: list[int] = []
    for path in services.watched_batch_files():
        watch = services.try_read_watched_batch(path)
        if not watch or watch.get("status") != "active":
            continue
        try:
            pid = int(watch.get("watcher_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if not services.process_alive(pid):
            started.append(services._spawn_watched_batch(path))
    return started


def create_watched_batch(
    args: argparse.Namespace,
    *,
    source_dir: Path | None = None,
    output_dir: Path | None = None,
    recursive: bool | None = None,
    resume_queue: bool = True, services) -> tuple[Path, dict[str, Any]]:
    source = (source_dir or services.Path(args.source_dir)).expanduser().resolve()
    output = (output_dir or services.Path(args.output_dir)).expanduser().resolve()
    if not source.is_dir():
        raise services.PipelineError(f"Watched source folder was not found: {source}")
    if output == source:
        raise services.PipelineError("Watched output folder must be different from the source folder")
    output.mkdir(parents=True, exist_ok=True)
    settings = services.resolve_conversion_settings(args)
    vlc_root = services.find_patched_vlc_root(getattr(args, "vlc_root", None))
    if vlc_root is None:
        raise services.PipelineError("Patched VLC was not found; set it up before starting a watched batch")
    services.WATCH_ROOT.mkdir(parents=True, exist_ok=True)
    for path in services.watched_batch_files():
        existing = services.try_read_watched_batch(path)
        if not existing or existing.get("status") != "active":
            continue
        if (
            services.Path(str(existing.get("source_dir"))).resolve() == source
            and services.Path(str(existing.get("output_dir"))).resolve() == output
        ):
            raise services.PipelineError(
                f"This folder pair is already watched by {existing.get('id')}; stop or reset that watch first"
            )
    watch_id = services.make_watched_batch_id(source)
    watch_path = services.WATCH_ROOT / f"{watch_id}.json"
    watch: dict[str, Any] = {
        "schema": "dvd2hevc-watched-batch-v1", "version": services.__version__,
        "id": watch_id, "status": "active",
        "source_dir": str(source), "output_dir": str(output),
        "recursive": bool(getattr(args, "recursive", False) if recursive is None else recursive),
        "poll_seconds": max(5.0, float(getattr(args, "poll_seconds", 15.0) or 15.0)),
        "settle_seconds": max(0.0, float(
            60.0 if getattr(args, "settle_seconds", None) is None else args.settle_seconds
        )),
        "add_filename_tags": bool(getattr(args, "add_filename_tags", True)),
        "label": getattr(args, "label", None),
        "name_prefix": getattr(args, "name_prefix", None),
        "vlc_root": str(vlc_root), "settings": settings, "ledger": {},
        "created_at": services.time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    watch["summary"] = services.watched_batch_summary(watch)
    services.write_json_atomic(watch_path, watch)
    queue_was_paused = services.queue_is_paused()
    cancel_generation = services.current_cancel_generation()
    queue_resumed = False
    if resume_queue and queue_was_paused:
        queue_resumed = services.update_queue_control(
            paused=False,
            reason=f"start-watch:{watch_id}",
            expected_cancel_generation=cancel_generation,
        )
    if queue_resumed:
        watch["queue_resumed_at_start"] = True
        watch.pop("paused_by_queue", None)
        services.write_json_atomic(watch_path, watch)
    try:
        services._spawn_watched_batch(watch_path)
    except BaseException as exc:
        if queue_resumed:
            services.update_queue_control(paused=True, reason=f"watch-start-failed:{watch_id}")
        watch["status"] = "failed"
        watch["error"] = f"Could not start watched-batch helper: {exc}"
        watch["stopped_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
        services.write_json_atomic(watch_path, watch)
        raise
    watch = services.read_json(watch_path)
    return watch_path, watch


def stop_watched_batch(identifier: str, *, services) -> dict[str, Any]:
    path, watch = services.resolve_watched_batch(identifier)
    watch["status"] = "stopped"
    watch["stopped_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
    services.write_json_atomic(path, watch)
    return watch


def reset_watched_batch(identifier: str, *, services) -> tuple[Path, dict[str, Any]]:
    old_path, old = services.resolve_watched_batch(identifier)
    old["status"] = "stopped"
    old["stopped_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
    old["replaced_by_reset"] = True
    services.write_json_atomic(old_path, old)
    args = services._watch_namespace(
        old,
        services.Path(str(old["source_dir"])) / "watched-source.iso",
        services.Path(str(old["output_dir"])) / "watched-output.iso",
    )
    args.source_dir = old["source_dir"]
    args.output_dir = old["output_dir"]
    args.recursive = bool(old.get("recursive"))
    args.poll_seconds = float(old.get("poll_seconds") or 15.0)
    args.settle_seconds = float(old.get("settle_seconds") or 60.0)
    args.name_prefix = old.get("name_prefix")
    return services.create_watched_batch(args)


def resume_watched_batch(identifier: str, *, services) -> dict[str, Any]:
    """Restart a watcher in place while retaining its discovery ledger."""
    path, watch = services.resolve_watched_batch(identifier)
    try:
        watcher_pid = int(watch.get("watcher_pid") or 0)
    except (TypeError, ValueError):
        watcher_pid = 0
    if watch.get("status") == "active" and services.process_alive(watcher_pid):
        return watch

    cancel_generation = services.current_cancel_generation()
    if services.queue_is_paused():
        services.update_queue_control(
            paused=False,
            reason=f"resume-watch:{watch.get('id')}",
            expected_cancel_generation=cancel_generation,
        )
    watch["status"] = "active"
    watch["watcher_pid"] = None
    watch["resumed_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
    recovered = 0
    for entry in (watch.get("ledger") or {}).values():
        if (
            isinstance(entry, dict)
            and entry.get("status") == "planning-failed"
            and services.retryable_watcher_planning_error(str(entry.get("error") or ""))
        ):
            entry["status"] = "ready"
            entry["waiting_reason"] = "watcher-restarted-after-runtime-repair"
            entry.pop("failed_at", None)
            recovered += 1
    if recovered:
        watch["recovered_runtime_entries"] = recovered
    for key in (
        "error", "stopped_at", "stopped_by", "paused_by_queue",
        "runtime_error", "runtime_error_at",
    ):
        watch.pop(key, None)
    services.write_json_atomic(path, watch)
    try:
        services._spawn_watched_batch(path)
    except BaseException as exc:
        watch = services.read_json(path)
        watch["status"] = "failed"
        watch["error"] = f"Could not resume watched-batch helper: {exc}"
        watch["stopped_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
        services.write_json_atomic(path, watch)
        raise
    return services.read_json(path)


def watcher_main(watch_path_value: str, *, services) -> int:
    watch_path = services.Path(watch_path_value).expanduser().resolve()
    try:
        watch = services.read_json(watch_path)
        watch["watcher_pid"] = services.os.getpid()
        services.write_json_atomic(watch_path, watch)
        while True:
            watch = services.read_json(watch_path)
            if watch.get("status") != "active":
                return 0
            services.scan_watched_batch(watch_path)
            deadline = services.time.time() + max(5.0, float(watch.get("poll_seconds") or 15.0))
            while services.time.time() < deadline:
                services.time.sleep(min(1.0, max(0.0, deadline - services.time.time())))
                if services.read_json(watch_path).get("status") != "active":
                    return 0
    except BaseException as exc:
        try:
            watch = services.read_json(watch_path)
            watch["status"] = "failed"
            watch["error"] = str(exc)
            watch["stopped_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
            services.write_json_atomic(watch_path, watch)
        except BaseException:
            pass
        return 1


def cmd_watch_folder(args: argparse.Namespace, *, services) -> int:
    path, watch = services.create_watched_batch(args)
    print("DVD2HEVC watched batch started")
    print(f"Watch: {watch['id']}")
    print(f"Source: {watch['source_dir']}")
    print(f"Output: {watch['output_dir']}")
    print(
        f"Polling every {watch['poll_seconds']:g}s; each ISO must remain unchanged "
        f"for {watch['settle_seconds']:g}s before planning"
    )
    if watch.get("queue_resumed_at_start"):
        print("The paused global queue was resumed so this watched batch can run.")
    print(f"Ledger: {path}")
    print(f"Stop: python dvd2hevc.py stop-watch {watch['id']}")
    return 0


def cmd_watches(args: argparse.Namespace, *, services) -> int:
    watches = [
        watch for path in services.watched_batch_files()
        if (watch := services.try_read_watched_batch(path))
    ]
    if not watches:
        print("No watched batches were found.")
        return 0
    limit = max(1, int(getattr(args, "limit", 20) or 20))
    for watch in watches[:limit]:
        summary = services.watched_batch_summary(watch)
        try:
            pid = int(watch.get("watcher_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        state = str(watch.get("status") or "unknown")
        if state == "active" and not services.process_alive(pid):
            state = "interrupted"
        elif state == "active" and services.queue_is_paused():
            state = "paused"
        print(
            f"{watch.get('id')}  {state}  found={summary['detected']} "
            f"active={summary['active']} waiting={summary['pending']} done={summary['done']} "
            f"attention={summary['attention']}\n  {watch.get('source_dir')} -> {watch.get('output_dir')}"
        )
    return 0


def cmd_stop_watch(args: argparse.Namespace, *, services) -> int:
    watch = services.stop_watched_batch(args.watch)
    print(f"Stopped watched batch {watch['id']}. Existing conversion jobs are not canceled.")
    return 0


def cmd_reset_watch(args: argparse.Namespace, *, services) -> int:
    _path, watch = services.reset_watched_batch(args.watch)
    print(f"Started fresh watched batch {watch['id']} with an empty discovery ledger.")
    print("Existing output ISOs are kept and will be marked as already handled.")
    return 0


def cmd_resume_watch(args: argparse.Namespace, *, services) -> int:
    watch = services.resume_watched_batch(args.watch)
    print(f"Resumed watched batch {watch['id']} with its existing discovery ledger.")
    print(
        f"Remembered: {services.watched_batch_summary(watch)['detected']} ISO(s); "
        "completed outputs will not be queued again."
    )
    return 0
