from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from purchased_daily import (  # noqa: E402
    PURCHASED_DAILY_PROVIDER,
    PurchasedDailyProvider,
    audit_purchased_daily_archive,
    import_historical_daily,
)
from market_data import Instrument, MarketStore, sync_daily_bars  # noqa: E402


HEADER = (
    "ts_code,trade_date,name,open,high,low,close,pre_close,change,pct_chg,"
    "vol,amount,adj_factor,first_adj,last_adj,open_qfq,high_qfq,low_qfq,"
    "close_qfq,pre_close_qfq,change_qfq,pct_chg_qfq,open_hfq,high_hfq,"
    "low_hfq,close_hfq,pre_close_hfq,change_hfq,pct_chg_hfq"
)


def write_fixture(
    root: Path,
    symbol: str = "600900.SH",
    second_symbol: str | None = None,
    last_date: str = "20260731",
) -> Path:
    path = root / f"{symbol}.csv"
    second_symbol = second_symbol or symbol
    path.write_text(
        "\n".join(
            [
                HEADER,
                f"{symbol},20260730,长江电力,28,29,27,28.5,28,0.5,1.78,100,200,2,1,2,14,14.5,13.5,14.25,14,0.25,1.78,56,58,54,57,56,1,1.78",
                f"{second_symbol},{last_date},长江电力,29,30,28,29.5,28.5,1,3.5,120,240,2.1,1,2.1,14.5,15,14,14.75,14.25,0.5,3.5,58,60,56,59,57,2,3.5",
            ]
        ),
        encoding="utf-8",
    )
    return path


class FixtureProvider:
    name = "fixture"

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def fetch_daily(self, symbol, instrument_type, start, end, adjustment):
        del symbol, instrument_type, start, end, adjustment
        return self.frame.copy()


class PurchasedDailyTests(unittest.TestCase):
    def test_provider_converts_tushare_units_and_selects_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root)
            provider = PurchasedDailyProvider(root)

            raw = provider.fetch_daily("600900", "stock", date(2026, 7, 30), date(2026, 7, 31), "raw")
            qfq = provider.fetch_daily("600900", "stock", date(2026, 7, 30), date(2026, 7, 31), "qfq")

            self.assertEqual(PURCHASED_DAILY_PROVIDER, provider.name)
            self.assertEqual(["2026-07-30", "2026-07-31"], raw["trade_date"].astype(str).tolist())
            self.assertEqual(12000, raw.iloc[-1]["volume"])
            self.assertEqual(240000, raw.iloc[-1]["amount"])
            self.assertEqual(29.5, raw.iloc[-1]["close"])
            self.assertEqual(14.75, qfq.iloc[-1]["close"])
            self.assertEqual(2.1, raw.iloc[-1]["adj_factor"])
            self.assertTrue(pd.isna(raw.iloc[-1]["turnover"]))

    def test_provider_rejects_symbol_mismatch_in_later_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root, second_symbol="600519.SH")
            with self.assertRaisesRegex(ValueError, "symbol mismatch"):
                PurchasedDailyProvider(root).fetch_daily(
                    "600900", "stock", date(2026, 7, 30), date(2026, 7, 31), "raw"
                )

    def test_north_exchange_mapping_does_not_treat_92_as_shanghai(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root, symbol="920001.BJ")
            provider = PurchasedDailyProvider(root)
            self.assertTrue(provider.has_symbol("920001"))
            self.assertEqual(
                2,
                len(provider.fetch_daily("920001", "stock", date(2026, 7, 30), date(2026, 7, 31), "raw")),
            )

    def test_doctor_reports_snapshot_shape_and_missing_reference_instruments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root)
            report = audit_purchased_daily_archive(root, expected_date=date(2026, 7, 31))

            self.assertTrue(report["ok"])
            self.assertEqual(1, report["file_count"])
            self.assertEqual(1, report["latest_date_file_count"])
            self.assertIn("159139.SZ", report["missing_reference_files"])
            self.assertIn("000300.SH", report["missing_reference_files"])
            self.assertEqual([], report["errors"])

    def test_doctor_flags_malformed_last_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root, last_date="2026-07-31")
            report = audit_purchased_daily_archive(root)
            self.assertFalse(report["ok"])
            self.assertIn("600900.SH.csv:missing_last_trade_date", report["file_errors"])

    def test_doctor_marks_snapshot_stale_against_expected_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root)
            report = audit_purchased_daily_archive(root, expected_date=date(2026, 8, 3))
            self.assertEqual("stale", report["source_status"])
            self.assertEqual("metadata_only; row_content_validated_on_import", report["audit_scope"])

    def test_import_merges_history_without_relabeling_canonical_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "archive"
            archive.mkdir()
            write_fixture(archive)
            store = MarketStore(root / "market")
            store.upsert_instrument(
                Instrument("600900", "长江电力", "stock", "SH", lifecycle="tracking", source="fixture")
            )
            current = pd.DataFrame(
                [
                    {
                        "date": "2026-07-31",
                        "open": 29,
                        "high": 30,
                        "low": 28,
                        "close": 29.5,
                        "preclose": 28.5,
                        "volume": 12000,
                        "amount": 240000,
                        "turn": 1,
                        "tradestatus": "1",
                    }
                ]
            )
            sync_daily_bars(
                store,
                FixtureProvider(current),
                "600900",
                date(2026, 7, 31),
                date(2026, 7, 31),
                adjustment="raw",
            )

            result = import_historical_daily(
                store,
                PurchasedDailyProvider(archive),
                {"600900"},
                start=date(2026, 7, 1),
                end=date(2026, 7, 31),
            )

            self.assertEqual([], result["failed"])
            frame = store.read_daily("600900", adjustment="raw")
            self.assertEqual(["2026-07-30", "2026-07-31"], frame["trade_date"].astype(str).tolist())
            coverage = next(item for item in store.get_coverage("600900") if item["adjustment"] == "raw")
            self.assertEqual("2026-07-30", coverage["start_date"])
            self.assertEqual("2026-07-31", coverage["end_date"])
            self.assertEqual("fixture", coverage["provider"])


if __name__ == "__main__":
    unittest.main()
