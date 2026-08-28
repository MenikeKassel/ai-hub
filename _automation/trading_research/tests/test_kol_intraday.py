from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_intraday import backfill_event_intraday  # noqa: E402
from kol_tracker import EventRecord  # noqa: E402
from market_data import MarketStore  # noqa: E402


class FixtureEventStore:
    def __init__(self, event: EventRecord):
        self.event = event

    def load_events(self):
        return [self.event]


class FixtureMinuteProvider:
    name = "fixture-minute"

    def fetch_minute(self, symbol, instrument_type, start, end, frequency, adjustment):
        values = []
        for day_offset in range(5):
            current_date = start + timedelta(days=day_offset)
            session_minutes = list(range(0, 120)) + list(range(210, 330))
            for offset in session_minutes:
                current = datetime.combine(current_date, datetime.min.time()).replace(hour=9, minute=30) + timedelta(minutes=offset)
                absolute_offset = day_offset * 240 + offset
                values.append(
                    {
                        "symbol": symbol,
                        "instrument_type": instrument_type,
                        "trade_datetime": current,
                        "open": 10 + absolute_offset / 100,
                        "high": 10.02 + absolute_offset / 100,
                        "low": 9.98 + absolute_offset / 100,
                        "close": 10 + absolute_offset / 100,
                        "preclose": 10,
                        "volume": 100,
                        "amount": 1000,
                        "turnover": 1,
                        "adjustment": adjustment,
                        "provider": self.name,
                    }
                )
        return pd.DataFrame(values)


class KolIntradayTests(unittest.TestCase):
    def test_event_window_uses_first_executable_bar_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketStore(Path(tmp) / "market")
            start = date(2026, 7, 20)
            store.replace_calendar([start + timedelta(days=index) for index in range(5)], provider="fixture")
            event = EventRecord(
                event_id="KOL-INTRA-1",
                kol_name="Fixture",
                platform="X",
                source_url="https://example.invalid/post",
                source_note="fixture",
                posted_at="2026-07-20T09:35:00+08:00",
                symbol="600900",
                security_name="Fixture",
                direction="long",
                thesis="test",
                status="active",
            )
            event_store = FixtureEventStore(event)

            first = backfill_event_intraday(store, event_store, provider=FixtureMinuteProvider())
            second = backfill_event_intraday(store, event_store, provider=FixtureMinuteProvider())
            context = store.get_event_intraday_context(event.event_id)

            self.assertEqual([event.event_id], first["created"])
            self.assertEqual([event.event_id], second["skipped"])
            self.assertEqual("complete", context["status"])
            self.assertEqual(10.05, context["first_price"])
            self.assertEqual(10.1, context["close_5m"])
            self.assertEqual("minute_1m", store.get_coverage("600900")[0]["dataset"])
