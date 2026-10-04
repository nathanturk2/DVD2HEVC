"""Queue dispatcher: explicit services preserve the public facade and test seams."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def _exclusive_lock_age(path: Path, *, services) -> float:
    try:
        return max(0.0, services.time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def _unlink_lock_file(path: Path, *, services) -> None:
    """Remove a lock despite brief Windows sharing conflicts from readers."""
    last_error: OSError | None = None
    for _attempt in range(100):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last_error = exc
            services.time.sleep(0.02)
    if last_error is not None:
        raise last_error


def acquire_active_work_lock(owner: str, *, wait: bool = True, progress: Callable[[str], None] | None = None, services) -> str:
    """Acquire the one global work slot, optionally returning promptly if busy."""
    services.JOB_ROOT.mkdir(parents=True, exist_ok=True)
    token = f"{services.os.getpid()}-{services.time.time_ns()}"
    payload = services.json.dumps(
        {
            "schema": "dvd2hevc-active-work-lock-v1",
            "pid": services.os.getpid(),
            "token": token,
            "owner": owner,
            "acquired_at": services.time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
    ).encode("utf-8")
    while True:
        try:
            descriptor = services.os.open(services.ACTIVE_WORK_LOCK, services.os.O_CREAT | services.os.O_EXCL | services.os.O_WRONLY)
        except FileExistsError:
            if progress:
                progress("Waiting for the current disc before planning…")
                progress = None
            try:
                current = services.read_json(services.ACTIVE_WORK_LOCK)
                holder_alive = services.process_alive(int(current.get("pid") or 0))
            except (services.PipelineError, TypeError, ValueError):
                # The owner may be between O_EXCL creation and its first write.
                # Never steal a freshly created, temporarily unreadable lock.
                if services._exclusive_lock_age(services.ACTIVE_WORK_LOCK) < 5.0:
                    if not wait:
                        raise services.WorkSlotBusy("Another disc is currently being planned or converted")
                    services.time.sleep(0.2)
                    continue
                holder_alive = False
            if holder_alive:
                if not wait:
                    raise services.WorkSlotBusy("Another disc is currently being planned or converted")
                services.time.sleep(0.5)
                continue
            try:
                services.ACTIVE_WORK_LOCK.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                services.time.sleep(0.2)
            continue
        try:
            services.os.write(descriptor, payload)
            services.os.fsync(descriptor)
        finally:
            services.os.close(descriptor)
        return token


def release_active_work_lock(token: str, *, services) -> None:
    """Release the global work slot only when this caller still owns it."""
    try:
        current = services.read_json(services.ACTIVE_WORK_LOCK)
    except services.PipelineError:
        return
    if current.get("token") != token:
        return
    services._unlink_lock_file(services.ACTIVE_WORK_LOCK)


def live_dispatcher_pid(services) -> int | None:
    """Return the live lock owner, preferring truth over advisory state."""
    if services.DISPATCHER_LOCK.is_file():
        try:
            pid = int(services.read_json(services.DISPATCHER_LOCK).get("pid") or 0)
        except (services.PipelineError, TypeError, ValueError):
            pid = 0
        if services.process_alive(pid):
            return pid
    if services.DISPATCHER_STATE.is_file() and services._exclusive_lock_age(services.DISPATCHER_STATE) <= 10.0:
        try:
            state = services.read_json(services.DISPATCHER_STATE)
            if state.get("stopped_at"):
                return None
            pid = int(state.get("pid") or 0)
        except (services.PipelineError, TypeError, ValueError):
            return None
        if services.process_alive(pid):
            return pid
    return None


def dispatcher_is_running(services) -> bool:
    return services.live_dispatcher_pid() is not None


def acquire_dispatcher_start_lock(services) -> str:
    """Serialize the short check-and-spawn sequence used by queue producers."""
    services.JOB_ROOT.mkdir(parents=True, exist_ok=True)
    token = f"{services.os.getpid()}-{services.time.time_ns()}"
    payload = services.json.dumps({"pid": services.os.getpid(), "token": token}).encode("utf-8")
    while True:
        try:
            descriptor = services.os.open(services.DISPATCHER_START_LOCK, services.os.O_CREAT | services.os.O_EXCL | services.os.O_WRONLY)
        except FileExistsError:
            try:
                current = services.read_json(services.DISPATCHER_START_LOCK)
                holder_alive = services.process_alive(int(current.get("pid") or 0))
            except (services.PipelineError, TypeError, ValueError):
                if services._exclusive_lock_age(services.DISPATCHER_START_LOCK) < 5.0:
                    services.time.sleep(0.1)
                    continue
                holder_alive = False
            if holder_alive:
                services.time.sleep(0.1)
                continue
            try:
                services.DISPATCHER_START_LOCK.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                services.time.sleep(0.1)
            continue
        try:
            services.os.write(descriptor, payload)
            services.os.fsync(descriptor)
        finally:
            services.os.close(descriptor)
        return token


def release_dispatcher_start_lock(token: str, *, services) -> None:
    try:
        current = services.read_json(services.DISPATCHER_START_LOCK)
    except services.PipelineError:
        return
    if current.get("token") != token:
        return
    services._unlink_lock_file(services.DISPATCHER_START_LOCK)


def ensure_dispatcher(services) -> int:
    services.JOB_ROOT.mkdir(parents=True, exist_ok=True)
    pid = services.live_dispatcher_pid()
    if pid is not None:
        return pid
    token = services.acquire_dispatcher_start_lock()
    try:
        # Another queue producer may have completed startup while this caller
        # waited for the short spawn lock.
        pid = services.live_dispatcher_pid()
        if pid is not None:
            return pid
        command = [services.sys.executable, "-m", "dvd2hevc_app.frontend", "_dispatch"]
        kwargs: dict[str, Any] = {
            "cwd": str(services.ROOT),
            "stdin": services.subprocess.DEVNULL,
            "stdout": services.subprocess.DEVNULL,
            "stderr": services.subprocess.DEVNULL,
        }
        kwargs.update(services.hidden_subprocess_kwargs())
        process = services.subprocess.Popen(command, **kwargs)
        services.write_json_atomic(
            services.DISPATCHER_STATE,
            {
                "schema": "dvd2hevc-dispatcher-v1",
                "pid": process.pid,
                "started_at": services.time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )
        return process.pid
    finally:
        services.release_dispatcher_start_lock(token)


def acquire_dispatcher_lock(services) -> int | None:
    services.JOB_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            descriptor = services.os.open(services.DISPATCHER_LOCK, services.os.O_CREAT | services.os.O_EXCL | services.os.O_WRONLY)
            break
        except FileExistsError:
            try:
                value = services.read_json(services.DISPATCHER_LOCK)
                if services.process_alive(int(value.get("pid") or 0)):
                    return None
            except (services.PipelineError, TypeError, ValueError):
                if services._exclusive_lock_age(services.DISPATCHER_LOCK) < 5.0:
                    services.time.sleep(0.1)
                    continue
            try:
                services.DISPATCHER_LOCK.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return None
    services.os.write(descriptor, services.json.dumps({"pid": services.os.getpid()}).encode("utf-8"))
    services.os.fsync(descriptor)
    return descriptor


def dispatcher_main(services) -> int:
    descriptor = services.acquire_dispatcher_lock()
    if descriptor is None:
        return 0
    services.write_json_atomic(
        services.DISPATCHER_STATE,
        {
            "schema": "dvd2hevc-dispatcher-v1",
            "pid": services.os.getpid(),
            "started_at": services.time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )
    try:
        services.recover_jobs_interrupted_by_restart()
        idle_checks = 0
        while idle_checks < 2:
            if services.queue_is_paused():
                idle_checks = 0
                services.time.sleep(1)
                continue
            jobs = services.queued_jobs()
            if not jobs:
                idle_checks += 1
                services.time.sleep(1)
                continue
            idle_checks = 0
            job_path, job = jobs[0]
            try:
                services.run_job(job_path, quiet=True)
            except BaseException as exc:
                # A queue dispatcher must not strand every later disc because
                # one job wrapper failed outside the normal runner exit path.
                job = services.read_json(job_path)
                job["status"] = "failed"
                job["error"] = f"queue dispatcher: {exc}"
                job["returncode"] = 1
                job["ended_at"] = services.time.strftime("%Y-%m-%dT%H:%M:%S%z")
                services.save_job(job_path, job)
    finally:
        services.os.close(descriptor)
        try:
            services._unlink_lock_file(services.DISPATCHER_LOCK)
        except OSError:
            pass
        services.write_json_atomic(
            services.DISPATCHER_STATE,
            {
                "schema": "dvd2hevc-dispatcher-v1",
                "pid": services.os.getpid(),
                "stopped_at": services.time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )
    return 0
