from __future__ import annotations

import ast
import os
import subprocess
import unittest
from pathlib import Path

from dvd2hevc_app.subprocess_utils import hidden_subprocess_kwargs


ROOT = Path(__file__).resolve().parents[1]


class ProcessPolicyTests(unittest.TestCase):
    def test_windows_policy_suppresses_console_creation(self) -> None:
        values = hidden_subprocess_kwargs()
        if os.name == "nt":
            self.assertTrue(values["creationflags"] & subprocess.CREATE_NO_WINDOW)
            self.assertIn("startupinfo", values)
        else:
            self.assertEqual(values, {})

    def test_every_python_subprocess_uses_the_shared_hidden_policy(self) -> None:
        failures: list[str] = []
        for path in sorted((ROOT / "dvd2hevc_app").glob("*.py")):
            if path.name == "subprocess_utils.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in {"run", "Popen"}
                ):
                    continue
                shared = any(
                    keyword.arg is None
                    and isinstance(keyword.value, ast.Call)
                    and isinstance(keyword.value.func, ast.Name)
                    and keyword.value.func.id == "hidden_subprocess_kwargs"
                    for keyword in node.keywords
                )
                indirect = any(
                    keyword.arg is None and isinstance(keyword.value, (ast.Name, ast.DictComp))
                    for keyword in node.keywords
                )
                if not (shared or indirect):
                    failures.append(f"{path.name}:{node.lineno}")
        self.assertEqual(failures, [], f"Subprocess calls missing hidden policy: {failures}")

    def test_background_powershell_launchers_are_hidden(self) -> None:
        failures: list[str] = []
        for path in sorted((ROOT / "tools").glob("*.ps1")):
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            offset = 0
            while True:
                marker = text.find("Start-Process", offset)
                if marker < 0:
                    break
                statement = text[marker : marker + 500]
                if "-WindowStyle Hidden" not in statement:
                    failures.append(f"{path.name}:{text.count(chr(10), 0, marker) + 1}")
                offset = marker + len("Start-Process")
        self.assertEqual(failures, [], f"Visible background launchers: {failures}")


if __name__ == "__main__":
    unittest.main()
