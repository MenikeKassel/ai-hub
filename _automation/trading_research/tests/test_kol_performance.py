from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_performance import KolPerformanceService, bootstrap_ci  # noqa: E402
from kol_tracker import EventRecord, KolStore  # noqa: E402


def event(event_id: str, source: str, symbol: str, *, posted: str = "2026-01-01") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        kol_name="Fixture KOL",
        platform="X",
        source_url=f"https://x.com/fixture/status/{source}",
        source_note=f"post:{source}",
        posted_at=f"{posted}T09:00:00+08:00",
        symbol=symbol,
        security_name="Fixture",
        direction="long",
        thesis="Fixture thesis",
        status="active",
        kol_id="7",
        kol_handle="fixture",
        source_post_id=source,
    )


def checkpoint(event_id: str, trade_date: str, excess: float) -> dict[str, str]:
    return {
        "event_id": event_id,
        "horizon": "1W",
        "target_days": "5",
        "trade_date": trade_date,
        "close_raw": "10",
        "raw_return": str(excess + 0.01),
        "adjusted_return": str(excess + 0.01),
        "directional_return": str(excess + 0.01),
        "benchmark_return": "0.01",
        "directional_excess_return": str(excess),
        "max_adverse_return": "-0.02",
        "max_favorable_return": str(excess + 0.01),
        "primary_source": "fixture",
        "secondary_source": "",
        "secondary_close": "",
        "verification_status": "verified",
        "finalized_at": trade_date,
    }


class KolPerformanceTests(unittest.TestCase):
    def test_same_post_multi_stock_is_one_equal_weighted_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([event("E1", "POST-1", "600000"), event("E2", "POST-1", "600001")], backup=False)
            store.freeze_checkpoints([
                checkpoint("E1", "2026-01-10", 0.08),
                checkpoint("E2", "2026-01-10", 0.18),
            ])
            result = KolPerformanceService(store).compute(as_of=date(2026, 1, 12), horizon="1W", window="all")
            metrics = result["rows"][0]["metrics"]
            self.assertEqual(1, metrics["batch_count"])
            self.assertEqual(2, metrics["event_count"])
            self.assertEqual(2, metrics["unique_symbols"])
            self.assertAlmostEqual(0.13, metrics["median_excess"])

    def test_different_posts_remain_separate_and_recent_window_uses_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([
                event("E1", "POST-1", "600000", posted="2025-12-01"),
                event("E2", "POST-2", "600001", posted="2025-12-02"),
            ], backup=False)
            store.freeze_checkpoints([
                checkpoint("E1", "2026-01-01", 0.02),
                checkpoint("E2", "2026-01-30", 0.10),
            ])
            service = KolPerformanceService(store)
            all_result = service.compute(as_of=date(2026, 1, 31), horizon="1W", window="all")
            recent_result = service.compute(as_of=date(2026, 1, 31), horizon="1W", window="7")
            self.assertEqual(2, all_result["rows"][0]["metrics"]["batch_count"])
            self.assertEqual(1, recent_result["rows"][0]["metrics"]["batch_count"])
            self.assertAlmostEqual(0.10, recent_result["rows"][0]["metrics"]["median_excess"])

    def test_bootstrap_is_deterministic_and_small_samples_have_no_interval(self) -> None:
        self.assertIsNone(bootstrap_ci([0.1, 0.2, 0.3, 0.4], seed="fixture"))
        values = [0.01, 0.02, 0.03, 0.04, 0.05]
        self.assertEqual(bootstrap_ci(values, seed="fixture"), bootstrap_ci(values, seed="fixture"))

    def test_refresh_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([event("E1", "POST-1", "600000")], backup=False)
            store.freeze_checkpoints([checkpoint("E1", "2026-01-10", 0.02)])
            service = KolPerformanceService(store)
            self.assertEqual(4, service.refresh(as_of=date(2026, 1, 12))["snapshots_inserted"])
            self.assertEqual(0, service.refresh(as_of=date(2026, 1, 12))["snapshots_inserted"])
            one_month = service.store.series("X:7", horizon="1M", window_name="all")
            self.assertEqual(1, len(one_month))
            self.assertEqual(0, one_month[0]["payload"]["metrics"]["batch_count"])


if __name__ == "__main__":
    unittest.main()
