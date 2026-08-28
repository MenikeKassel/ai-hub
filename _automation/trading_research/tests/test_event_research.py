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

    def test_zero_close_pct_change_inf_never_becomes_a_limit_streak(self) -> None:
        # A previous close of zero produces inf in pct_change; inf must not be
        # counted as a limit move (it previously inflated limit_move_streak).
        daily = daily_fixture()
        daily = daily.copy()
        daily.loc[daily.index[60], "close"] = 0.0
        event_date = str(daily.iloc[70]["trade_date"])

        result = analyze_event_methods(
            event_id="KOL-ZERO-CLOSE",
            symbol="600900",
            posted_at=f"{event_date}T16:05:00+08:00",
            qfq_daily=daily,
        )

        streak = result["lenses"]["short_term_leader"]["facts"]["limit_move_streak_local_approx"]
        self.assertIsInstance(streak, int)
        self.assertGreaterEqual(streak, 0)
        # The fixture does not contain real limit moves, so the streak must
        # stay small and never explode to inf-driven values.
        self.assertLessEqual(streak, 2)

    def test_daily_indicators_normalise_inf_to_nan(self) -> None:
        from market_indicators import compute_daily_indicators

        daily = daily_fixture()
        # Inject a zero close so return_20d and atr14_pct divide by zero.
        daily = daily.copy()
        daily.loc[daily.index[30], "close"] = 0.0
        indicators = compute_daily_indicators(daily)

        for column in (
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "rsi14",
            "atr14",
            "atr14_pct",
            "volume_ratio_5",
            "return_20d",
            "distance_60d_high",
        ):
            # Leading rows are legitimately NaN (rolling warm-up); the bug we
            # guard against is inf/-inf leaking into downstream consumers.
            self.assertFalse(
                indicators[column].isin([float("inf"), float("-inf")]).any(),
                f"{column} contains inf/-inf",
            )
        self.assertFalse(
            indicators[["atr14_pct", "return_20d"]].isin([float("inf"), float("-inf")]).any().any()
        )

    def test_numeric_is_st_flag_is_recognised(self) -> None:
        from market_cross_section import normalise_cross_section

        frame = pd.DataFrame(
            {
                "trade_date": ["2026-08-05", "2026-08-05"],
                "symbol": ["000001", "000002"],
                "close": [10.0, 12.0],
                "amount": [1e8, 2e8],
                "preclose": [9.5, 11.5],
                "is_st": [1.0, 0.0],
            }
        )
        normalised = normalise_cross_section(frame)
        self.assertTrue(bool(normalised.set_index("symbol").loc["000001", "is_st"]))
        self.assertFalse(bool(normalised.set_index("symbol").loc["000002", "is_st"]))

    def test_pct_change_inf_is_normalised_to_nan(self) -> None:
        from market_cross_section import normalise_cross_section

        frame = pd.DataFrame(
            {
                "trade_date": ["2026-08-05"],
                "symbol": ["000001"],
                "close": [10.0],
                "amount": [1e8],
                "preclose": [0.0],
            }
        )
        normalised = normalise_cross_section(frame)
        self.assertTrue(
            pd.isna(normalised.iloc[0]["pct_change"]),
            "zero preclose must not produce inf pct_change",
        )


if __name__ == "__main__":
    unittest.main()
