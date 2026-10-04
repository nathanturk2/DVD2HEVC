"""Privacy-conscious diagnostic bundles for DVD2HEVC support reports."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from .paths import REPORT_ROOT
from typing import Any

from .runtime_support import publish_file, read_log_tail, reject_overlap, swap_directory

from . import __version__
from .frontend import ROOT, read_json, resolve_job
from .pipeline import PipelineError
from .tools import discover_tools


def path_variants(path: Path) -> set[str]:
    values = {str(path), str(path.expanduser()), str(path.expanduser().resolve()), path.as_posix()}
    values.update(value.replace("\\", "/") for value in tuple(values))
    for value in tuple(values):
        if re.match(r"^[A-Za-z]:/", value):
            values.add("/mnt/" + value[0].lower() + value[2:])
    return values | {json.dumps(item)[1:-1] for item in values}


def redaction_map(paths: list[Path]) -> dict[str, str]:
    values: list[tuple[Path, str]] = [(ROOT, "<dvd2hevc-root>"), (Path.home(), "<home>")]
    values.extend((path, f"<private-path-{index}>") for index, path in enumerate(paths, start=1))
    mapping: dict[str, str] = {}
    for path, replacement in values:
        for variant in path_variants(path):
            mapping.setdefault(variant, replacement)
    return dict(sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True))


def redact_text(value: str, mapping: dict[str, str]) -> str:
    result = value
    for original, replacement in mapping.items():
        result = result.replace(original, replacement)
        if os.name == "nt":
            result = re.sub(re.escape(original), replacement, result, flags=re.IGNORECASE)
    return result


def redact_value(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, mapping)
    if isinstance(value, list):
        return [redact_value(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, mapping) for key, item in value.items()}
    return value


def read_tail(path: Path, lines: int) -> str:
    return read_log_tail(path, lines)


def collect_status_reports(work_root: Path) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    if not work_root.is_dir():
        return reports
    for path in work_root.rglob("status.json"):
        try:
            reports[path.relative_to(work_root).as_posix()] = read_json(path)
        except PipelineError:
            continue
    return reports


def create_diagnostic_bundle(
    identifier: str | None,
    *,
    output: Path | None = None,
    log_lines: int = 1200,
    force: bool = False,
) -> Path:
    job_path, job = resolve_job(identifier)
    source = Path(str(job.get("source")))
    destination = Path(str(job.get("output")))
    work_root = Path(str(job.get("work_root")))
    mapping = redaction_map([source, destination, work_root, job_path])
    diagnostic_root = REPORT_ROOT / "diagnostics"
    diagnostic_root.mkdir(parents=True, exist_ok=True)
    output = (
        output.expanduser().resolve()
        if output
        else diagnostic_root / f"{job.get('id', 'job')}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    )
    try:
        reject_overlap(output, [source, destination, work_root, job_path])
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    if output.exists() and not force:
        raise PipelineError(f"Diagnostic output exists: {output}. Use --force to replace the bundle.")
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        "dvd2hevc_version": __version__,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": sys.version,
        "tools": discover_tools(),
    }
    summary = {
        "environment": redact_value(environment, mapping),
        "job": redact_value(job, mapping),
        "source": {
            "name": source.name,
            "exists": source.is_file(),
            "size_bytes": source.stat().st_size if source.is_file() else None,
        },
        "output": {
            "name": destination.name,
            "exists": destination.is_file(),
            "size_bytes": destination.stat().st_size if destination.is_file() else None,
        },
        "status_reports": redact_value(collect_status_reports(work_root), mapping),
    }
    with tempfile.TemporaryDirectory(prefix="dvd2hevc-diagnostic-") as temporary:
        root = Path(temporary) / "DVD2HEVC-diagnostic"
        root.mkdir()
        (root / "README.txt").write_text(
            "DVD2HEVC diagnostic bundle\n\n"
            "Attach this zip to a bug report. It contains redacted generated reports, "
            "tool information, and a log tail. It intentionally contains no ISO, VOB, "
            "decryption keys, or copied disc assets.\n",
            encoding="utf-8",
        )
        (root / "diagnostic.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log_path = Path(str(job.get("log") or ""))
        log_tail = redact_text(read_tail(log_path, log_lines), mapping)
        if log_tail:
            (root / "run.log.tail.txt").write_text(log_tail, encoding="utf-8")
        import uuid
        temporary_output = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
            publish_file(temporary_output, output, force=force)
        finally:
            temporary_output.unlink(missing_ok=True)

    return output


def cmd_diagnose(args: argparse.Namespace) -> int:
    bundle = create_diagnostic_bundle(
        args.job,
        output=Path(args.output) if args.output else None,
        log_lines=args.log_lines,
        force=getattr(args, "force", False),
    )
    print("DVD2HEVC diagnostic bundle created.")
    print(f"Saved to: {bundle}")
    print("It contains no ISO/VOB media or decryption keys.")
    return 0


def add_diagnose_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("diagnose", help="Create a redacted support bundle for a conversion job")
    parser.add_argument("job", nargs="?", help="Job id; defaults to the newest job")
    parser.add_argument("--output", help="Destination zip path")
    parser.add_argument("--force", action="store_true", help="Replace an existing diagnostic bundle; media and workspaces remain protected")
    parser.add_argument("--log-lines", type=int, default=1200)
    parser.set_defaults(func=cmd_diagnose)
