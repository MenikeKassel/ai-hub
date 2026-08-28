from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_data import (  # noqa: E402
    BaoStockMarketProvider,
    FreeStockDBMarketProvider,
    Instrument,
    MarketStore,
    _classify_baostock_instrument,
    audit_daily_bars,
    normalise_daily_bars,
    sync_daily_bars,
)
from trading_cli import _completed_market_sync_date, _drain_market_queue, _sync_with_fallback  # noqa: E402


def daily_frame(*, invalid: bool = False) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-07-10",
                "open": "10.00",
                "high": "10.50",
                "low": "9.80",
                "close": "10.20",
                "preclose": "9.90",
                "volume": "100000",
                "amount": "1020000",
                "turn": "1.2",
                "tradestatus": "1",
            },
            {
                "date": "2026-07-13",
                "open": "10.20",
                "high": "9.90" if invalid else "10.80",
                "low": "10.00",
                "close": "10.60",
                "preclose": "10.20",
                "volume": "120000",
                "amount": "1260000",
                "turn": "1.4",
                "tradestatus": "1",
            },
        ]
    )


class FixtureProvider:
    name = "fixture"

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def fetch_daily(self, symbol, instrument_type, start, end, adjustment):
        return self.frame.copy()


class FailingProvider:
    name = "failing"

    def fetch_daily(self, symbol, instrument_type, start, end, adjustment):
        raise RuntimeError("fixture provider unavailable")


class MarketDataTests(unittest.TestCase):
    def test_freestockdb_provider_reuses_persistent_http_client(self) -> None:
        class Response:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json():
                return []

        class Client:
            def __init__(self):
                self.calls = []
                self.closed = False

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

            def close(self):
                self.closed = True

        client = Client()
        provider = FreeStockDBMarketProvider(client=client)
        provider._request_json({"cmd": "first"})
        provider._request_json({"cmd": "second"})
        provider.close()

        self.assertEqual(2, len(client.calls))
        self.assertTrue(all(call[0] == "http://127.0.0.1:7899/" for call in client.calls))
        self.assertFalse(client.closed, "injected clients remain owned by their caller")

    def test_freestockdb_daily_adapter_normalizes_http_payload(self) -> None:
        provider = FreeStockDBMarketProvider(base_url="http://127.0.0.1:7899")
        provider._request_json = lambda params: [
            {
                "date": 20260710,
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "pre_close": 9.9,
                "volume": 100,
                "amount": 1000,
            },
            {
                "date": 20260713,
                "open": 10.2,
                "high": 10.8,
                "low": 10.0,
                "close": 10.6,
                "pre_close": 10.2,
                "volume": 120,
                "amount": 1260,
            },
        ]

        frame = provider.fetch_daily(
            "600900", "stock", date(2026, 7, 10), date(2026, 7, 13), "raw"
        )

        self.assertEqual(["2026-07-10", "2026-07-13"], frame["date"].tolist())
        self.assertEqual([10.2, 10.6], frame["close"].tolist())
        self.assertEqual([9.9, 10.2], frame["preclose"].tolist())

    def test_freestockdb_daily_adapter_applies_qfq_factors(self) -> None:
        provider = FreeStockDBMarketProvider()
        responses = iter(
            [
                [{"date": 20260710, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1}],
                [
                    ["复权:600900:20260710", {"cum": 1}],
                    ["复权:600900:20260713", {"cum": 2}],
                ],
            ]
        )
        provider._request_json = lambda params: next(responses)

        frame = provider.fetch_daily(
            "600900", "stock", date(2026, 7, 10), date(2026, 7, 13), "qfq"
        )

        self.assertEqual(5.0, frame.iloc[0]["close"])

    def test_freestockdb_minute_adapter_aggregates_to_requested_frequency(self) -> None:
        provider = FreeStockDBMarketProvider()
        provider._request_json = lambda params: [
            {"date": 20260710100000, "open": 10, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 10, "amount": 100},
            {"date": 20260710100100, "open": 10.1, "high": 10.3, "low": 10, "close": 10.2, "volume": 12, "amount": 120},
        ]

        frame = provider.fetch_minute(
            "600900", "stock", date(2026, 7, 10), date(2026, 7, 10), frequency="5m", adjustment="raw"
        )

        self.assertEqual(1, len(frame))
        self.assertEqual(10.0, frame.iloc[0]["open"])
        self.assertEqual(10.2, frame.iloc[0]["close"])
        self.assertEqual(22, frame.iloc[0]["volume"])

    def test_freestockdb_cross_section_adapter_keeps_vendor_fields(self) -> None:
        provider = FreeStockDBMarketProvider()
        captured = {}

        def request(params):
            captured.update(params)
            return [
                {
                    "date": 20260727,
                    "code": "600900",
                    "name": "长江电力",
                    "close": 30.5,
                    "pre_close": 30.0,
                    "amount": 123456789,
                    "pct_chg": 1.67,
                    "is_st": False,
                    "float_mv": 700000000000,
                }
            ]

        provider._request_json = request
        frame = provider.fetch_daily_cross_section(date(2026, 7, 27))

        self.assertEqual("vals", captured["cmd"])
        self.assertEqual("all:", captured["k1"])
        self.assertEqual("key:20260727", captured["k2"])
        self.assertEqual("600900", frame.iloc[0]["symbol"])
        self.assertEqual("2026-07-27", frame.iloc[0]["trade_date"])
        self.assertEqual(1.67, frame.iloc[0]["pct_change_pct"])

    def test_minute_snapshot_keeps_frequency_and_adjustment_in_warehouse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            frame = pd.DataFrame(
                {
                    "trade_datetime": [datetime(2026, 7, 10, 10, 0)],
                    "symbol": ["600900"],
                    "close": [10.0],
                }
            )
            result = store.save_minute_snapshot(
                provider="freestockdb",
                symbol="600900",
                as_of=date(2026, 7, 10),
                frequency="5m",
                adjustment="qfq",
                frame=frame,
            )

            self.assertEqual("qfq", result.adjustment)
            self.assertTrue(Path(result.normalized_paths[0]).exists())
            coverage = store.get_coverage("600900")
            self.assertEqual("minute_5m", coverage[0]["dataset"])
            self.assertEqual("qfq", coverage[0]["adjustment"])

    def test_cross_section_and_method_snapshots_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            cross_section = pd.DataFrame(
                [
                    {
                        "trade_date": "2026-07-27",
                        "symbol": "600900",
                        "close": 30.5,
                        "preclose": 30.0,
                        "amount": 1000000,
                        "pct_change_pct": 1.67,
                        "is_st": False,
                    },
                    {
                        "trade_date": "2026-07-27",
                        "symbol": "510300",
                        "name": "沪深300ETF",
                        "close": 4.0,
                        "preclose": 4.0,
                        "amount": 500000,
                        "pct_change_pct": 0.0,
                        "is_st": False,
                    },
                ]
            )
            saved = store.save_cross_section_snapshot(
                provider="fixture",
                as_of=date(2026, 7, 27),
                frame=cross_section,
            )
            self.assertEqual(1, saved["row_count"])
            self.assertEqual(2, saved["raw_row_count"])
            with gzip.open(saved["raw_path"], "rt", encoding="utf-8") as stream:
                self.assertIn("510300", stream.read())
            self.assertEqual(
                ["600900"],
                store.read_cross_sections([date(2026, 7, 27)])["symbol"].tolist(),
            )
            replacement = cross_section.copy()
            replacement.loc[
                replacement["symbol"] == "600900",
                "close",
            ] = 31.0
            revised = store.save_cross_section_snapshot(
                provider="fixture",
                as_of=date(2026, 7, 27),
                frame=replacement,
            )
            self.assertNotEqual(saved["parquet_path"], revised["parquet_path"])
            self.assertTrue(Path(saved["parquet_path"]).exists())
            self.assertTrue(Path(revised["parquet_path"]).exists())
            self.assertEqual(
                31.0,
                float(
                    store.read_cross_sections([date(2026, 7, 27)]).iloc[0][
                        "close"
                    ]
                ),
            )

            record = {
                "snapshot_id": "research-1",
                "event_id": "KOL-1",
                "method_version": "v1",
                "input_hash": "input",
                "symbol": "600900",
                "posted_at": "2026-07-27T16:00:00+08:00",
                "as_of_trade_date": "2026-07-27",
                "status": "partial",
                "payload": {"lenses": {}},
                "warnings": ["fixture"],
                "computed_at": "2026-07-28T08:00:00+08:00",
            }
            self.assertTrue(store.save_event_method_research(record))
            self.assertFalse(store.save_event_method_research(record))
            self.assertEqual(
                {"lenses": {}},
                store.get_event_method_research("KOL-1")["payload"],
            )

            interpretation = {
                "interpretation_id": "interpretation-1",
                "event_id": "KOL-1",
                "research_snapshot_id": "research-1",
                "provider": "fixture",
                "model": "fixture",
                "prompt_version": "v1",
                "input_hash": "interpret-input",
                "status": "ready",
                "payload": {"interpretations": []},
                "validation": {"ok": True},
                "error": "",
                "created_at": "2026-07-28T08:01:00+08:00",
            }
            self.assertTrue(store.save_event_method_interpretation(interpretation))
            self.assertFalse(store.save_event_method_interpretation(interpretation))
            self.assertEqual(
                "ready",
                store.list_event_method_interpretations("KOL-1")[0]["status"],
            )

    def test_cross_section_rejects_a_sharply_incomplete_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            rows = [
                {
                    "trade_date": "2026-07-27",
                    "symbol": f"600{index:03d}",
                    "close": 10 + index,
                    "amount": 1000000 + index,
                    "is_st": False,
                }
                for index in range(10)
            ]
            accepted = store.save_cross_section_snapshot(
                provider="fixture",
                as_of=date(2026, 7, 27),
                frame=pd.DataFrame(rows),
            )
            degraded = store.save_cross_section_snapshot(
                provider="fixture",
                as_of=date(2026, 7, 27),
                frame=pd.DataFrame(rows[:1]),
            )

            self.assertEqual("ready", accepted["status"])
            self.assertEqual("quarantined", degraded["status"])
            self.assertEqual(
                10,
                len(store.read_cross_sections([date(2026, 7, 27)])),
            )

    def test_market_fallback_promotes_freestockdb_after_primary_sources_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(Instrument("600900", "fixture", "stock", "SH"))
            with patch("trading_cli.BaoStockMarketProvider", return_value=FailingProvider()), patch(
                "trading_cli.AKShareMarketProvider", return_value=FailingProvider()
            ), patch("trading_cli.FreeStockDBMarketProvider", return_value=FixtureProvider(daily_frame())):
                results = _sync_with_fallback(
                    store, "600900", date(2026, 7, 10), date(2026, 7, 13), "raw"
                )

            self.assertEqual(["failing", "failing", "fixture"], [item["provider"] for item in results])
            self.assertEqual("valid", results[-1]["quality_status"])
            self.assertEqual("2026-07-13", store.get_coverage("600900")[0]["end_date"])

    def test_baostock_classification_keeps_exchange_code_collisions_distinct(self) -> None:
        self.assertEqual(
            "stock",
            _classify_baostock_instrument("SZ", "000688", "国城矿业", "1"),
        )
        self.assertEqual(
            "index",
            _classify_baostock_instrument("SH", "000688", "科创50", "2"),
        )

    def test_market_sync_clamps_requested_date_to_last_completed_session(self) -> None:
        class Store:
            @staticmethod
            def health():
                return {"latest_open_date": "2026-07-22"}

        self.assertEqual(
            date(2026, 7, 22),
            _completed_market_sync_date(Store(), date(2026, 7, 23)),
        )
        self.assertEqual(
            date(2026, 7, 20),
            _completed_market_sync_date(Store(), date(2026, 7, 20)),
        )

    def test_normalization_and_quality_checks_reject_incoherent_ohlc(self) -> None:
        valid = normalise_daily_bars(
            daily_frame(), symbol="600900", instrument_type="stock", provider="fixture", adjustment="raw"
        )
        invalid = normalise_daily_bars(
            daily_frame(invalid=True), symbol="600900", instrument_type="stock", provider="fixture", adjustment="raw"
        )

        self.assertEqual("2026-07-10", valid.iloc[0]["trade_date"].isoformat())
        self.assertEqual("valid", audit_daily_bars(valid).status)
        invalid_audit = audit_daily_bars(invalid)
        self.assertEqual("quarantined", invalid_audit.status)
        self.assertIn("ohlc_inconsistent", {issue.code for issue in invalid_audit.issues})

    def test_sync_writes_immutable_raw_parquet_manifest_and_duckdb_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("600900", "长江电力", "stock", "SH", lifecycle="pinned", source="fixture")
            )

            result = sync_daily_bars(
                store,
                FixtureProvider(daily_frame()),
                "600900",
                date(2026, 7, 10),
                date(2026, 7, 13),
                adjustment="raw",
            )
            coverage = store.get_coverage("600900")
            manifests = [json.loads(line) for line in store.manifest_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual("valid", result.quality_status)
            self.assertTrue(Path(result.raw_path).exists())
            self.assertTrue(Path(result.normalized_paths[0]).exists())
            self.assertEqual("2026-07-13", coverage[0]["end_date"])
            self.assertEqual(result.run_id, manifests[-1]["run_id"])
            self.assertEqual(2, len(store.read_daily("600900", adjustment="raw")))

    def test_write_daily_handles_empty_and_invalid_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")

            # Empty frame: silent no-op (previously raised KeyError and
            # crashed the whole market sync).
            self.assertEqual([], store.write_daily(pd.DataFrame(), symbol="600900", adjustment="qfq"))

            # Missing the grouping column: clear ValueError instead of KeyError.
            with self.assertRaises(ValueError):
                store.write_daily(
                    pd.DataFrame({"close": [1.0]}),
                    symbol="600900",
                    adjustment="qfq",
                )

            # A valid frame still round-trips.
            frame = daily_frame().rename(columns={"date": "trade_date"})
            paths = store.write_daily(frame, symbol="600900", adjustment="qfq")
            self.assertEqual(1, len(paths))
            self.assertEqual(len(frame), len(store.read_daily("600900", adjustment="qfq")))

    def test_sync_drops_provider_rows_outside_requested_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("920367", "fixture", "stock", "BJ", lifecycle="tracking")
            )
            frame = daily_frame()
            frame.loc[0, "date"] = "2026-07-21"
            frame.loc[1, "date"] = "2026-07-23"

            result = sync_daily_bars(
                store,
                FixtureProvider(frame),
                "920367",
                date(2026, 7, 21),
                date(2026, 7, 22),
                adjustment="raw",
            )
            stored = store.read_daily("920367", adjustment="raw")

            self.assertEqual(1, result.rows)
            self.assertEqual([date(2026, 7, 21)], stored["trade_date"].tolist())

    def test_health_exposes_provider_lag_against_latest_open_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("600900", "长江电力", "stock", "SH", lifecycle="pinned")
            )
            store.replace_calendar([date.today()], provider="fixture")
            sync_daily_bars(
                store,
                FixtureProvider(daily_frame()),
                "600900",
                date(2026, 7, 10),
                date.today(),
                adjustment="raw",
            )

            health = store.health(
                as_of=datetime.combine(
                    date.today(), datetime.max.time(), ZoneInfo("Asia/Shanghai")
                )
            )

            self.assertEqual("provider_pending", health["daily_data_status"])
            self.assertEqual(["600900"], health["lagging_symbols"])
            self.assertEqual(date.today().isoformat(), health["latest_open_date"])
            self.assertEqual("2026-07-13", health["latest_daily_date"])

    def test_health_does_not_require_an_unfinished_trading_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("600900", "fixture", "stock", "SH", lifecycle="pinned")
            )
            previous_day = date(2026, 7, 22)
            trading_day = date(2026, 7, 23)
            store.replace_calendar([previous_day, trading_day], provider="fixture")
            frame = daily_frame()
            frame.loc[1, "date"] = previous_day.isoformat()
            sync_daily_bars(
                store,
                FixtureProvider(frame),
                "600900",
                date(2026, 7, 10),
                previous_day,
                adjustment="raw",
            )

            health = store.health(
                as_of=datetime(2026, 7, 23, 9, 44, tzinfo=ZoneInfo("Asia/Shanghai"))
            )

            self.assertEqual("trading", health["market_session_status"])
            self.assertEqual(previous_day.isoformat(), health["latest_open_date"])
            self.assertEqual("current", health["daily_data_status"])
            self.assertEqual([], health["lagging_symbols"])

    def test_quarantined_batch_does_not_replace_last_good_normalized_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(Instrument("159139", "科创创业人工智能ETF", "etf", "SZ"))
            good = sync_daily_bars(
                store,
                FixtureProvider(daily_frame()),
                "159139",
                date(2026, 7, 10),
                date(2026, 7, 13),
                adjustment="raw",
            )
            bad = sync_daily_bars(
                store,
                FixtureProvider(daily_frame(invalid=True)),
                "159139",
                date(2026, 7, 10),
                date(2026, 7, 13),
                adjustment="raw",
            )

            self.assertEqual("valid", good.quality_status)
            self.assertEqual("quarantined", bad.quality_status)
            self.assertEqual(2, len(store.read_daily("159139", adjustment="raw")))
            self.assertTrue(Path(bad.raw_path).exists())
            self.assertEqual("valid", store.get_coverage("159139")[0]["quality_status"])

    def test_tracking_lifecycle_archives_after_120_open_sessions_and_can_reactivate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            start = date(2026, 1, 2)
            open_dates = [start + timedelta(days=index) for index in range(170)]
            store.replace_calendar(open_dates, provider="fixture")
            store.upsert_instrument(
                Instrument(
                    "002414",
                    "高德红外",
                    "stock",
                    "SZ",
                    lifecycle="tracking",
                    last_mentioned_at=start.isoformat(),
                )
            )

            archived = store.refresh_lifecycles(as_of=open_dates[-1], tracking_sessions=120)
            store.touch_mention("002414", open_dates[-1])

            self.assertEqual(["002414"], archived)
            self.assertEqual("tracking", store.get_instrument("002414")["lifecycle"])

    def test_master_refresh_does_not_downgrade_user_lifecycle_or_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("159139", "人工智能ETF", "etf", "SZ", lifecycle="pinned", source="holding")
            )
            store.upsert_instrument(
                Instrument("601991", "大唐发电", "stock", "SH", lifecycle="tracking", source="kol_lead")
            )

            store.upsert_instruments(
                [
                    Instrument("159139", "人工智能ETF", "etf", "SZ", lifecycle="archived", source="akshare_master"),
                    Instrument("601991", "大唐发电", "stock", "SH", lifecycle="archived", source="baostock_master"),
                ]
            )

            self.assertEqual("pinned", store.get_instrument("159139")["lifecycle"])
            self.assertEqual("holding", store.get_instrument("159139")["source"])
            self.assertEqual("tracking", store.get_instrument("601991")["lifecycle"])
            self.assertEqual("kol_lead", store.get_instrument("601991")["source"])

    def test_fallback_snapshot_does_not_replace_existing_passing_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(Instrument("600900", "长江电力", "stock", "SH"))
            sync_daily_bars(
                store, FixtureProvider(daily_frame()), "600900",
                date(2026, 7, 10), date(2026, 7, 13), adjustment="raw",
            )
            fallback_frame = daily_frame().copy()
            for column in ("open", "high", "low", "close", "preclose"):
                fallback_frame[column] = fallback_frame[column].astype(float) - 1

            fallback = sync_daily_bars(
                store, FixtureProvider(fallback_frame), "600900",
                date(2026, 7, 10), date(2026, 7, 13), adjustment="raw", promote=False,
            )

            self.assertEqual([], fallback.normalized_paths)
            self.assertEqual([10.2, 10.6], store.read_daily("600900", adjustment="raw")["close"].tolist())
            self.assertIn("fallback_not_promoted", {issue["code"] for issue in store.quality_issues()})

    def test_archived_instrument_is_removed_from_pending_sync_without_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("600900", "长江电力", "stock", "SH", lifecycle="archived")
            )
            store.enqueue_sync("600900", reason="stale_queue")

            with patch("trading_cli._sync_with_fallback") as fetch:
                result = _drain_market_queue(store, as_of=date(2026, 7, 14))

            fetch.assert_not_called()
            self.assertEqual(1, result["skipped"])
            self.assertEqual([], store.pending_sync())

    def test_unpromoted_fallback_keeps_sync_failed_and_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("600900", "长江电力", "stock", "SH", lifecycle="tracking")
            )
            store.enqueue_sync("600900", reason="daily_sync")
            attempt = {
                "quality_status": "warning",
                "normalized_paths": [],
                "provider": "akshare",
            }

            with patch("trading_cli._sync_with_fallback", return_value=[attempt]):
                result = _drain_market_queue(store, as_of=date(2026, 7, 14))

            self.assertEqual(1, result["failed"])
            self.assertEqual("failed", store.pending_sync()[0]["status"])

    def test_manual_backfill_can_extend_coverage_backwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            store.upsert_instrument(
                Instrument("600900", "长江电力", "stock", "SH", lifecycle="tracking")
            )
            sync_daily_bars(
                store,
                FixtureProvider(daily_frame()),
                "600900",
                date(2026, 7, 10),
                date(2026, 7, 13),
                adjustment="raw",
            )
            store.enqueue_sync(
                "600900",
                start=date(2020, 1, 1),
                end=date(2020, 12, 31),
                reason="manual_backfill",
            )
            attempt = {"quality_status": "valid", "normalized_paths": ["fixture.parquet"]}

            with patch("trading_cli._sync_with_fallback", return_value=[attempt]) as fetch:
                result = _drain_market_queue(store, as_of=date(2020, 12, 31))

            self.assertEqual(1, result["completed"])
            self.assertEqual(date(2020, 1, 1), fetch.call_args_list[0].args[2])

    def test_baostock_routes_beijing_symbols_to_bj_exchange(self) -> None:
        self.assertEqual("bj.430047", BaoStockMarketProvider.provider_code("430047", "stock"))
        self.assertEqual("bj.920001", BaoStockMarketProvider.provider_code("920001", "stock"))

    def test_instrument_master_snapshot_has_raw_parquet_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            instruments = [
                Instrument("600900", "长江电力", "stock", "SH", source="fixture_master"),
                Instrument("399006", "创业板指", "index", "SZ", source="fixture_master"),
            ]

            result = store.save_instrument_snapshot("fixture", instruments, date(2026, 7, 14))

            self.assertTrue(Path(result.raw_path).exists())
            self.assertTrue(Path(result.normalized_paths[0]).exists())
            self.assertEqual(2, result.rows)
            saved = pd.read_parquet(result.normalized_paths[0])
            self.assertEqual(["399006", "600900"], sorted(saved["symbol"].tolist()))
            self.assertIn('"dataset": "instrument_master"', store.manifest_path.read_text(encoding="utf-8"))

    def test_exchange_qualified_catalog_preserves_stock_index_code_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            contracts = [
                Instrument("000001", "平安银行", "stock", "SZ", lifecycle="archived"),
                Instrument("000001", "上证指数", "index", "SH", lifecycle="archived"),
            ]

            snapshot = store.save_instrument_snapshot("fixture", contracts, date(2026, 7, 14))
            store.upsert_instrument_catalog(contracts, provider="fixture", snapshot_date=date(2026, 7, 14))
            store.upsert_instruments(contracts)

            self.assertEqual("valid", snapshot.quality_status)
            self.assertEqual(2, store.instrument_catalog_count())
            self.assertEqual("stock", store.get_instrument("000001")["instrument_type"])


if __name__ == "__main__":
    unittest.main()
