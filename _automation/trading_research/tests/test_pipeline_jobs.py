from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_jobs import (  # noqa: E402
    read_refresh_state,
    reconcile_refresh_state,
    run_post_approval_refresh,
)


class PipelineJobTests(unittest.TestCase):
    def test_failed_refresh_is_superseded_by_newer_current_publication(self) -> None:
        state = {
            "status": "failed",
            "as_of": "2026-08-29",
            "error": "market sync failed",
        }

        result = reconcile_refresh_state(
            state,
            {
                "market_status": "current",
                "published_as_of": "2026-09-08",
            },
        )

        self.assertEqual("superseded", result["status"])
        self.assertEqual("failed", result["legacy_state"])
        self.assertEqual("2026-09-08", result["superseded_by"])

    def test_failed_refresh_remains_failed_while_market_is_stale(self) -> None:
        state = {"status": "failed", "as_of": "2026-09-08"}

        result = reconcile_refresh_state(
            state,
            {
                "market_status": "stale",
                "published_as_of": "2026-09-08",
            },
        )

        self.assertIs(state, result)

    def test_approval_refresh_runs_market_before_returns_and_records_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "_automation" / "trading_research" / "trading_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# fixture", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, "{}", "")

            with patch("pipeline_jobs.subprocess.run", side_effect=[completed, completed]) as run:
                run_post_approval_refresh(
                    root / "runtime",
                    root,
                    ["002414", "002414"],
                    as_of=date(2026, 7, 15),
                    python=sys.executable,
                )

            state = read_refresh_state(root / "runtime")
            self.assertEqual("completed", state["status"])
            self.assertEqual(["002414"], state["symbols"])
            self.assertIn("market-sync", run.call_args_list[0].args[0])
            self.assertIn("kol-update", run.call_args_list[1].args[0])

    def test_approval_refresh_retries_transient_duckdb_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "_automation" / "trading_research" / "trading_cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text("# fixture", encoding="utf-8")
            locked = subprocess.CompletedProcess([], 1, "", "Cannot open file: another process")
            completed = subprocess.CompletedProcess([], 0, "{}", "")

            with (
                patch("pipeline_jobs.subprocess.run", side_effect=[locked, completed, completed]) as run,
                patch("pipeline_jobs.time.sleep") as sleep,
            ):
                result = run_post_approval_refresh(
                    root / "runtime",
                    root,
                    ["002414"],
                    as_of=date(2026, 7, 15),
                    python=sys.executable,
                )

            self.assertEqual("completed", result["status"])
            self.assertEqual(3, run.call_count)
            sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
