from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from board_mainline import (  # noqa: E402
    BoardMainlineStore,
    EastmoneyBoardProvider,
    board_key,
    sync_board_snapshot,
)
from market_data import MarketStore  # noqa: E402


def seed_boards(
    store: BoardMainlineStore,
    *,
    board_type: str,
    count: int,
    latest: date = date(2026, 7, 24),
) -> list[str]:
    dates = pd.bdate_range(end=latest, periods=270)
    keys = [board_key(board_type, f"BK{index:04d}") for index in range(1, count + 1)]
    timestamp = "2026-07-24T09:00:00+08:00"
    catalog_rows = []
    daily_rows = []
    for index, key in enumerate(keys, start=1):
        code = key.split(":", 1)[1]
        catalog_rows.append(
            [key, code, f"{board_type}-{index}", board_type, "active", "fixture", timestamp, timestamp, timestamp]
        )
        daily_rate = 0.0005 * index
        for offset, trade_date in enumerate(dates):
            close = 100 * ((1 + daily_rate) ** offset)
            is_leader = index == count
            recent = offset >= len(dates) - 5
            turnover = 2.0 if is_leader and recent else 1.0
            up_count = 8 if is_leader and recent else 5 if recent else None
            down_count = 2 if is_leader and recent else 5 if recent else None
            daily_rows.append(
                [
                    key,
                    trade_date.date().isoformat(),
                    close * 0.99,
                    close * 1.01,
                    close * 0.98,
                    close,
                    1000.0,
                    close * 1000,
                    turnover,
                    up_count,
                    down_count,
                    "",
                    None,
                    "fixture",
                    "history",
                    True,
                    timestamp,
                    f"{key}-{trade_date.date().isoformat()}",
                ]
            )
    with store.market.connect() as db:
        db.executemany("INSERT INTO board_catalog VALUES (?,?,?,?,?,?,?,?,?)", catalog_rows)
        db.executemany("INSERT INTO board_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", daily_rows)
    return keys


class BoardMainlineTests(unittest.TestCase):
    def test_rps_ranks_each_board_type_and_applies_transparent_mainline_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            industry = seed_boards(store, board_type="industry", count=10)
            concept = seed_boards(store, board_type="concept", count=5)

            result = store.compute_rps(as_of=date(2026, 7, 24))
            industry_page = store.list_mainline(board_type="industry")
            concept_page = store.list_mainline(board_type="concept")

            self.assertTrue(result["ok"])
            strongest_industry = next(
                item for item in industry_page["items"] if item["board_key"] == industry[-1]
            )
            strongest_concept = next(
                item for item in concept_page["items"] if item["board_key"] == concept[-1]
            )
            self.assertAlmostEqual(100.0, strongest_industry["rps_50"])
            self.assertAlmostEqual(100.0, strongest_concept["rps_50"])
            self.assertEqual("persistent_candidate", strongest_industry["status"])
            self.assertGreaterEqual(strongest_industry["breadth"], 0.60)
            self.assertGreaterEqual(strongest_industry["turnover_ratio_20"], 1.0)

    def test_health_reports_partial_latest_session_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            seed_boards(store, board_type="industry", count=10)
            concepts = seed_boards(store, board_type="concept", count=10)
            with store.market.connect() as db:
                latest = db.execute(
                    "SELECT MAX(trade_date) FROM board_daily WHERE board_key LIKE 'concept:%'"
                ).fetchone()[0]
                db.execute(
                    "DELETE FROM board_daily WHERE trade_date=? AND board_key IN (?,?)",
                    [latest, concepts[0], concepts[1]],
                )
            store.compute_rps(as_of=date(2026, 7, 24))

            health = store.health()

            self.assertEqual("partial_coverage", health["status"])
            self.assertAlmostEqual(0.8, health["coverage_ratios"]["concept"])
            self.assertEqual(8, health["rps_counts"]["concept"])

    def test_rank_series_defaults_to_120_dates_and_reports_full_range_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            keys = seed_boards(store, board_type="industry", count=4)
            concept_keys = seed_boards(store, board_type="concept", count=2)
            store.compute_rps(as_of=date(2026, 7, 24))

            quick_series = store.rank_series(keys[0].split(":", 1)[1], board_type="industry")
            all_series = store.rank_series(
                keys[0].split(":", 1)[1], board_type="industry", range_name="all"
            )

            self.assertFalse(all_series["truncated"])
            self.assertEqual(all_series["point_count"], len(all_series["points"]))
            self.assertGreater(all_series["point_count"], 120)
            self.assertLessEqual(quick_series["point_count"], 120)
            self.assertTrue(quick_series["truncated"])
            self.assertEqual(all_series["point_count"], quick_series["total_point_count"])
            self.assertEqual(quick_series["point_count"], quick_series["returned_point_count"])
            self.assertEqual(all_series["available_from"], quick_series["available_from"])
            self.assertEqual(
                quick_series["points"][0]["trade_date"],
                quick_series["display_from"],
            )
            self.assertTrue(all(item["rank"] >= 1 for item in all_series["points"]))

            with store.market.connect() as db:
                db.execute("DELETE FROM board_rank")
            compatibility_series = store.rank_series(
                keys[0].split(":", 1)[1], board_type="industry", range_name="all"
            )
            compatibility_concept = store.rank_series(
                concept_keys[0].split(":", 1)[1], board_type="concept", range_name="all"
            )
            self.assertEqual(all_series["point_count"], compatibility_series["point_count"])
            self.assertEqual(all_series["points"][-1]["rank"], compatibility_series["points"][-1]["rank"])
            self.assertLessEqual(
                compatibility_concept["points"][-1]["rank"],
                len(concept_keys),
            )

    def test_tied_returns_use_average_rank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            keys = seed_boards(store, board_type="industry", count=3)
            with store.market.connect() as db:
                db.execute(
                    """
                    UPDATE board_daily target
                    SET close=source.close
                    FROM board_daily source
                    WHERE target.board_key=? AND source.board_key=?
                      AND target.trade_date=source.trade_date
                    """,
                    [keys[1], keys[0]],
                )

            store.compute_rps(as_of=date(2026, 7, 24))
            items = {item["board_key"]: item for item in store.list_mainline(board_type="industry")["items"]}

            self.assertAlmostEqual(25.0, items[keys[0]]["rps_50"])
            self.assertAlmostEqual(25.0, items[keys[1]]["rps_50"])
            self.assertAlmostEqual(100.0, items[keys[2]]["rps_50"])

    def test_persistent_candidate_does_not_require_latest_day_to_be_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            keys = seed_boards(store, board_type="industry", count=10)
            with store.market.connect() as db:
                db.execute(
                    """
                    UPDATE board_daily
                    SET up_count=4,down_count=6
                    WHERE board_key=? AND trade_date='2026-07-24'
                    """,
                    [keys[-1]],
                )

            store.compute_rps(as_of=date(2026, 7, 24))
            items = {item["board_key"]: item for item in store.list_mainline(board_type="industry")["items"]}

            self.assertLess(items[keys[-1]]["breadth"], 0.60)
            self.assertEqual("persistent_candidate", items[keys[-1]]["status"])

    def test_partial_universe_never_publishes_mainline_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            keys = seed_boards(store, board_type="industry", count=10)
            with store.market.connect() as db:
                for key in keys[:2]:
                    db.execute(
                        "DELETE FROM board_daily WHERE board_key=? AND trade_date<?",
                        [key, "2026-06-01"],
                    )

            store.compute_rps(as_of=date(2026, 7, 24))
            items = store.list_mainline(board_type="industry")["items"]

            self.assertTrue(items)
            self.assertTrue(all(item["coverage_ratio"] < 0.9 for item in items))
            self.assertTrue(all(item["status"] == "partial_universe" for item in items))

    def test_current_partial_coverage_cannot_inherit_persistent_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            keys = seed_boards(store, board_type="industry", count=10)
            with store.market.connect() as db:
                for key in keys[:2]:
                    db.execute(
                        "DELETE FROM board_daily WHERE board_key=? AND trade_date='2026-07-24'",
                        [key],
                    )

            store.compute_rps(as_of=date(2026, 7, 24))
            items = {item["board_key"]: item for item in store.list_mainline(board_type="industry")["items"]}

            self.assertLess(items[keys[-1]]["coverage_ratio"], 0.9)
            self.assertNotEqual("persistent_candidate", items[keys[-1]]["status"])
            self.assertEqual("partial_universe", items[keys[-1]]["status"])

    def test_provider_opens_circuit_after_three_failed_requests(self) -> None:
        provider = EastmoneyBoardProvider(
            min_interval=0,
            max_interval=0,
            max_retries=5,
            failure_threshold=3,
            sleep=lambda _: None,
        )
        calls = 0

        def fail() -> pd.DataFrame:
            nonlocal calls
            calls += 1
            raise ConnectionError("fixture disconnect")

        with self.assertRaisesRegex(RuntimeError, "source_blocked"):
            provider._call("fixture", fail)
        self.assertEqual(3, calls)
        with self.assertRaisesRegex(RuntimeError, "circuit open"):
            provider._call("fixture-again", fail)
        self.assertEqual(3, calls)

    def test_ths_history_columns_and_millisecond_dates_normalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            key = board_key("industry", "BK881121")
            timestamp = "2026-07-24T09:00:00+08:00"
            board = {
                "board_key": key,
                "board_code": "BK881121",
                "board_name": "半导体",
                "board_type": "industry",
            }
            with store.market.connect() as db:
                db.execute(
                    "INSERT INTO board_catalog VALUES (?,?,?,?,?,?,?,?,?)",
                    [key, "BK881121", "半导体", "industry", "active", "fixture", timestamp, timestamp, timestamp],
                )
                db.execute(
                    "INSERT INTO board_fetch_queue VALUES (?,?,?,?,?,?)",
                    [key, 10, "pending", 0, "", timestamp],
                )
            frame = pd.DataFrame(
                [
                    {
                        "日期": 1784764800000,
                        "开盘价": 100.0,
                        "最高价": 102.0,
                        "最低价": 99.0,
                        "收盘价": 101.0,
                        "成交量": 1000,
                        "成交额": 100000,
                    }
                ]
            )

            saved = store.save_history(
                frame,
                board=board,
                provider="akshare-ths",
                run_id="fixture-ths",
                max_sessions=321,
            )

            with store.market.connect() as db:
                row = db.execute(
                    "SELECT trade_date,open,close FROM board_daily WHERE board_key=?",
                    [key],
                ).fetchone()
            self.assertEqual(1, saved)
            self.assertEqual(date(2026, 7, 23), row[0])
            self.assertEqual(100.0, row[1])
            self.assertEqual(101.0, row[2])

    def test_snapshot_uses_latest_open_session_instead_of_calendar_date(self) -> None:
        class Provider:
            name = "fixture"

            @staticmethod
            def fetch_catalog(_: str) -> pd.DataFrame:
                return pd.DataFrame(
                    [
                        {
                            "板块代码": "BK0001",
                            "板块名称": "示例板块",
                            "最新价": 123.45,
                            "换手率": 1.2,
                            "上涨家数": 7,
                            "下跌家数": 3,
                        }
                    ]
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            store.market.replace_calendar([date(2026, 7, 23)], provider="fixture")

            result = sync_board_snapshot(
                store,
                Provider(),
                as_of=date(2026, 7, 25),
                board_types=("industry",),
            )

            with store.market.connect() as db:
                trade_date = db.execute(
                    "SELECT trade_date FROM board_daily WHERE board_key='industry:BK0001'"
                ).fetchone()[0]
            self.assertEqual("completed", result.status)
            self.assertEqual(date(2026, 7, 23), trade_date)

    def test_intraday_snapshot_uses_previous_completed_session(self) -> None:
        class Provider:
            name = "fixture"

            @staticmethod
            def fetch_catalog(_: str) -> pd.DataFrame:
                return pd.DataFrame(
                    [{"板块代码": "BK0002", "板块名称": "盘中保护", "最新价": 98.76}]
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = BoardMainlineStore(MarketStore(Path(tmp) / "market"))
            store.market.replace_calendar(
                [date(2026, 7, 23), date(2026, 7, 24)],
                provider="fixture",
            )

            result = sync_board_snapshot(
                store,
                Provider(),
                as_of=date(2026, 7, 24),
                board_types=("industry",),
                current_time=datetime.fromisoformat("2026-07-24T10:30:00+08:00"),
            )

            with store.market.connect() as db:
                trade_date = db.execute(
                    "SELECT trade_date FROM board_daily WHERE board_key='industry:BK0002'"
                ).fetchone()[0]
            self.assertEqual("completed", result.status)
            self.assertEqual(date(2026, 7, 23), trade_date)


if __name__ == "__main__":
    unittest.main()
