from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_tracker import (  # noqa: E402
    EventRecord,
    KolStore,
    UpdateResult,
    WarehousePriceProvider,
    calculate_event_history,
    generate_dashboard,
    initialize_seed_events,
    update_kol_tracking,
    validate_event,
)
from trading_cli import _send_pending_notifications, kol_update  # noqa: E402


def price_frame(dates: list[str], opens: list[float], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": opens,
            "high": [max(a, b) for a, b in zip(opens, closes)],
            "low": [min(a, b) for a, b in zip(opens, closes)],
            "close": closes,
            "volume": [100000.0] * len(dates),
        }
    )


def active_event(**overrides: str) -> EventRecord:
    values = {
        "event_id": "KOL-T001",
        "kol_name": "测试KOL",
        "platform": "X",
        "source_url": "https://x.com/test/status/1",
        "source_note": "01_Sources/test.md",
        "posted_at": "2026-07-08T10:00:00+08:00",
        "symbol": "600000",
        "security_name": "浦发银行",
        "direction": "long",
        "thesis": "事前明确看多并给出理由",
        "status": "active",
    }
    values.update(overrides)
    return EventRecord(**values)


class EventValidationTests(unittest.TestCase):
    def test_active_event_requires_six_elements_and_one_symbol(self) -> None:
        missing_source = active_event(source_url="")
        self.assertIn("source_url", validate_event(missing_source))

        multi_symbol = active_event(symbol="600000,000001")
        self.assertIn("one_symbol_per_event", validate_event(multi_symbol))

    def test_candidate_can_preserve_incomplete_evidence_without_tracking(self) -> None:
        candidate = active_event(status="candidate", source_url="", posted_at="")
        self.assertEqual([], validate_event(candidate))

    def test_unknown_status_is_rejected(self) -> None:
        self.assertEqual(["status"], validate_event(active_event(status="unknown")))


class EventAmendmentTests(unittest.TestCase):
    def test_metadata_amendment_keeps_returns_and_writes_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp) / "kol")
            event = active_event(baseline_date="2026-07-08", baseline_price_raw="10.00")
            store.register_event(event)
            store.upsert_marks([{"event_id": event.event_id, "trade_date": "2026-07-09", "close_raw": "10.50"}])

            updated, revision = store.amend_event(
                event.event_id,
                {"thesis": "人工核对后修正推荐理由"},
                reason="AI理由概括错误",
            )

            self.assertEqual("人工核对后修正推荐理由", updated.thesis)
            self.assertEqual("2026-07-08", updated.baseline_date)
            self.assertEqual(1, len(store.load_marks()))
            self.assertFalse(revision["recalculation_required"])
            self.assertEqual(revision["revision_id"], store.list_event_revisions(event.event_id)[0]["revision_id"])

    def test_core_amendment_archives_and_clears_derived_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp) / "kol")
            event = active_event(
                baseline_rule="same_day_close",
                baseline_date="2026-07-08",
                baseline_price_raw="10.00",
                benchmark_baseline_price="4000",
            )
            store.register_event(event)
            store.upsert_marks([{"event_id": event.event_id, "trade_date": "2026-07-09", "close_raw": "10.50"}])
            store.freeze_checkpoints([{
                "event_id": event.event_id,
                "horizon": "1W",
                "verification_status": "verified",
            }])

            updated, revision = store.amend_event(
                event.event_id,
                {"symbol": "000001", "security_name": "平安银行"},
                reason="AI股票映射错误",
            )

            self.assertEqual("000001", updated.symbol)
            self.assertEqual("", updated.baseline_date)
            self.assertEqual("", updated.baseline_price_raw)
            self.assertEqual([], store.load_marks())
            self.assertEqual([], store.load_checkpoints())
            self.assertTrue(revision["recalculation_required"])
            self.assertTrue(any(store.backups_dir.glob("*_daily_marks.csv")))
            self.assertTrue(any(store.backups_dir.glob("*_checkpoints.csv")))


class WarehousePriceProviderTests(unittest.TestCase):
    def test_reads_and_slices_validated_local_daily_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            warehouse = Path(tmp) / "warehouse"
            stock = price_frame(
                ["2026-07-01", "2026-07-02", "2026-07-03"],
                [10.0, 10.5, 11.0],
                [10.2, 10.8, 11.2],
            ).rename(columns={"date": "trade_date"})
            benchmark = price_frame(
                ["2026-07-01", "2026-07-02", "2026-07-03"],
                [100.0, 101.0, 102.0],
                [100.5, 101.5, 102.5],
            ).rename(columns={"date": "trade_date"})
            for adjustment in ["raw", "qfq"]:
                path = warehouse / "daily" / "600000" / adjustment / "2026.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                stock.to_parquet(path, index=False)
            index_path = warehouse / "daily" / "000300" / "raw" / "2026.parquet"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            benchmark.to_parquet(index_path, index=False)

            provider = WarehousePriceProvider(warehouse)
            selected = provider.fetch_stock(
                "600000", date(2026, 7, 2), date(2026, 7, 3), adjusted=False
            )
            selected_benchmark = provider.fetch_benchmark(date(2026, 7, 2), date(2026, 7, 3))

            self.assertEqual(["2026-07-02", "2026-07-03"], selected["date"].dt.strftime("%Y-%m-%d").tolist())
            self.assertEqual([101.5, 102.5], selected_benchmark["close"].tolist())
            self.assertEqual(2, len(provider._cache))


class BaselineAndReturnTests(unittest.TestCase):
    def test_suspension_counts_market_time_but_uses_last_trade_valuation(self) -> None:
        dates = [
            "2026-07-22",
            "2026-07-23",
            "2026-07-24",
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
        ]
        raw = price_frame(dates, [7.2, 7.23, 7.23, 7.23, 7.23, 7.23, 6.9], [7.23, 7.23, 7.23, 7.23, 7.23, 7.23, 7.1])
        raw["trade_status"] = [1, 0, 0, 0, 0, 0, 1]
        benchmark = price_frame(dates, [100] * len(dates), [100, 101, 102, 103, 104, 105, 106])
        event = active_event(posted_at="2026-07-22T09:00:11+08:00")

        result = calculate_event_history(event, raw, raw, benchmark)
        suspended = result.marks[1]
        checkpoint = next(item for item in result.checkpoints if item["horizon"] == "1W")

        self.assertEqual("0", suspended["market_open"])
        self.assertEqual("1", suspended["suspended"])
        self.assertEqual("2026-07-22", suspended["last_trade_date"])
        self.assertEqual("7.23000000", suspended["valuation_close"])
        self.assertEqual("1", checkpoint["suspended_at_checkpoint"])
        self.assertEqual("0", checkpoint["executable"])
        self.assertEqual("2026-07-29", checkpoint["trade_date"])

    def test_after_close_uses_next_trading_day_open(self) -> None:
        event = active_event(
            posted_at="2026-07-08T19:35:49+08:00",
            execution_warning="conditional_intraday_entry_unverified",
        )
        raw = price_frame(
            ["2026-07-08", "2026-07-09", "2026-07-10"],
            [9.5, 10.0, 10.6],
            [9.8, 10.5, 10.8],
        )
        benchmark = price_frame(
            ["2026-07-08", "2026-07-09", "2026-07-10"],
            [100.0, 101.0, 102.0],
            [100.5, 101.5, 102.5],
        )

        result = calculate_event_history(event, raw, raw, benchmark)

        self.assertEqual("2026-07-09", result.event.baseline_date)
        self.assertEqual("next_open", result.event.baseline_rule)
        self.assertAlmostEqual(10.0, float(result.event.baseline_price_raw))
        self.assertAlmostEqual(101.0, float(result.event.benchmark_baseline_price))
        self.assertIn("conditional_intraday_entry_unverified", result.event.execution_warning)

    def test_lunch_post_uses_same_day_close(self) -> None:
        event = active_event(posted_at="2026-07-10T12:32:29+08:00")
        raw = price_frame(["2026-07-10", "2026-07-13"], [49.0, 51.0], [50.0, 52.0])
        benchmark = price_frame(["2026-07-10", "2026-07-13"], [100.0, 101.0], [100.0, 102.0])

        result = calculate_event_history(event, raw, raw, benchmark)

        self.assertEqual("2026-07-10", result.event.baseline_date)
        self.assertEqual("same_day_close", result.event.baseline_rule)
        self.assertAlmostEqual(50.0, float(result.event.baseline_price_raw))

    def test_weekend_or_suspension_uses_first_future_open_and_warns(self) -> None:
        event = active_event(posted_at="2026-07-11T10:00:00+08:00")
        raw = price_frame(["2026-07-10", "2026-07-14"], [9.5, 10.0], [9.8, 10.5])
        benchmark = price_frame(["2026-07-10", "2026-07-14"], [100, 101], [100.5, 101.5])

        result = calculate_event_history(event, raw, raw, benchmark)

        self.assertEqual("2026-07-14", result.event.baseline_date)
        self.assertEqual("next_open", result.event.baseline_rule)
        self.assertIn("delayed_baseline", result.event.execution_warning)

    def test_short_direction_is_viewpoint_performance(self) -> None:
        event = active_event(direction="short")
        raw = price_frame(["2026-07-08", "2026-07-09"], [10, 10], [10, 9])
        benchmark = price_frame(["2026-07-08", "2026-07-09"], [100, 100], [100, 102])

        result = calculate_event_history(event, raw, raw, benchmark)
        last = result.marks[-1]

        self.assertEqual("0.10000000", last["directional_return"])
        self.assertEqual("0.12000000", last["directional_excess_return"])

    def test_returns_and_fifth_trading_day_checkpoint_are_literal(self) -> None:
        dates = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]
        event = active_event(posted_at="2026-07-01T10:00:00+08:00")
        raw = price_frame(dates, [10, 10, 11, 12, 13, 14], [10, 11, 12, 13, 14, 15])
        benchmark = price_frame(dates, [100] * 6, [100, 101, 102, 103, 104, 105])

        result = calculate_event_history(event, raw, raw, benchmark)
        last = result.marks[-1]

        self.assertEqual("5", last["tracking_days"])
        self.assertEqual("0.50000000", last["raw_return"])
        self.assertEqual("0.05000000", last["benchmark_return"])
        self.assertEqual("0.45000000", last["directional_excess_return"])
        self.assertEqual("0.50000000", last["max_favorable_return"])
        self.assertEqual(1, len(result.checkpoints))
        self.assertEqual("1W", result.checkpoints[0]["horizon"])
        self.assertEqual("2026-07-08", result.checkpoints[0]["trade_date"])
        self.assertEqual("0.50000000", result.checkpoints[0]["max_favorable_return"])


class StoreAndDashboardTests(unittest.TestCase):
    def test_legacy_return_csv_is_backed_up_and_migrated_with_mfe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(parents=True, exist_ok=True)
            with (root / "daily_marks.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["event_id", "trade_date", "directional_return"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"event_id": "KOL-T001", "trade_date": "2026-07-01", "directional_return": "-0.02"},
                        {"event_id": "KOL-T001", "trade_date": "2026-07-02", "directional_return": "0.05"},
                        {"event_id": "KOL-T002", "trade_date": "2026-07-02", "directional_return": "broken"},
                    ]
                )
            with (root / "checkpoints.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["event_id", "horizon", "trade_date"])
                writer.writeheader()
                writer.writerow({"event_id": "KOL-T001", "horizon": "1W", "trade_date": "2026-07-02"})

            store = KolStore(root)
            marks = store.load_marks()
            checkpoints = store.load_checkpoints()

            self.assertEqual(["0.00000000", "0.05000000", ""], [row["max_favorable_return"] for row in marks])
            self.assertEqual("0.05000000", checkpoints[0]["max_favorable_return"])
            self.assertTrue(any(path.name.endswith("_daily_marks.csv") for path in store.backups_dir.iterdir()))
            self.assertTrue(any(path.name.endswith("_checkpoints.csv") for path in store.backups_dir.iterdir()))
            self.assertIn("mfe_migration_warning", store.runs_path.read_text(encoding="utf-8"))

    def test_notification_failure_does_not_fail_a_completed_return_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            KolStore(root).save_events([])
            result = UpdateResult("run", 0, 0, [], [], [], [])
            output = StringIO()
            with (
                patch("trading_cli.KOL_ROOT", root),
                patch("trading_cli.KOL_DASHBOARD", root / "dashboard.md"),
                patch("trading_cli.update_kol_tracking", return_value=result),
                patch("trading_cli._send_pending_notifications", return_value=["notification failed"]),
                redirect_stdout(output),
            ):
                kol_update(Namespace(as_of="2026-07-15", dry_run=False, notify=True))

            self.assertIn('"ok": true', output.getvalue())
            self.assertIn("notification failed", output.getvalue())

    def test_registration_and_daily_updates_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            event = active_event()
            created_first = store.register_event(event)
            created_second = store.register_event(event)

            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(1, len(store.load_events()))

            mark = {
                "event_id": event.event_id,
                "trade_date": "2026-07-08",
                "close_raw": "10.00000000",
                "raw_return": "0.00000000",
            }
            store.upsert_marks([mark])
            store.upsert_marks([mark])
            self.assertEqual(1, len(store.load_marks()))

    def test_frozen_checkpoint_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            first = {
                "event_id": "KOL-T001",
                "horizon": "1W",
                "trade_date": "2026-07-08",
                "raw_return": "0.10000000",
                "verification_status": "verified",
                "finalized_at": "2026-07-08T20:00:00+08:00",
            }
            revised = dict(first, raw_return="0.90000000")
            store.freeze_checkpoints([first])
            store.freeze_checkpoints([revised])

            self.assertEqual("0.10000000", store.load_checkpoints()[0]["raw_return"])

    def test_checkpoint_release_change_is_logged_without_overwriting_frozen_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            first = {
                "event_id": "KOL-T001",
                "horizon": "1W",
                "trade_date": "2026-07-08",
                "raw_return": "0.10000000",
                "verification_status": "verified_suspended",
                "foundation_release_id": "release-old",
                "finalized_at": "2026-07-08T20:00:00+08:00",
            }
            refreshed = dict(first, foundation_release_id="release-new", finalized_at="2026-07-09T20:00:00+08:00")
            store.freeze_checkpoints([first])
            store.freeze_checkpoints([refreshed])

            self.assertEqual("release-old", store.load_checkpoints()[0]["foundation_release_id"])
            revisions = [
                line
                for line in store.checkpoint_revisions_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(1, len(revisions))
            self.assertIn("release-new", revisions[0])

    def test_dashboard_is_generated_separately_from_manual_event_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            event = active_event()
            store.register_event(event)
            store.upsert_marks(
                [
                    {
                        "event_id": event.event_id,
                        "trade_date": "2026-07-08",
                        "close_raw": "10.50000000",
                        "raw_return": "0.05000000",
                        "directional_return": "0.05000000",
                        "benchmark_return": "0.01000000",
                        "directional_excess_return": "0.04000000",
                        "max_adverse_return": "0.00000000",
                        "tracking_days": "0",
                        "data_source": "fixture",
                        "data_status": "ok",
                    }
                ]
            )
            dashboard = root / "KOL推荐收益看板.md"
            manual = root / "KOL推荐事件表.md"
            manual.write_text("manual content\n", encoding="utf-8")

            generate_dashboard(store, dashboard)

            self.assertIn("KOL 推荐收益看板", dashboard.read_text(encoding="utf-8"))
            self.assertIn("5.00%", dashboard.read_text(encoding="utf-8"))
            self.assertEqual("manual content\n", manual.read_text(encoding="utf-8"))

    def test_conditional_intraday_event_is_not_counted_as_executable_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(
                active_event(
                    status="completed",
                    execution_warning="conditional_intraday_entry_unverified",
                )
            )
            dashboard = root / "dashboard.md"

            generate_dashboard(store, dashboard)

            content = dashboard.read_text(encoding="utf-8")
            self.assertIn("暂无完成120交易日跟踪的事件", content)
            self.assertNotIn("测试KOL: 1 条", content)
            self.assertIn("含未验证盘中条件", content)

    def test_seed_migration_activates_only_0004_and_0005(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            initialize_seed_events(store)
            statuses = {row.event_id: row.status for row in store.load_events()}

            self.assertEqual("active", statuses["KOL-0004"])
            self.assertEqual("active", statuses["KOL-0005"])
            self.assertEqual("candidate", statuses["KOL-0001"])
            self.assertEqual("candidate", statuses["KOL-0002"])
            self.assertEqual("excluded", statuses["KOL-0003"])

    def test_notification_queue_is_deduplicated_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            payload = {
                "kind": "checkpoint",
                "key": "checkpoint:KOL-T001:1W",
                "message": "reached 1W",
                "event_id": "KOL-T001",
            }

            self.assertTrue(store.queue_notification(payload))
            self.assertFalse(store.queue_notification(payload))
            self.assertEqual(1, len(store.pending_notifications()))
            store.record_notification(payload["key"], payload)
            self.assertEqual([], store.pending_notifications())

    def test_pending_notifications_are_sent_as_one_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolStore(Path(tmp))
            for event_id in ["KOL-T001", "KOL-T002"]:
                store.queue_notification(
                    {
                        "kind": "source_failure",
                        "key": f"source_failure:{event_id}",
                        "message": f"{event_id} data source failed",
                        "event_id": event_id,
                    }
                )

            with patch("trading_cli._send_feishu", return_value=True) as send:
                failures = _send_pending_notifications(store)

            self.assertEqual([], failures)
            send.assert_called_once()
            self.assertIn("数据源异常 2", send.call_args.args[0])
            self.assertEqual([], store.pending_notifications())


class FakeProvider:
    def __init__(
        self,
        name: str,
        stocks: dict[str, pd.DataFrame],
        benchmark: pd.DataFrame,
    ) -> None:
        self.name = name
        self.stocks = stocks
        self.benchmark = benchmark

    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame:
        return self.stocks[symbol].copy()

    def fetch_benchmark(self, start: date, end: date) -> pd.DataFrame:
        return self.benchmark.copy()


class FailingProvider(FakeProvider):
    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame:
        raise RuntimeError("source offline")


class UpdateCoordinatorTests(unittest.TestCase):
    def _six_day_prices(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        dates = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]
        stock = price_frame(dates, [10, 10, 11, 12, 13, 14], [10, 11, 12, 13, 14, 15])
        benchmark = price_frame(dates, [100] * 6, [100, 101, 102, 103, 104, 105])
        return stock, benchmark

    def test_verified_checkpoint_is_frozen_once(self) -> None:
        stock, benchmark = self._six_day_prices()
        primary = FakeProvider("primary", {"600000": stock}, benchmark)
        secondary = FakeProvider("secondary", {"600000": stock}, benchmark)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(active_event(posted_at="2026-07-01T10:00:00+08:00"))
            first = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=date(2026, 7, 8),
                dashboard_path=root / "dashboard.md",
            )
            second = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=date(2026, 7, 8),
                dashboard_path=root / "dashboard.md",
            )

            self.assertEqual(["1W"], [row["horizon"] for row in store.load_checkpoints()])
            self.assertEqual(1, len(first.new_checkpoints))
            self.assertEqual(0, len(second.new_checkpoints))
            self.assertEqual(6, len(store.load_marks()))

    def test_completed_event_continues_tracking_after_120_days(self) -> None:
        dates = pd.bdate_range("2026-01-05", periods=130)
        date_values = [value.date().isoformat() for value in dates]
        stock = price_frame(
            date_values,
            [10.0 + index * 0.01 for index in range(len(dates))],
            [10.1 + index * 0.01 for index in range(len(dates))],
        )
        benchmark = price_frame(
            date_values,
            [100.0 + index * 0.05 for index in range(len(dates))],
            [100.1 + index * 0.05 for index in range(len(dates))],
        )
        primary = FakeProvider("primary", {"600000": stock}, benchmark)
        secondary = FakeProvider("secondary", {"600000": stock}, benchmark)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(
                active_event(
                    status="completed",
                    posted_at=f"{date_values[0]}T10:00:00+08:00",
                )
            )
            store.freeze_checkpoints(
                [
                    {
                        "event_id": "KOL-T001",
                        "horizon": "6M",
                        "trade_date": date_values[119],
                        "directional_return": "0.10000000",
                        "verification_status": "verified",
                    }
                ]
            )

            result = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=dates[-1].date(),
                dashboard_path=root / "dashboard.md",
            )

            marks = store.load_marks()
            checkpoint = next(
                row for row in store.load_checkpoints() if row["horizon"] == "6M"
            )
            self.assertEqual(130, len(marks))
            self.assertEqual(date_values[-1], marks[-1]["trade_date"])
            self.assertEqual("completed", store.load_events()[0].status)
            self.assertEqual("0.10000000", checkpoint["directional_return"])
            self.assertEqual(1, result.updated_events)

    def test_source_conflict_does_not_freeze_checkpoint(self) -> None:
        stock, benchmark = self._six_day_prices()
        conflicting = stock.copy()
        conflicting.loc[conflicting.index[-1], "close"] = 16.0
        primary = FakeProvider("primary", {"600000": stock}, benchmark)
        secondary = FakeProvider("secondary", {"600000": conflicting}, benchmark)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(active_event(posted_at="2026-07-01T10:00:00+08:00"))
            result = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=date(2026, 7, 8),
                dashboard_path=root / "dashboard.md",
            )

            self.assertEqual([], store.load_checkpoints())
            self.assertTrue(any(item["kind"] == "data_conflict" for item in result.notifications))

    def test_dry_run_writes_nothing(self) -> None:
        stock, benchmark = self._six_day_prices()
        primary = FakeProvider("primary", {"600000": stock}, benchmark)
        secondary = FakeProvider("secondary", {"600000": stock}, benchmark)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(active_event(posted_at="2026-07-01T10:00:00+08:00"))
            events_before = store.events_path.read_bytes()
            result = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=date(2026, 7, 8),
                dashboard_path=root / "dashboard.md",
                dry_run=True,
            )

            self.assertEqual(events_before, store.events_path.read_bytes())
            self.assertFalse(store.marks_path.exists())
            self.assertFalse(store.checkpoints_path.exists())
            self.assertFalse((root / "dashboard.md").exists())
            self.assertEqual(1, len(result.new_checkpoints))

    def test_primary_failure_uses_secondary_but_does_not_freeze_node(self) -> None:
        stock, benchmark = self._six_day_prices()
        primary = FailingProvider("primary", {"600000": stock}, benchmark)
        secondary = FakeProvider("secondary", {"600000": stock}, benchmark)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(active_event(posted_at="2026-07-01T10:00:00+08:00"))
            result = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=date(2026, 7, 8),
                dashboard_path=root / "dashboard.md",
            )

            self.assertEqual(6, len(store.load_marks()))
            self.assertEqual([], store.load_checkpoints())
            self.assertEqual("secondary", store.load_marks()[0]["data_source"])
            self.assertTrue(any(item["kind"] == "source_failure" for item in result.notifications))

    def test_event_waiting_for_its_first_market_bar_is_not_a_calculation_failure(self) -> None:
        stock = price_frame(["2026-07-14"], [10], [10.5])
        benchmark = price_frame(["2026-07-14"], [100], [101])
        primary = FakeProvider("primary", {"600000": stock}, benchmark)
        secondary = FakeProvider("secondary", {"600000": stock}, benchmark)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolStore(root / "kol")
            store.register_event(active_event(posted_at="2026-07-15T10:00:00+08:00"))

            result = update_kol_tracking(
                store,
                primary,
                secondary,
                as_of=date(2026, 7, 15),
                dashboard_path=root / "dashboard.md",
            )

            self.assertEqual([], result.errors)
            self.assertEqual([], store.load_marks())
            self.assertFalse(any(item["kind"] == "calculation_failure" for item in result.notifications))


if __name__ == "__main__":
    unittest.main()
