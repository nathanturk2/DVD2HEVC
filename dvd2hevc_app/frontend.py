"""Phase 8 end-user conversion, background job, and playback workflows."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import __version__
from .audio import parse_audio_bitrate
from .compatibility import build_compatibility_plan
from .compat_registry import record_job_outcome
from .config import (
    all_presets,
    config_root,
    remove_named_preset,
    resolve_preset,
    save_named_preset,
)
from .contracts import JOB_SCHEMA, capability_contract, contract_compatible
from .encoders import (
    HEVC_ENCODERS,
    available_hevc_encoders,
    encoder_label,
    probe_hevc_encoder,
)
from .pipeline import PipelineError
from .runtime_support import read_log_tail, read_progress_log
from .progress import PROGRESS_PREFIX
from .scan import scan_disc
from .subprocess_utils import hidden_subprocess_kwargs
from .tools import discover_tools
from .vlc_setup import find_ready_vlc_root
from .uhd import find_stock_vlc_root, require_uhd_tools, check_structure, add_output_options, add_uhd_commands


from .paths import ROOT, REPORT_ROOT, STATE_ROOT, TOOL_ROOT
JOB_ROOT = REPORT_ROOT / "jobs"
DISPATCHER_STATE = JOB_ROOT / "dispatcher.json"
DISPATCHER_LOCK = JOB_ROOT / "dispatcher.lock"
DISPATCHER_START_LOCK = JOB_ROOT / "dispatcher-start.lock"
ACTIVE_WORK_LOCK = JOB_ROOT / "active-work.lock"
QUEUE_CONTROL = JOB_ROOT / "queue-control.json"
WATCH_ROOT = JOB_ROOT / "watched-batches"
GENERAL_RUNNER = ROOT / "tools" / "run-phase7-general-disc.ps1"
TERMINAL_JOB_STATES = {"passed", "failed", "canceled"}
ABRUPT_RUNNER_ERROR = "Conversion runner exited before recording a terminal result"
SUPPORTED_COMPATIBILITY_BLOCKERS = {
    "interleaved-branching-cells",
    "title-does-not-map-to-one-pgc",
}
QUALITY_ALIASES = {
    "balanced": "cq:24",
    "compact": "cq:27",
    "high": "cq:20",
    "high-quality": "cq:20",
}
DISK_FULL_LOG_MARKERS = (
    "no space left on device",
    "there is not enough space on the disk",
    "[winerror 112]",
    "errno 28",
    "enospc",
    "0x80070070",
)

AUDIO_LANGUAGE_RE = re.compile(r"^([A-Za-z]{2,3}|other|und)=(passthrough|compact-stereo)$")


class WorkSlotBusy(PipelineError):
    """The single planning/conversion slot is busy, but nothing has failed."""


class WorkspaceSpaceLow(PipelineError):
    """The managed conversion workspace needs more free storage."""


def retryable_watcher_planning_error(error: BaseException | str) -> bool:
    """Return whether a planning error belongs to the watcher, not the disc.

    Missing dependencies and temporary filesystem denial affect every source
    identically.  Treating each ISO as permanently bad both floods Attention
    and prevents an in-place recovery after the environment is repaired.
    """
    text = str(error).casefold()
    return any(
        marker in text
        for marker in (
            "missing conversion requirements:",
            "access is denied",
            "permission denied",
            "sharing violation",
            "being used by another process",
            "temporarily unavailable",
        )
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Could not read JSON report: {path}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"Expected a JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Publish JSON atomically despite transient Windows readers.

    Explorer, antivirus, and the GUI can open a state file without delete
    sharing.  Windows then rejects an otherwise atomic replacement with access
    denied.  Retrying the same private temporary file preserves the all-or-
    nothing contract without exposing a partially written document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(60):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                retryable = isinstance(exc, PermissionError) or (
                    os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 33}
                )
                if not retryable or attempt >= 59:
                    raise
                time.sleep(min(0.25, 0.025 * (attempt + 1)))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def current_boot_epoch() -> float | None:
    """Return the start of this Windows boot without requiring event-log access."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetTickCount64.argtypes = ()
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        uptime_seconds = float(kernel32.GetTickCount64()) / 1000.0
    except (AttributeError, OSError, ValueError):
        return None
    return time.time() - uptime_seconds


def current_boot_session_id() -> str | None:
    """Return a stable-enough identifier for the current Windows boot."""
    boot_epoch = current_boot_epoch()
    if boot_epoch is None:
        return None
    return str(int(boot_epoch // 60))


def _timestamp_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def safe_slug(value: str, *, fallback: str = "dvd") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return slug[:80] or fallback


def canonical_quality(value: str | int | float) -> str:
    text = str(value).strip().lower()
    if text in {"auto", "auto-cq", "compact-auto", "target", "target-bitrate", "bitrate"}:
        return "target-bitrate"
    text = QUALITY_ALIASES.get(text, text)
    if text.startswith("cq:"):
        text = text[3:]
    try:
        quality = float(text)
    except ValueError as exc:
        aliases = ", ".join(sorted(QUALITY_ALIASES))
        raise PipelineError(
            f"Invalid video mode {value!r}. Use target-bitrate, cq:N, or one of: {aliases}"
        ) from exc
    if not 0 <= quality <= 51:
        raise PipelineError("CQ quality must be between 0 and 51")
    rendered = str(int(quality)) if quality.is_integer() else f"{quality:g}"
    return f"cq:{rendered}"


def parse_audio_language_overrides(values: Any) -> dict[str, str]:
    if not values:
        return {}
    if isinstance(values, dict):
        pairs = [f"{key}={value}" for key, value in values.items()]
    elif isinstance(values, str):
        pairs = [item.strip() for item in values.split(",") if item.strip()]
    else:
        pairs = [str(item).strip() for item in values if str(item).strip()]
    result: dict[str, str] = {}
    for pair in pairs:
        match = AUDIO_LANGUAGE_RE.fullmatch(pair.lower())
        if not match:
            raise PipelineError(
                f"Invalid audio language override {pair!r}; use eng=passthrough or other=compact-stereo"
            )
        language, mode = match.groups()
        aliases = {"en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "ja": "jpn"}
        result[aliases.get(language, language)] = mode
    return result


def resolve_conversion_settings(args: argparse.Namespace) -> dict[str, Any]:
    preset = resolve_preset(getattr(args, "preset", None))
    selected_output = getattr(args, "output_format", None) or preset.get("output_format") or "uhd-bd"
    if selected_output not in {"uhd-bd", "dvd-hevc"}: raise PipelineError("Unsupported output format: " + str(selected_output))
    quality = canonical_quality(getattr(args, "quality", None) or preset["quality"])
    encoder = str(
        getattr(args, "encoder", None) or preset.get("encoder") or "hevc_nvenc"
    ).lower()
    if encoder not in HEVC_ENCODERS:
        raise PipelineError(f"Unsupported HEVC encoder: {encoder}")
    encoder_preset = str(
        getattr(args, "encoder_preset", None) or preset.get("encoder_preset") or "p6"
    ).lower()
    if not re.fullmatch(r"p[1-7]", encoder_preset):
        raise PipelineError("Encoder efficiency preset must be p1 through p7")
    deinterlace = str(
        getattr(args, "deinterlace", None) or preset.get("deinterlace") or "auto"
    )
    cadence = {
        "auto": "auto",
        "always": "deinterlace50",
        "off": "progressive",
    }[deinterlace]
    audio_mode = str(
        getattr(args, "audio_mode", None) or preset.get("audio_mode") or "passthrough"
    )
    if audio_mode not in {"passthrough", "compact-stereo"}:
        raise PipelineError("Audio mode must be passthrough or compact-stereo")
    stereo_audio_bitrate = parse_audio_bitrate(
        getattr(args, "stereo_audio_bitrate", None)
        or preset.get("stereo_audio_bitrate")
        or "256k"
    )
    mono_audio_bitrate = parse_audio_bitrate(
        getattr(args, "mono_audio_bitrate", None)
        or preset.get("mono_audio_bitrate")
        or "128k"
    )
    audio_workers = int(
        getattr(args, "audio_workers", None) or preset.get("audio_workers") or 2
    )
    pipeline_depth = int(
        getattr(args, "pipeline_depth", None) or preset.get("pipeline_depth") or 2
    )
    if not 1 <= audio_workers <= 8 or not 1 <= pipeline_depth <= 8:
        raise PipelineError("Audio workers and pipeline depth must be between 1 and 8")
    requested_multiplier = getattr(args, "target_bitrate_multiplier", None)
    if requested_multiplier is None:
        requested_multiplier = preset.get(
            "target_bitrate_multiplier", preset.get("auto_cq_multiplier", 1.0)
        )
    target_bitrate_multiplier = float(requested_multiplier)
    if not 0.25 <= target_bitrate_multiplier <= 4.0:
        raise PipelineError("Target bitrate multiplier must be between 0.25 and 4.0")
    bitrate_mode = str(
        getattr(args, "bitrate_mode", None) or preset.get("bitrate_mode") or "vbr"
    ).lower()
    if bitrate_mode not in {"vbr", "cbr"}:
        raise PipelineError("Bitrate mode must be vbr or cbr")
    explicit = bool(getattr(args, "settings_are_explicit", False))
    def setting(name: str, preset_name: str | None = None) -> Any:
        value = getattr(args, name, None)
        return value if explicit else value or preset.get(preset_name or name)
    main_title_quality_value = setting("main_title_quality")
    top_n_quality_value = setting("top_n_quality")
    top_n_count = int(setting("top_n_count") or 0)
    if main_title_quality_value and (top_n_quality_value or top_n_count):
        raise PipelineError("--main-title-quality and --top-n-quality are mutually exclusive")
    if bool(top_n_quality_value) != bool(top_n_count):
        raise PipelineError("Top-N quality requires --top-n-count and --top-n-quality together")
    main_title_quality = canonical_quality(main_title_quality_value) if main_title_quality_value else None
    top_n_quality = canonical_quality(top_n_quality_value) if top_n_quality_value else None
    if main_title_quality == "target-bitrate" or top_n_quality == "target-bitrate":
        raise PipelineError("Title overrides must use an explicit HandBrake-style cq:N value")
    audio_language_overrides = parse_audio_language_overrides(
        setting("audio_language", "audio_language_overrides")
    )
    return {
        "output_format": selected_output,
        "tsmuxer": getattr(args, "tsmuxer", None),
        "udf_tool": getattr(args, "udf_tool", None),
        "java_home": getattr(args, "java_home", None),
        "named_preset": str(getattr(args, "gui_preset_name", None) or preset["name"]),
        "quality": quality,
        "target_bitrate_multiplier": target_bitrate_multiplier,
        "bitrate_mode": bitrate_mode,
        "main_title_quality": main_title_quality,
        "top_n_quality": top_n_quality,
        "top_n_count": top_n_count,
        "encoder": encoder,
        "encoder_preset": encoder_preset,
        "deinterlace": deinterlace,
        "cadence": cadence,
        "ambiguous_cadence": "deinterlace50" if deinterlace == "auto" else cadence,
        "audio_mode": audio_mode,
        "audio_language_overrides": audio_language_overrides,
        "stereo_audio_bitrate": stereo_audio_bitrate,
        "mono_audio_bitrate": mono_audio_bitrate,
        "audio_workers": audio_workers,
        "pipeline_depth": pipeline_depth,
    }


def normalize_volume_label(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.upper()).strip("_")
    return (normalized or "DVD2HEVC")[:32]


def default_output_for(source: Path, *, add_filename_tags: bool = True, output_format: str = "uhd-bd") -> Path:
    """Return a safe, idempotent generated output name for one source ISO."""
    # Play Movie uses visible media-format tags and a hidden technical tag.
    # Normalize both generated forms so repeated planning stays idempotent.
    stem = re.sub(r"\s*\((?:DVD|HEVC|UHD-BD)\)", "", source.stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+-\s+converted$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s{2,}", " ", stem).strip() or "DVD"
    suffix = (" (DVD) (UHD-BD)" if output_format == "uhd-bd" else " (DVD) (HEVC)") if add_filename_tags else " - converted"
    return source.with_name(f"{stem}{suffix}.iso")


def iso_is_write_quiet(source: Path) -> bool:
    """Return false while another Windows process still has the ISO open for writing."""
    if os.name != "nt":
        try:
            with source.open("rb"):
                return True
        except OSError:
            return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    generic_read = 0x80000000
    # Permit other readers and deletion/rename, but deliberately deny writers.
    # Windows rejects this open while any existing handle has write access.
    share_read_and_delete = 0x00000001 | 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    handle = kernel32.CreateFileW(
        str(source), generic_read, share_read_and_delete, None,
        open_existing, file_attribute_normal, None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle or handle is None:
        return False
    kernel32.CloseHandle(handle)
    return True


def find_patched_vlc_root(explicit: str | Path | None = None) -> Path | None:
    # Playback must never silently accept a stock or partially prepared VLC.
    # The shared verifier checks the pinned VLC release, patch markers, and a
    # release hash or exact build manifest.
    return find_ready_vlc_root(explicit)


def ffmpeg_has_nvenc(ffmpeg: str) -> bool:
    """Compatibility wrapper retained for older callers and third-party scripts."""
    return "hevc_nvenc" in available_hevc_encoders(ffmpeg)


def find_authoring_backend(wsl_distro: str = "Ubuntu-24.04") -> str | None:
    native = shutil.which("genisoimage") or shutil.which("mkisofs")
    if native:
        return native
    wsl = shutil.which("wsl.exe")
    if not wsl:
        return None
    try:
        result = subprocess.run(
            [wsl, "-d", wsl_distro, "-e", "sh", "-c", "command -v genisoimage || command -v mkisofs"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=15,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    executable = result.stdout.strip()
    return f"WSL {wsl_distro}: {executable}" if result.returncode == 0 and executable else None


def conversion_preflight(
    source: Path,
    vlc_root: Path | None,
    *,
    encoder: str = "hevc_nvenc",
    encoder_preset: str = "p6",
    rate_control: int | float | str = 27,
    output_format: str = "uhd-bd", tsmuxer=None, udf_tool=None, java_home=None,
) -> dict[str, Any]:
    if os.name != "nt":
        raise PipelineError("The Phase 8 full-disc runner currently requires Windows")
    if not source.is_file():
        raise PipelineError(f"Source DVD ISO was not found: {source}")
    if source.suffix.lower() != ".iso":
        raise PipelineError("The end-user workflow currently accepts decrypted DVD ISO backups")
    if not GENERAL_RUNNER.is_file():
        raise PipelineError(f"Full-disc runner is missing: {GENERAL_RUNNER}")
    tools = discover_tools()
    missing: list[str] = []
    for name in ("ffmpeg", "ffprobe", "handbrake", "dvdinspect"):
        if not tools.get(name):
            missing.append(name)
    if not tools.get("python_pycdlib"):
        missing.append("Python package pycdlib")
    if missing:
        raise PipelineError("Missing conversion requirements: " + ", ".join(missing))
    available_encoders = available_hevc_encoders(str(tools["ffmpeg"]))
    if encoder not in available_encoders:
        available = ", ".join(available_encoders) or "none"
        raise PipelineError(
            f"FFmpeg does not report the requested HEVC encoder {encoder}. "
            f"Available DVD2HEVC encoders: {available}."
        )
    encoder_probe = probe_hevc_encoder(
        str(tools["ffmpeg"]), str(tools["ffprobe"]), encoder, encoder_preset,
        rate_control,
    )
    if not encoder_probe.get("operational"):
        raise PipelineError(
            f"{encoder_label(encoder)} is compiled into FFmpeg but failed the "
            f"DVD2HEVC runtime profile probe: {encoder_probe.get('error') or 'unknown error'}"
        )
    uhd_tools = require_uhd_tools(vlc_root=vlc_root, tsmuxer=tsmuxer, udf_tool=udf_tool, java_home=java_home) if output_format == "uhd-bd" else None
    if uhd_tools: check_structure(source)
    if uhd_tools: vlc_root = Path(uhd_tools['stock_vlc'])
    authoring_backend = uhd_tools['udf_tool'] if uhd_tools else find_authoring_backend()
    if not authoring_backend:
        raise PipelineError(
            "genisoimage/mkisofs was not found natively or in WSL Ubuntu-24.04"
        )
    if vlc_root is None:
        raise PipelineError(
            "Patched VLC was not found. Put it in vlc-dvdhevc, set "
            "DVD2HEVC_VLC_ROOT, or pass --vlc-root."
        )
    return {
        "tools": tools,
        "encoder_probe": encoder_probe,
        "vlc_root": str(vlc_root),
        "authoring_backend": authoring_backend,
        "uhd_tools": uhd_tools,
    }


def build_quick_compatibility_plan(source: Path, destination: Path) -> dict[str, Any]:
    scan = scan_disc(
        source,
        inspect_vobs=False,
        use_handbrake=False,
        use_native=True,
        min_title_duration=1,
    )
    plan = build_compatibility_plan(scan)
    unsupported = sorted(
        {
            str(blocker.get("code"))
            for blocker in plan.get("blockers") or []
            if str(blocker.get("code")) not in SUPPORTED_COMPATIBILITY_BLOCKERS
        }
    )
    if unsupported:
        raise PipelineError(
            "This disc has unsupported compatibility blockers: " + ", ".join(unsupported)
        )
    write_json_atomic(destination, plan)
    return plan


def job_paths(job_id: str) -> dict[str, Path]:
    root = JOB_ROOT / job_id
    return {
        "root": root,
        "job": root / "job.json",
        "plan": root / "plan.json",
        "work": root / "work",
    }


def managed_work_base() -> Path:
    """Return the per-user, non-roaming root for bulky managed artifacts."""
    override = os.environ.get("DVD2HEVC_WORK_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "DVD2HEVC" / "work").resolve()
    return (config_root() / "work").resolve()


def managed_job_work_root(job_id: str) -> Path:
    """Return a short, private workspace path for a newly planned job.

    Report directories intentionally retain descriptive disc names, but using
    those names as the binary workspace pushed cell artifacts beyond the
    traditional Windows 260-character boundary.  A timestamp plus stable hash
    remains recognizable while keeping FFmpeg, HandBrake, Git and Explorer on
    safe paths. Existing jobs keep the work_root already recorded in job.json.
    """
    base = managed_work_base()
    timestamp = job_id[:15] if re.fullmatch(r"\d{8}-\d{6}.*", job_id) else "job"
    token = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return base / f"{timestamp}-{token}"


def make_job_id(source: Path, requested: str | None = None) -> str:
    base = safe_slug(requested or source.stem)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = f"{timestamp}-{base}"
    suffix = 2
    while (JOB_ROOT / candidate).exists():
        candidate = f"{timestamp}-{base}-{suffix}"
        suffix += 1
    return candidate


def known_job_files() -> list[Path]:
    if not JOB_ROOT.is_dir():
        return []
    return sorted(
        JOB_ROOT.glob("*/job.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def try_read_job(path: Path) -> dict[str, Any] | None:
    try:
        value = read_json(path)
    except PipelineError:
        return None
    value["job_file"] = str(path.resolve())
    return value


def resolve_job(identifier: str | None) -> tuple[Path, dict[str, Any]]:
    jobs = [(path, job) for path in known_job_files() if (job := try_read_job(path))]
    if not jobs:
        raise PipelineError("No DVD2HEVC background jobs were found")
    if not identifier:
        return jobs[0]
    direct = Path(identifier).expanduser()
    if direct.is_dir() and (direct / "job.json").is_file():
        direct = direct / "job.json"
    if direct.is_file():
        return direct.resolve(), read_json(direct.resolve())
    exact = [(path, job) for path, job in jobs if job.get("id") == identifier]
    if exact:
        return exact[0]
    matches = [(path, job) for path, job in jobs if identifier.lower() in str(job.get("id", "")).lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PipelineError(f"DVD2HEVC job not found: {identifier}")
    raise PipelineError(f"Job name is ambiguous: {identifier}")


def save_job(path: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json_atomic(path, job)


def _existing_storage_path(path: Path) -> Path:
    """Return the closest existing parent used for a volume free-space check."""
    candidate = path.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def required_workspace_bytes(source: Path) -> int:
    """Estimate a safe working allowance for extraction plus encoded domains."""
    return max(20 * 1024**3, source.stat().st_size * 6)


def ensure_workspace_capacity(source: Path, work_root: Path) -> dict[str, int | str]:
    """Fail before encoding when the selected workspace cannot hold one disc."""
    storage_path = _existing_storage_path(work_root)
    free = int(shutil.disk_usage(storage_path).free)
    required = required_workspace_bytes(source)
    report: dict[str, int | str] = {
        "volume_path": str(storage_path),
        "free_bytes": free,
        "required_bytes": required,
    }
    if free < required:
        free_gib = free / 1024**3
        required_gib = required / 1024**3
        raise WorkspaceSpaceLow(
            f"Conversion workspace is low on space: {free_gib:.1f} GiB free at "
            f"{storage_path}; allow about {required_gib:.1f} GiB for {source.name}. "
            "Completed managed work is pruned automatically; this disc will retry "
            "when enough space is available."
        )
    return report


def prune_completed_work(job_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    """Remove bulky reproducible artifacts after a verified managed conversion."""
    report: dict[str, Any] = {
        "performed": False,
        "files_removed": 0,
        "logical_bytes_removed": 0,
    }
    if str(job.get("status") or "") != "passed":
        report["reason"] = "job-not-passed"
        return report
    output = Path(str(job.get("output") or ""))
    if not output.is_file():
        report["reason"] = "verified-output-missing"
        return report
    work_root = Path(str(job.get("work_root") or "")).expanduser().resolve()
    legacy_default = (job_path.parent / "work").resolve()
    current_default = managed_job_work_root(str(job.get("id") or job_path.parent.name)).resolve()
    managed = bool(job.get("managed_work_root", work_root == legacy_default))
    if not managed or work_root not in {legacy_default, current_default} or not work_root.is_dir():
        report["reason"] = "external-or-missing-workspace"
        return report

    disposable_suffixes = {".vob", ".ts", ".dvd-pes", ".ac3", ".part", ".tmp"}
    for path in work_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in disposable_suffixes:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        report["files_removed"] += 1
        report["logical_bytes_removed"] += size
    # Empty artifact directories make reports easier to browse, while all
    # logs, IFO/BUP data, plans, validation reports, and status remain.
    for path in sorted(
        (candidate for candidate in work_root.rglob("*") if candidate.is_dir()),
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
    report["performed"] = True
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return report


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is a harmless existence probe on POSIX.  On Windows,
        # CPython routes non-console-control signals through TerminateProcess;
        # signal 0 can therefore kill the very helper being checked.  Query the
        # process handle and exit code without mutating it instead.
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            # Access denied still establishes that a process owns this PID.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_tree_postorder(root_pid: int) -> list[int]:
    """Return live descendants before their parents using Toolhelp32."""
    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W))
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W))
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in {None, ctypes.c_void_p(-1).value}:
        return [root_pid]
    children: dict[int, list[int]] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        found = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while found:
            pid = int(entry.th32ProcessID)
            parent = int(entry.th32ParentProcessID)
            children.setdefault(parent, []).append(pid)
            found = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)

    result: list[int] = []
    visited: set[int] = set()

    def visit(pid: int) -> None:
        if pid in visited:
            return
        visited.add(pid)
        for child in children.get(pid, []):
            visit(child)
        result.append(pid)

    visit(root_pid)
    return result


def terminate_process_tree(pid: int, *, timeout: float = 2.0) -> bool:
    """Immediately stop a conversion runner and all of its descendants."""
    pid = int(pid or 0)
    if pid <= 0 or pid == os.getpid():
        return False
    deadline = time.monotonic() + max(0.1, timeout)
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        # Repeat the snapshot so a child spawned during cancellation cannot
        # escape between the first enumeration and parent termination.
        while time.monotonic() < deadline and process_alive(pid):
            for target in _windows_process_tree_postorder(pid):
                handle = kernel32.OpenProcess(0x0001, False, target)
                if not handle:
                    continue
                try:
                    kernel32.TerminateProcess(handle, 3)
                finally:
                    kernel32.CloseHandle(handle)
            if process_alive(pid):
                time.sleep(0.05)
        return not process_alive(pid)

    try:
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return not process_alive(pid)
    while time.monotonic() < deadline and process_alive(pid):
        time.sleep(0.05)
    if process_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    return not process_alive(pid)


def cancel_running_job(
    job_path: Path,
    job: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Persist cancellation first, then promptly terminate the runner tree."""
    now_text = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    marker = Path(job["work_root"]) / "cancel.requested"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(now_text, encoding="utf-8")
    job["cancel_requested_at"] = now_text
    job["cancel_reason"] = reason
    save_job(job_path, job)
    try:
        runner_pid = int(job.get("runner_pid") or 0)
    except (TypeError, ValueError):
        runner_pid = 0
    stopped = not process_alive(runner_pid) or terminate_process_tree(runner_pid)
    latest = read_json(job_path)
    if stopped:
        latest["status"] = "canceled"
        latest["canceled_at"] = now_text
        latest["ended_at"] = now_text
        latest["returncode"] = 3
        latest["termination_mode"] = "immediate-process-tree"
        latest.pop("error", None)
    else:
        latest["status"] = "running"
        latest["termination_error"] = "The conversion process tree did not exit promptly"
    save_job(job_path, latest)
    return latest


def _exclusive_lock_age(path: Path) -> float:
    from . import queue_dispatcher
    return queue_dispatcher._exclusive_lock_age(path, services=sys.modules[__name__])


def _unlink_lock_file(path: Path) -> None:
    """Remove a lock despite brief Windows sharing conflicts from readers."""
    from . import queue_dispatcher
    return queue_dispatcher._unlink_lock_file(path, services=sys.modules[__name__])


def acquire_active_work_lock(owner: str, *, wait: bool = True, progress: Callable[[str], None] | None = None) -> str:
    """Acquire the one global work slot, optionally returning promptly if busy."""
    from . import queue_dispatcher
    return queue_dispatcher.acquire_active_work_lock(owner, wait=wait, progress=progress, services=sys.modules[__name__])


def release_active_work_lock(token: str) -> None:
    """Release the global work slot only when this caller still owns it."""
    from . import queue_dispatcher
    return queue_dispatcher.release_active_work_lock(token, services=sys.modules[__name__])


def _prepare_job_unlocked(
    args: argparse.Namespace,
    *,
    source: Path | None = None,
    output: Path | None = None,
    requested_name: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    source = (source or Path(args.source)).expanduser().resolve()
    output_value = output or (
        Path(args.output)
        if getattr(args, "output", None)
        else default_output_for(
            source,
            add_filename_tags=bool(getattr(args, "add_filename_tags", True)),
            output_format=resolve_conversion_settings(args)["output_format"],
        )
    )
    output_path = output_value.expanduser().resolve()
    output_existed_at_plan = output_path.exists()
    if output_path == source:
        raise PipelineError("Output ISO must be different from the source ISO")
    if getattr(args, "resume", False) and not getattr(args, "work_dir", None):
        raise PipelineError("--resume requires the original --work-dir")
    if output_existed_at_plan and not getattr(args, "resume", False):
        raise PipelineError(f"Output already exists (use --resume only with its original work directory): {output_path}")
    settings = resolve_conversion_settings(args)
    vlc_root = (find_stock_vlc_root if settings["output_format"] == "uhd-bd" else find_patched_vlc_root)(getattr(args, "vlc_root", None))
    if settings["output_format"] == "uhd-bd" and getattr(args, "vlc_root", None) and vlc_root is None:
        raise PipelineError("The selected VLC directory lacks the stock VLC/libbluray BD-J runtime")
    preflight = conversion_preflight(
        source,
        vlc_root,
        encoder=str(settings["encoder"]),
        encoder_preset=str(settings["encoder_preset"]),
        rate_control=(
            f"{settings['bitrate_mode']}:1500000"
            if settings["quality"] == "target-bitrate"
            else settings["quality"]
        ),
        output_format=settings["output_format"], tsmuxer=settings["tsmuxer"],
        udf_tool=settings["udf_tool"], java_home=settings["java_home"],
    )
    job_id = make_job_id(source, requested_name or getattr(args, "name", None))
    paths = job_paths(job_id)
    plan = build_quick_compatibility_plan(source, paths["plan"])
    explicit_work = getattr(args, "work_dir", None)
    work_root = (
        Path(explicit_work).expanduser().resolve()
        if explicit_work
        else managed_job_work_root(job_id).resolve()
    )
    workspace_capacity = ensure_workspace_capacity(source, work_root)
    label = normalize_volume_label(getattr(args, "label", None) or f"{source.stem}_HEVC")
    summary = dict(plan.get("summary") or {})
    job: dict[str, Any] = {
        "schema": JOB_SCHEMA,
        "version": __version__,
        "contracts": capability_contract(settings["output_format"]),
        "id": job_id,
        "status": "planned",
        "queue_order": time.time_ns(),
        "source": str(source),
        "output": str(output_path),
        "output_existed_at_plan": output_existed_at_plan,
        "output_created_by_job": not output_existed_at_plan,
        "add_filename_tags": bool(getattr(args, "add_filename_tags", True)),
        "work_root": str(work_root),
        "managed_work_root": explicit_work is None,
        "plan": str(paths["plan"].resolve()),
        "log": str(work_root / "run.log"),
        "label": label,
        "settings": settings,
        "vlc_root": str(vlc_root),
        "plan_summary": summary,
        "preflight": {
            "passed": True,
            "player": str(vlc_root),
            "output_format": settings["output_format"],
            "vlc_patch_required": settings["output_format"] == "dvd-hevc",
            "uhd_tools": preflight.get("uhd_tools"),
            "ffmpeg": preflight["tools"].get("ffmpeg"),
            "ffprobe": preflight["tools"].get("ffprobe"),
            "handbrake": preflight["tools"].get("handbrake"),
            "dvdinspect": preflight["tools"].get("dvdinspect"),
            "encoder": preflight["encoder_probe"],
            "authoring_backend": preflight["authoring_backend"],
            "full_decryption_scan": "runs before encoding",
            "workspace_capacity": workspace_capacity,
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_job(paths["job"], job)
    return paths["job"], job


def prepare_job(
    args: argparse.Namespace,
    *,
    source: Path | None = None,
    output: Path | None = None,
    requested_name: str | None = None,
    wait_for_slot: bool = True,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Plan one disc while excluding every other planning or conversion job."""
    source_value = (source or Path(args.source)).expanduser().resolve()
    cancel_generation = current_cancel_generation()
    planning_started_paused = queue_is_paused()
    lock_options = {"progress": progress} if progress else {}
    token = acquire_active_work_lock(f"plan:{source_value}", wait=wait_for_slot, **lock_options)
    try:
        if progress:
            progress(f"Planning and checking {source_value.name}â€¦")
        if current_cancel_generation() != cancel_generation:
            raise PipelineError("Planning was canceled before this disc acquired the work slot")
        if not planning_started_paused and queue_is_paused():
            raise PipelineError("Planning paused before this disc acquired the work slot")
        job_path, job = _prepare_job_unlocked(
            args,
            source=source_value,
            output=output,
            requested_name=requested_name,
        )
        job["cancel_generation"] = cancel_generation
        save_job(job_path, job)
        return job_path, job
    finally:
        release_active_work_lock(token)


def runner_command(job: dict[str, Any]) -> list[str]:
    from . import command_generation
    return command_generation.runner_command(job, services=sys.modules[__name__])


def _run_job_unlocked(job_path: Path, *, quiet: bool) -> int:
    job = read_json(job_path)
    planned_generation = job.get("cancel_generation")
    if planned_generation is not None and int(planned_generation) != current_cancel_generation():
        job["status"] = "canceled"
        job["canceled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        job["ended_at"] = job["canceled_at"]
        job["error"] = "Canceled by a Cancel all request issued while this disc was being planned"
        save_job(job_path, job)
        return 0
    current_state = str(job.get("status") or "")
    if current_state == "canceled":
        return 0
    if current_state not in {"planned", "queued"}:
        raise PipelineError(
            f"Job {job.get('id', job_path.parent.name)} cannot start from state {current_state!r}"
        )
    expected_contract = capability_contract(job.get("settings", {}).get("output_format", "dvd-hevc"))
    recorded_contract = job.get("contracts")
    if recorded_contract is not None and (
        not contract_compatible(recorded_contract)
        or recorded_contract.get("format_profile") != expected_contract["format_profile"]
        or recorded_contract.get("player_abi") != expected_contract["player_abi"]
    ):
        raise PipelineError(
            "This job was planned for an incompatible DVD2HEVC format/player contract; "
            "replan it with the current converter"
        )
    job["status"] = "running"
    job["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    job["runner_pid"] = None
    boot_session = current_boot_session_id()
    if boot_session is not None:
        job["runner_boot_session"] = boot_session
    save_job(job_path, job)
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "check": False,
    }
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    if os.name != "nt":
        kwargs["start_new_session"] = True
    kwargs.update(hidden_subprocess_kwargs())
    try:
        process = subprocess.Popen(runner_command(job), **{key: value for key, value in kwargs.items() if key != "check"})
        job["runner_pid"] = process.pid
        save_job(job_path, job)
        returncode = process.wait()
    except KeyboardInterrupt:
        job["status"] = "canceled"
        job["returncode"] = 130
        job["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_job(job_path, job)
        raise
    except OSError as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["returncode"] = 1
        job["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        job = handle_output_storage_failure(job)
        save_job(job_path, job)
        return 1
    job = read_json(job_path)
    status_path = Path(job["work_root"]) / "status.json"
    pipeline_state = None
    if status_path.is_file():
        try:
            pipeline_state = read_json(status_path).get("state")
        except PipelineError:
            pass
    job["returncode"] = returncode
    if (
        job.get("status") == "canceled"
        or bool(job.get("cancel_requested_at"))
        or pipeline_state == "canceled"
        or returncode == 3
    ):
        job["status"] = "canceled"
    else:
        job["status"] = "passed" if returncode == 0 and pipeline_state == "passed" else "failed"
    job["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if job["status"] == "failed":
        job = handle_output_storage_failure(job)
    save_job(job_path, job)
    if job["status"] == "passed":
        try:
            job["cleanup"] = prune_completed_work(job_path, job)
        except Exception as exc:
            # Cleanup is an optimization after output verification. Never
            # downgrade a good disc because a disposable file was locked.
            job["cleanup"] = {
                "performed": False,
                "reason": "cleanup-error",
                "error": str(exc),
            }
        save_job(job_path, job)
    try:
        compatibility = record_job_outcome(job_path, job)
        job["compatibility_history"] = {
            "recorded": True,
            "verdict": compatibility["outcome"]["verdict"],
        }
    except Exception as exc:
        # Compatibility history is diagnostic metadata. A locked registry must
        # never downgrade or strand an otherwise verified conversion.
        job["compatibility_history"] = {
            "recorded": False,
            "error": str(exc),
        }
    save_job(job_path, job)
    return returncode


def run_job(job_path: Path, *, quiet: bool) -> int:
    """Run one conversion through the same machine-wide slot as disc planning."""
    job_path = job_path.expanduser().resolve()
    initial = read_json(job_path)
    token = acquire_active_work_lock(f"run:{initial.get('id', job_path.parent.name)}")
    try:
        return _run_job_unlocked(job_path, quiet=quiet)
    finally:
        release_active_work_lock(token)


def live_dispatcher_pid() -> int | None:
    """Return the live lock owner, preferring truth over advisory state."""
    from . import queue_dispatcher
    return queue_dispatcher.live_dispatcher_pid(services=sys.modules[__name__])


def dispatcher_is_running() -> bool:
    from . import queue_dispatcher
    return queue_dispatcher.dispatcher_is_running(services=sys.modules[__name__])


def acquire_dispatcher_start_lock() -> str:
    """Serialize the short check-and-spawn sequence used by queue producers."""
    from . import queue_dispatcher
    return queue_dispatcher.acquire_dispatcher_start_lock(services=sys.modules[__name__])


def release_dispatcher_start_lock(token: str) -> None:
    from . import queue_dispatcher
    return queue_dispatcher.release_dispatcher_start_lock(token, services=sys.modules[__name__])


def ensure_dispatcher() -> int:
    from . import queue_dispatcher
    return queue_dispatcher.ensure_dispatcher(services=sys.modules[__name__])


def acquire_dispatcher_lock() -> int | None:
    from . import queue_dispatcher
    return queue_dispatcher.acquire_dispatcher_lock(services=sys.modules[__name__])


def queued_jobs() -> list[tuple[Path, dict[str, Any]]]:
    values = [
        (path, job)
        for path in known_job_files()
        if (job := try_read_job(path)) and job.get("status") == "queued"
    ]
    return sorted(values, key=lambda item: int(item[1].get("queue_order") or 0))


def queue_is_paused() -> bool:
    return bool(queue_control_state().get("paused"))


def queue_pause_reason() -> str | None:
    state = queue_control_state()
    if not state.get("paused"):
        return None
    reason = state.get("reason")
    return str(reason) if reason else None


def queue_control_state() -> dict[str, Any]:
    if not QUEUE_CONTROL.is_file():
        return {"schema": "dvd2hevc-queue-control-v1", "paused": False, "cancel_generation": 0}
    try:
        value = read_json(QUEUE_CONTROL)
    except PipelineError:
        return {"schema": "dvd2hevc-queue-control-v1", "paused": False, "cancel_generation": 0}
    value.setdefault("cancel_generation", 0)
    return value


def current_cancel_generation() -> int:
    try:
        return int(queue_control_state().get("cancel_generation") or 0)
    except (TypeError, ValueError):
        return 0


def update_queue_control(
    *,
    paused: bool,
    reason: str,
    expected_cancel_generation: int | None = None,
    cancel_generation: int | None = None,
) -> bool:
    """Update global pause state without racing Cancel all against Start watch."""
    lock_path = QUEUE_CONTROL.with_suffix(".lock")
    token = f"{os.getpid()}-{time.time_ns()}"
    payload = json.dumps({"pid": os.getpid(), "token": token, "owner": reason}).encode("utf-8")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = read_json(lock_path)
                holder_alive = process_alive(int(holder.get("pid") or 0))
            except (PipelineError, TypeError, ValueError):
                if _exclusive_lock_age(lock_path) < 5.0:
                    time.sleep(0.05)
                    continue
                holder_alive = False
            if holder_alive:
                time.sleep(0.05)
                continue
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                time.sleep(0.05)
            continue
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        break
    try:
        control = queue_control_state()
        if (
            expected_cancel_generation is not None
            and current_cancel_generation() != expected_cancel_generation
        ):
            return False
        control.update({
            "schema": "dvd2hevc-queue-control-v1",
            "paused": bool(paused),
            "reason": reason,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        if cancel_generation is not None:
            control["cancel_generation"] = int(cancel_generation)
            control["cancel_all_at"] = control["updated_at"]
        write_json_atomic(QUEUE_CONTROL, control)
        return True
    finally:
        try:
            current = read_json(lock_path)
        except PipelineError:
            current = {}
        if current.get("token") == token:
            _unlink_lock_file(lock_path)


def log_tail_text(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes), os.SEEK_SET)
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def output_storage_failure_evidence(job: dict[str, Any]) -> list[str]:
    """Return current-job evidence files/text that explicitly report ENOSPC."""
    work_value = str(job.get("work_root") or "")
    evidence_paths = [Path(str(job.get("log") or ""))]
    if work_value:
        work_root = Path(work_value)
        evidence_paths.extend([
            work_root / "status.json",
            work_root / "final-output-reports" / "author.log",
        ])
    matches: list[str] = []
    inline = str(job.get("error") or "")
    if any(marker in inline.casefold() for marker in DISK_FULL_LOG_MARKERS):
        matches.append("job.error")
    started_at: float | None = None
    if job.get("started_at"):
        try:
            started_at = datetime.fromisoformat(str(job["started_at"])).timestamp()
        except (TypeError, ValueError):
            pass
    for path in evidence_paths:
        if not str(path) or not path.is_file():
            continue
        try:
            if started_at is not None and path.stat().st_mtime < started_at - 2.0:
                continue
        except OSError:
            continue
        text = log_tail_text(path).casefold()
        if any(marker in text for marker in DISK_FULL_LOG_MARKERS):
            matches.append(str(path))
    return matches


def remove_new_partial_output(job: dict[str, Any]) -> tuple[list[str], str | None]:
    """Remove only this job's new ISO and its exact authoring `.part` siblings."""
    created_by_job = job.get("output_created_by_job")
    if created_by_job is False or (
        created_by_job is None and bool(job.get("output_existed_at_plan"))
    ):
        return [], "output existed before this job and was retained"
    output_value = str(job.get("output") or "")
    if not output_value:
        return [], "job has no output path"
    output = Path(output_value).expanduser()
    source_value = str(job.get("source") or "")
    source = Path(source_value).expanduser() if source_value else None
    resolved = output.resolve()
    if resolved == Path(resolved.anchor) or (
        source is not None and resolved == source.resolve()
    ):
        return [], "refused unsafe partial-output path"

    targets: list[Path] = []
    if output.exists():
        targets.append(output)
    try:
        siblings = output.parent.iterdir() if output.parent.is_dir() else ()
        part_prefix = f".{output.name}."
        for candidate in siblings:
            if (
                candidate.name.startswith(part_prefix)
                and candidate.name.endswith(".part")
                and candidate.is_file()
            ):
                targets.append(candidate)
    except OSError as exc:
        return [], str(exc)

    removed: list[str] = []
    errors: list[str] = []
    for target in targets:
        try:
            if target.is_dir():
                errors.append(f"refused unexpected directory: {target}")
                continue
            target.unlink()
            removed.append(str(target))
        except OSError as exc:
            errors.append(f"{target}: {exc}")
    return removed, "; ".join(errors) if errors else None


def handle_output_storage_failure(job: dict[str, Any]) -> dict[str, Any]:
    """Clean output-drive partials and latch all work after a proven ENOSPC."""
    evidence = output_storage_failure_evidence(job)
    if not evidence:
        return job
    removed, cleanup_error = remove_new_partial_output(job)
    output = str(job.get("output") or "")
    identity = job.get("id") or (Path(output).name if output else "this disc")
    reason = (
        f"Output disk full while converting {identity}. "
        "Free space, then explicitly resume the queue."
    )
    update_queue_control(paused=True, reason=reason)
    job["failure_reason"] = "output-disk-full"
    job["storage_failure_evidence"] = evidence
    job["partial_output_removed"] = bool(removed)
    job["partial_output_removed_paths"] = removed
    job["queue_paused_on_failure"] = True
    if cleanup_error:
        job["partial_output_cleanup_error"] = cleanup_error
    else:
        job.pop("partial_output_cleanup_error", None)

    log_value = str(job.get("log") or "")
    if log_value:
        try:
            with Path(log_value).open("a", encoding="utf-8", errors="replace") as log:
                if removed:
                    log.write(
                        "DVD2HEVC removed incomplete output after disk-full failure: "
                        + ", ".join(removed) + "\n"
                    )
                elif cleanup_error:
                    log.write(
                        "DVD2HEVC retained output after cleanup could not safely complete: "
                        f"{cleanup_error}\n"
                    )
                else:
                    log.write(
                        "DVD2HEVC found no partial output to remove after disk-full failure.\n"
                    )
                log.write(
                    "DVD2HEVC paused queued conversions and watched-folder discovery. "
                    "Free output space, then explicitly resume the queue.\n"
                )
        except OSError:
            pass
    return job


def _rotate_resume_artifacts(work_root: Path) -> None:
    rotated_at = time.strftime("%Y%m%d-%H%M%S")
    for name in ("status.json", "run.log"):
        current = work_root / name
        if not current.is_file():
            continue
        previous = current.with_name(f"{current.stem}.previous-{rotated_at}{current.suffix}")
        suffix = 2
        while previous.exists():
            previous = current.with_name(
                f"{current.stem}.previous-{rotated_at}-{suffix}{current.suffix}"
            )
            suffix += 1
        current.replace(previous)


def _reset_job_for_resume(
    job_path: Path,
    job: dict[str, Any],
    *,
    preserve_queue_order: bool,
    recovery_reason: str | None = None,
) -> dict[str, Any]:
    work_root = Path(str(job.get("work_root") or ""))
    _rotate_resume_artifacts(work_root)
    previous_boot_session = job.get("runner_boot_session")
    for field in (
        "error",
        "returncode",
        "runner_pid",
        "runner_boot_session",
        "ended_at",
        "canceled_at",
        "cancel_requested_at",
        "cancel_reason",
        "termination_error",
    ):
        job.pop(field, None)
    (work_root / "cancel.requested").unlink(missing_ok=True)
    if not preserve_queue_order or not job.get("queue_order"):
        job["queue_order"] = time.time_ns()
    job["cancel_generation"] = current_cancel_generation()
    if recovery_reason:
        job["recovered_after_restart_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        job["recovery_reason"] = recovery_reason
        job["restart_recovery_count"] = int(job.get("restart_recovery_count") or 0) + 1
        if previous_boot_session:
            job["interrupted_boot_session"] = previous_boot_session
    if not queue_prepared_job(job_path, job):
        raise PipelineError("The job was canceled by a concurrent Cancel all request")
    return job


def _job_was_interrupted_by_restart(
    job: dict[str, Any],
    *,
    boot_session: str | None,
    boot_epoch: float | None,
) -> bool:
    if bool(job.get("cancel_requested_at")):
        return False
    state = str(job.get("status") or "")
    if state not in {"running", "failed"}:
        return False
    if state == "failed" and str(job.get("error") or "") != ABRUPT_RUNNER_ERROR:
        return False
    output_value = str(job.get("output") or "")
    if output_value and Path(output_value).exists():
        return False

    recorded_boot = str(job.get("runner_boot_session") or "")
    if recorded_boot and boot_session:
        return recorded_boot != boot_session

    # Compatibility for jobs created before boot-session markers existed. A raw
    # running record predating this boot is unambiguous. The generic abrupt
    # failure is accepted only close to boot, covering Windows Update's
    # multi-restart servicing sequence without retrying old converter failures.
    started_epoch = _timestamp_epoch(job.get("started_at"))
    if boot_epoch is None or started_epoch is None or started_epoch >= boot_epoch - 5.0:
        return False
    if state == "running":
        return True
    ended_epoch = _timestamp_epoch(job.get("ended_at"))
    return ended_epoch is not None and abs(ended_epoch - boot_epoch) <= 30.0 * 60.0


def recover_jobs_interrupted_by_restart() -> list[str]:
    """Requeue only jobs whose runner belonged to an earlier Windows boot."""
    boot_epoch = current_boot_epoch()
    boot_session = (
        str(int(boot_epoch // 60))
        if boot_epoch is not None
        else current_boot_session_id()
    )
    recovered: list[str] = []
    for job_path in known_job_files():
        job = try_read_job(job_path)
        if not job or not _job_was_interrupted_by_restart(
            job,
            boot_session=boot_session,
            boot_epoch=boot_epoch,
        ):
            continue
        _reset_job_for_resume(
            job_path,
            job,
            preserve_queue_order=True,
            recovery_reason="Windows restarted while the conversion runner was active",
        )
        recovered.append(str(job.get("id") or job_path.parent.name))
    return recovered


def dispatcher_main() -> int:
    from . import queue_dispatcher
    return queue_dispatcher.dispatcher_main(services=sys.modules[__name__])


def print_plan_summary(job: dict[str, Any], *, dry_run: bool) -> None:
    summary = job.get("plan_summary") or {}
    print(f"DVD2HEVC {'dry run' if dry_run else 'conversion'}: {Path(job['source']).stem}")
    print(f"Source: {job['source']}")
    print(f"Output: {job['output']}")
    print(
        f"Titles: {int(summary.get('global_titles') or 0)}  "
        f"VTS sets: {int(summary.get('title_sets') or summary.get('vts_count') or 0)}  "
        f"physical cells: {int(summary.get('unique_physical_cells') or 0)}"
    )
    print(
        f"Quality: {job['settings']['quality']} (backend scale)  "
        f"Encoder: {job['settings'].get('encoder', 'hevc_nvenc')} "
        f"{job['settings']['encoder_preset']}  "
        f"deinterlace: {job['settings']['deinterlace']}"
    )
    audio = job["settings"].get("audio_mode", "passthrough")
    audio_detail = (
        f"compact-stereo AC-3 ({int(job['settings']['stereo_audio_bitrate']) // 1000} kb/s)"
        if audio == "compact-stereo" else "passthrough"
    )
    print(f"Audio: {audio_detail}  Menus/extras/subtitles/navigation: preserved")
    print("Full source decryption and sector-integrity scan: runs before encoding")
    print(f"Plan: {job['plan']}")


def active_jobs() -> list[dict[str, Any]]:
    return [
        job
        for path in known_job_files()
        if (job := try_read_job(path)) and job.get("status") in {"queued", "running"}
    ]


def cmd_auto(args: argparse.Namespace) -> int:
    if not args.dry_run and active_jobs():
        raise PipelineError(
            "A background conversion queue is active. Use 'start' to join it or wait for it to finish."
        )
    job_path, job = prepare_job(args)
    print_plan_summary(job, dry_run=args.dry_run)
    if args.dry_run:
        print("No output ISO was written.")
        return 0
    print("Starting foreground conversion. This workflow is resumable from its job work directory.")
    returncode = run_job(job_path, quiet=False)
    finished = read_json(job_path)
    if returncode == 0 and finished.get("status") == "passed":
        print("DVD2HEVC conversion passed all automated gates.")
        print(f"Output: {finished['output']}")
        print(f"Job: {finished['id']}")
        return 0
    print(f"DVD2HEVC conversion failed. Log: {finished.get('log')}", file=sys.stderr)
    return returncode or 2


def queue_prepared_job(job_path: Path, job: dict[str, Any]) -> bool:
    planned_generation = job.get("cancel_generation")
    if planned_generation is not None and int(planned_generation) != current_cancel_generation():
        job["status"] = "canceled"
        job["canceled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        job["ended_at"] = job["canceled_at"]
        job["error"] = "Canceled by a Cancel all request issued while this disc was being planned"
        save_job(job_path, job)
        return False
    job["status"] = "queued"
    job["queued_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_job(job_path, job)
    return True


def cmd_start(args: argparse.Namespace) -> int:
    job_path, job = prepare_job(args)
    if not queue_prepared_job(job_path, job):
        raise PipelineError("The job was canceled by a concurrent Cancel all request")
    dispatcher_pid = ensure_dispatcher()
    print("DVD2HEVC background conversion queued")
    print(f"Job: {job['id']}")
    print(f"Output: {job['output']}")
    print(f"Log: {job['log']}")
    print(f"Queue dispatcher PID: {dispatcher_pid}")
    print(f"Check progress: python dvd2hevc.py status {job['id']}")
    print(f"Watch progress: python dvd2hevc.py status {job['id']} --watch")
    return 0


def collect_iso_sources(values: list[str], *, recursive: bool) -> list[Path]:
    sources: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".iso":
            sources.append(path)
        elif path.is_dir():
            iterator = path.rglob("*.iso") if recursive else path.glob("*.iso")
            sources.extend(sorted(item.resolve() for item in iterator if item.is_file()))
        else:
            raise PipelineError(f"DVD ISO or source directory not found: {path}")
    unique: list[Path] = []
    seen: set[str] = set()
    for source in sources:
        key = str(source).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(source)
    if not unique:
        raise PipelineError("No DVD ISO backups were found to queue")
    return unique


def cmd_queue(args: argparse.Namespace) -> int:
    sources = collect_iso_sources(args.sources, recursive=args.recursive)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Planning and queueing {len(sources)} DVD conversion job(s)...")
    prepared: list[tuple[Path, dict[str, Any]]] = []
    batch_started_paused = queue_is_paused()
    batch_generation = current_cancel_generation()
    for index, source in enumerate(sources, start=1):
        if current_cancel_generation() != batch_generation:
            break
        if not batch_started_paused and queue_is_paused():
            break
        print(f"[{index}/{len(sources)}] {source.name}", flush=True)
        output = output_dir / default_output_for(
            source,
            add_filename_tags=bool(getattr(args, "add_filename_tags", True)),
            output_format=resolve_conversion_settings(args)["output_format"],
        ).name
        requested = f"{args.name_prefix}-{source.stem}" if args.name_prefix else source.stem
        job_path, job = prepare_job(args, source=source, output=output, requested_name=requested)
        prepared.append((job_path, job))
    queued = [(job_path, job) for job_path, job in prepared if queue_prepared_job(job_path, job)]
    jobs = [job for _, job in queued]
    if not jobs:
        raise PipelineError("No jobs remained after the batch was canceled")
    dispatcher_pid = ensure_dispatcher()
    print(f"Queued {len(jobs)} job(s); conversions run one at a time.")
    print(f"Queue dispatcher PID: {dispatcher_pid}")
    print("Check queue: python dvd2hevc.py jobs")
    if jobs:
        print(f"Watch first job: python dvd2hevc.py status {jobs[0]['id']} --watch")
    return 0


def watched_batch_files() -> list[Path]:
    from . import watched_batches
    return watched_batches.watched_batch_files(services=sys.modules[__name__])


def make_watched_batch_id(source_dir: Path) -> str:
    from . import watched_batches
    return watched_batches.make_watched_batch_id(source_dir, services=sys.modules[__name__])


def try_read_watched_batch(path: Path) -> dict[str, Any] | None:
    from . import watched_batches
    return watched_batches.try_read_watched_batch(path, services=sys.modules[__name__])


def resolve_watched_batch(identifier: str) -> tuple[Path, dict[str, Any]]:
    from . import watched_batches
    return watched_batches.resolve_watched_batch(identifier, services=sys.modules[__name__])


def watched_batch_summary(watch: dict[str, Any]) -> dict[str, int]:
    from . import watched_batches
    return watched_batches.watched_batch_summary(watch, services=sys.modules[__name__])


def _watch_namespace(watch: dict[str, Any], source: Path, output: Path) -> argparse.Namespace:
    from . import watched_batches
    return watched_batches._watch_namespace(watch, source, output, services=sys.modules[__name__])


def _watched_iso_sources(watch: dict[str, Any]) -> list[Path]:
    from . import watched_batches
    return watched_batches._watched_iso_sources(watch, services=sys.modules[__name__])


def _reconcile_renamed_watch_entry(
    ledger: dict[str, dict[str, Any]],
    *,
    key: str,
    source: Path,
    fingerprint: str,
    source_keys: set[str],
    now: float,
) -> dict[str, Any] | None:
    """Move a durable ledger entry with its ISO instead of duplicating it."""
    from . import watched_batches
    return watched_batches._reconcile_renamed_watch_entry(ledger, key=key, source=source, fingerprint=fingerprint, source_keys=source_keys, now=now, services=sys.modules[__name__])


def scan_watched_batch(watch_path: Path, *, now: float | None = None) -> dict[str, Any]:
    """Discover stable ISOs, queue each fingerprint once, and persist the ledger."""
    from . import watched_batches
    return watched_batches.scan_watched_batch(watch_path, now=now, services=sys.modules[__name__])


def _spawn_watched_batch(watch_path: Path) -> int:
    from . import watched_batches
    return watched_batches._spawn_watched_batch(watch_path, services=sys.modules[__name__])


def ensure_active_watchers() -> list[int]:
    from . import watched_batches
    return watched_batches.ensure_active_watchers(services=sys.modules[__name__])


def session_recovery_main() -> int:
    """Restore durable queue/watch work at Windows logon without unpausing it."""
    recover_jobs_interrupted_by_restart()
    if queue_is_paused():
        return 0
    ensure_active_watchers()
    if queued_jobs():
        ensure_dispatcher()
    return 0


def create_watched_batch(
    args: argparse.Namespace,
    *,
    source_dir: Path | None = None,
    output_dir: Path | None = None,
    recursive: bool | None = None,
    resume_queue: bool = True,
) -> tuple[Path, dict[str, Any]]:
    from . import watched_batches
    return watched_batches.create_watched_batch(args, source_dir=source_dir, output_dir=output_dir, recursive=recursive, resume_queue=resume_queue, services=sys.modules[__name__])


def stop_watched_batch(identifier: str) -> dict[str, Any]:
    from . import watched_batches
    return watched_batches.stop_watched_batch(identifier, services=sys.modules[__name__])


def reset_watched_batch(identifier: str) -> tuple[Path, dict[str, Any]]:
    from . import watched_batches
    return watched_batches.reset_watched_batch(identifier, services=sys.modules[__name__])


def resume_watched_batch(identifier: str) -> dict[str, Any]:
    """Restart a watcher in place while retaining its discovery ledger."""
    from . import watched_batches
    return watched_batches.resume_watched_batch(identifier, services=sys.modules[__name__])


def watcher_main(watch_path_value: str) -> int:
    from . import watched_batches
    return watched_batches.watcher_main(watch_path_value, services=sys.modules[__name__])


def cmd_watch_folder(args: argparse.Namespace) -> int:
    from . import watched_batches
    return watched_batches.cmd_watch_folder(args, services=sys.modules[__name__])


def cmd_watches(args: argparse.Namespace) -> int:
    from . import watched_batches
    return watched_batches.cmd_watches(args, services=sys.modules[__name__])


def cmd_stop_watch(args: argparse.Namespace) -> int:
    from . import watched_batches
    return watched_batches.cmd_stop_watch(args, services=sys.modules[__name__])


def cmd_reset_watch(args: argparse.Namespace) -> int:
    from . import watched_batches
    return watched_batches.cmd_reset_watch(args, services=sys.modules[__name__])


def cmd_resume_watch(args: argparse.Namespace) -> int:
    from . import watched_batches
    return watched_batches.cmd_resume_watch(args, services=sys.modules[__name__])


def pipeline_status(job: dict[str, Any]) -> dict[str, Any]:
    from . import job_progress
    return job_progress.pipeline_status(job, services=sys.modules[__name__])


# Empirical wall-clock calibration from uninterrupted compact-first jobs.
#
# Mission: Impossible and its bonus disc contributed 3,355 seconds of clean
# post-optimization timing. Their pooled work split was 0.50% setup, 79.75%
# video, 5.89% compact planning/audio layout, 6.47% compact-domain writing,
# 4.78% ISO authoring, and 2.61% final verification/audit/VLC gates. Keep these
# as cumulative milestones so the overall bar describes elapsed conversion
# work; the independent video/audio/mux bars continue to describe their lanes.
PIPELINE_SETUP_END = 0.5
PIPELINE_VIDEO_END = 80.5
PIPELINE_COMPACT_PLAN_END = 84.8
PIPELINE_BASE_STAGE_END = 85.9
PIPELINE_AUDIO_LAYOUT_END = 86.3
PIPELINE_COMPACT_DOMAINS_END = 92.7
PIPELINE_AUTHOR_END = 97.4
PIPELINE_VERIFY_END = 97.7
PIPELINE_AUDIT_END = 97.9


def stage_percent(stage: str, state: str) -> float:
    from . import job_progress
    return job_progress.stage_percent(stage, state, services=sys.modules[__name__])


def progress_bar(percent: float, width: int=30) -> str:
    from . import job_progress
    return job_progress.progress_bar(percent, width, services=sys.modules[__name__])


def nested_progress_detail(job: dict[str, Any]) -> str | None:
    from . import job_progress
    return job_progress.nested_progress_detail(job, services=sys.modules[__name__])


def progress_events(job: dict[str, Any]) -> list[dict[str, Any]]:
    from . import job_progress
    return job_progress.progress_events(job, services=sys.modules[__name__])


def measured_progress(event: dict[str, Any]) -> tuple[float, str] | None:
    from . import job_progress
    return job_progress.measured_progress(event, services=sys.modules[__name__])


def _compact_domain_fraction(events: list[dict[str, Any]]) -> float:
    from . import job_progress
    return job_progress._compact_domain_fraction(events, services=sys.modules[__name__])


def _latest_lane_detail(values: list[dict[str, Any]], lane: str) -> str:
    from . import job_progress
    return job_progress._latest_lane_detail(values, lane, services=sys.modules[__name__])


def _video_work_weights(job: dict[str, Any], status: dict[str, Any]) -> tuple[dict[int, float], list[float]]:
    from . import job_progress
    return job_progress._video_work_weights(job, status, services=sys.modules[__name__])


def _physical_vts_fractions(job: dict[str, Any], physical_weights: dict[int, float], events: list[dict[str, Any]]) -> tuple[dict[int, float], dict[int, str]]:
    from . import job_progress
    return job_progress._physical_vts_fractions(job, physical_weights, events, services=sys.modules[__name__])


def _ordinary_video_fraction(job: dict[str, Any], status: dict[str, Any], events: list[dict[str, Any]]) -> tuple[float, str] | None:
    from . import job_progress
    return job_progress._ordinary_video_fraction(job, status, events, services=sys.modules[__name__])


def lane_progress_snapshot(job: dict[str, Any], status: dict[str, Any] | None=None) -> dict[str, tuple[float, str]]:
    from . import job_progress
    return job_progress.lane_progress_snapshot(job, status, services=sys.modules[__name__])


def task_lane_lines(job: dict[str, Any], *, width: int=16, status: dict[str, Any] | None=None) -> list[str]:
    from . import job_progress
    return job_progress.task_lane_lines(job, width=width, status=status, services=sys.modules[__name__])


def _pipeline_percent_current(job: dict[str, Any], status: dict[str, Any]) -> float:
    from . import job_progress
    return job_progress._pipeline_percent_current(job, status, services=sys.modules[__name__])


def _pipeline_event_floor(job: dict[str, Any]) -> float:
    from . import job_progress
    return job_progress._pipeline_event_floor(job, services=sys.modules[__name__])


def pipeline_percent(job: dict[str, Any], status: dict[str, Any]) -> float:
    from . import job_progress
    return job_progress.pipeline_percent(job, status, services=sys.modules[__name__])


def refresh_job(job_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    status = pipeline_status(job)
    state = str(status.get("state") or "")
    if str(job.get("status") or "") in {"running", "failed"} and bool(job.get("cancel_requested_at")):
        try:
            runner_pid = int(job.get("runner_pid") or 0)
        except (TypeError, ValueError):
            runner_pid = 0
        if not process_alive(runner_pid):
            job["status"] = "canceled"
            job["returncode"] = 3
            job["canceled_at"] = str(job.get("cancel_requested_at"))
            job["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            job.pop("error", None)
            save_job(job_path, job)
        return job
    if state in {"passed", "failed"} and job.get("status") == "running":
        job["status"] = state
        save_job(job_path, job)
        return job
    if job.get("status") == "running":
        try:
            runner_pid = int(job.get("runner_pid") or 0)
        except (TypeError, ValueError):
            runner_pid = 0
        if runner_pid > 0 and not process_alive(runner_pid):
            canceled = bool(job.get("cancel_requested_at")) or state == "canceled"
            job["status"] = "canceled" if canceled else "failed"
            job["returncode"] = 3 if canceled else 1
            job["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if not canceled:
                job["error"] = ABRUPT_RUNNER_ERROR
            save_job(job_path, job)
    return job


def job_status_lines(job_path: Path, job: dict[str, Any], *, width: int) -> list[str]:
    job = refresh_job(job_path, job)
    status = pipeline_status(job)
    state = str(job.get("status") or "unknown")
    stage = str(status.get("stage") or ("waiting for queue" if state == "queued" else state))
    percent = pipeline_percent(job, status)
    lines = [
        f"Disc: {Path(job['source']).stem}",
        f"Job: {job['id']}",
        f"{progress_bar(percent, width)} {percent:5.1f}% pipeline  Status: {state}",
        f"Stage: {stage}",
    ]
    message = status.get("message")
    if message:
        lines.append(f"Detail: {message}")
    nested = nested_progress_detail(job)
    if nested:
        lines.append(f"Active: {nested}")
    lines.extend(task_lane_lines(job, status=status))
    lines.extend(
        [
            f"Quality: {job['settings']['quality']}  "
            f"Encoder: {job['settings'].get('encoder', 'hevc_nvenc')} "
            f"{job['settings']['encoder_preset']}  "
            f"deinterlace: {job['settings']['deinterlace']}  "
            f"audio: {job['settings'].get('audio_mode', 'passthrough')}",
            f"Output: {job['output']}",
            f"Log: {job['log']}",
        ]
    )
    if state == "failed" and status.get("message"):
        lines.append("Run 'dvd2hevc diagnose JOB' to create a shareable support bundle.")
    return lines


def cmd_job_status(args: argparse.Namespace) -> int:
    job_path, job = resolve_job(getattr(args, "workspace", None))
    interval = float(getattr(args, "watch", 0) or 0)
    if getattr(args, "json", False):
        payload = {
            "job": refresh_job(job_path, job),
            "pipeline": pipeline_status(job),
        }
        print(json.dumps(payload, indent=2))
        return 2 if payload["job"].get("status") == "failed" else 0
    first = True
    previous_line_count = 0
    while True:
        job = read_json(job_path)
        lines = job_status_lines(job_path, job, width=args.width)
        if interval and sys.stdout.isatty() and not first:
            print(f"\x1b[{previous_line_count}A", end="")
        print("\n".join(lines), flush=True)
        previous_line_count = len(lines)
        first = False
        state = str(read_json(job_path).get("status") or "")
        if not interval or state in TERMINAL_JOB_STATES:
            return 2 if state == "failed" else 0
        time.sleep(interval)


def cmd_jobs(args: argparse.Namespace) -> int:
    jobs = [(path, job) for path in known_job_files() if (job := try_read_job(path))]
    if not jobs:
        print("No DVD2HEVC jobs found.")
        return 0
    if args.active:
        jobs = [(path, job) for path, job in jobs if job.get("status") in {"queued", "running"}]
    if args.failed:
        jobs = [(path, job) for path, job in jobs if job.get("status") == "failed"]
    if args.completed:
        jobs = [(path, job) for path, job in jobs if job.get("status") == "passed"]
    jobs = jobs[: args.limit]
    if not jobs:
        print("No matching DVD2HEVC jobs found.")
        return 0
    positions = {
        str(job.get("id")): index
        for index, (_path, job) in enumerate(queued_jobs(), start=1)
    }
    print(f"DVD2HEVC jobs  queue={'paused' if queue_is_paused() else 'running'}")
    print(f"{'status':9} {'queue':5} {'quality':7} {'audio':15} {'job'}")
    print(f"{'-' * 9} {'-' * 5} {'-' * 7} {'-' * 15} {'-' * 40}")
    for path, job in jobs:
        refreshed = refresh_job(path, job)
        position = positions.get(str(refreshed.get("id")))
        print(
            f"{str(refreshed.get('status', 'unknown')):9} "
            f"{('#' + str(position)) if position else '-':5} "
            f"{str((refreshed.get('settings') or {}).get('quality', '')):7} "
            f"{str((refreshed.get('settings') or {}).get('audio_mode', 'passthrough')):15} "
            f"{refreshed.get('id')}"
        )
    return 0


def cmd_pause_queue(args: argparse.Namespace) -> int:
    update_queue_control(paused=True, reason="pause-all")
    print("DVD2HEVC paused. No later disc will start and watched folders will not add jobs.")
    return 0


def cmd_resume_queue(args: argparse.Namespace) -> int:
    update_queue_control(paused=False, reason="resume-all")
    dispatcher_pid = ensure_dispatcher()
    print(f"DVD2HEVC resumed. Queued discs and watched-folder discovery may continue. Dispatcher PID: {dispatcher_pid}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    job_path, job = resolve_job(args.job)
    job = refresh_job(job_path, job)
    state = str(job.get("status") or "unknown")
    if state == "running":
        runner_pid = int(job.get("runner_pid") or 0)
        if not process_alive(runner_pid):
            job["status"] = "canceled"
            job["canceled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            job["ended_at"] = job["canceled_at"]
            job["error"] = "Canceled after its recorded runner process had already exited"
            save_job(job_path, job)
            print(f"Canceled stale DVD2HEVC job: {job['id']}")
            print("Its resumable work and plan were retained.")
            return 0
        canceled = cancel_running_job(
            job_path, job, reason="Canceled by user",
        )
        if canceled.get("status") != "canceled":
            raise PipelineError(
                f"Cancellation was recorded, but the runner did not exit promptly: {job['id']}"
            )
        print(f"Canceled running DVD2HEVC job immediately: {job['id']}")
        print("Its resumable work and completed atomic stages were retained.")
        return 0
    if state in TERMINAL_JOB_STATES:
        if state == "canceled":
            print(f"Job is already canceled: {job['id']}")
            return 0
        raise PipelineError(f"Only planned or queued jobs can be canceled; this job is {state}")
    if state not in {"planned", "queued"}:
        raise PipelineError(f"Job cannot be canceled from state {state!r}")
    job["status"] = "canceled"
    job["canceled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_job(job_path, job)
    print(f"Canceled DVD2HEVC job: {job['id']}")
    print("Its resumable work and plan were retained. Use 'resume JOB' to queue it again.")
    return 0


def cmd_cancel_all(args: argparse.Namespace) -> int:
    """Stop discovery and cancel every non-terminal job without deleting work."""
    now_text = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    update_queue_control(
        paused=True,
        reason="cancel-all",
        cancel_generation=time.time_ns(),
    )

    stopped_watches = 0
    for path in watched_batch_files():
        watch = try_read_watched_batch(path)
        if not watch or watch.get("status") != "active":
            continue
        watch["status"] = "stopped"
        watch["stopped_at"] = now_text
        watch["stopped_by"] = "cancel-all"
        write_json_atomic(path, watch)
        stopped_watches += 1

    canceled_waiting = 0
    requested_running = 0
    canceled_stale = 0
    errors: list[str] = []
    for job_path in known_job_files():
        job = try_read_job(job_path)
        if not job:
            continue
        try:
            job = refresh_job(job_path, job)
            state = str(job.get("status") or "")
            if state in {"planned", "queued"}:
                job["status"] = "canceled"
                job["canceled_at"] = now_text
                job["ended_at"] = now_text
                job["error"] = "Canceled by Cancel all"
                save_job(job_path, job)
                canceled_waiting += 1
                continue
            if state != "running":
                continue
            try:
                runner_pid = int(job.get("runner_pid") or 0)
            except (TypeError, ValueError):
                runner_pid = 0
            if not process_alive(runner_pid):
                job["status"] = "canceled"
                job["canceled_at"] = now_text
                job["ended_at"] = now_text
                job["error"] = "Canceled after its recorded runner process had already exited"
                save_job(job_path, job)
                canceled_stale += 1
                continue
            canceled = cancel_running_job(
                job_path, job, reason="Canceled by Cancel all",
            )
            if canceled.get("status") == "canceled":
                requested_running += 1
            else:
                errors.append(f"{job.get('id')}: runner did not exit promptly")
        except (OSError, PipelineError, TypeError, ValueError) as exc:
            errors.append(f"{job.get('id', job_path.parent.name)}: {exc}")

    print("DVD2HEVC Cancel all completed.")
    print(f"Watched batches stopped: {stopped_watches}")
    print(f"Waiting jobs canceled: {canceled_waiting}")
    print(f"Running jobs stopped immediately: {requested_running}")
    print(f"Stale running records canceled immediately: {canceled_stale}")
    print("The queue remains paused. Resumable work and completed outputs were retained.")
    if errors:
        raise PipelineError("Some jobs could not be canceled:\n" + "\n".join(errors))
    return 0


def cmd_resume_job(args: argparse.Namespace) -> int:
    job_path, job = resolve_job(args.job)
    job = refresh_job(job_path, job)
    state = str(job.get("status") or "unknown")
    if state in {"queued", "running"}:
        raise PipelineError(f"Job is already {state}: {job['id']}")
    if state == "passed":
        raise PipelineError(f"Job has already passed: {job['id']}")
    if state not in {"planned", "failed", "canceled"}:
        raise PipelineError(f"Job cannot be resumed from state {state!r}")
    output = Path(str(job.get("output") or ""))
    if output.exists() and not _existing_output_is_authored_job_artifact(job):
        raise PipelineError(
            f"The output already exists, so it will not be overwritten: {output}"
        )
    _reset_job_for_resume(job_path, job, preserve_queue_order=False)
    dispatcher_pid = ensure_dispatcher()
    print(f"Resumed DVD2HEVC job: {job['id']}")
    print(f"Queue dispatcher PID: {dispatcher_pid}")
    print(f"Watch progress: python dvd2hevc.py status {job['id']} --watch")
    return 0


def _existing_output_is_authored_job_artifact(job: dict[str, Any]) -> bool:
    """Recognize an output this same job already authored before a later failure."""
    output = Path(str(job.get("output") or ""))
    work_root = Path(str(job.get("work_root") or ""))
    if job.get("settings", {}).get("output_format") == "uhd-bd":
        receipt = work_root / "uhd/uhd-output.json"
        try:
            value = read_json(receipt)
            return (output.is_file() and value.get("output") == str(output.resolve())
                    and value.get("stock_vlc_gate", {}).get("passed", False)
                    and isinstance(value.get("manifest"), dict))
        except (PipelineError, OSError):
            return False
    report_path = work_root / "final-output-reports" / "author.json"
    if not output.is_file() or not report_path.is_file():
        return False
    try:
        report = read_json(report_path)
        destination = Path(str(report.get("destination") or "")).resolve()
        stage_report = Path(str(report.get("stage_report") or ""))
        recorded_size = int(report.get("size") or -1)
    except (OSError, PipelineError, TypeError, ValueError):
        return False
    return (
        report.get("schema") == "dvd2hevc-authored-iso-v0"
        and report.get("status") == "authored"
        and destination == output.resolve()
        and recorded_size == output.stat().st_size
        and stage_report.is_file()
    )


def cmd_play(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser()
    if not target.is_file():
        _, job = resolve_job(args.target)
        target = Path(job["output"])
    target = target.resolve()
    if not target.is_file():
        raise PipelineError(f"DVD2HEVC ISO was not found: {target}")
    from .uhd import discover_uhd_tools
    from dvd2uhd.udf_reader import UdfImage
    try:
        is_uhd = any(name.upper() == "BDMV/INDEX.BDMV" for name in UdfImage(target).files)
    except (ValueError, OSError, KeyError):
        is_uhd = False
    vlc_root = (find_stock_vlc_root if is_uhd else find_patched_vlc_root)(args.vlc_root)
    if vlc_root is None:
        raise PipelineError("Suitable VLC was not found; pass --vlc-root (stock VLC with BD-J for UHD-BD)")
    environment = os.environ.copy()
    if is_uhd:
        playback = discover_uhd_tools(vlc_root)
        if not playback['java']: raise PipelineError('Java is required for UHD-BD DVD navigation')
        environment['JAVA_HOME'] = playback['java_home']
    uri = ("bluray:///" if is_uhd else "dvd:///") + quote(target.as_posix(), safe="/:")
    subprocess.Popen(
        [
            str(vlc_root / "vlc.exe"),
            "--no-one-instance",
            "--no-video-title-show",
            "--disc-caching=1000",
            uri,
        ],
        cwd=str(vlc_root),
        env=environment,
        **hidden_subprocess_kwargs(),
    )
    print(f"Opened {'UHD-BD in stock VLC' if is_uhd else 'legacy HEVC DVD in private VLC'}: {target}")
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    if args.preset_command == "list":
        print("DVD2HEVC presets")
        for name, settings in sorted(all_presets().items()):
            suffix = " (built-in)" if settings.get("builtin") else ""
            print(
                f"  {name}{suffix}: {settings.get('quality')}  "
                f"{settings.get('encoder', 'hevc_nvenc')} {settings.get('encoder_preset')}  "
                f"deinterlace={settings.get('deinterlace')}  "
                f"audio={settings.get('audio_mode', 'passthrough')}"
            )
        return 0
    if args.preset_command == "show":
        print(json.dumps(resolve_preset(args.name), indent=2))
        return 0
    if args.preset_command == "save":
        quality = canonical_quality(args.quality)
        path = save_named_preset(
            args.name,
            quality=quality,
            encoder=args.encoder,
            encoder_preset=args.encoder_preset,
            deinterlace=args.deinterlace,
            audio_mode=args.audio_mode,
            stereo_audio_bitrate=args.stereo_audio_bitrate,
            mono_audio_bitrate=args.mono_audio_bitrate,
            audio_workers=args.audio_workers,
            pipeline_depth=args.pipeline_depth,
            target_bitrate_multiplier=args.target_bitrate_multiplier,
            bitrate_mode=args.bitrate_mode,
            main_title_quality=(canonical_quality(args.main_title_quality) if args.main_title_quality else None),
            top_n_quality=(canonical_quality(args.top_n_quality) if args.top_n_quality else None),
            top_n_count=args.top_n_count,
            audio_language_overrides=parse_audio_language_overrides(args.audio_language),
        )
        print(f"Saved preset {args.name!r}: {path}")
        return 0
    if args.preset_command == "remove":
        path = remove_named_preset(args.name)
        print(f"Removed preset {args.name!r}: {path}")
        return 0
    raise PipelineError("A preset subcommand is required")


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui
    return launch_gui()


def add_conversion_options(parser: argparse.ArgumentParser, *, include_output: bool = True) -> None:
    add_output_options(parser)
    parser.add_argument("source", help="Decrypted DVD ISO backup")
    if include_output:
        parser.add_argument(
            "output", nargs="?",
            help="Output ISO; defaults to the tagged generated name, or an untagged safe name with --no-filename-tags",
        )
    parser.add_argument(
        "--no-filename-tags", "--no-output-tags",
        dest="add_filename_tags", action="store_false", default=True,
        help="Generate an untagged 'SOURCE - converted.iso' name instead of adding (DVD) (HEVC)",
    )
    parser.add_argument(
        "--quality",
        help="target-bitrate, HandBrake-style cq:N, or a quality alias; overrides the named preset",
    )
    parser.add_argument(
        "--target-bitrate-multiplier", type=float,
        help="Multiply the 25%% source-derived video target (default: 1.0; range: 0.25-4.0)",
    )
    parser.add_argument(
        "--bitrate-mode", choices=("vbr", "cbr"),
        help="Target-bitrate rate control: variable bitrate (default) or constant bitrate",
    )
    parser.add_argument(
        "--auto-cq-multiplier", dest="target_bitrate_multiplier", type=float,
        help=argparse.SUPPRESS,
    )
    title_quality = parser.add_mutually_exclusive_group()
    title_quality.add_argument(
        "--main-title-quality",
        help="Manual CQ override for the longest title; shared cells apply it to the whole VTS",
    )
    title_quality.add_argument(
        "--top-n-quality",
        help="Manual CQ override for the longest --top-n-count titles",
    )
    parser.add_argument("--top-n-count", type=int, help="Number of longest titles for --top-n-quality")
    parser.add_argument("--preset", help="Saved or built-in conversion preset (default: balanced)")
    parser.add_argument(
        "--encoder", choices=HEVC_ENCODERS,
        help="HEVC encoder backend (default: preset value, normally hevc_nvenc)",
    )
    parser.add_argument(
        "--encoder-preset",
        help="Backend-neutral efficiency preset p1-p7; p1 fastest, p7 slowest (default: p6)",
    )
    parser.add_argument(
        "--deinterlace",
        choices=("auto", "always", "off"),
        help="Deinterlace policy (default: auto; ambiguous cells are deinterlaced)",
    )
    parser.add_argument(
        "--audio-mode",
        choices=("passthrough", "compact-stereo"),
        help="Preserve source audio or downmix each title track to compact DVD AC-3 stereo",
    )
    parser.add_argument(
        "--audio-language", action="append", metavar="LANG=MODE",
        help="Per-language override, repeatable (for example eng=passthrough or other=compact-stereo)",
    )
    parser.add_argument("--stereo-audio-bitrate", help="Compact-stereo rate (default: 256k)")
    parser.add_argument("--mono-audio-bitrate", help="Compact mono-source rate (default: 128k)")
    parser.add_argument("--audio-workers", type=int, choices=range(1, 9), help="Parallel tracks per title set")
    parser.add_argument("--pipeline-depth", type=int, choices=range(1, 9), help="Parallel compact-audio title sets")
    parser.add_argument("--label", help="DVD volume label (ASCII, maximum 32 characters)")
    parser.add_argument("--work-dir", help="Resumable conversion workspace")
    parser.add_argument("--vlc-root", help="Stock VLC for UHD-BD; private patched VLC for legacy dvd-hevc")
    parser.add_argument("--name", help="Friendly job name")
    parser.add_argument("--resume", action="store_true", help="Reuse an explicit --work-dir after interruption")


def add_user_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    add_uhd_commands(commands)
    p_gui = commands.add_parser("gui", help="Open the DVD2HEVC Windows graphical interface")
    p_gui.set_defaults(func=cmd_gui)

    p_auto = commands.add_parser(
        "auto",
        help="Convert one decrypted DVD ISO to a compact menu-preserving HEVC ISO",
        description="Plan, convert, compact, author, and verify a complete DVD backup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python dvd2hevc.py auto MOVIE.iso\n"
            "  python dvd2hevc.py auto MOVIE.iso 'MOVIE (DVD) (HEVC).iso' --quality cq:24\n"
            "  python dvd2hevc.py auto MOVIE.iso --no-filename-tags\n"
            "  python dvd2hevc.py auto MOVIE.iso --preset compact --dry-run"
        ),
    )
    add_conversion_options(p_auto)
    p_auto.add_argument("--dry-run", action="store_true", help="Run preflight and compatibility planning only")
    p_auto.set_defaults(func=cmd_auto)

    p_start = commands.add_parser(
        "start",
        help="Queue one conversion in the background and return immediately",
    )
    add_conversion_options(p_start)
    p_start.set_defaults(func=cmd_start)

    p_queue = commands.add_parser(
        "queue",
        help="Queue multiple DVD ISOs for sequential background conversion",
    )
    add_output_options(p_queue)
    p_queue.add_argument("sources", nargs="+", help="ISO files or directories containing DVD ISOs")
    p_queue.add_argument("--output-dir", required=True, help="Destination directory for converted ISOs")
    p_queue.add_argument("--recursive", action="store_true", help="Search source directories recursively")
    p_queue.add_argument(
        "--no-filename-tags", "--no-output-tags",
        dest="add_filename_tags", action="store_false", default=True,
        help="Generate untagged 'SOURCE - converted.iso' names instead of adding (DVD) (HEVC)",
    )
    p_queue.add_argument("--quality", help="target-bitrate, HandBrake-style cq:N, or an alias")
    p_queue.add_argument("--target-bitrate-multiplier", type=float)
    p_queue.add_argument("--bitrate-mode", choices=("vbr", "cbr"))
    p_queue.add_argument("--auto-cq-multiplier", dest="target_bitrate_multiplier", type=float, help=argparse.SUPPRESS)
    queue_title_quality = p_queue.add_mutually_exclusive_group()
    queue_title_quality.add_argument("--main-title-quality")
    queue_title_quality.add_argument("--top-n-quality")
    p_queue.add_argument("--top-n-count", type=int)
    p_queue.add_argument("--preset", help="Saved or built-in conversion preset")
    p_queue.add_argument("--encoder", choices=HEVC_ENCODERS)
    p_queue.add_argument("--encoder-preset", help="Efficiency preset p1-p7")
    p_queue.add_argument("--deinterlace", choices=("auto", "always", "off"))
    p_queue.add_argument("--audio-mode", choices=("passthrough", "compact-stereo"))
    p_queue.add_argument("--audio-language", action="append", metavar="LANG=MODE")
    p_queue.add_argument("--stereo-audio-bitrate")
    p_queue.add_argument("--mono-audio-bitrate")
    p_queue.add_argument("--audio-workers", type=int, choices=range(1, 9))
    p_queue.add_argument("--pipeline-depth", type=int, choices=range(1, 9))
    p_queue.add_argument("--vlc-root", help="Stock VLC for UHD-BD; private player for legacy dvd-hevc")
    p_queue.add_argument("--label", help="Shared volume-label base; individual source names remain the default")
    p_queue.add_argument("--name-prefix", help="Prefix for generated job names")
    p_queue.set_defaults(func=cmd_queue, output=None, work_dir=None, name=None, resume=False)

    p_watch = commands.add_parser(
        "watch-folder",
        help="Continuously add new DVD ISOs from a folder to the background queue",
    )
    add_output_options(p_watch)
    p_watch.add_argument("source_dir", help="Folder to keep watching for decrypted DVD ISOs")
    p_watch.add_argument("--output-dir", required=True, help="Destination folder for converted ISOs")
    p_watch.add_argument("--recursive", action="store_true", help="Watch subfolders too")
    p_watch.add_argument("--poll-seconds", type=float, default=15.0, help="Folder rescan interval (minimum 5 seconds)")
    p_watch.add_argument(
        "--settle-seconds", type=float, default=60.0,
        help="Require an ISO to remain unchanged for this long before planning it",
    )
    p_watch.add_argument(
        "--no-filename-tags", "--no-output-tags",
        dest="add_filename_tags", action="store_false", default=True,
        help="Generate untagged 'SOURCE - converted.iso' names instead of adding (DVD) (HEVC)",
    )
    p_watch.add_argument("--quality", help="target-bitrate, HandBrake-style cq:N, or an alias")
    p_watch.add_argument("--target-bitrate-multiplier", type=float)
    p_watch.add_argument("--bitrate-mode", choices=("vbr", "cbr"))
    watch_title_quality = p_watch.add_mutually_exclusive_group()
    watch_title_quality.add_argument("--main-title-quality")
    watch_title_quality.add_argument("--top-n-quality")
    p_watch.add_argument("--top-n-count", type=int)
    p_watch.add_argument("--preset", help="Saved or built-in conversion preset")
    p_watch.add_argument("--encoder", choices=HEVC_ENCODERS)
    p_watch.add_argument("--encoder-preset", help="Efficiency preset p1-p7")
    p_watch.add_argument("--deinterlace", choices=("auto", "always", "off"))
    p_watch.add_argument("--audio-mode", choices=("passthrough", "compact-stereo"))
    p_watch.add_argument("--audio-language", action="append", metavar="LANG=MODE")
    p_watch.add_argument("--stereo-audio-bitrate")
    p_watch.add_argument("--mono-audio-bitrate")
    p_watch.add_argument("--audio-workers", type=int, choices=range(1, 9))
    p_watch.add_argument("--pipeline-depth", type=int, choices=range(1, 9))
    p_watch.add_argument("--vlc-root", help="Stock VLC for UHD-BD; private player for legacy dvd-hevc")
    p_watch.add_argument("--label", help="Shared volume-label base")
    p_watch.add_argument("--name-prefix", help="Prefix for generated job names")
    p_watch.set_defaults(
        func=cmd_watch_folder, output=None, work_dir=None, name=None, resume=False,
    )

    p_watches = commands.add_parser("watches", help="List persistent watched batches")
    p_watches.add_argument("--limit", type=int, default=20)
    p_watches.set_defaults(func=cmd_watches)

    p_stop_watch = commands.add_parser("stop-watch", help="Stop discovering new ISOs for a watched batch")
    p_stop_watch.add_argument("watch", help="Watched batch id or unique id fragment")
    p_stop_watch.set_defaults(func=cmd_stop_watch)

    p_reset_watch = commands.add_parser(
        "reset-watch", help="Start a fresh watched batch with the same folders and settings"
    )
    p_reset_watch.add_argument("watch", help="Watched batch id or unique id fragment")
    p_reset_watch.set_defaults(func=cmd_reset_watch)

    p_resume_watch = commands.add_parser(
        "resume-watch", help="Restart a stopped or failed watched batch without clearing its history"
    )
    p_resume_watch.add_argument("watch", help="Watched batch id or unique id fragment")
    p_resume_watch.set_defaults(func=cmd_resume_watch)

    p_jobs = commands.add_parser("jobs", help="List foreground and background conversion jobs")
    filters = p_jobs.add_mutually_exclusive_group()
    filters.add_argument("--active", action="store_true")
    filters.add_argument("--failed", action="store_true")
    filters.add_argument("--completed", action="store_true")
    p_jobs.add_argument("--limit", type=int, default=20)
    p_jobs.set_defaults(func=cmd_jobs)

    p_cancel = commands.add_parser("cancel", help="Withdraw a planned or queued conversion job")
    p_cancel.add_argument("job", help="Job id or unique job-name fragment")
    p_cancel.set_defaults(func=cmd_cancel)

    p_cancel_all = commands.add_parser(
        "cancel-all",
        help="Stop watched batches and cancel every waiting or running conversion",
    )
    p_cancel_all.set_defaults(func=cmd_cancel_all)

    p_resume = commands.add_parser("resume", help="Resume a planned, failed, or canceled job")
    p_resume.add_argument("job", help="Job id or unique job-name fragment")
    p_resume.set_defaults(func=cmd_resume_job)

    p_pause_queue = commands.add_parser(
        "pause-queue", help="Pause later discs and stop watched folders from adding jobs"
    )
    p_pause_queue.set_defaults(func=cmd_pause_queue)

    p_resume_queue = commands.add_parser("resume-queue", help="Resume queued discs and watched-folder discovery")
    p_resume_queue.set_defaults(func=cmd_resume_queue)

    p_play = commands.add_parser("play", help="Open an output using stock VLC for UHD-BD or the legacy private player")
    p_play.add_argument("target", help="Converted ISO path or job id")
    p_play.add_argument("--vlc-root", help="Stock VLC for UHD-BD; private player for legacy dvd-hevc")
    p_play.set_defaults(func=cmd_play)

    p_preset = commands.add_parser("preset", help="Manage reusable conversion presets")
    preset_commands = p_preset.add_subparsers(dest="preset_command", required=True)
    preset_commands.add_parser("list", help="List built-in and saved presets")
    p_show = preset_commands.add_parser("show", help="Show one preset as JSON")
    p_show.add_argument("name")
    p_save = preset_commands.add_parser("save", help="Save a named preset")
    p_save.add_argument("name")
    p_save.add_argument("--quality", required=True, help="target-bitrate or HandBrake-style cq:N")
    p_save.add_argument("--target-bitrate-multiplier", type=float, default=1.0)
    p_save.add_argument("--bitrate-mode", choices=("vbr", "cbr"), default="vbr")
    p_save.add_argument("--auto-cq-multiplier", dest="target_bitrate_multiplier", type=float, help=argparse.SUPPRESS)
    preset_title_quality = p_save.add_mutually_exclusive_group()
    preset_title_quality.add_argument("--main-title-quality")
    preset_title_quality.add_argument("--top-n-quality")
    p_save.add_argument("--top-n-count", type=int, default=0)
    p_save.add_argument("--encoder", default="hevc_nvenc", choices=HEVC_ENCODERS)
    p_save.add_argument("--encoder-preset", default="p6", choices=tuple(f"p{i}" for i in range(1, 8)))
    p_save.add_argument("--deinterlace", default="auto", choices=("auto", "always", "off"))
    p_save.add_argument("--audio-mode", default="passthrough", choices=("passthrough", "compact-stereo"))
    p_save.add_argument("--audio-language", action="append", default=[], metavar="LANG=MODE")
    p_save.add_argument("--stereo-audio-bitrate", default="256k")
    p_save.add_argument("--mono-audio-bitrate", default="128k")
    p_save.add_argument("--audio-workers", type=int, default=2, choices=range(1, 9))
    p_save.add_argument("--pipeline-depth", type=int, default=2, choices=range(1, 9))
    p_remove = preset_commands.add_parser("remove", help="Remove a saved preset")
    p_remove.add_argument("name")
    p_preset.set_defaults(func=cmd_preset)


if __name__ == "__main__":
    if sys.argv[1:] == ["_dispatch"]:
        raise SystemExit(dispatcher_main())
    if sys.argv[1:] == ["_recover-session"]:
        raise SystemExit(session_recovery_main())
    if len(sys.argv) == 3 and sys.argv[1] == "_watch":
        raise SystemExit(watcher_main(sys.argv[2]))
    raise SystemExit(2)
