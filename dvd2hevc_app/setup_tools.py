"""Build the optional native inspector into writable user storage."""
from __future__ import annotations
import argparse
import subprocess
import tempfile
from pathlib import Path
from .paths import ROOT, STATE_ROOT, TOOL_ROOT
from .pipeline import PipelineError
from .subprocess_utils import hidden_subprocess_kwargs
from .runtime_support import read_log_tail


def cmd_build_inspector(args):
    with tempfile.TemporaryDirectory(prefix="dvd2hevc-native-") as temporary:
        log = Path(temporary) / "build.log"
        with log.open("wb") as output:
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "native" / "build-native.ps1"), "-WorkRoot", str(STATE_ROOT / "native-build"),
                "-Destination", str(TOOL_ROOT)], stdout=output, stderr=subprocess.STDOUT,
                **hidden_subprocess_kwargs())
        print(read_log_tail(log, 200), flush=True)
    if result.returncode: raise PipelineError("Native inspector build failed; see the build output above")
    print("Native inspector built in", TOOL_ROOT)
    return 0


def add_build_inspector_command(commands):
    parser = commands.add_parser("build-inspector", help="Build the native DVD inspector into per-user tool storage")
    parser.set_defaults(func=cmd_build_inspector)
