from __future__ import annotations

import argparse
import base64
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "invoke_codex.py"
SPEC = importlib.util.spec_from_file_location("invoke_codex", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _env_without_ai_vars(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("AI_HUB_HOME", "AI_WORKSPACE_ROOT")
    }
    if extra:
        env.update(extra)
    return env


class InvokeCodexTests(unittest.TestCase):
    def test_reads_utf8_base64_without_losing_chinese(self) -> None:
        task = "重构 Obsidian，并保留未提交的文件。"
        encoded = base64.b64encode(task.encode("utf-8")).decode("ascii")
        args = argparse.Namespace(
            task=None,
            task_base64=encoded,
            prompt_file=None,
            stdin=False,
        )

        self.assertEqual(MODULE.read_task(args), task)

    def test_command_does_not_contain_user_task(self) -> None:
        command = MODULE.make_command(
            "codex.cmd",
            MODULE_PATH.resolve().parents[2],
            "write",
            Path(tempfile.gettempdir()) / "answer.txt",
        )

        self.assertIn("danger-full-access", command)
        self.assertEqual(command[-1], "-")


class InvokeCodexPathResolutionTests(unittest.TestCase):
    def test_default_workspace_derives_from_script_location(self) -> None:
        with patch.dict(os.environ, _env_without_ai_vars(), clear=True):
            self.assertEqual(
                MODULE._default_workspace(), MODULE_PATH.resolve().parents[2]
            )

    def test_default_workspace_prefers_ai_hub_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                _env_without_ai_vars({"AI_HUB_HOME": temp_dir}),
                clear=True,
            ):
                self.assertEqual(
                    MODULE._default_workspace(), Path(temp_dir).resolve()
                )

    def test_default_workspace_falls_back_to_ai_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                _env_without_ai_vars({"AI_WORKSPACE_ROOT": temp_dir}),
                clear=True,
            ):
                self.assertEqual(
                    MODULE._default_workspace(), Path(temp_dir).resolve() / "ai-hub"
                )

    def test_missing_workspace_is_argparse_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with patch.object(
                sys,
                "argv",
                ["invoke_codex.py", "--task", "x", "--workspace", str(missing)],
            ):
                with self.assertRaises(SystemExit) as ctx:
                    MODULE.parse_args()
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_ai_hub_home_default_is_argparse_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with patch.dict(
                os.environ,
                _env_without_ai_vars({"AI_HUB_HOME": str(missing)}),
                clear=True,
            ), patch.object(sys, "argv", ["invoke_codex.py", "--task", "x"]):
                with self.assertRaises(SystemExit) as ctx:
                    MODULE.parse_args()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
