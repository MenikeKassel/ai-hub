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
            '"list-kols"',
            '"add-kol"',
            '"set-kol-status"',
            '"list-drafts"',
            '"approve-draft"',
            '"reject-draft"',
            '"list-events"',
        ):
            self.assertIn(action, operator)

        for action in (
            '"platform-status"',
            '"discover-accounts"',
            '"list-candidates"',
            '"score-candidate"',
            '"reject-candidate"',
            '"retry-candidate"',
            '"kol-profile"',
            '"fetch-kol"',
        ):
            self.assertIn(action, operator)
        self.assertNotIn('"accept-candidate"', operator)

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
        self.assertIn("listener_pid", starter)
        self.assertIn("supervisor_pid", starter)
        self.assertIn("Test-ExpectedListener", starter)
        self.assertIn("Get-NetTCPConnection", starter)
        for action in ("already_running", "port_conflict", "startup_failed", "unhealthy"):
            self.assertIn(action, operator)
        self.assertIn("stderr_tail", operator)

    def test_operator_accepts_a_healthy_api_when_process_details_are_hidden(self) -> None:
        operator = (ROOT / "scripts" / "hermes-kol-operator.ps1").read_text(encoding="utf-8")

        managed_listener = re.search(
            r"function Test-ManagedListener\s*\{(?P<body>.*?)\n\}", operator, re.S
        )
        server_probe = re.search(r"function Get-ServerProbe\s*\{(?P<body>.*?)\n\}", operator, re.S)
        self.assertIsNotNone(managed_listener)
        self.assertIsNotNone(server_probe)
        self.assertIn("$ProcessInfo.listener_pid", managed_listener.group("body"))
        self.assertIn("if ($status)", server_probe.group("body"))
        self.assertNotIn("if ($status -and $managed)", server_probe.group("body"))
        self.assertIn("ownership_verified", operator)

    def test_market_dry_run_is_preview_only_and_failures_keep_child_logs(self) -> None:
        operator = (ROOT / "scripts" / "hermes-kol-operator.ps1").read_text(encoding="utf-8")
        runner = (ROOT / "scripts" / "market-data-sync.ps1").read_text(encoding="utf-8")

        market_block = re.search(r'"market"\s*\{(?P<body>.*?)\n\s*\}\n\s*"data-refresh"', operator, re.S)
        self.assertIsNotNone(market_block)
        self.assertIn("if (-not $DryRun)", market_block.group("body"))
        self.assertIn("market-daily-publish --as-of $AsOf", market_block.group("body"))
        self.assertIn('NotePropertyValue "preview"', market_block.group("body"))
        self.assertIn('$ErrorActionPreference = "Continue"', runner)
        self.assertIn("market-sync-failed-$attemptId.stderr.log", runner)
        self.assertIn('"--candidate-root", $CandidateRoot', runner)
        self.assertIn("$maxAttempts = 3", runner)
        self.assertIn("PyEval_SaveThread", runner)
        self.assertIn('_runtime\\trading\\market-task-logs', runner)
        self.assertNotIn('_runtime\\trading\\market\\logs"', runner)
        self.assertNotIn(
            "Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue\n    }\n",
            runner,
        )

    def test_freestockdb_start_task_can_stop_its_elevated_listener(self) -> None:
        starter = (ROOT / "scripts" / "start-freestockdb.ps1").read_text(encoding="utf-8")
        runtime = (
            ROOT / "_automation" / "trading_research" / "freestockdb_runtime.py"
        ).read_text(encoding="utf-8")

        self.assertIn("freestockdb-stop.request.json", starter)
        self.assertIn("freestockdb-stop.result.json", starter)
        self.assertIn("KOL_FreeStockDB_Start", runtime)
        self.assertIn("_save_process_metadata", runtime)

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

    def test_hermes_is_operator_only_and_source_changes_use_worktree_pr(self) -> None:
        skill = (ROOT / "_skills" / "kol-research-operator" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "scripts" / "install-hermes-kol-research.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("Hermes operates the workbench", skill)
        self.assertIn("canonical skill source", skill)
        self.assertIn("D:\\aiworkspace\\_worktrees\\ai-hub-hermes", skill)
        self.assertIn("hermes/<task-id>", skill)
        self.assertIn("opening a PR", skill)
        self.assertIn("Use only the native `kol_operator` tool", skill)
        self.assertIn("Never use the `terminal` or `read_file` tool", skill)
        self.assertNotIn("pre_tool_call", installer)
        self.assertNotIn("ai-hub-source-guard.py", installer)
        self.assertNotIn("shell-hooks-allowlist.json", installer)
        self.assertNotIn("hermes_ai_hub_source_guard.py", installer)

    def test_private_adapter_does_not_offer_a_second_console(self) -> None:
        adapter = (ROOT / "_automation" / "trading_research" / "public_core_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn('"serve-private"', adapter)
        self.assertNotIn('8125', adapter)


if __name__ == "__main__":
    unittest.main()
