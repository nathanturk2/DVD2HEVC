"""Small durable filesystem publications shared by conversion workers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def replace_with_retry(source: Path, destination: Path, *, attempts: int = 60) -> None:
    """Replace a file despite brief Windows reader/antivirus sharing locks."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or (
                os.name == "nt" and getattr(exc, "winerror", None) in {5, 32, 33}
            )
            if not retryable or attempt >= attempts - 1:
                raise
            time.sleep(min(0.25, 0.025 * (attempt + 1)))


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write a complete JSON object and publish it atomically with retries."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.part"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
