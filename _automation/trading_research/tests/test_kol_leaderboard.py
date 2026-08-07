from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_leaderboard import build_kol_leaderboard  # noqa: E402
from kol_tracker import EventRecord  # noqa: E402


def event(event_id: str, kol: str, warning: str = "", direction: str = "long") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        kol_name=kol,
        platform="X",
        source_url=f"https://x.com/example/status/{event_id}",
        source_note=f"post:{event_id}",
        posted_at="2026-01-01T08:00:00+08:00",
        symbol="600900",
        security_name="Fixture",
        direction=direction,
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

    def test_short_events_do_not_contribute_to_samples_or_rank(self) -> None:
        events = [event(f"A-{index}", "Alpha", direction="short") for index in range(10)]
        events += [event(f"B-{index}", "Beta") for index in range(10)]
        checkpoints = [checkpoint(f"A-{index}", 1.0) for index in range(10)]
        checkpoints += [checkpoint(f"B-{index}", 0.01) for index in range(10)]

        rows = build_kol_leaderboard(events, checkpoints)
        alpha = next(item for item in rows if item["kol_name"] == "Alpha")
        beta = next(item for item in rows if item["kol_name"] == "Beta")

        self.assertEqual(10, alpha["event_count"])
        self.assertEqual(0, alpha["executable_event_count"])
        self.assertEqual(0, alpha["horizons"]["1M"]["samples"])
        self.assertEqual("collecting", alpha["tier"])
        self.assertIsNone(alpha["rank"])
        self.assertEqual(1, beta["rank"])

    def test_directional_event_counts_split_long_and_short(self) -> None:
        events = [
            event("A-1", "Alpha"),
            event("A-2", "Alpha"),
            event("A-3", "Alpha"),
            event("A-S1", "Alpha", direction="short"),
            event("A-S2", "Alpha", direction="short"),
            event("A-W1", "Alpha", warning="conditional_intraday_entry_unverified"),
            event("A-W2", "Alpha", warning="secondhand"),
        ]
        checkpoints = [
            checkpoint("A-1", 0.04),
            checkpoint("A-2", 0.03),
            checkpoint("A-3", 0.02),
        ]
        rows = build_kol_leaderboard(events, checkpoints)
        alpha = next(item for item in rows if item["kol_name"] == "Alpha")

        self.assertEqual(7, alpha["event_count"])
        self.assertEqual(5, alpha["long_event_count"])
        self.assertEqual(2, alpha["short_event_count"])
        # Legacy caliber: long and executable (NON_EXECUTABLE_WARNINGS only).
        self.assertEqual(4, alpha["executable_event_count"])
        # Shared caliber with kol_performance: executable and free of primary warnings.
        self.assertEqual(3, alpha["executable_long_event_count"])
        self.assertEqual(7, alpha["audit_event_count"])
        self.assertEqual(3, alpha["horizons"]["1M"]["samples"])

    def test_short_only_kol_reports_counts_without_samples(self) -> None:
        events = [
            event("B-1", "Beta", direction="short"),
            event("B-2", "Beta", direction="short"),
        ]
        rows = build_kol_leaderboard(events, [])
        beta = next(item for item in rows if item["kol_name"] == "Beta")

        self.assertEqual(0, beta["long_event_count"])
        self.assertEqual(2, beta["short_event_count"])
        self.assertEqual(0, beta["executable_long_event_count"])
        self.assertEqual(2, beta["audit_event_count"])
        self.assertEqual(0, beta["horizons"]["1M"]["samples"])
        self.assertEqual("collecting", beta["tier"])

    def test_invalid_return_values_never_fake_a_loss(self) -> None:
        # Checkpoints whose return strings are unparsable ("x") or empty must
        # not be counted as 0.0 returns (previously this faked win_rate 0.0
        # and inflated the sample count for ranking).
        events = [event(f"A-{index}", "Alpha") for index in range(5)]
        checkpoints = [
            checkpoint(f"A-{index}", 0.03)
            if index % 2 == 0
            else {
                **checkpoint(f"A-{index}", 0.0),
                "directional_excess_return": "x",
                "directional_return": "",
            }
            for index in range(5)
        ]
        rows = build_kol_leaderboard(events, checkpoints)
        alpha = next(item for item in rows if item["kol_name"] == "Alpha")

        self.assertEqual(3, alpha["horizons"]["1M"]["samples"])
        self.assertGreater(alpha["horizons"]["1M"]["win_rate"], 0.5)

    def test_all_invalid_checkpoints_report_no_samples(self) -> None:
        events = [event(f"A-{index}", "Alpha") for index in range(5)]
        checkpoints = [
            {
                **checkpoint(f"A-{index}", 0.0),
                "directional_excess_return": "not-a-number",
                "directional_return": "",
                "max_adverse_return": "",
            }
            for index in range(5)
        ]
        rows = build_kol_leaderboard(events, checkpoints)
        alpha = next(item for item in rows if item["kol_name"] == "Alpha")

        self.assertEqual(0, alpha["horizons"]["1M"]["samples"])
        self.assertIsNone(alpha["score"])
        self.assertEqual("collecting", alpha["tier"])
        self.assertIsNone(alpha["rank"])


if __name__ == "__main__":
    unittest.main()
