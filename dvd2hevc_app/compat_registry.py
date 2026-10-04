"""Media-free local compatibility history for disposable source ISO libraries."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .atomic import write_json_atomic
from .contracts import COMPATIBILITY_REGISTRY_SCHEMA, capability_contract
from .pipeline import PipelineError


from .paths import ROOT, REPORT_ROOT, STATE_ROOT, TOOL_ROOT
DEFAULT_REGISTRY = REPORT_ROOT / "compatibility" / "registry-v1.json"
SAMPLE_BYTES = 1024 * 1024


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sampled_fingerprint(path: Path) -> dict[str, Any]:
    """Identify a large image across renames without hashing the whole disc.

    The fingerprint is diagnostic identity, not a cryptographic authenticity
    claim.  It reads at most two MiB and survives after the source ISO is
    deleted because the result is retained in the registry.
    """
    result: dict[str, Any] = {"exists_at_recording": path.is_file()}
    if not path.is_file():
        return result
    stat = path.stat()
    size = int(stat.st_size)
    digest = hashlib.sha256()
    digest.update(size.to_bytes(8, "big", signed=False))
    with path.open("rb") as handle:
        digest.update(handle.read(SAMPLE_BYTES))
        if size > SAMPLE_BYTES:
            handle.seek(max(0, size - SAMPLE_BYTES))
            digest.update(handle.read(SAMPLE_BYTES))
    result.update({
        "size_bytes": size,
        "sample_sha256": digest.hexdigest(),
    })
    return result


def classify_outcome(job: dict[str, Any], pipeline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map low-level state to a stable, user-meaningful verdict."""
    status = str(job.get("status") or "unknown")
    warnings = [str(item) for item in (job.get("warnings") or []) if str(item).strip()]
    message = str(job.get("error") or (pipeline or {}).get("message") or "")
    folded = message.casefold()
    if status == "passed":
        verdict = "passed-with-warnings" if warnings else "passed"
        category = "verified-output"
    elif status == "canceled":
        verdict = "canceled"
        category = "user-control"
    elif any(marker in folded for marker in (
        "scrambled", "invalid dvd", "source backup", "source iso changed",
        "unsupported compatibility blocker", "could not read udf",
    )):
        verdict = "source-invalid"
        category = "source-media"
    elif any(marker in folded for marker in (
        "missing conversion requirements", "access is denied", "permission denied",
        "sharing violation", "being used by another process", "runner exited",
        "not enough space", "low on space", "temporarily unavailable",
    )):
        verdict = "environment-interrupted"
        category = "runtime-environment"
    else:
        verdict = "conversion-failed" if status == "failed" else status
        category = "converter"
    return {
        "verdict": verdict,
        "category": category,
        "warnings": warnings,
        "message": message or None,
    }


def _duration_seconds(job: dict[str, Any]) -> float | None:
    try:
        start = datetime.fromisoformat(str(job["started_at"]))
        end = datetime.fromisoformat(str(job["ended_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def _report_evidence(work_root: Path) -> dict[str, Any]:
    candidates = {
        "pipeline": work_root / "status.json",
        "source_scan": work_root / "source-full-scan.json",
        "author": work_root / "final-output-reports" / "author.json",
        "iso_verification": work_root / "final-output-reports" / "verification.json",
        "css_audit": work_root / "final-output-reports" / "css-safe-audit.json",
        "css_verification": work_root / "final-output-reports" / "css-safe-verification.json",
    }
    evidence: dict[str, Any] = {}
    for name, path in candidates.items():
        report = _read_object(path)
        if report is None:
            continue
        evidence[name] = {
            key: report[key]
            for key in ("schema", "state", "status", "passed")
            if key in report
        }
    return evidence


def compatibility_entry(job_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(job.get("source") or ""))
    output = Path(str(job.get("output") or ""))
    work_root = Path(str(job.get("work_root") or ""))
    pipeline = _read_object(work_root / "status.json") or {}
    return {
        "job_id": str(job.get("id") or job_path.parent.name),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "application_version": job.get("version"),
        "contracts": job.get("contracts") or capability_contract(),
        "disc": {
            "source_name": source.name,
            "source_fingerprint": _sampled_fingerprint(source),
            "output_name": output.name,
            "output_fingerprint": _sampled_fingerprint(output),
        },
        "outcome": classify_outcome(job, pipeline),
        "duration_seconds": _duration_seconds(job),
        "plan_summary": dict(job.get("plan_summary") or {}),
        "settings": dict(job.get("settings") or {}),
        "evidence": _report_evidence(work_root),
    }


def _load_registry(path: Path) -> dict[str, Any]:
    value = _read_object(path)
    if value is None:
        return {"schema": COMPATIBILITY_REGISTRY_SCHEMA, "records": {}}
    if value.get("schema") != COMPATIBILITY_REGISTRY_SCHEMA or not isinstance(value.get("records"), dict):
        raise PipelineError(f"Unsupported DVD2HEVC compatibility registry: {path}")
    return value


def record_job_outcome(
    job_path: Path,
    job: dict[str, Any],
    *,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    registry = _load_registry(registry_path)
    entry = compatibility_entry(job_path, job)
    previous = registry["records"].get(entry["job_id"])
    if isinstance(previous, dict):
        if previous.get("manual_review"):
            entry["manual_review"] = previous["manual_review"]
        # A rebuild may run after the user has deleted or renamed either ISO.
        # Never replace a retained sampled identity with "no longer exists".
        for key in ("source_fingerprint", "output_fingerprint"):
            old_value = (previous.get("disc") or {}).get(key)
            new_value = entry["disc"].get(key)
            if (
                isinstance(old_value, dict)
                and old_value.get("sample_sha256")
                and isinstance(new_value, dict)
                and not new_value.get("sample_sha256")
            ):
                entry["disc"][key] = old_value
    registry["records"][entry["job_id"]] = entry
    registry["updated_at"] = entry["recorded_at"]
    write_json_atomic(registry_path, registry)
    return entry


def rebuild_registry(
    job_files: Iterable[Path],
    *,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    existing = _load_registry(registry_path)
    reviews = {
        key: value.get("manual_review")
        for key, value in existing["records"].items()
        if isinstance(value, dict) and value.get("manual_review")
    }
    records: dict[str, Any] = {}
    for job_path in sorted(job_files):
        job = _read_object(job_path)
        if not job or str(job.get("status")) not in {"passed", "failed", "canceled"}:
            continue
        entry = compatibility_entry(job_path, job)
        previous = existing["records"].get(entry["job_id"])
        if isinstance(previous, dict):
            for key in ("source_fingerprint", "output_fingerprint"):
                old_value = (previous.get("disc") or {}).get(key)
                new_value = entry["disc"].get(key)
                if (
                    isinstance(old_value, dict)
                    and old_value.get("sample_sha256")
                    and isinstance(new_value, dict)
                    and not new_value.get("sample_sha256")
                ):
                    entry["disc"][key] = old_value
        if entry["job_id"] in reviews:
            entry["manual_review"] = reviews[entry["job_id"]]
        records[entry["job_id"]] = entry
    registry = {
        "schema": COMPATIBILITY_REGISTRY_SCHEMA,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "records": records,
    }
    write_json_atomic(registry_path, registry)
    return registry


def _registry_job_files() -> list[Path]:
    from .frontend import JOB_ROOT
    return list(JOB_ROOT.glob("*/job.json")) if JOB_ROOT.is_dir() else []


def cmd_compatibility_registry(args: argparse.Namespace) -> int:
    registry = (
        rebuild_registry(_registry_job_files())
        if args.rebuild
        else _load_registry(DEFAULT_REGISTRY)
    )
    records = list(registry["records"].values())
    if args.json:
        print(json.dumps(registry, indent=2))
        return 0
    counts: dict[str, int] = {}
    for entry in records:
        verdict = str((entry.get("outcome") or {}).get("verdict") or "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
    print(f"DVD2HEVC compatibility history: {len(records)} disc job(s)")
    print("  " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    for entry in sorted(records, key=lambda item: str(item.get("recorded_at")), reverse=True)[: args.limit]:
        print(
            f"{(entry.get('outcome') or {}).get('verdict', 'unknown'):22} "
            f"{(entry.get('disc') or {}).get('source_name', entry.get('job_id'))}"
        )
    return 0


def cmd_review_job(args: argparse.Namespace) -> int:
    from .frontend import resolve_job
    job_path, job = resolve_job(args.job)
    entry = record_job_outcome(job_path, job)
    registry = _load_registry(DEFAULT_REGISTRY)
    review = {
        "result": args.result,
        "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": args.note or None,
    }
    registry["records"][entry["job_id"]]["manual_review"] = review
    registry["updated_at"] = review["reviewed_at"]
    write_json_atomic(DEFAULT_REGISTRY, registry)
    print(f"Recorded {args.result} manual review for {entry['job_id']}")
    return 0


def add_compatibility_registry_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    registry = commands.add_parser(
        "compatibility-history",
        help="Show or rebuild the media-free local disc compatibility history",
    )
    registry.add_argument("--rebuild", action="store_true", help="Rebuild from retained terminal job reports")
    registry.add_argument("--json", action="store_true")
    registry.add_argument("--limit", type=int, default=20)
    registry.set_defaults(func=cmd_compatibility_registry)

    review = commands.add_parser(
        "review-job",
        help="Record a qualitative playback review without changing conversion verification",
    )
    review.add_argument("job", help="Job id or unique part of its name")
    review.add_argument("--result", required=True, choices=("passed", "issues-found", "not-tested"))
    review.add_argument("--note")
    review.set_defaults(func=cmd_review_job)
