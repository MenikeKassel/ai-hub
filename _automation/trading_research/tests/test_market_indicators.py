from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_indicators import INDICATOR_VERSION, compute_daily_indicators  # noqa: E402


def price_frame(periods: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=periods)
    closes = pd.Series([10.0 + index * 0.1 for index in range(periods)])
    return pd.DataFrame(
        {
            "symbol": "600900",
            "trade_date": dates.date,
            "open": closes - 0.05,
            "high": closes + 0.20,
            "low": closes - 0.20,
            "close": closes,
            "volume": [1000.0 + index * 10.0 for index in range(periods)],
            "amount": [10000.0 + index * 100.0 for index in range(periods)],
            "trade_status": ["1"] * periods,
            "adjustment": ["qfq"] * periods,
            "provider": ["fixture"] * periods,
        }
    )


class DailyIndicatorTests(unittest.TestCase):
    def test_computes_moving_averages_macd_rsi_and_atr_without_future_data(self) -> None:
        frame = price_frame()
        result = compute_daily_indicators(frame)
        latest = result.iloc[-1]

        self.assertEqual(INDICATOR_VERSION, latest["indicator_version"])
        self.assertAlmostEqual(frame.iloc[-5:]["close"].mean(), latest["ma5"])
        self.assertAlmostEqual(frame.iloc[-60:]["close"].mean(), latest["ma60"])
        self.assertAlmostEqual(100.0, latest["rsi14"])
        self.assertIsNotNone(latest["macd_dif"])
        self.assertIsNotNone(latest["macd_dea"])
        self.assertIsNotNone(latest["macd_hist"])
        self.assertGreater(latest["atr14_pct"], 0)

        changed = frame.copy()
        changed.loc[changed.index[-1], "close"] = 999.0
        replayed = compute_daily_indicators(changed)
        self.assertAlmostEqual(result.iloc[-2]["ma20"], replayed.iloc[-2]["ma20"])
        self.assertAlmostEqual(result.iloc[-2]["rsi14"], replayed.iloc[-2]["rsi14"])

    def test_short_history_uses_nulls_instead_of_zero_fill(self) -> None:
        result = compute_daily_indicators(price_frame(10))
        latest = result.iloc[-1]

        self.assertTrue(pd.isna(latest["ma20"]))
        self.assertTrue(pd.isna(latest["ma60"]))
        self.assertTrue(pd.isna(latest["rsi14"]))
        self.assertTrue(pd.isna(latest["macd_hist"]))
        self.assertTrue(pd.isna(latest["atr14_pct"]))
        self.assertNotEqual(0, latest["ma5"])

    def test_duplicate_dates_are_deterministically_replaced_by_latest_row(self) -> None:
        frame = price_frame(20)
        duplicate = frame.iloc[[-1]].copy()
        duplicate["close"] = 88.0
        result = compute_daily_indicators(pd.concat([frame, duplicate], ignore_index=True))

        self.assertEqual(20, len(result))
        self.assertEqual(88.0, result.iloc[-1]["close"])


if __name__ == "__main__":
    unittest.main()
