"""Windows-safe subprocess defaults used throughout DVD2HEVC.

DVD2HEVC launches command-line tools from both python.exe and pythonw.exe.  On
Windows, a console program started by a windowed parent may create a new
console window unless creation is explicitly suppressed.  Keeping this policy
in one helper prevents a newly added probe or helper from reintroducing popup
PowerShell, cmd, or WSL windows.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_subprocess_kwargs(*, creationflags: int = 0) -> dict[str, Any]:
    """Return subprocess keyword arguments that never create a console window."""
    if os.name != "nt":
        return {"creationflags": creationflags} if creationflags else {}

    flags = int(creationflags) | int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    values: dict[str, Any] = {"creationflags": flags}
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        values["startupinfo"] = startupinfo
    return values
