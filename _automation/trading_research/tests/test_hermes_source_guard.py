from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "scripts" / "hermes_ai_hub_source_guard.py"


class HermesSourceGuardTests(unittest.TestCase):
    def run_guard(self, tool_name: str, tool_input: dict | None = None) -> dict:
        completed = subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps({"tool_name": tool_name, "tool_input": tool_input or {}}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return json.loads(completed.stdout)

    def test_all_general_purpose_writers_are_blocked(self) -> None:
        for tool_name in ("write_file", "patch", "terminal", "execute_code"):
            with self.subTest(tool_name=tool_name):
                result = self.run_guard(tool_name, {"command": "anything"})
                self.assertEqual(result["action"], "block")

    def test_generic_subagents_cannot_bypass_codex(self) -> None:
        result = self.run_guard("delegate_task", {"task": "edit source"})

        self.assertEqual(result["action"], "block")
        self.assertIn("codex_delegate", result["message"])

    def test_non_mutating_native_tool_is_untouched(self) -> None:
        self.assertEqual(self.run_guard("kol_operator", {"action": "status"}), {})


if __name__ == "__main__":
    unittest.main()
