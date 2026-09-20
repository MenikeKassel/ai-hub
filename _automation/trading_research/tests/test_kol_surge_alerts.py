from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from kol_posts import KolPostStore  # noqa: E402
from kol_sources.core import PostRecord  # noqa: E402
from kol_surge_alerts import (  # noqa: E402
    SHANGHAI,
    evaluate_pair,
    format_surge_message,
    scan_surge_alerts,
)


def frame_of(bars: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(day) for day, _ in bars],
            "open": [close for _, close in bars],
            "close": [close for _, close in bars],
        }
    )


def bars_of(frame: pd.DataFrame) -> list[tuple]:
    return [(pd.Timestamp(day).date(), float(close)) for day, close in zip(frame["date"], frame["close"])]


class EvaluatePairTest(unittest.TestCase):
    def test_intraday_post_counts_the_post_day_move(self) -> None:
        bars = bars_of(frame_of([("2026-09-11", 10.0), ("2026-09-15", 10.4), ("2026-09-16", 11.5)]))
        posted = datetime(2026, 9, 15, 10, 0, tzinfo=SHANGHAI)
        outcome = evaluate_pair(bars, posted, window_days=7, threshold=0.10, today=datetime(2026, 9, 20).date())
        self.assertEqual("alert", outcome["status"])
        self.assertEqual("2026-09-11", outcome["baseline_date"])
        self.assertAlmostEqual(0.15, outcome["max_gain"], places=4)

    def test_after_close_post_anchors_on_same_day_close(self) -> None:
        bars = bars_of(frame_of([("2026-09-11", 10.0), ("2026-09-15", 10.4), ("2026-09-16", 11.5)]))
        posted = datetime(2026, 9, 15, 16, 0, tzinfo=SHANGHAI)
        outcome = evaluate_pair(bars, posted, window_days=7, threshold=0.10, today=datetime(2026, 9, 20).date())
        self.assertEqual("2026-09-15", outcome["baseline_date"])
        self.assertAlmostEqual(11.5 / 10.4 - 1, outcome["max_gain"], places=4)

    def test_below_threshold_inside_window_is_recheckable(self) -> None:
        bars = bars_of(frame_of([("2026-09-11", 10.0), ("2026-09-16", 10.8)]))
        posted = datetime(2026, 9, 15, 10, 0, tzinfo=SHANGHAI)
        outcome = evaluate_pair(bars, posted, window_days=7, threshold=0.10, today=datetime(2026, 9, 20).date())
        self.assertEqual("checked", outcome["status"])
        self.assertFalse(outcome["window_closed"])

    def test_below_threshold_after_window_is_closed(self) -> None:
        bars = bars_of(frame_of([("2026-09-11", 10.0), ("2026-09-16", 10.8)]))
        posted = datetime(2026, 9, 15, 10, 0, tzinfo=SHANGHAI)
        outcome = evaluate_pair(bars, posted, window_days=7, threshold=0.10, today=datetime(2026, 9, 30).date())
        self.assertEqual("checked", outcome["status"])
        self.assertTrue(outcome["window_closed"])

    def test_no_forward_bars_yet_is_pending(self) -> None:
        bars = bars_of(frame_of([("2026-09-11", 10.0)]))
        posted = datetime(2026, 9, 15, 10, 0, tzinfo=SHANGHAI)
        outcome = evaluate_pair(bars, posted, window_days=7, threshold=0.10, today=datetime(2026, 9, 16).date())
        self.assertEqual("pending", outcome["status"])


class ScanIntegrationTest(unittest.TestCase):
    def build_store(self, tmp: str, *, posted_at: str = "2026-09-15T10:00:00+08:00") -> tuple[KolPostStore, int]:
        store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
        kol_id, _ = store.add_kol("测试KOL", "fixturekol", platform="X")
        post = PostRecord(
            post_id="post-surge-1",
            kol_id=kol_id,
            platform="X",
            handle="fixturekol",
            author_name="测试KOL",
            url="https://x.com/fixturekol/status/1",
            text="看好 测试电子 下周表现",
            article_title="",
            article_text="",
            quoted_id="",
            quoted_text="",
            quoted_author="",
            reply_to_id="",
            reply_to_author="",
            posted_at=posted_at,
            posted_at_utc="2026-09-15T02:00:00+00:00",
            post_type="post",
            language="zh",
            media=[],
            metrics={},
            raw_payload={},
            content_hash="hash",
            fetched_at="2026-09-15T02:00:00+00:00",
        )
        store.upsert_post(post)
        store.upsert_stock_lead(
            {
                "post_id": "post-surge-1",
                "kol_id": kol_id,
                "symbol": "600001",
                "security_name": "测试电子",
                "direction": "long",
                "mention_kind": "recommendation",
                "evidence_text": "看好 测试电子",
                "extraction_method": "rules",
                "confidence": 0.9,
                "status": "pending",
            }
        )
        return store, kol_id

    def test_scan_alerts_once_then_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.build_store(tmp)
            frames = {
                "600001": frame_of([("2026-09-11", 10.0), ("2026-09-16", 10.3), ("2026-09-17", 11.6)]),
            }

            class FakeProvider:
                def fetch_stock(self, symbol, start, end, *, adjusted):
                    if symbol not in frames:
                        raise RuntimeError("no warehouse data")
                    return frames[symbol]

            state_path = Path(tmp) / "surge_state.json"
            now = datetime(2026, 9, 20, 22, 0, tzinfo=SHANGHAI)
            first = scan_surge_alerts(store, FakeProvider(), state_path=state_path, now=now)
            self.assertEqual(1, len(first["new_alerts"]))
            alert = first["new_alerts"][0]
            self.assertEqual("600001", alert["symbol"])
            self.assertAlmostEqual(11.6 / 10.0 - 1, alert["max_gain"], places=4)
            self.assertTrue(state_path.is_file())
            self.assertEqual("alerted", json.loads(state_path.read_text(encoding="utf-8"))["post-surge-1|600001"]["status"])

            second = scan_surge_alerts(store, FakeProvider(), state_path=state_path, now=now)
            self.assertEqual([], second["new_alerts"])

    def test_scan_reports_unknown_symbols_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self.build_store(tmp)

            class EmptyProvider:
                def fetch_stock(self, symbol, start, end, *, adjusted):
                    raise RuntimeError("no warehouse data")

            result = scan_surge_alerts(
                store,
                EmptyProvider(),
                state_path=Path(tmp) / "state.json",
                now=datetime(2026, 9, 20, 22, 0, tzinfo=SHANGHAI),
            )
            self.assertEqual([], result["new_alerts"])
            self.assertEqual(1, len(result["errors"]))

    def test_format_message(self) -> None:
        message = format_surge_message(
            [
                {
                    "handle": "Valen9223",
                    "kol": "valen",
                    "security_name": "中瓷电子",
                    "symbol": "003031",
                    "max_gain": 0.123,
                    "peak_date": "2026-09-25",
                    "peak_close": 51.3,
                    "baseline_date": "2026-09-19",
                    "baseline_close": 45.67,
                    "posted_at": "2026-09-20T21:04:24+08:00",
                    "url": "https://x.com/Valen9223/status/2101658736247058860",
                }
            ],
            window_days=7,
            threshold=0.10,
        )
        self.assertIn("+12.3%", message)
        self.assertIn("中瓷电子(003031)", message)
        self.assertIn("https://x.com/Valen9223/status/2101658736247058860", message)


if __name__ == "__main__":
    unittest.main()
