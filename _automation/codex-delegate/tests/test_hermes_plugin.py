from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "hermes_plugin.py"
SPEC = importlib.util.spec_from_file_location("codex_delegate_hermes_plugin", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HermesCodexDelegatePluginTests(unittest.TestCase):
    def test_schema_exposes_only_structured_delegation(self) -> None:
        schema = MODULE.CODEX_DELEGATE_SCHEMA["parameters"]

        self.assertEqual(schema["required"], ["task"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["mode"]["enum"], ["read-only", "write"])
        self.assertNotIn("command", schema["properties"])

    def test_command_uses_argument_array_and_base64_task(self) -> None:
        with patch.object(MODULE, "DELEGATE", MODULE_PATH):
            command = MODULE._build_command(
                {"task": "修复源码并运行测试", "mode": "write"}
            )

        self.assertIsInstance(command, list)
        self.assertIn("--task-base64", command)
        self.assertNotIn("修复源码并运行测试", command)
        self.assertEqual(command[-2:], ["--mode", "write"])

    def test_workspace_cannot_escape_aiworkspace(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace must be under"):
            MODULE._workspace_path(r"C:\Windows")

    def test_nonzero_result_is_structured(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python"],
            returncode=1,
            stdout='{"ok":false,"error":"quota exhausted"}\n',
            stderr="",
        )
        with patch.object(MODULE, "_build_command", return_value=["python"]), patch.object(
            MODULE, "_workspace_path", return_value=MODULE.DEFAULT_WORKSPACE
        ), patch.object(MODULE.subprocess, "run", return_value=completed):
            result = json.loads(MODULE.run_codex_delegate({"task": "fix"}))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "quota exhausted")


if __name__ == "__main__":
    unittest.main()
