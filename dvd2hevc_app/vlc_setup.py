"""Safe discovery and preparation of the private DVD2HEVC VLC player."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .contracts import PLAYER_ABI
from .pipeline import PipelineError
from .subprocess_utils import hidden_subprocess_kwargs


from .paths import ROOT, REPORT_ROOT, STATE_ROOT, TOOL_ROOT
EXPECTED_VLC_VERSION = "3.0.23"
EXPECTED_VLC_COMMIT = "578d28f6c9f2379164516e689418f92ac74a3445"
PATCH_PATH = ROOT / "patches" / "vlc" / "0001-dvdnav-accept-hevc-program-stream-maps.patch"
PREPARE_SCRIPT = ROOT / "tools" / "prepare-vlc-dvdhevc.ps1"
MANIFEST_NAME = "DVD2HEVC-VLC-BUILD.json"
PLUGIN_RELATIVE = Path("plugins") / "access" / "libdvdnav_plugin.dll"
PLUGIN_MARKERS = (
    b"DVD-HEVC program stream map accepted",
    b"resetting clock and restarting video for HEVC menu cell",
)

# This is the release-gated private plugin currently used by the difficult-disc
# fixtures. New local builds are accepted through their exact build manifest,
# so compiler updates do not require adding another hash here.
KNOWN_PLUGIN_SHA256 = {
    "8F68FB1123E127B9EB129C916231E1AF897D5EFF46DBBA1922B3E8AE907D3A81",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _contains(path: Path, value: bytes) -> bool:
    try:
        with path.open("rb") as handle:
            overlap = max(0, len(value) - 1)
            previous = b""
            while chunk := handle.read(1024 * 1024):
                data = previous + chunk
                if value in data:
                    return True
                previous = data[-overlap:] if overlap else b""
    except OSError:
        return False
    return False


def managed_vlc_root() -> Path:
    """Return the private player destination managed by this project."""
    configured = os.environ.get("DVD2HEVC_MANAGED_VLC_ROOT")
    return Path(configured).expanduser().resolve() if configured else (STATE_ROOT / "vlc-dvdhevc").resolve()


def private_vlc_candidates(explicit: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("DVD2HEVC_VLC_ROOT"):
        candidates.append(Path(os.environ["DVD2HEVC_VLC_ROOT"]))
    candidates.extend((managed_vlc_root(), ROOT / "work" / "phase2" / "vlc-dvdhevc"))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def inspect_private_vlc(root: str | Path) -> dict[str, Any]:
    """Verify that a private VLC contains this release's DVD-HEVC plugin."""
    path = Path(root).expanduser().resolve()
    executable = path / "vlc.exe"
    plugin = path / PLUGIN_RELATIVE
    manifest_path = path / MANIFEST_NAME
    reasons: list[str] = []
    if not executable.is_file():
        reasons.append("vlc.exe is missing")
    if not plugin.is_file():
        reasons.append("the DVD navigation plugin is missing")
    version_ok = executable.is_file() and (
        _contains(executable, EXPECTED_VLC_VERSION.encode("ascii"))
        or _contains(executable, EXPECTED_VLC_VERSION.encode("utf-16le"))
    )
    if executable.is_file() and not version_ok:
        reasons.append(f"VLC is not the supported {EXPECTED_VLC_VERSION} release")
    markers_ok = plugin.is_file() and all(_contains(plugin, marker) for marker in PLUGIN_MARKERS)
    if plugin.is_file() and not markers_ok:
        reasons.append("the plugin does not contain the DVD2HEVC playback changes")

    plugin_sha256 = _sha256(plugin) if plugin.is_file() else None
    manifest: dict[str, Any] | None = None
    manifest_ok = False
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            manifest_ok = (
                manifest.get("schema") == "dvd2hevc-vlc-build-v1"
                and str(manifest.get("vlc_version", "")).startswith(EXPECTED_VLC_VERSION)
                and manifest.get("vlc_source_commit") == EXPECTED_VLC_COMMIT
                and str(manifest.get("patch_sha256", "")).upper() == _sha256(PATCH_PATH)
                and str(manifest.get("plugin_sha256", "")).upper() == plugin_sha256
                and str(manifest.get("player_abi", PLAYER_ABI)) == PLAYER_ABI
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            manifest_ok = False
        if not manifest_ok:
            reasons.append("the private-player build manifest does not match this DVD2HEVC release")

    known_binary = plugin_sha256 in KNOWN_PLUGIN_SHA256 if plugin_sha256 else False
    provenance_ok = manifest_ok or known_binary
    if plugin.is_file() and markers_ok and not provenance_ok:
        reasons.append("the plugin build is not verified by a matching manifest")
    ready = bool(executable.is_file() and plugin.is_file() and version_ok and markers_ok and provenance_ok)
    return {
        "root": str(path),
        "ready": ready,
        "vlc_version": EXPECTED_VLC_VERSION if version_ok else None,
        "player_abi": PLAYER_ABI if ready else None,
        "plugin_sha256": plugin_sha256,
        "verified_by": "manifest" if manifest_ok else ("release hash" if known_binary else None),
        "manifest": manifest,
        "reasons": [] if ready else reasons,
    }


def find_ready_vlc_root(explicit: str | Path | None = None) -> Path | None:
    for candidate in private_vlc_candidates(explicit):
        if inspect_private_vlc(candidate)["ready"]:
            return candidate
    return None


def _first_directory(candidates: Iterable[Path], required: Path) -> Path | None:
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if (path / required).exists():
            return path
    return None


def discover_vlc_base() -> Path | None:
    configured = [Path(os.environ["DVD2HEVC_VLC_BASE"])] if os.environ.get("DVD2HEVC_VLC_BASE") else []
    candidates = [
        *configured,
        Path(r"C:\Program Files\VideoLAN\VLC"),
        Path(r"C:\Program Files (x86)\VideoLAN\VLC"),
    ]
    base = _first_directory(candidates, Path("vlc.exe"))
    if base and inspect_vlc_base(base)["ready"]:
        return base
    return None


def discover_vlc_source() -> Path | None:
    configured = [Path(os.environ["DVD2HEVC_VLC_SOURCE"])] if os.environ.get("DVD2HEVC_VLC_SOURCE") else []
    return _first_directory(
        [*configured, STATE_ROOT / "vlc-source"],
        Path(".git"),
    )


def inspect_vlc_base(root: str | Path) -> dict[str, Any]:
    path = Path(root).expanduser().resolve()
    executable = path / "vlc.exe"
    ready = executable.is_file() and (
        _contains(executable, EXPECTED_VLC_VERSION.encode("ascii"))
        or _contains(executable, EXPECTED_VLC_VERSION.encode("utf-16le"))
    )
    return {
        "root": str(path),
        "ready": bool(ready),
        "reason": None if ready else f"Choose an unmodified VLC {EXPECTED_VLC_VERSION} Windows folder.",
    }


def validate_vlc_source(root: str | Path) -> dict[str, Any]:
    path = Path(root).expanduser().resolve()
    reasons: list[str] = []
    # A normal checkout has a .git directory; a Git worktree has a .git file.
    if not (path / ".git").exists():
        reasons.append("The selected folder is not a VLC Git checkout.")
    if not (path / "win64" / "modules").is_dir():
        reasons.append("The VLC checkout has not been prepared through the Windows build workflow.")
    git = shutil.which("git")
    if not git:
        reasons.append("Git is not available.")
    elif not reasons:
        completed = subprocess.run(
            [git, "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode or completed.stdout.strip() != EXPECTED_VLC_COMMIT:
            reasons.append(f"The source must be pinned to VLC {EXPECTED_VLC_VERSION} commit {EXPECTED_VLC_COMMIT}.")
    if not Path(r"C:\msys64\usr\bin\bash.exe").is_file():
        reasons.append(r"MSYS2 MINGW64 is required under C:\msys64 to build the plugin.")
    return {"root": str(path), "ready": not reasons, "reasons": reasons}


def prepare_private_vlc(
    source: str | Path,
    base: str | Path,
    destination: str | Path | None = None,
) -> dict[str, Any]:
    """Prepare a private player, returning without writes when it is ready."""
    target = Path(destination).expanduser().resolve() if destination else managed_vlc_root()
    current = inspect_private_vlc(target)
    if current["ready"]:
        return {**current, "changed": False, "message": "The private HEVC DVD player was already ready; no files were changed."}
    if target.exists() and not (target / MANIFEST_NAME).is_file():
        raise PipelineError(
            f"Refusing to overwrite an unrecognized folder: {target}. "
            "Choose an empty private-player destination or move that folder aside."
        )
    base_status = inspect_vlc_base(base)
    if not base_status["ready"]:
        raise PipelineError(str(base_status["reason"]))
    source_status = validate_vlc_source(source)
    if not source_status["ready"]:
        raise PipelineError("\n".join(source_status["reasons"]))
    if not PREPARE_SCRIPT.is_file() or not PATCH_PATH.is_file():
        raise PipelineError("The DVD2HEVC VLC preparation files are missing from this installation.")
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PREPARE_SCRIPT),
        "-SourcePath", str(Path(source).expanduser().resolve()),
        "-BaseVlcRoot", str(Path(base).expanduser().resolve()),
        "-Destination", str(target),
    ]
    if target.exists():
        command.append("-UpdateExisting")
    completed = subprocess.run(
        command, cwd=ROOT, check=False, capture_output=True, text=True,
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode:
        detail = "\n".join((completed.stdout + "\n" + completed.stderr).splitlines()[-20:]).strip()
        raise PipelineError(f"Private VLC setup failed. The normal VLC installation was not changed.\n\n{detail}")
    verified = inspect_private_vlc(target)
    if not verified["ready"]:
        raise PipelineError(
            "VLC preparation finished, but verification failed: " + "; ".join(verified["reasons"])
        )
    return {**verified, "changed": True, "message": f"Private HEVC DVD player prepared and verified at:\n{target}"}
