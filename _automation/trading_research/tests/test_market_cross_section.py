from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_cross_section import (  # noqa: E402
    build_short_term_leader_lens,
    normalise_cross_section,
    prepare_short_term_leader_universe,
)


def fixture() -> pd.DataFrame:
    rows = []
    dates = pd.bdate_range("2026-06-01", periods=21)
    symbols = [f"600{index:03d}" for index in range(100)]
    for day_index, current in enumerate(dates):
        for symbol_index, symbol in enumerate(symbols):
            close = 10 + symbol_index * 0.02 + day_index * (0.001 + symbol_index * 0.0003)
            pct = 10.0 if symbol == "600099" and day_index >= 19 else 0.2 + symbol_index * 0.01
            rows.append(
                {
                    "trade_date": current.date().isoformat(),
                    "symbol": symbol,
                    "close": close,
                    "preclose": close / (1 + pct / 100),
                    "pct_change_pct": pct,
                    "amount": 1_000_000 + symbol_index * 1_000_000,
                    "float_mv": 10_000_000_000 + symbol_index * 100_000_000,
                    "is_st": False,
                }
            )
    return pd.DataFrame(rows)


class MarketCrossSectionTests(unittest.TestCase):
    def test_normalise_cross_section_does_not_treat_false_text_as_st(self) -> None:
        frame = normalise_cross_section(
            pd.DataFrame(
                [
                    {
                        "trade_date": "2026-07-27",
                        "symbol": "600900",
                        "close": 10,
                        "amount": 100,
                        "is_st": "false",
                    },
                    {
                        "trade_date": "2026-07-27",
                        "symbol": "600901",
                        "close": 10,
                        "amount": 100,
                        "is_st": "1",
                    },
                ]
            )
        )

        self.assertFalse(bool(frame.iloc[0]["is_st"]))
        self.assertTrue(bool(frame.iloc[1]["is_st"]))

    def test_builds_independent_leader_candidate_types(self) -> None:
        snapshots = fixture()
        prepared = prepare_short_term_leader_universe(snapshots)
        result = build_short_term_leader_lens(
            symbol="600099",
            snapshots=snapshots,
            latest_daily={
                "close": 20.0,
                "ma20": 18.0,
                "ma60": 17.0,
                "distance_60d_high": -0.01,
            },
            board_rows=[
                {
                    "board_code": "BK001",
                    "board_name": "示例板块",
                    "rps_50": 95,
                }
            ],
            board_members={"BK001": [f"600{index:03d}" for index in range(90, 100)]},
            prepared_universe=prepared,
        )

        candidates = result["facts"]["candidate_types"]
        self.assertTrue(candidates["limit_up_leader"]["candidate"])
        self.assertTrue(candidates["trend_leader"]["candidate"])
        self.assertTrue(candidates["liquidity_core"]["candidate"])
        self.assertTrue(candidates["board_leader"]["candidate"])
        self.assertEqual("candidate", result["conclusion"])
        self.assertNotIn("score", result)

    def test_missing_history_is_partial_instead_of_zero_filled(self) -> None:
        short = fixture()
        short = short[short["trade_date"].isin(sorted(short["trade_date"].unique())[-3:])]
        result = build_short_term_leader_lens(
            symbol="600050",
            snapshots=short,
            latest_daily={
                "close": 12,
                "ma20": 11,
                "ma60": 10,
                "distance_60d_high": -0.01,
            },
        )

        self.assertEqual("partial", result["status"])
        trend = result["facts"]["candidate_types"]["trend_leader"]
        self.assertIsNone(trend["candidate"])
        self.assertIsNone(trend["return_20d_percentile"])
        self.assertIsNone(
            result["facts"]["candidate_types"]["liquidity_core"]["candidate"]
        )
        self.assertIsNone(
            result["facts"]["candidate_types"]["board_leader"]["candidate"]
        )
        self.assertIn("cross_section_history_less_than_21_sessions", result["warnings"])

    def test_stale_cross_section_does_not_publish_candidate_flags(self) -> None:
        result = build_short_term_leader_lens(
            symbol="600099",
            snapshots=fixture(),
            latest_daily={
                "trade_date": "2026-07-01",
                "close": 20,
                "ma20": 18,
                "ma60": 17,
                "distance_60d_high": -0.01,
            },
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual("not_determined", result["conclusion"])
        self.assertEqual({}, result["facts"]["candidate_types"])
        self.assertIn("cross_section_stale_for_event", result["warnings"])

    def test_st_limit_threshold_is_distinct(self) -> None:
        frame = fixture()
        frame.loc[frame["symbol"] == "600001", "is_st"] = True
        latest_dates = sorted(frame["trade_date"].unique())[-2:]
        frame.loc[
            (frame["symbol"] == "600001") & frame["trade_date"].isin(latest_dates),
            "pct_change_pct",
        ] = 5.0
        result = build_short_term_leader_lens(
            symbol="600001",
            snapshots=frame,
            latest_daily={
                "close": 12,
                "ma20": 11,
                "ma60": 10,
                "distance_60d_high": -0.01,
            },
        )

        self.assertEqual(
            2,
            result["facts"]["candidate_types"]["limit_up_leader"]["limit_streak"],
        )
