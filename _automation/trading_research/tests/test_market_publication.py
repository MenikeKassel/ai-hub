from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_publication import MarketDailyPublisher  # noqa: E402


class _CalendarProvider:
    def fetch_calendar(self, _start: date, _end: date) -> list[date]:
        return [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]

    def close(self) -> None:
        return None


class MarketPublicationDateTests(unittest.TestCase):
    @patch("market_publication.BaoStockMarketProvider", return_value=_CalendarProvider())
    def test_auto_target_excludes_current_trading_day_before_close(self, _provider) -> None:
        publisher = MarketDailyPublisher(
            Path("market"),
            object(),
            now_provider=lambda: datetime(2026, 9, 3, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(date(2026, 9, 2), publisher.resolve_target_date("auto"))

    @patch("market_publication.BaoStockMarketProvider", return_value=_CalendarProvider())
    def test_auto_target_allows_current_trading_day_after_close(self, _provider) -> None:
        publisher = MarketDailyPublisher(
            Path("market"),
            object(),
            now_provider=lambda: datetime(2026, 9, 3, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual(date(2026, 9, 3), publisher.resolve_target_date("auto"))


if __name__ == "__main__":
    unittest.main()
