from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "hermes_plugin.py"


def _load_module(env: dict[str, str] | None = None) -> importlib.util.ModuleType:
    """Load hermes_plugin.py with a controlled AI_* environment.

    AI_HUB_HOME / AI_WORKSPACE_ROOT are removed from the inherited
    environment (unless overridden in ``env``) so path resolution is
    deterministic regardless of the machine that runs the tests.
    """
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in ("AI_HUB_HOME", "AI_WORKSPACE_ROOT")
    }
    if env:
        base.update(env)
    with patch.dict(os.environ, base, clear=True):
        spec = importlib.util.spec_from_file_location(
            "codex_delegate_hermes_plugin_test", MODULE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


MODULE = _load_module()


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

    def test_workspace_cannot_escape_workspace_root(self) -> None:
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


class HermesCodexDelegatePathResolutionTests(unittest.TestCase):
    def test_defaults_derive_from_script_location(self) -> None:
        expected = MODULE_PATH.resolve().parents[2]
        self.assertEqual(MODULE.WORKSPACE_ROOT, expected)
        self.assertEqual(MODULE.DEFAULT_WORKSPACE, expected)
        self.assertEqual(
            MODULE.DELEGATE,
            expected / "_automation" / "codex-delegate" / "invoke_codex.py",
        )

    def test_ai_hub_home_env_is_used_as_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            module = _load_module({"AI_HUB_HOME": temp_dir})
            expected = Path(temp_dir).resolve()
            self.assertEqual(module.WORKSPACE_ROOT, expected)
            self.assertEqual(module.DEFAULT_WORKSPACE, expected)

    def test_ai_hub_home_missing_directory_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with self.assertRaisesRegex(RuntimeError, "AI_HUB_HOME"):
                _load_module({"AI_HUB_HOME": str(missing)})

    def test_ai_workspace_root_env_is_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            module = _load_module({"AI_WORKSPACE_ROOT": temp_dir})
            expected = Path(temp_dir).resolve()
            self.assertEqual(module.WORKSPACE_ROOT, expected)
            self.assertEqual(module.DEFAULT_WORKSPACE, expected / "ai-hub")

    def test_ai_hub_home_takes_priority_over_ai_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            module = _load_module(
                {"AI_HUB_HOME": temp_dir, "AI_WORKSPACE_ROOT": temp_dir}
            )
            expected = Path(temp_dir).resolve()
            self.assertEqual(module.WORKSPACE_ROOT, expected)
            self.assertEqual(module.DEFAULT_WORKSPACE, expected)


if __name__ == "__main__":
    unittest.main()
