from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_operator_plugin import (  # noqa: E402
    KOL_OPERATOR_SCHEMA,
    _build_command,
    run_operator,
)


class HermesOperatorPluginTests(unittest.TestCase):
    def test_schema_exposes_only_fixed_actions(self) -> None:
        action_schema = KOL_OPERATOR_SCHEMA["parameters"]["properties"]["action"]

        self.assertIn("status", action_schema["enum"])
        self.assertIn("approve-draft", action_schema["enum"])
        self.assertIn("discover-accounts", action_schema["enum"])
        self.assertNotIn("accept-candidate", action_schema["enum"])
        self.assertFalse(KOL_OPERATOR_SCHEMA["parameters"]["additionalProperties"])

    def test_discovery_actions_expose_fixed_fields_without_source_mutation(self) -> None:
        properties = KOL_OPERATOR_SCHEMA["parameters"]["properties"]

        self.assertEqual("^cand_[a-f0-9]{24}$", properties["candidate_id"]["pattern"])
        self.assertEqual(30, properties["days"]["maximum"])
        self.assertNotIn("schema", properties)
        self.assertNotIn("source_path", properties)

    @patch("hermes_operator_plugin.OPERATOR")
    @patch("hermes_operator_plugin.shutil.which", return_value=r"C:\Windows\powershell.exe")
    def test_command_uses_argument_array_and_explicit_identifiers(
        self,
        _which,
        operator,
    ) -> None:
        operator.is_file.return_value = True
        operator.__str__.return_value = r"E:\aiworkspace\ai-hub\scripts\hermes-kol-operator.ps1"

        command = _build_command(
            {"action": "approve-draft", "id": 42, "note": "confirmed"}
        )

        self.assertEqual(command[0], r"C:\Windows\powershell.exe")
        self.assertIn("approve-draft", command)
        self.assertIn("42", command)
        self.assertIn("confirmed", command)

    @patch("hermes_operator_plugin._build_command", return_value=["powershell.exe"])
    @patch("hermes_operator_plugin.subprocess.run")
    def test_handler_returns_last_json_line(self, run, _build) -> None:
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout='startup noise\n{"ok":true,"running":true}\n',
            stderr="",
        )

        result = json.loads(run_operator({"action": "status"}))

        self.assertTrue(result["ok"])
        self.assertTrue(result["running"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @patch("hermes_operator_plugin._build_command", return_value=["powershell.exe"])
    @patch("hermes_operator_plugin.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 90))
    def test_timeout_is_structured(self, _run, _build) -> None:
        result = json.loads(run_operator({"action": "status"}))

        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])


if __name__ == "__main__":
    unittest.main()
