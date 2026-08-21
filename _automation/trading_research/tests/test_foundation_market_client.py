from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parents[1]
PUBLIC_SRC = HERE.parents[2] / "kol-audit-workbench" / "src"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PUBLIC_SRC))

from foundation_market_client import FoundationBackedMarketStore  # noqa: E402
from kol_audit.market.store import MarketStore  # noqa: E402


def _write_dataset(root: Path, name: str, frame: pd.DataFrame) -> None:
    path = root / "warehouse" / name / "release_id=fixture-source"
    path.mkdir(parents=True)
    frame.to_parquet(path / "part-000.parquet", index=False)


def _make_foundation(root: Path) -> None:
    pointer = {
        "release_id": "fixture-release",
        "as_of": "2026-08-07",
        "created_at": "2026-08-07T16:00:00+08:00",
        "datasets": ["daily_raw", "daily_adjusted", "instruments", "trading_calendar"],
        "dataset_sources": {
            "daily_raw": "fixture-source",
            "daily_adjusted": "fixture-source",
            "instruments": "fixture-source",
            "trading_calendar": "fixture-source",
        },
    }
    root.mkdir(parents=True)
    (root / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
    raw = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "000001.SZ"],
            "trade_date": [date(2026, 8, 6), date(2026, 8, 7)],
            "open": [11.0, 11.1],
            "high": [11.2, 11.3],
            "low": [10.9, 11.0],
            "close": [11.1, 11.19],
            "volume": [100.0, 120.0],
            "amount": [1_100.0, 1_340.0],
        }
    )
    adjusted = raw.assign(adjustment="qfq", adjustment_factor=[138.9, 139.008])
    instruments = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "name": ["平安银行"],
            "exchange": ["SZ"],
            "status": ["active"],
            "list_date": [date(1991, 4, 3)],
        }
    )
    calendar = pd.DataFrame(
        {
            "trade_date": [date(2026, 8, 6), date(2026, 8, 7)],
            "is_open": [True, True],
        }
    )
    _write_dataset(root, "daily_raw", raw)
    _write_dataset(root, "daily_adjusted", adjusted)
    _write_dataset(root, "instruments", instruments)
    _write_dataset(root, "trading_calendar", calendar)


class FoundationMarketClientTests(unittest.TestCase):
    def test_reads_pinned_daily_facts_and_keeps_local_store_as_workflow_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foundation_root = root / "foundation"
            _make_foundation(foundation_root)
            local_store = MarketStore(root / "market")
            store = FoundationBackedMarketStore(local_store, foundation_root)

            bootstrap = store.bootstrap_reference_data()
            unchanged = store.bootstrap_reference_data()
            daily = store.read_daily("000001", adjustment="qfq")
            store.enqueue_sync("000001", reason="test")

            self.assertTrue(bootstrap["ok"])
            self.assertEqual(1, bootstrap["instrument_count"])
            self.assertTrue(bootstrap["unchanged"])
            self.assertTrue(unchanged["unchanged"])
            self.assertEqual("平安银行", store.get_instrument("000001")["name"])
            self.assertEqual([], local_store.list_instruments())
            self.assertEqual(date(2026, 8, 7), store.latest_open_date(date(2026, 8, 9)))
            self.assertEqual(11.19, float(daily.iloc[-1]["close"]))
            self.assertEqual("fixture-release", daily.iloc[-1]["foundation_release_id"])
            coverage = store.foundation.coverage()
            self.assertTrue(coverage["complete"])
            self.assertEqual(1, coverage["active_catalog"])
            self.assertEqual(1, coverage["observed"])
            self.assertEqual([], store.pending_sync())
            self.assertFalse((root / "market" / "warehouse" / "daily").exists())

    def test_consumer_is_read_only_and_reports_missing_hfq(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foundation_root = root / "foundation"
            _make_foundation(foundation_root)
            store = FoundationBackedMarketStore(
                MarketStore(root / "market"), foundation_root
            )

            self.assertTrue(store.read_daily("000001", adjustment="hfq").empty)
            with self.assertRaises(PermissionError):
                store.write_daily(pd.DataFrame(), symbol="000001", adjustment="raw")

    def test_release_is_pinned_per_operation_and_refreshes_afterward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foundation_root = root / "foundation"
            _make_foundation(foundation_root)
            store = FoundationBackedMarketStore(
                MarketStore(root / "market"), foundation_root
            )

            with store.foundation.pinned_release():
                first = store.read_daily("000001")
                pointer = json.loads(
                    (foundation_root / "current.json").read_text(encoding="utf-8")
                )
                pointer["release_id"] = "new-release"
                pointer["as_of"] = "2026-08-08"
                (foundation_root / "current.json").write_text(
                    json.dumps(pointer), encoding="utf-8"
                )
                still_pinned = store.read_daily("000001")

            refreshed = store.read_daily("000001")
            self.assertEqual("fixture-release", first.iloc[-1]["foundation_release_id"])
            self.assertEqual(
                "fixture-release", still_pinned.iloc[-1]["foundation_release_id"]
            )
            self.assertEqual("new-release", refreshed.iloc[-1]["foundation_release_id"])

    def test_missing_foundation_symbol_does_not_fall_back_to_local_daily_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foundation_root = root / "foundation"
            _make_foundation(foundation_root)
            local_store = MarketStore(root / "market")
            local_store.write_daily(
                pd.DataFrame(
                    {
                        "trade_date": [date(2026, 8, 7)],
                        "open": [1.0],
                        "high": [1.1],
                        "low": [0.9],
                        "close": [1.0],
                        "volume": [10.0],
                        "amount": [10.0],
                    }
                ),
                symbol="600000",
                adjustment="raw",
            )
            store = FoundationBackedMarketStore(local_store, foundation_root)
            self.assertTrue(store.read_daily("600000", adjustment="raw").empty)


if __name__ == "__main__":
    unittest.main()
