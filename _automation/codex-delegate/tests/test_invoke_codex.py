from __future__ import annotations

import argparse
import base64
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "invoke_codex.py"
SPEC = importlib.util.spec_from_file_location("invoke_codex", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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
            Path(r"<AI_HUB_HOME>\ai-hub"),
            "write",
            Path(r"C:\Temp\answer.txt"),
        )

        self.assertIn("danger-full-access", command)
        self.assertEqual(command[-1], "-")


if __name__ == "__main__":
    unittest.main()
