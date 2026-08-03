from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_research import METHOD_RESEARCH_VERSION, analyze_event_methods  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")


def daily_fixture() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=90)
    rows = []
    previous = 10.0
    for index, value in enumerate(dates):
        close = 10.0 + index * 0.055 + math.sin(index / 3.5) * 0.55
        rows.append(
            {
                "symbol": "600900",
                "trade_date": value.date().isoformat(),
                "open": previous,
                "high": max(previous, close) + 0.28,
                "low": min(previous, close) - 0.24,
                "close": close,
                "preclose": previous,
                "volume": 1_000_000 + index * 10_000 + (index % 7) * 80_000,
                "amount": close * 1_000_000,
                "adjustment": "qfq",
                "provider": "fixture",
            }
        )
        previous = close
    return pd.DataFrame(rows)


def minute_fixture(trade_date: str) -> pd.DataFrame:
    morning = pd.date_range(f"{trade_date} 09:30:00", periods=120, freq="min")
    afternoon = pd.date_range(f"{trade_date} 13:00:00", periods=120, freq="min")
    rows = []
    previous = 14.0
    for index, value in enumerate([*morning, *afternoon]):
        close = 14.0 + index * 0.0015 + math.sin(index / 13) * 0.08
        rows.append(
            {
                "symbol": "600900",
                "trade_datetime": value,
                "open": previous,
                "high": max(previous, close) + 0.025,
                "low": min(previous, close) - 0.025,
                "close": close,
                "volume": 10_000 + (index % 12) * 900,
                "amount": close * (10_000 + (index % 12) * 900),
                "adjustment": "raw",
                "provider": "fixture",
            }
        )
        previous = close
    return pd.DataFrame(rows)


class EventResearchTests(unittest.TestCase):
    def test_builds_five_evidence_lenses_without_future_data(self) -> None:
        daily = daily_fixture()
        event_date = str(daily.iloc[79]["trade_date"])
        posted_at = datetime.combine(
            pd.Timestamp(event_date).date(),
            time(16, 5),
            tzinfo=SHANGHAI,
        ).isoformat()

        result = analyze_event_methods(
            event_id="KOL-TEST",
            symbol="600900",
            posted_at=posted_at,
            qfq_daily=daily,
            minute_bars=minute_fixture(event_date),
            board_rows=[
                {
                    "board_code": "BK0001",
                    "board_name": "示例行业",
                    "rps_50": 92.0,
                    "rps_120": 84.0,
                    "rps_250": 77.0,
                    "status": "mainline_candidate",
                }
            ],
            computed_at="2026-07-29T09:00:00+08:00",
        )

        self.assertEqual(METHOD_RESEARCH_VERSION, result["version"])
        self.assertEqual(event_date, result["as_of_trade_date"])
        self.assertEqual(80, result["data_lineage"]["daily_bars"])
        self.assertEqual(
            {
                "short_term_leader",
                "dow_wave_gann",
                "price_action",
                "ict",
                "wyckoff_orderflow",
            },
            set(result["lenses"]),
        )
        self.assertEqual(
            "not_determined",
            result["lenses"]["short_term_leader"]["conclusion"],
        )
        self.assertIn(
            "full_market_cross_section_unavailable",
            result["lenses"]["short_term_leader"]["warnings"],
        )

        dow = result["lenses"]["dow_wave_gann"]
        self.assertIsNotNone(dow["facts"]["atr14_pct"])
        self.assertIsNotNone(dow["facts"]["ma20"])
        self.assertGreaterEqual(len(dow["facts"]["fib_levels"]), 5)
        self.assertFalse(dow["facts"]["wave_count"]["available"])
        self.assertFalse(dow["facts"]["gann_geometry"]["available"])

        orderflow = result["lenses"]["wyckoff_orderflow"]
        self.assertEqual("bar_approximation", orderflow["facts"]["volume_profile"]["method"])
        self.assertIsNotNone(orderflow["facts"]["vwap"]["session_vwap"])
        self.assertIsNotNone(orderflow["facts"]["tpo"]["poc"])
        self.assertFalse(orderflow["facts"]["cvd"]["available"])
        self.assertFalse(orderflow["facts"]["option_wall"]["available"])
        self.assertNotIn("score", result)
        self.assertNotIn("signal", result)

    def test_missing_minute_data_is_explicit_and_never_fabricates_order_flow(self) -> None:
        daily = daily_fixture()
        event_date = str(daily.iloc[70]["trade_date"])
        posted_at = f"{event_date}T16:05:00+08:00"

        result = analyze_event_methods(
            event_id="KOL-NO-MINUTE",
            symbol="600900",
            posted_at=posted_at,
            qfq_daily=daily,
            minute_bars=pd.DataFrame(),
        )

        orderflow = result["lenses"]["wyckoff_orderflow"]
        self.assertEqual("partial", orderflow["status"])
        self.assertIn("minute_session_unavailable", orderflow["warnings"])
        self.assertIsNone(orderflow["facts"]["vwap"]["session_vwap"])
        self.assertIsNone(orderflow["facts"]["volume_profile"]["poc"])
        self.assertFalse(orderflow["facts"]["cvd"]["available"])
        self.assertEqual(
            "requires aggressor-side trade data",
            orderflow["facts"]["cvd"]["reason"],
        )

    def test_premarket_event_uses_only_the_previous_complete_session(self) -> None:
        daily = daily_fixture()
        event_day = pd.Timestamp(daily.iloc[80]["trade_date"])
        posted_at = f"{event_day.date().isoformat()}T08:30:00+08:00"
        previous_date = str(daily.iloc[79]["trade_date"])
        minute = pd.concat(
            [
                minute_fixture(previous_date),
                minute_fixture(event_day.date().isoformat()),
            ],
            ignore_index=True,
        )

        result = analyze_event_methods(
            event_id="KOL-PREMARKET",
            symbol="600900",
            posted_at=posted_at,
            qfq_daily=daily,
            minute_bars=minute,
        )

        self.assertEqual(previous_date, result["as_of_trade_date"])
        self.assertEqual(previous_date, result["data_lineage"]["minute_session_date"])
        self.assertEqual(240, result["data_lineage"]["minute_bars"])

    def test_intraday_event_excludes_minutes_after_the_post(self) -> None:
        daily = daily_fixture()
        event_day = str(daily.iloc[80]["trade_date"])
        previous_date = str(daily.iloc[79]["trade_date"])
        minute = pd.concat(
            [minute_fixture(previous_date), minute_fixture(event_day)],
            ignore_index=True,
        )

        result = analyze_event_methods(
            event_id="KOL-INTRADAY",
            symbol="600900",
            posted_at=f"{event_day}T10:00:00+08:00",
            qfq_daily=daily,
            minute_bars=minute,
        )

        self.assertEqual(previous_date, result["as_of_trade_date"])
        self.assertEqual(event_day, result["data_lineage"]["minute_session_date"])
        self.assertEqual("session_to_post", result["data_lineage"]["minute_session_mode"])
        self.assertEqual(31, result["data_lineage"]["minute_bars"])
        self.assertLessEqual(
            result["lenses"]["wyckoff_orderflow"]["facts"]["tpo"]["initial_balance_high"],
            minute[minute["trade_datetime"] <= f"{event_day} 10:00:00"]["high"].max(),
        )

    def test_stock_option_wall_is_not_applicable_without_fabrication(self) -> None:
        daily = daily_fixture()
        event_date = str(daily.iloc[70]["trade_date"])
        result = analyze_event_methods(
            event_id="KOL-STOCK-OPTIONS",
            symbol="600900",
            posted_at=f"{event_date}T16:05:00+08:00",
            qfq_daily=daily,
        )

        option_wall = result["lenses"]["wyckoff_orderflow"]["facts"]["option_wall"]
        self.assertFalse(option_wall["available"])
        self.assertEqual("not_applicable", option_wall["status"])


if __name__ == "__main__":
    unittest.main()
