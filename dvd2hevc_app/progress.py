"""Machine-readable progress events shared by conversion task lanes."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


PROGRESS_PREFIX = "DVD2HEVC_PROGRESS "
_WRITE_LOCK = threading.Lock()


def progress_event(lane: str, event: str, task: str, **fields: Any) -> None:
    """Emit one append-only JSON event for foreground and background renderers."""
    payload = {
        "timestamp": time.time(),
        "lane": lane,
        "event": event,
        "task": task,
        **{key: value for key, value in fields.items() if value is not None},
    }
    line = PROGRESS_PREFIX + json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    with _WRITE_LOCK:
        stream = sys.stdout if os.environ.get("DVD2HEVC_PROGRESS_STDOUT") == "1" else sys.stderr
        print(line, file=stream, flush=True)
        progress_path = os.environ.get("DVD2HEVC_PROGRESS_FILE")
        if progress_path:
            path = Path(progress_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
