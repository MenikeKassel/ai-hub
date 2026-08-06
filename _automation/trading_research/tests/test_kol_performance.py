from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_performance import (  # noqa: E402
    DeepSeekPerformanceInterpreter,
    KolPerformanceService,
    bootstrap_ci,
)
from kol_tracker import EventRecord, KolStore  # noqa: E402


def event(event_id: str, source: str, symbol: str, *, posted: str = "2026-01-01", direction: str = "long", warning: str = "") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        kol_name="Fixture KOL",
        platform="X",
        source_url=f"https://x.com/fixture/status/{source}",
        source_note=f"post:{source}",
        posted_at=f"{posted}T09:00:00+08:00",
        symbol=symbol,
        security_name="Fixture",
        direction=direction,
        thesis="Fixture thesis",
        status="active",
        kol_id="7",
        kol_handle="fixture",
        source_post_id=source,
        execution_warning=warning,
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
    def test_narrative_provider_uses_opencode_go_deepseek_v4_flash(self) -> None:
        self.assertEqual("deepseek-v4-flash", DeepSeekPerformanceInterpreter.model_name)
        self.assertEqual(
            "https://opencode.ai/zen/go/v1/chat/completions",
            DeepSeekPerformanceInterpreter.api_url,
        )

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

    def test_short_events_are_excluded_from_batch_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([
                event("E1", "POST-1", "600000"),
                event("E2", "POST-1", "600001", direction="short"),
                event("E3", "POST-2", "600002", direction="short"),
            ], backup=False)
            store.freeze_checkpoints([
                checkpoint("E1", "2026-01-10", 0.04),
                checkpoint("E2", "2026-01-10", 0.99),
            ])
            result = KolPerformanceService(store).compute(as_of=date(2026, 1, 12), horizon="1W", window="all")
            metrics = result["rows"][0]["metrics"]
            self.assertEqual(1, metrics["batch_count"])
            self.assertEqual(1, metrics["event_count"])
            self.assertEqual(1, metrics["unique_symbols"])
            self.assertEqual(0, metrics["unmatured_batch_count"])
            self.assertAlmostEqual(0.04, metrics["median_excess"])

    def test_long_only_metrics_carry_directional_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([
                event("E1", "POST-1", "600000"),
                event("E2", "POST-2", "600001"),
            ], backup=False)
            store.freeze_checkpoints([
                checkpoint("E1", "2026-01-10", 0.04),
                checkpoint("E2", "2026-01-10", 0.06),
            ])
            result = KolPerformanceService(store).compute(as_of=date(2026, 1, 12), horizon="1W", window="all")
            metrics = result["rows"][0]["metrics"]
            self.assertEqual(2, metrics["batch_count"])
            self.assertEqual(2, metrics["event_count"])
            self.assertEqual(2, metrics["long_event_count"])
            self.assertEqual(0, metrics["short_event_count"])
            self.assertEqual(2, metrics["executable_long_event_count"])
            self.assertEqual(2, metrics["audit_event_count"])
            self.assertAlmostEqual(0.05, metrics["median_excess"])

    def test_short_only_events_have_no_samples_but_report_short_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([
                event("S1", "POST-1", "600000", direction="short"),
                event("S2", "POST-2", "600001", direction="short"),
            ], backup=False)
            store.freeze_checkpoints([checkpoint("S1", "2026-01-10", 0.99)])
            result = KolPerformanceService(store).compute(as_of=date(2026, 1, 12), horizon="1W", window="all")
            metrics = result["rows"][0]["metrics"]
            self.assertEqual(0, metrics["batch_count"])
            self.assertEqual(0, metrics["event_count"])
            self.assertEqual(0, metrics["long_event_count"])
            self.assertEqual(2, metrics["short_event_count"])
            self.assertEqual(0, metrics["executable_long_event_count"])
            self.assertEqual(2, metrics["audit_event_count"])
            self.assertEqual("no_mature_samples", metrics["sample_status"])
            self.assertIsNone(metrics["median_excess"])

    def test_mixed_long_and_short_counts_do_not_change_long_returns_or_rank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_long, tempfile.TemporaryDirectory() as tmp_mixed:
            long_store = KolStore(Path(tmp_long))
            long_store.save_events([
                event("E1", "POST-1", "600000"),
                event("E2", "POST-2", "600001"),
            ], backup=False)
            long_store.freeze_checkpoints([
                checkpoint("E1", "2026-01-01", 0.04),
                checkpoint("E2", "2026-01-01", 0.06),
            ])
            mixed_store = KolStore(Path(tmp_mixed))
            mixed_store.save_events([
                event("E1", "POST-1", "600000"),
                event("E2", "POST-2", "600001"),
                event("S1", "POST-3", "600002", direction="short"),
            ], backup=False)
            mixed_store.freeze_checkpoints([
                checkpoint("E1", "2026-01-01", 0.04),
                checkpoint("E2", "2026-01-01", 0.06),
                checkpoint("S1", "2026-01-10", 0.99),
            ])
            long_result = KolPerformanceService(long_store).compute(as_of=date(2026, 1, 12), horizon="1W", window="all")
            mixed_result = KolPerformanceService(mixed_store).compute(as_of=date(2026, 1, 12), horizon="1W", window="all")
            long_metrics = long_result["rows"][0]["metrics"]
            mixed_metrics = mixed_result["rows"][0]["metrics"]
            for field in (
                "batch_count", "event_count", "long_event_count", "unique_symbols",
                "recommendation_days", "median_return", "mean_return", "median_excess",
                "mean_excess", "win_rate", "median_mae", "median_mfe", "worst_batch_excess",
                "p10_excess", "sample_status",
            ):
                self.assertEqual(long_metrics[field], mixed_metrics[field], field)
            self.assertEqual(0, long_metrics["short_event_count"])
            self.assertEqual(1, mixed_metrics["short_event_count"])
            self.assertEqual(3, mixed_metrics["audit_event_count"])
            self.assertEqual(long_result["rows"][0]["rank"], mixed_result["rows"][0]["rank"])
            # short_event_count is audit caliber: it survives the recent window
            # even when the long return sample is filtered out.
            recent = KolPerformanceService(mixed_store).compute(as_of=date(2026, 1, 12), horizon="1W", window="7")
            recent_metrics = recent["rows"][0]["metrics"]
            self.assertEqual(0, recent_metrics["long_event_count"])
            self.assertEqual(1, recent_metrics["short_event_count"])
            self.assertEqual(1, recent_metrics["audit_event_count"])

    def test_executable_long_count_excludes_primary_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            store.save_events([
                event("E1", "POST-1", "600000"),
                event("E2", "POST-2", "600001", warning="conditional_intraday_entry_unverified"),
                event("E3", "POST-3", "600002", warning="secondhand"),
            ], backup=False)
            store.freeze_checkpoints([
                checkpoint("E1", "2026-01-10", 0.04),
                checkpoint("E2", "2026-01-10", 0.99),
                checkpoint("E3", "2026-01-10", 0.99),
            ])
            service = KolPerformanceService(store)
            primary = service.compute(as_of=date(2026, 1, 12), horizon="1W", window="all", primary_only=True)
            primary_metrics = primary["rows"][0]["metrics"]
            self.assertEqual(1, primary_metrics["batch_count"])
            self.assertEqual(1, primary_metrics["long_event_count"])
            self.assertEqual(1, primary_metrics["executable_long_event_count"])
            # The secondary view admits the warned events into the sample, so the
            # sample count exceeds the executable long universe count.
            secondary = service.compute(as_of=date(2026, 1, 12), horizon="1W", window="all", primary_only=False)
            secondary_metrics = secondary["rows"][0]["metrics"]
            self.assertEqual(2, secondary_metrics["batch_count"])
            self.assertEqual(2, secondary_metrics["long_event_count"])
            self.assertEqual(1, secondary_metrics["executable_long_event_count"])
            self.assertEqual(0, secondary_metrics["short_event_count"])
            self.assertEqual(2, secondary_metrics["audit_event_count"])

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
