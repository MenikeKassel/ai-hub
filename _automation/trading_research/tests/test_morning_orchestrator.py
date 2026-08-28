from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from morning_orchestrator import MorningOrchestrator, effective_phase  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MorningOrchestratorTests(unittest.TestCase):
    def test_late_start_runs_only_one_final_catch_up(self) -> None:
        current = datetime(2026, 7, 18, 11, 10, tzinfo=SHANGHAI)
        calls: list[tuple[str, float]] = []
        orchestrator = MorningOrchestrator(
            runner=lambda phase, runtime: calls.append((phase, runtime)) or {"ok": True},
            now_provider=lambda: current,
        )

        result = orchestrator.run()

        self.assertEqual("final", effective_phase(current))
        self.assertEqual([("final", 25)], calls)
        self.assertEqual(["final"], result["phases"])

    def test_initial_run_that_crosses_refresh_boundary_chains_refresh(self) -> None:
        moments = iter([
            datetime(2026, 7, 18, 7, 20, tzinfo=SHANGHAI),
            datetime(2026, 7, 18, 8, 25, tzinfo=SHANGHAI),
            datetime(2026, 7, 18, 8, 30, tzinfo=SHANGHAI),
        ])
        calls: list[str] = []
        orchestrator = MorningOrchestrator(
            runner=lambda phase, runtime: calls.append(phase) or {"ok": True},
            now_provider=lambda: next(moments),
        )

        result = orchestrator.run()

        self.assertEqual(["initial", "refresh"], calls)
        self.assertEqual(["initial", "refresh"], result["phases"])

    def test_refresh_that_crosses_final_boundary_chains_final(self) -> None:
        moments = iter([
            datetime(2026, 7, 18, 8, 20, tzinfo=SHANGHAI),
            datetime(2026, 7, 18, 8, 47, tzinfo=SHANGHAI),
            datetime(2026, 7, 18, 8, 55, tzinfo=SHANGHAI),
        ])
        calls: list[str] = []
        orchestrator = MorningOrchestrator(
            runner=lambda phase, runtime: calls.append(phase) or {"ok": True},
            now_provider=lambda: next(moments),
        )

        orchestrator.run()

        self.assertEqual(["refresh", "final"], calls)


if __name__ == "__main__":
    unittest.main()
