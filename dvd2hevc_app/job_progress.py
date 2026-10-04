"""Focused workflow component; explicit services preserve the public facade."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def pipeline_status(job: dict[str, Any], *, services) -> dict[str, Any]:
    path = services.Path(job["work_root"]) / "status.json"
    if not path.is_file():
        return {}
    try:
        return services.read_json(path)
    except services.PipelineError:
        return {}


def stage_percent(stage: str, state: str, *, services) -> float:
    if state == "passed" or stage == "complete":
        return 100.0
    if state in {"planned", "queued"} and stage in {"", state, "waiting", "waiting for queue"}:
        return 0.0
    ordered = (
        ("initial", 0.0),
        ("full-source", 0.0),
        ("resolve-quality", 0.45),
        ("resolve-audio", 0.48),
        ("plan-full-disc", services.PIPELINE_VIDEO_END),
        ("plan-compact-audio", services.PIPELINE_BASE_STAGE_END),
        ("plan-", services.PIPELINE_SETUP_END),
        ("convert-", services.PIPELINE_SETUP_END),
        ("ordinary-and-menu", services.PIPELINE_SETUP_END),
        ("stage-all", services.PIPELINE_COMPACT_PLAN_END),
        ("verify-all", services.PIPELINE_BASE_STAGE_END),
        ("compact-audio", services.PIPELINE_BASE_STAGE_END),
        ("compact-", services.PIPELINE_AUDIO_LAYOUT_END),
        ("relocate-", services.PIPELINE_AUDIO_LAYOUT_END),
        ("stage-vts", services.PIPELINE_AUDIO_LAYOUT_END),
        ("author-final", services.PIPELINE_COMPACT_DOMAINS_END),
        ("verify-final", services.PIPELINE_AUTHOR_END),
        ("audit-", services.PIPELINE_VERIFY_END),
        ("vlc-title", services.PIPELINE_AUDIT_END),
        ("vlc-menu", 99.0),
    )
    for prefix, percent in ordered:
        if stage.startswith(prefix):
            return percent
    # A failure should retain the point reached instead of making a nearly
    # complete conversion appear to jump backwards to zero.
    return 0.0


def progress_bar(percent: float, width: int=30, *, services) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    if percent < 100.0 and width > 0:
        # Rounding 97-99% into every cell looks indistinguishable from done.
        # Reserve the completely filled visual for an actual 100% result.
        filled = min(filled, width - 1)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def nested_progress_detail(job: dict[str, Any], *, services) -> str | None:
    work = services.Path(job["work_root"])
    candidates = list((work / "physical").glob("vts*/status.json"))
    candidates.append(work / "ordinary-and-menus" / "status.json")
    running: list[dict[str, services.Any]] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            value = services.read_json(path)
        except services.PipelineError:
            continue
        if value.get("state") == "running":
            running.append(value)
    if not running:
        return None
    current = max(running, key=lambda value: str(value.get("updated") or ""))
    if current.get("schema") == "dvd2hevc-phase7-vts-status-v0":
        vts = int(current.get("vts") or 0)
        completed = int(current.get("completed_tasks") or 0)
        total = int(current.get("total_tasks") or 0)
        reports = work / "physical" / f"vts{vts:02d}" / "tasks"
        completed = max(
            completed,
            sum(1 for _ in reports.glob("task-*/interleaved-cell-report.json")),
        )
        return (
            f"active VTS {vts}: "
            f"{min(completed, total)}/{total} physical cells"
        )
    return str(current.get("message") or current.get("step") or "") or None


def progress_events(job: dict[str, Any], *, services) -> list[dict[str, Any]]:
    """Read append-only lane events without depending on a live child process."""
    work = services.Path(job["work_root"])
    paths = [services.Path(job["log"]), work / "compact" / "audio" / "progress.jsonl"]
    events: list[dict[str, services.Any]] = []
    seen: set[tuple[services.Any, ...]] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = services.read_progress_log(path, services.PROGRESS_PREFIX).splitlines()
        except OSError:
            continue
        for line in lines:
            marker = line.find(services.PROGRESS_PREFIX)
            if marker < 0:
                continue
            try:
                event = services.json.loads(line[marker + len(services.PROGRESS_PREFIX) :])
            except services.json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("lane") not in {"video", "audio", "mux"}:
                continue
            identity = (
                event.get("timestamp"), event.get("lane"), event.get("event"),
                event.get("task"), event.get("current"), event.get("total"),
            )
            if identity not in seen:
                seen.add(identity)
                events.append(event)
    return sorted(events, key=lambda item: float(item.get("timestamp") or 0))


def measured_progress(event: dict[str, Any], *, services) -> tuple[float, str] | None:
    """Return a percentage and human-readable measurement for one event."""
    try:
        total = float(event.get("total") or 0)
        current = max(0.0, min(total, float(event.get("current") or 0)))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    percent = 100.0 * current / total
    if event.get("unit") == "ticks":
        def clock(ticks: float) -> str:
            seconds = max(0, round(ticks / 90_000))
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"

        detail = f"{clock(current)}/{clock(total)}"
    else:
        current_text = f"{current:g}"
        total_text = f"{total:g}"
        detail = f"{current_text}/{total_text}"
    return percent, detail


def _compact_domain_fraction(events: list[dict[str, Any]], *, services) -> float:
    """Return one monotonic fraction across raw writing and compact staging."""
    fraction = 0.0
    for event in events:
        if event.get("lane") != "mux":
            continue
        measurement = services.measured_progress(event)
        if measurement is None:
            continue
        value = measurement[0] / 100.0
        if event.get("task") == "compact-domain-batch":
            # Raw VOB writing accounts for the first three quarters of the
            # phase; control-file rewrites and staging complete the last one.
            fraction = max(fraction, 0.75 * value)
        elif event.get("task") == "compact-domains":
            fraction = max(fraction, value)
    return min(1.0, fraction)


def _latest_lane_detail(values: list[dict[str, Any]], lane: str, *, services) -> str:
    if not values:
        return "waiting"
    latest = values[-1]
    task = str(latest.get("task") or lane)
    measured = next(
        (
            item for item in reversed(values)
            if item.get("task") == latest.get("task") and services.measured_progress(item) is not None
        ),
        None,
    )
    if measured is not None:
        _percent, amount = services.measured_progress(measured)  # type: ignore[misc]
        phase = str(latest.get("phase") or latest.get("event") or "working")
        return f"{amount}  {phase}: {task}"
    return f"{latest.get('event') or 'working'}: {task}"


def _video_work_weights(job: dict[str, Any], status: dict[str, Any], *, services) -> tuple[dict[int, float], list[float]]:
    try:
        plan = services.read_json(services.Path(job["plan"]))
        physical_vts = {int(value) for value in status.get("physical_vts") or []}
        physical = {
            int(row["vts"]): max(1.0, float(row.get("title_vobus") or 0) * 0.48)
            for row in plan.get("vts") or []
            if int(row.get("vts") or 0) in physical_vts
        }
        ordinary: list[float] = []
        for task in plan.get("title_tasks") or []:
            if task.get("status") == "ready" and int(task.get("vts") or 0) not in physical_vts:
                ordinary.append(max(1.0, float(task.get("duration_seconds") or 0)))
        for task in plan.get("menu_tasks") or []:
            ordinary.append(max(1.0, float(task.get("vobus") or 0) * 0.48))
        return physical, ordinary
    except (KeyError, services.PipelineError, OSError, TypeError, ValueError):
        return {}, []


def _physical_vts_fractions(job: dict[str, Any], physical_weights: dict[int, float], events: list[dict[str, Any]], *, services) -> tuple[dict[int, float], dict[int, str]]:
    """Measure durable, per-cell progress inside each physical VTS encode."""
    done_vts = {
        int(match.group(1))
        for event in events
        if event.get("lane") == "video"
        and event.get("event") == "done"
        and (match := services.re.match(r"convert-vts(\d+)-physical$", str(event.get("task") or "")))
    }
    fractions: dict[int, float] = {}
    details: dict[int, str] = {}
    work = services.Path(job["work_root"])
    for vts in physical_weights:
        if vts in done_vts:
            fractions[vts] = 1.0
            details[vts] = "complete"
            continue

        task_name = f"convert-vts{vts:02d}-physical"
        event_fraction = max(
            (
                services.measured_progress(event)[0] / 100.0
                for event in events
                if event.get("lane") == "video"
                and event.get("task") == task_name
                and event.get("scope") == "video-task-duration"
                and services.measured_progress(event) is not None
            ),
            default=0.0,
        )
        fraction = event_fraction
        status_path = work / "physical" / f"vts{vts:02d}" / "status.json"
        try:
            nested = services.read_json(status_path)
        except (services.PipelineError, OSError):
            nested = {}
        try:
            total = max(0, int(nested.get("total_tasks") or 0))
            recorded_completed = max(0, int(nested.get("completed_tasks") or 0))
        except (TypeError, ValueError):
            total = recorded_completed = 0
        # Completed task reports are retained across cancellation/resume.  They
        # keep the displayed high-water mark intact even if a restarted worker
        # briefly rewrites its live counter from zero.
        reports = work / "physical" / f"vts{vts:02d}" / "tasks"
        completed_reports = sum(1 for _ in reports.glob("task-*/interleaved-cell-report.json"))
        completed = max(recorded_completed, completed_reports)
        if str(nested.get("state") or "") == "passed":
            fraction = 1.0
        elif total > 0:
            fraction = max(fraction, min(1.0, completed / total))
            details[vts] = f"{min(completed, total)}/{total} physical cells"
        fractions[vts] = min(1.0, max(0.0, fraction))
        if vts not in details:
            details[vts] = "waiting" if fraction <= 0 else f"{fraction * 100.0:.1f}%"
    return fractions, details


def _ordinary_video_fraction(job: dict[str, Any], status: dict[str, Any], events: list[dict[str, Any]], *, services) -> tuple[float, str] | None:
    nested_path = services.Path(job["work_root"]) / "ordinary-and-menus" / "status.json"
    try:
        nested = services.read_json(nested_path)
        completed = max(0, int(nested.get("completed_video_tasks") or 0))
        total = max(0, int(nested.get("total_video_tasks") or 0))
    except (services.PipelineError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    step = str(nested.get("step") or "")
    expected_task = step[len("convert-"):] if step.startswith("convert-") else ""
    measured = next(
        (
            event for event in reversed(events)
            if event.get("lane") == "video"
            and event.get("scope") == "video-task-duration"
            and (not expected_task or event.get("task") == expected_task)
            and services.measured_progress(event) is not None
        ),
        None,
    )
    active_fraction = services.measured_progress(measured)[0] / 100.0 if measured else 0.0
    physical_weights, weights = services._video_work_weights(job, status)
    physical_total = sum(physical_weights.values())
    if len(weights) == total and sum(weights) > 0:
        completed_weight = sum(weights[: min(completed, total)])
        active_weight = weights[completed] * active_fraction if completed < total else 0.0
        fraction = min(
            1.0,
            (physical_total + completed_weight + active_weight)
            / (physical_total + sum(weights)),
        )
    else:
        fraction = min(1.0, (completed + active_fraction) / total)
    detail = services._latest_lane_detail(
        [event for event in events if event.get("lane") == "video"], "video"
    )
    return fraction, detail


def lane_progress_snapshot(job: dict[str, Any], status: dict[str, Any] | None=None, *, services) -> dict[str, tuple[float, str]]:
    """Return cumulative, monotonic video/audio/mux lane progress."""
    status = status or {}
    events = services.progress_events(job)
    stage = str(status.get("stage") or "")
    state = str(job.get("status") or "")
    lanes = {
        lane: [event for event in events if event.get("lane") == lane]
        for lane in ("video", "audio", "mux")
    }
    video_finished = state == "passed" or stage.startswith((
        "stage-all", "verify-all", "plan-full", "compact-audio", "plan-compact-audio",
        "compact-", "relocate-", "stage-vts", "author-", "verify-final", "audit-", "vlc-", "complete",
    ))
    video_value = 100.0 if video_finished else 0.0
    video_detail = "complete" if video_finished else services._latest_lane_detail(lanes["video"], "video")
    if stage.startswith("ordinary-and-menu"):
        ordinary = services._ordinary_video_fraction(job, status, events)
        if ordinary is not None:
            video_value, video_detail = ordinary[0] * 100.0, ordinary[1]
    elif not video_finished:
        physical_weights, ordinary_weights = services._video_work_weights(job, status)
        total_weight = sum(physical_weights.values()) + sum(ordinary_weights)
        if physical_weights and total_weight > 0:
            fractions, physical_details = services._physical_vts_fractions(
                job, physical_weights, events,
            )
            completed_weight = sum(
                weight * fractions.get(vts, 0.0)
                for vts, weight in physical_weights.items()
            )
            active_match = services.re.match(r"convert-vts(\d+)-physical$", stage)
            active_vts = int(active_match.group(1)) if active_match else 0
            if active_vts in physical_details:
                video_detail = physical_details[active_vts]
            video_value = 100.0 * completed_weight / total_weight
        else:
            measurements = [
                services.measured_progress(event)[0]
                for event in lanes["video"]
                if event.get("scope") == "video-task-duration" and services.measured_progress(event)
            ]
            video_value = max(measurements, default=0.0)

    configured_audio_mode = (job.get("settings") or {}).get("audio_mode")
    audio_mode = str(
        configured_audio_mode
        or ("compact-stereo" if lanes["audio"] else "passthrough")
    )
    audio_finished = state == "passed" or (
        audio_mode == "compact-stereo" and stage.startswith((
            "plan-compact-audio", "compact-", "relocate-", "stage-vts", "author-",
            "verify-final", "audit-", "vlc-", "complete",
        )) and not stage.startswith("compact-audio")
    )
    if audio_mode == "passthrough":
        audio_value, audio_detail = 100.0, "passthrough"
    elif audio_finished:
        audio_value, audio_detail = 100.0, "complete"
    else:
        global_audio = [
            services.measured_progress(event)[0]
            for event in lanes["audio"]
            if event.get("scope") == "audio-batch-cells" and services.measured_progress(event)
        ]
        if global_audio:
            audio_value = max(global_audio)
        else:
            legacy = [
                services.measured_progress(event)[0]
                for event in lanes["audio"]
                if event.get("event") in {"batch-progress", "batch-done"}
                and services.measured_progress(event)
            ]
            audio_value = max(legacy, default=0.0)
        audio_detail = services._latest_lane_detail(lanes["audio"], "audio")

    def mux_stage_value() -> float:
        if state == "passed" or stage == "complete": return 100.0
        if stage.startswith("vlc-"): return 99.0
        if stage.startswith("audit-"): return 97.0
        if stage.startswith("verify-final"): return 95.0
        if stage.startswith("author-"): return 90.0
        if stage.startswith(("compact-", "relocate-", "stage-vts")) and not stage.startswith("compact-audio"):
            return 45.0 + 40.0 * services._compact_domain_fraction(lanes["mux"])
        if stage.startswith("plan-compact-audio"): return 45.0
        if stage.startswith("compact-audio"): return 40.0
        if stage.startswith("verify-all"): return 35.0
        if stage.startswith("stage-all"): return 30.0
        if stage.startswith("plan-full"): return 25.0
        if stage.startswith("ordinary-and-menu"): return 20.0
        if stage.startswith("convert-"): return 15.0
        if stage.startswith("plan-"): return 10.0
        if stage.startswith("resolve-audio"): return 8.0
        if stage.startswith("resolve-quality"): return 5.0
        if stage.startswith("full-source"): return 2.0
        return 0.0

    mux_value = mux_stage_value()
    mux_milestones = {
        "resolve-quality-policy": 5.0, "resolve-audio-policy": 8.0,
        "plan-full-disc-compaction": 25.0, "stage-all-compact-base": 30.0,
        "verify-all-compact-base": 35.0, "author-final-iso": 90.0,
        "verify-final-iso": 95.0, "audit-css-safe-psm": 97.0,
    }
    for event in lanes["mux"]:
        if event.get("event") in {"done", "batch-done"}:
            mux_value = max(mux_value, mux_milestones.get(str(event.get("task")), 0.0))
    compact_fraction = services._compact_domain_fraction(lanes["mux"])
    if compact_fraction > 0:
        mux_value = max(mux_value, 45.0 + 40.0 * compact_fraction)
    mux_detail = services._latest_lane_detail(lanes["mux"], "mux")
    if job.get('settings',{}).get('output_format') == 'uhd-bd':
        mux_value = min(65.0,mux_value * 65.0/85.0)
        uhd = _uhd_output_progress(events)
        if uhd:
            mux_value = max(mux_value,65.0 + 35.0*(uhd-80.0)/20.0)
            last = next(e for e in reversed(events) if e.get('scope') == 'uhd-output')
            mux_detail = str(last['task']).replace('uhd-','UHD-BD ').replace('-',' ')
    return {
        "video": (round(min(100.0, video_value), 1), video_detail),
        "audio": (round(min(100.0, audio_value), 1), audio_detail),
        "mux": (round(min(100.0, mux_value), 1), mux_detail),
    }


def task_lane_lines(job: dict[str, Any], *, width: int=16, status: dict[str, Any] | None=None, services) -> list[str]:
    snapshot = services.lane_progress_snapshot(job, status)
    return [
        f"{lane.title():5}: {services.progress_bar(snapshot[lane][0], width)} {snapshot[lane][1]}"
        for lane in ("video", "audio", "mux")
    ]


def _pipeline_percent_current(job: dict[str, Any], status: dict[str, Any], *, services) -> float:
    """Interpolate coarse pipeline stages with real bounded-task counters."""
    state = str(job.get("status") or "")
    stage = str(status.get("stage") or state)
    base = services.stage_percent(stage, state)
    physical_match = services.re.match(r"convert-vts(\d+)-physical$", stage)
    if physical_match:
        events = services.progress_events(job)
        physical_weights, ordinary_weights = services._video_work_weights(job, status)
        total_video_weight = sum(physical_weights.values()) + sum(ordinary_weights)
        if physical_weights and total_video_weight > 0:
            fractions, _details = services._physical_vts_fractions(job, physical_weights, events)
            completed_video_weight = sum(
                weight * fractions.get(vts, 0.0)
                for vts, weight in physical_weights.items()
            )
            fraction = completed_video_weight / total_video_weight
            return round(
                services.PIPELINE_SETUP_END
                + (services.PIPELINE_VIDEO_END - services.PIPELINE_SETUP_END) * fraction,
                1,
            )
    if stage.startswith(("compact-", "relocate-", "stage-vts")) and not stage.startswith("compact-audio"):
        fraction = services._compact_domain_fraction(services.progress_events(job))
        if fraction > 0:
            return round(
                services.PIPELINE_AUDIO_LAYOUT_END
                + (services.PIPELINE_COMPACT_DOMAINS_END - services.PIPELINE_AUDIO_LAYOUT_END) * fraction,
                1,
            )
    if stage.startswith("ordinary-and-menu"):
        nested_path = services.Path(job["work_root"]) / "ordinary-and-menus" / "status.json"
        try:
            nested = services.read_json(nested_path)
        except services.PipelineError:
            nested = {}
        try:
            completed = max(0, int(nested.get("completed_video_tasks") or 0))
            total = max(0, int(nested.get("total_video_tasks") or 0))
        except (TypeError, ValueError):
            completed = total = 0
        if total > 0:
            active_fraction = 0.0
            step = str(nested.get("step") or "")
            expected_task = step[len("convert-"):] if step.startswith("convert-") else ""
            measured = next(
                (
                    event for event in reversed(services.progress_events(job))
                    if event.get("lane") == "video"
                    and event.get("scope") == "video-task-duration"
                    and (not expected_task or event.get("task") == expected_task)
                    and services.measured_progress(event) is not None
                ),
                None,
            )
            if measured:
                active_fraction = services.measured_progress(measured)[0] / 100.0  # type: ignore[index]
            physical_weights, weights = services._video_work_weights(job, status)
            physical_weight = sum(physical_weights.values())
            if len(weights) == total and sum(weights) > 0:
                completed_weight = sum(weights[: min(completed, total)])
                active_weight = weights[completed] * active_fraction if completed < total else 0.0
                fraction = min(
                    1.0,
                    (physical_weight + completed_weight + active_weight)
                    / (physical_weight + sum(weights)),
                )
            else:
                fraction = min(1.0, (completed + active_fraction) / total)
            return round(
                services.PIPELINE_SETUP_END
                + (services.PIPELINE_VIDEO_END - services.PIPELINE_SETUP_END) * fraction,
                1,
            )

    # Compact audio is deliberately prepared alongside video. Its lane bar may
    # advance independently, but it must not drag the overall wall-clock bar
    # forward merely because overlapping work happened to finish early.
    return base


def _pipeline_event_floor(job: dict[str, Any], *, services) -> float:
    """Recover the greatest completed pipeline milestone from durable events."""
    floor = 0.0
    milestones = {
        "full-source-decryption-scan": 0.45,
        "resolve-quality-policy": 0.48,
        "resolve-audio-policy": services.PIPELINE_SETUP_END,
        "ordinary-and-menu-domains": services.PIPELINE_VIDEO_END,
        "plan-full-disc-compaction": services.PIPELINE_COMPACT_PLAN_END,
        "stage-all-compact-base": services.PIPELINE_BASE_STAGE_END,
        "verify-all-compact-base": services.PIPELINE_BASE_STAGE_END,
        "compact-stereo-batch": services.PIPELINE_BASE_STAGE_END,
        "author-final-iso": services.PIPELINE_AUTHOR_END,
        "verify-final-iso": services.PIPELINE_VERIFY_END,
        "audit-css-safe-psm": services.PIPELINE_AUDIT_END,
    }
    events = services.progress_events(job)
    for event in events:
        if event.get("event") in {"done", "batch-done"}:
            floor = max(floor, milestones.get(str(event.get("task")), 0.0))
    compact_fraction = services._compact_domain_fraction(events)
    if compact_fraction > 0:
        floor = max(
            floor,
            services.PIPELINE_AUDIO_LAYOUT_END
            + (
                services.PIPELINE_COMPACT_DOMAINS_END - services.PIPELINE_AUDIO_LAYOUT_END
            ) * compact_fraction,
        )
    return round(min(100.0, floor), 1)


def _uhd_output_progress(events):
    bounds = {'uhd-source-check':(80,81), 'uhd-author':(81,90),
              'uhd-audit':(90,94), 'uhd-author-iso':(94,96),
              'uhd-verify-iso':(96,98), 'uhd-stock-vlc':(98,99),
              'uhd-source-stable':(99,99.5), 'uhd-complete':(99.5,99.9)}
    value=0.0
    for event in events:
        if event.get('scope') != 'uhd-output': continue
        low,high = bounds.get(event.get('task'),(0,0))
        completed,total = float(event.get('completed') or 0),float(event.get('total') or 1)
        value=max(value,low+(high-low)*min(1.0,max(0.0,completed/total)))
    return value

def pipeline_percent(job: dict[str, Any], status: dict[str, Any], *, services) -> float:
    """Return durable high-water progress; completed work never disappears."""
    if str(job.get("status") or "") == "passed":
        return 100.0
    percent = max(services._pipeline_percent_current(job, status), services._pipeline_event_floor(job))
    if job.get('settings', {}).get('output_format') != 'uhd-bd': return percent
    # Reserve the final 20% for authoring, full media audit, UDF and playback.
    legacy = min(80.0, percent * 80.0 / services.PIPELINE_COMPACT_DOMAINS_END)
    return round(max(legacy,_uhd_output_progress(services.progress_events(job))),1)
