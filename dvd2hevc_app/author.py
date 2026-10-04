"""Create a DVD-Video ordered UDF image from a validated staged folder."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .udf_validation import UdfValidationError, validate_udf
from .contracts import capability_contract, contract_compatible

from .iso import _entry_name, close_iso_image, open_iso_image
from .native import find_dvdinspect, inspect_physical_graph
from .pipeline import PipelineError
from .subprocess_utils import hidden_subprocess_kwargs


def normalized_physical_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Remove acquisition diagnostics while retaining every DVD graph field."""
    return {
        key: value for key, value in graph.items()
        if key not in {"source", "tool", "stderr_tail"}
    }


def _wsl_path(path: Path, distro: str) -> str:
    completed = subprocess.run(
        ["wsl.exe", "-d", distro, "-e", "wslpath", "-a", str(path.resolve())],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise PipelineError(f"Could not translate Windows path for WSL: {completed.stderr.strip()}")
    return completed.stdout.strip()


def author_dvd_iso(
    stage: Path,
    destination: Path,
    *,
    label: str = "DVD2HEVC",
    wsl_distro: str = "Ubuntu-24.04",
    log_path: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    stage = stage.resolve()
    destination = destination.resolve()
    if destination == stage or stage in destination.parents:
        raise PipelineError("Refusing to author an ISO inside its staged DVD source")
    if not (stage / "VIDEO_TS" / "VIDEO_TS.IFO").is_file():
        raise PipelineError(f"Not a staged DVD root: {stage}")
    stage_report = stage / "dvd2hevc-stage-report.json"
    if not stage_report.is_file():
        raise PipelineError(f"Staged DVD report is missing: {stage_report}")
    stage_value = json.loads(stage_report.read_text(encoding="utf-8"))
    if stage_value.get("status") != "passed":
        raise PipelineError("Only a passed staged DVD can be authored")
    if destination.exists():
        raise PipelineError(f"ISO destination already exists: {destination}")
    if not 1 <= len(label) <= 32 or any(ord(character) > 0x7f for character in label):
        raise PipelineError("DVD volume label must be 1-32 ASCII characters")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if temporary.exists():
        raise PipelineError(f"Incomplete ISO already exists: {temporary}")

    native = shutil.which("genisoimage") or shutil.which("mkisofs")
    if native:
        command = [native, "-dvd-video", "-udf", "-V", label, "-o", str(temporary), str(stage)]
        backend = native
    elif shutil.which("wsl.exe"):
        probe = subprocess.run(
            ["wsl.exe", "-d", wsl_distro, "-e", "sh", "-c", "command -v genisoimage || command -v mkisofs"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        executable = probe.stdout.strip()
        if probe.returncode != 0 or not executable:
            raise PipelineError(
                f"genisoimage is unavailable in WSL distro {wsl_distro}; install the genisoimage package"
            )
        command = [
            "wsl.exe", "-d", wsl_distro, "-e", executable,
            "-dvd-video", "-udf", "-V", label,
            "-o", _wsl_path(temporary, wsl_distro), _wsl_path(stage, wsl_distro),
        ]
        backend = f"WSL {wsl_distro}: {executable}"
    else:
        raise PipelineError("genisoimage/mkisofs was not found natively or through WSL")

    log = (
        log_path.expanduser().resolve()
        if log_path is not None
        else destination.with_suffix(destination.suffix + ".author.log")
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise PipelineError(f"DVD ISO authoring failed; see {log}")
        if destination.exists():
            raise PipelineError(f"ISO destination already exists: {destination}")
        try:
            validate_udf(temporary)
        except UdfValidationError as exc:
            raise PipelineError(f"DVD ISO filesystem validation failed: {exc}") from exc
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    result = {
        "schema": "dvd2hevc-authored-iso-v0",
        "status": "authored",
        "contracts": capability_contract(),
        "stage": str(stage),
        "stage_report": str(stage_report),
        "destination": str(destination),
        "label": label,
        "backend": backend,
        "size": destination.stat().st_size,
        "log": str(log),
        "playback": {
            "requires_patched_vlc": True,
            "environment": {"DVDREAD_NOKEYS": "1"},
            "reason": (
                "The verified DVD2HEVC image is clear; skipping libdvdread's eager CSS-key pass "
                "avoids slow, futile key cracking against HEVC VOB payloads."
            ),
        },
    }
    report = (
        report_path.expanduser().resolve()
        if report_path is not None
        else destination.with_suffix(destination.suffix + ".json")
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


class _HashSink:
    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.position = 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def seek(self, *_args: object) -> int:
        return self.position


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_authored_iso(
    author_report: Path,
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Hash the authored UDF files and compare the libdvdread physical graph."""
    author_report = author_report.resolve()
    authored = json.loads(author_report.read_text(encoding="utf-8"))
    if authored.get("schema") != "dvd2hevc-authored-iso-v0":
        raise PipelineError(f"Unsupported authored ISO report: {author_report}")
    if authored.get("contracts") is not None and not contract_compatible(authored["contracts"]):
        raise PipelineError(f"Authored ISO uses an incompatible DVD2HEVC format contract: {author_report}")
    iso_path = Path(authored["destination"])
    try:
        validate_udf(iso_path)
    except UdfValidationError as exc:
        raise PipelineError(f"DVD ISO filesystem validation failed: {exc}") from exc
    stage = Path(authored["stage"])
    video_ts = stage / "VIDEO_TS"
    expected = {
        path.name.upper(): path
        for path in video_ts.iterdir()
        if path.is_file()
    }

    image = open_iso_image(iso_path)
    rows: list[dict[str, Any]] = []
    try:
        entries = {
            _entry_name(entry).upper(): entry
            for entry in image.list_children(udf_path="/VIDEO_TS")
            if entry is not None and not entry.is_dot() and not entry.is_dotdot() and entry.is_file()
        }
        if set(entries) != set(expected):
            raise PipelineError(
                f"Authored VIDEO_TS inventory mismatch: missing={sorted(set(expected)-set(entries))}, "
                f"extra={sorted(set(entries)-set(expected))}"
            )
        for name in sorted(expected):
            source_file = expected[name]
            entry = entries[name]
            sink = _HashSink()
            image.get_file_from_iso_fp(sink, udf_path=f"/VIDEO_TS/{_entry_name(entry)}")
            expected_hash = _file_sha256(source_file)
            actual_hash = sink.digest.hexdigest()
            rows.append({
                "name": name,
                "size": source_file.stat().st_size,
                "iso_size": int(entry.get_data_length()),
                "stage_sha256": expected_hash,
                "iso_sha256": actual_hash,
                "matches": (
                    source_file.stat().st_size == int(entry.get_data_length())
                    and expected_hash == actual_hash
                ),
            })
    finally:
        close_iso_image(image)

    dvdinspect = find_dvdinspect()
    if not dvdinspect:
        raise PipelineError("dvdinspect is required for authored ISO verification")
    stage_value = json.loads(Path(authored["stage_report"]).read_text(encoding="utf-8"))
    if stage_value.get("schema") == "dvd2hevc-compact-stage-v0":
        source_graph = inspect_physical_graph(stage, dvdinspect, timeout=300)
        graph_basis = "compact-stage"
    else:
        source_graph = inspect_physical_graph(Path(stage_value["source"]), dvdinspect, timeout=300)
        graph_basis = "source-disc"
    iso_graph = inspect_physical_graph(iso_path, dvdinspect, timeout=300)
    # Input location, executable path, and libdvdread diagnostics describe how
    # the graph was obtained, not the DVD graph itself. Folder access commonly
    # emits CSS/device fallback warnings that ISO access does not.
    graph_matches = normalized_physical_graph(source_graph) == normalized_physical_graph(iso_graph)
    result = {
        "schema": "dvd2hevc-authored-iso-verification-v0",
        "passed": all(row["matches"] for row in rows) and graph_matches,
        "contracts": authored.get("contracts") or capability_contract(),
        "author_report": str(author_report),
        "iso": str(iso_path),
        "udf_video_ts_opened": True,
        "files": rows,
        "physical_graph_matches_source": graph_matches,
        "physical_graph_basis": graph_basis,
        "summary": {
            "files": len(rows),
            "bytes": sum(row["size"] for row in rows),
            "matching_files": sum(1 for row in rows if row["matches"]),
        },
    }
    output = (
        report_path.expanduser().resolve()
        if report_path is not None
        else iso_path.with_suffix(iso_path.suffix + ".verification.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result["passed"]:
        raise PipelineError(f"Authored ISO verification failed; see {output}")
    return result
