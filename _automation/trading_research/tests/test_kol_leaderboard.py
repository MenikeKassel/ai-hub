from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_leaderboard import build_kol_leaderboard  # noqa: E402
from kol_tracker import EventRecord  # noqa: E402


def event(event_id: str, kol: str, warning: str = "") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        kol_name=kol,
        platform="X",
        source_url=f"https://x.com/example/status/{event_id}",
        source_note=f"post:{event_id}",
        posted_at="2026-01-01T08:00:00+08:00",
        symbol="600900",
        security_name="Fixture",
        direction="long",
        thesis="Fixture thesis",
        status="active",
        execution_warning=warning,
    )


def checkpoint(event_id: str, excess: float, *, status: str = "verified") -> dict[str, str]:
    return {
        "event_id": event_id,
        "horizon": "1M",
        "directional_return": str(excess + 0.01),
        "directional_excess_return": str(excess),
        "max_adverse_return": "-0.03",
        "verification_status": status,
    }


class KolLeaderboardTests(unittest.TestCase):
    def test_only_verified_executable_checkpoints_qualify_for_ranking(self) -> None:
        events = [event(f"A-{index}", "Alpha") for index in range(10)]
        events += [event(f"B-{index}", "Beta") for index in range(10)]
        events += [event("A-CONDITIONAL", "Alpha", "conditional_intraday_entry_unverified")]
        checkpoints = [checkpoint(f"A-{index}", 0.04) for index in range(10)]
        checkpoints += [checkpoint(f"B-{index}", 0.01) for index in range(10)]
        checkpoints += [checkpoint("A-CONDITIONAL", 1.0)]
        checkpoints += [checkpoint("B-0", 3.0, status="data_conflict")]

        rows = build_kol_leaderboard(events, checkpoints, kol_names=["No samples"])
        alpha = next(item for item in rows if item["kol_name"] == "Alpha")
        beta = next(item for item in rows if item["kol_name"] == "Beta")
        empty = next(item for item in rows if item["kol_name"] == "No samples")

        self.assertEqual("provisional", alpha["tier"])
        self.assertEqual(1, alpha["rank"])
        self.assertEqual(10, alpha["horizons"]["1M"]["samples"])
        self.assertEqual(2, beta["rank"])
        self.assertEqual("collecting", empty["tier"])
        self.assertIsNone(empty["rank"])

    def test_five_mature_events_are_watch_only(self) -> None:
        events = [event(f"A-{index}", "Alpha") for index in range(5)]
        rows = build_kol_leaderboard(
            events,
            [checkpoint(f"A-{index}", 0.02) for index in range(5)],
        )

        self.assertEqual("watch", rows[0]["tier"])
        self.assertIsNone(rows[0]["rank"])


if __name__ == "__main__":
    unittest.main()
