from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio import PortfolioStore, PortfolioTransaction  # noqa: E402


def tx(**overrides):
    value = {
        "executed_at": "2026-07-10 13:18:35",
        "symbol": "601888",
        "security_name": "中国中免",
        "broker_name": "中国中免",
        "action": "buy",
        "price": "52.980",
        "quantity": 100,
        "gross_amount": "5298.000",
        "source": "fixture",
    }
    value.update(overrides)
    return PortfolioTransaction.from_mapping(value)


class PortfolioTests(unittest.TestCase):
    def test_import_is_idempotent_and_preserves_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PortfolioStore(Path(tmp))
            item = tx()
            self.assertEqual("2026-07-10T13:18:35+08:00", item.executed_at)
            self.assertEqual(1, store.record_many([item])["created"])
            result = store.record_many([item])
            self.assertEqual(0, result["created"])
            self.assertEqual(1, result["duplicate"])
            self.assertEqual(1, len(store.load()))

    def test_summary_uses_moving_average_and_keeps_dividends_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PortfolioStore(Path(tmp))
            store.record_many(
                [
                    tx(),
                    tx(
                        executed_at="2026-07-13 16:00:00",
                        action="dividend",
                        price="50.910",
                        quantity=0,
                        gross_amount="45.000",
                    ),
                    tx(
                        executed_at="2026-07-15 10:09:39",
                        action="sell",
                        price="53.480",
                        gross_amount="5348.000",
                    ),
                    tx(
                        executed_at="2026-07-15 13:18:37",
                        action="buy",
                        price="54.780",
                        gross_amount="5478.000",
                    ),
                    tx(
                        executed_at="2026-07-16 09:44:03",
                        action="sell",
                        price="53.600",
                        gross_amount="5360.000",
                    ),
                    tx(
                        executed_at="2026-07-16 10:34:55",
                        action="buy",
                        price="54.680",
                        gross_amount="5468.000",
                    ),
                ]
            )
            summary = store.summary()
            position = summary["positions"][0]
            self.assertEqual(100, position["quantity"])
            self.assertEqual("54.680", position["average_cost"])
            self.assertEqual("-68.000", position["realized_trading_pnl"])
            self.assertEqual("45.000", position["dividends"])
            self.assertEqual("-23.000", position["realized_total_pnl"])

    def test_sell_without_recorded_inventory_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PortfolioStore(Path(tmp))
            store.record_many([tx(action="sell")])
            self.assertTrue(store.summary()["warnings"])


if __name__ == "__main__":
    unittest.main()
