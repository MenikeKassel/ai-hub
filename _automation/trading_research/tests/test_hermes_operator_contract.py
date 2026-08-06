from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class HermesOperatorContractTests(unittest.TestCase):
    def test_manual_collection_is_ai_independent(self) -> None:
        operator = (ROOT / "scripts" / "hermes-kol-operator.ps1").read_text(encoding="utf-8")
        fetcher = (ROOT / "scripts" / "kol-post-fetch.ps1").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install-kol-post-fetch.ps1").read_text(encoding="utf-8")

        self.assertIn("KOL_Post_Fetch_Manual_X", operator)
        self.assertIn("KOL_Post_Fetch_Manual_Zhihu", operator)
        self.assertIn("-SkipAiPrefill", installer)
        self.assertIn("-NotifyOnCompletion", installer)
        collect_block = re.search(r'"collect"\s*\{(?P<body>.*?)\n\s*\}\n\s*"review"', operator, re.S)
        self.assertIsNotNone(collect_block)
        self.assertNotIn("Start-Process", collect_block.group("body"))
        self.assertIn("[switch]$SkipAiPrefill", fetcher)
        self.assertNotIn('throw "evening AI prefill exited', fetcher)

    def test_feishu_notifications_use_utf8_files(self) -> None:
        helper = (ROOT / "scripts" / "lib" / "hermes-notify.ps1").read_text(encoding="utf-8")
        scripts = [
            ROOT / "scripts" / name
            for name in (
                "kol-post-fetch.ps1",
                "kol-review-agent.ps1",
                "kol-tracker.ps1",
                "kol-morning-pipeline.ps1",
                "kol-post-classify.ps1",
            )
        ]

        self.assertIn("--file", helper)
        self.assertIn("UTF8Encoding($false)", helper)
        for path in scripts:
            value = path.read_text(encoding="utf-8")
            self.assertIn("Send-HermesUtf8Message", value, path.name)
            self.assertNotIn("hermes send --to feishu", value, path.name)

        cli = (ROOT / "_automation" / "trading_research" / "trading_cli.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('["hermes", "send", "--to", "feishu", message]', cli)
        self.assertIn('["hermes", "send", "--to", "feishu", "--file"', cli)

    def test_operator_exposes_deterministic_audit_actions(self) -> None:
        operator = (ROOT / "scripts" / "hermes-kol-operator.ps1").read_text(encoding="utf-8")

        for action in (
            '"status"',
            '"start"',
            '"collect"',
            '"review"',
            '"market"',
            '"returns"',
            '"board-status"',
            '"board-sync"',
            '"list-kols"',
            '"add-kol"',
            '"set-kol-status"',
            '"list-drafts"',
            '"approve-draft"',
            '"reject-draft"',
            '"list-events"',
        ):
            self.assertIn(action, operator)

    def test_operator_decodes_utf8_and_keeps_list_results_compact(self) -> None:
        operator = (ROOT / "scripts" / "hermes-kol-operator.ps1").read_text(encoding="utf-8")

        self.assertIn("RawContentStream", operator)
        self.assertIn("System.Text.Encoding]::UTF8", operator)
        self.assertIn("Select-Object id, display_name, platform, handle", operator)
        self.assertIn("Select-Object id, post_id, handle, display_name", operator)

    def test_start_contract_is_environment_isolated_and_single_instance(self) -> None:
        starter = (ROOT / "scripts" / "start-kol-ui.ps1").read_text(encoding="utf-8")
        operator = (ROOT / "scripts" / "hermes-kol-operator.ps1").read_text(encoding="utf-8")

        for variable in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"):
            self.assertIn(variable, starter)
        self.assertIn("System.Threading.Mutex", starter)
        self.assertIn("server.process.json", starter)
        self.assertIn("Get-NetTCPConnection", starter)
        for action in ("already_running", "port_conflict", "startup_failed", "unhealthy"):
            self.assertIn(action, operator)
        self.assertIn("stderr_tail", operator)

    def test_skill_stops_after_start_failure(self) -> None:
        skill = (ROOT / "_skills" / "kol-research-operator" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("status -> running=false: call start once", skill)
        self.assertIn("After a start error, never use `terminal`", skill)
        self.assertIn("startup_failed/port_conflict/unhealthy", skill)
        self.assertIn("delegate-to-codex", skill)

    def test_skill_metadata_is_not_corrupted(self) -> None:
        skill = (ROOT / "_skills" / "kol-research-operator" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("??", skill)
        self.assertIn("Codex-independent", skill)
        self.assertIn("explicit draft ID", skill)

    def test_hermes_is_operator_only_and_source_changes_route_to_codex(self) -> None:
        skill = (ROOT / "_skills" / "kol-research-operator" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "scripts" / "install-hermes-kol-research.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("Hermes 是工作台操作者", skill)
        self.assertIn("2026-08-06 起用户已授予 Hermes 直接修改 ai-hub 源代码的权限", skill)
        self.assertIn("Use only the native `kol_operator` tool", skill)
        self.assertIn("Never use the `terminal` or `read_file` tool", skill)
        self.assertNotIn("pre_tool_call", installer)
        self.assertNotIn("ai-hub-source-guard.py", installer)
        self.assertNotIn("shell-hooks-allowlist.json", installer)
        self.assertNotIn("hermes_ai_hub_source_guard.py", installer)


if __name__ == "__main__":
    unittest.main()
