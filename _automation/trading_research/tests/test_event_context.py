from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_context import (  # noqa: E402
    FEATURE_VERSION,
    compute_event_technical_context,
    event_context_input_hash,
)
from market_data import MarketStore  # noqa: E402
from kol_tracker import EventRecord, KolStore  # noqa: E402
from trading_cli import _backfill_event_contexts  # noqa: E402


def feature_frame(periods: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range("2026-03-02", periods=periods)
    closes = pd.Series([10.0 + index * 0.12 for index in range(periods)])
    return pd.DataFrame(
        {
            "trade_date": dates.date,
            "open": closes - 0.05,
            "high": closes + 0.18,
            "low": closes - 0.20,
            "close": closes,
            "volume": [1000.0 + index * 10 for index in range(periods)],
        }
    )


class EventTechnicalContextTests(unittest.TestCase):
    def test_morning_post_uses_previous_complete_bar_without_lookahead(self) -> None:
        frame = feature_frame()
        posted_date = frame.iloc[-1]["trade_date"]
        posted_at = f"{posted_date.isoformat()}T10:00:00+08:00"

        original = compute_event_technical_context(
            event_id="KOL-T001",
            symbol="600900",
            posted_at=posted_at,
            qfq_prices=frame,
        )
        changed = frame.copy()
        changed.loc[changed.index[-1], ["high", "close", "volume"]] = [999.0, 999.0, 999999.0]
        replayed = compute_event_technical_context(
            event_id="KOL-T001",
            symbol="600900",
            posted_at=posted_at,
            qfq_prices=changed,
        )

        self.assertEqual(frame.iloc[-2]["trade_date"].isoformat(), original.as_of_trade_date)
        self.assertEqual(original.rsi14, replayed.rsi14)
        self.assertEqual(original.macd_hist_pct, replayed.macd_hist_pct)
        self.assertEqual("complete", original.status)

    def test_after_close_post_uses_same_day_and_computes_all_features(self) -> None:
        frame = feature_frame()
        posted_date = frame.iloc[-1]["trade_date"]
        context = compute_event_technical_context(
            event_id="KOL-T002",
            symbol="600900",
            posted_at=f"{posted_date.isoformat()}T15:00:00+08:00",
            qfq_prices=frame,
        )

        expected_volume_ratio = frame.iloc[-1]["volume"] / frame.iloc[-6:-1]["volume"].mean()
        expected_return = frame.iloc[-1]["close"] / frame.iloc[-21]["close"] - 1
        expected_high_distance = frame.iloc[-1]["close"] / frame.iloc[-60:]["close"].max() - 1
        self.assertEqual(posted_date.isoformat(), context.as_of_trade_date)
        self.assertAlmostEqual(expected_volume_ratio, context.volume_ratio_5 or 0.0)
        self.assertAlmostEqual(expected_return, context.return_20d or 0.0)
        self.assertAlmostEqual(expected_high_distance, context.distance_60d_high or 0.0)
        self.assertIsNotNone(context.rsi14)
        self.assertIsNotNone(context.macd_hist_pct)
        self.assertIsNotNone(context.atr14_pct)

    def test_short_history_is_partial_and_never_zero_filled(self) -> None:
        frame = feature_frame(10)
        posted_date = frame.iloc[-1]["trade_date"]
        context = compute_event_technical_context(
            event_id="KOL-T003",
            symbol="600900",
            posted_at=f"{posted_date.isoformat()}T16:00:00+08:00",
            qfq_prices=frame,
        )

        self.assertEqual("partial", context.status)
        self.assertIsNone(context.rsi14)
        self.assertIsNone(context.macd_hist)
        self.assertIsNone(context.return_20d)
        self.assertIsNone(context.distance_60d_high)
        self.assertIn("insufficient_rsi14", context.warnings)

    def test_market_store_keeps_append_only_event_snapshots_idempotently(self) -> None:
        frame = feature_frame()
        posted_date = frame.iloc[-1]["trade_date"]
        posted_at = f"{posted_date.isoformat()}T16:00:00+08:00"
        context = compute_event_technical_context(
            event_id="KOL-T004",
            symbol="600900",
            posted_at=posted_at,
            qfq_prices=frame,
            computed_at="2026-07-20T10:00:00+08:00",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            self.assertTrue(store.save_event_technical_context(context.to_record()))
            self.assertFalse(store.save_event_technical_context(context.to_record()))
            saved = store.get_event_technical_context(
                "KOL-T004",
                input_hash=event_context_input_hash("600900", posted_at),
            )

            self.assertIsNotNone(saved)
            self.assertEqual(FEATURE_VERSION, saved["feature_version"])
            self.assertEqual("complete", saved["status"])
            self.assertEqual([], saved["warnings"])
            self.assertEqual(1, len(store.list_event_technical_contexts("KOL-T004")))
            changed = frame.copy()
            changed.loc[changed.index[-5], "close"] += 0.5
            replacement = compute_event_technical_context(
                event_id="KOL-T004",
                symbol="600900",
                posted_at=posted_at,
                qfq_prices=changed,
                computed_at="2026-07-20T10:01:00+08:00",
            )
            self.assertTrue(store.save_event_technical_context(replacement.to_record(), force=True))
            self.assertEqual(replacement.source_hash, store.get_event_technical_context("KOL-T004")["source_hash"])
            self.assertEqual(2, len(store.list_event_technical_contexts("KOL-T004")))

    def test_stale_open_session_remains_pending_until_expected_bar_arrives(self) -> None:
        frame = feature_frame()
        expected = pd.Timestamp(frame.iloc[-1]["trade_date"]) + pd.offsets.BDay(1)
        posted_at = f"{expected.date().isoformat()}T16:00:00+08:00"
        stale = compute_event_technical_context(
            event_id="KOL-T-ST",
            symbol="600900",
            posted_at=posted_at,
            qfq_prices=frame,
            expected_trade_date=expected.date(),
            computed_at="2026-07-20T10:00:00+08:00",
        )
        new_close = float(frame.iloc[-1]["close"]) + 0.2
        extended = pd.concat(
            [
                frame,
                pd.DataFrame([{
                    "trade_date": expected.date(), "open": new_close - 0.1, "high": new_close + 0.2,
                    "low": new_close - 0.2, "close": new_close, "volume": 2000.0,
                }]),
            ],
            ignore_index=True,
        )
        current = compute_event_technical_context(
            event_id="KOL-T-ST",
            symbol="600900",
            posted_at=posted_at,
            qfq_prices=extended,
            expected_trade_date=expected.date(),
            computed_at="2026-07-20T10:01:00+08:00",
        )

        self.assertEqual("pending", stale.status)
        self.assertIn("expected_trade_date_missing", stale.warnings)
        self.assertEqual("complete", current.status)
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.save_event_technical_context(stale.to_record())
            store.save_event_technical_context(current.to_record())
            self.assertEqual(2, len(store.list_event_technical_contexts("KOL-T-ST")))
            self.assertEqual("complete", store.get_event_technical_context("KOL-T-ST")["status"])

    def test_weekend_uses_previous_open_session_with_visible_warning(self) -> None:
        frame = feature_frame()
        friday = frame.iloc[-1]["trade_date"]
        saturday = friday + pd.Timedelta(days=1)
        context = compute_event_technical_context(
            event_id="KOL-T-WEEKEND",
            symbol="600900",
            posted_at=f"{saturday.isoformat()}T16:00:00+08:00",
            qfq_prices=frame,
            expected_trade_date=friday,
        )

        self.assertEqual("complete", context.status)
        self.assertEqual(friday.isoformat(), context.as_of_trade_date)
        self.assertIn("prior_trade_date_used", context.warnings)

    def test_feature_history_boundaries_remain_null_until_defined(self) -> None:
        contexts = {}
        for periods in (14, 15, 20, 21, 34, 35, 59, 60):
            frame = feature_frame(periods)
            day = frame.iloc[-1]["trade_date"]
            contexts[periods] = compute_event_technical_context(
                event_id=f"KOL-T-{periods}",
                symbol="600900",
                posted_at=f"{day.isoformat()}T16:00:00+08:00",
                qfq_prices=frame,
            )

        self.assertIsNotNone(contexts[14].atr14)
        self.assertIsNone(contexts[14].rsi14)
        self.assertIsNotNone(contexts[15].rsi14)
        self.assertIsNone(contexts[20].return_20d)
        self.assertIsNotNone(contexts[21].return_20d)
        self.assertIsNone(contexts[34].macd_hist)
        self.assertIsNotNone(contexts[35].macd_hist)
        self.assertIsNone(contexts[59].distance_60d_high)
        self.assertIsNotNone(contexts[60].distance_60d_high)

    def test_same_stock_multiple_kols_and_event_amendment_keep_independent_history(self) -> None:
        frame = feature_frame()
        day = frame.iloc[-1]["trade_date"]
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            for event_id, hour in (("KOL-A", 16), ("KOL-B", 17)):
                context = compute_event_technical_context(
                    event_id=event_id,
                    symbol="600900",
                    posted_at=f"{day.isoformat()}T{hour}:00:00+08:00",
                    qfq_prices=frame,
                )
                store.save_event_technical_context(context.to_record())
            amended = compute_event_technical_context(
                event_id="KOL-A",
                symbol="600900",
                posted_at=f"{day.isoformat()}T18:00:00+08:00",
                qfq_prices=frame,
            )
            store.save_event_technical_context(amended.to_record())

            self.assertEqual(3, len(store.list_event_technical_contexts()))
            self.assertEqual(2, len(store.list_event_technical_contexts("KOL-A")))
            self.assertIsNotNone(store.get_event_technical_context(
                "KOL-A", input_hash=event_context_input_hash("600900", f"{day.isoformat()}T16:00:00+08:00")
            ))

    def test_duckdb_v3_migration_is_backed_up_and_preserves_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "market"
            root.mkdir(parents=True)
            db_path = root / "market.duckdb"
            db = duckdb.connect(str(db_path))
            db.execute("CREATE TABLE schema_meta(version INTEGER NOT NULL); INSERT INTO schema_meta VALUES (3)")
            db.execute(
                """
                CREATE TABLE event_technical_context(
                    event_id VARCHAR,feature_version VARCHAR,input_hash VARCHAR,symbol VARCHAR,posted_at VARCHAR,
                    as_of_trade_date VARCHAR,adjustment VARCHAR,rsi14 DOUBLE,macd_dif DOUBLE,macd_dea DOUBLE,
                    macd_hist DOUBLE,macd_hist_pct DOUBLE,atr14 DOUBLE,atr14_pct DOUBLE,volume_ratio_5 DOUBLE,
                    return_20d DOUBLE,distance_60d_high DOUBLE,history_bars BIGINT,status VARCHAR,
                    warnings_json VARCHAR,source_hash VARCHAR,computed_at VARCHAR,
                    PRIMARY KEY(event_id,feature_version,input_hash)
                )
                """
            )
            db.execute(
                "INSERT INTO event_technical_context VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ["KOL-OLD", FEATURE_VERSION, "input", "600900", "2026-07-01T16:00:00+08:00",
                 "2026-07-01", "qfq", 50.0, 0.1, 0.08, 0.02, 0.002, 0.5, 0.05, 1.0,
                 0.1, -0.02, 80, "complete", "[]", "source", "2026-07-02T00:00:00+08:00"],
            )
            db.close()

            store = MarketStore(root)
            with store.connect() as migrated:
                version = migrated.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
                columns = {row[1] for row in migrated.execute("PRAGMA table_info('event_technical_context')").fetchall()}
                intraday_tables = migrated.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='event_intraday_context'"
                ).fetchone()[0]

                self.assertEqual(9, version)
            self.assertIn("snapshot_id", columns)
            self.assertEqual(1, intraday_tables)
            self.assertEqual("KOL-OLD", store.list_event_technical_contexts()[0]["event_id"])
            self.assertTrue(any((root / "backups").glob("*_market.duckdb")))

    def test_backfill_processes_formal_events_and_is_idempotent(self) -> None:
        frame = feature_frame()
        posted_date = frame.iloc[-1]["trade_date"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market = MarketStore(root / "market")
            events = KolStore(root / "kol")
            market.write_daily(frame, symbol="600900", adjustment="qfq")
            events.register_event(
                EventRecord(
                    event_id="KOL-T005",
                    kol_name="fixture",
                    platform="X",
                    source_url="https://x.com/fixture/status/5",
                    source_note="post:5",
                    posted_at=f"{posted_date.isoformat()}T16:00:00+08:00",
                    symbol="600900",
                    security_name="fixture stock",
                    direction="long",
                    thesis="fixture thesis",
                    status="active",
                )
            )

            first = _backfill_event_contexts(market, events)
            second = _backfill_event_contexts(market, events)

            self.assertEqual(["KOL-T005"], first["created"])
            self.assertEqual(["KOL-T005"], second["skipped"])
            self.assertEqual([], second["errors"])

    def test_zero_reference_close_never_produces_inf_in_json(self) -> None:
        # A zero close 20 sessions back makes return_20d divide by zero.
        # The result must be None (JSON-safe), never inf ("Infinity").
        import json as json_module

        frame = feature_frame()
        frame = frame.copy()
        cutoff_index = len(frame) - 1
        frame.loc[frame.index[cutoff_index - 20], "close"] = 0.0

        context = compute_event_technical_context(
            event_id="KOL-INF-GUARD",
            symbol="600900",
            posted_at=f"{frame.iloc[-1]['trade_date'].isoformat()}T16:05:00+08:00",
            qfq_prices=frame,
        )

        self.assertIsNone(context.return_20d)
        self.assertIn("insufficient_return_20d", context.warnings)
        serialised = json_module.dumps(context.to_record(), ensure_ascii=False)
        self.assertNotIn("Infinity", serialised)
        self.assertNotIn("-Infinity", serialised)
        # The serialised payload must round-trip as valid JSON.
        json_module.loads(serialised)


if __name__ == "__main__":
    unittest.main()
