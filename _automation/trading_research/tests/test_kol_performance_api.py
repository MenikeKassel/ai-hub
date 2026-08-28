from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_api import ApiSettings, create_app  # noqa: E402
from kol_tracker import EventRecord  # noqa: E402


class KolPerformanceApiTests(unittest.TestCase):
    def test_performance_endpoints_expose_batch_weighted_results_and_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            event = EventRecord(
                event_id="KOL-PERF-API",
                kol_name="Fixture KOL",
                platform="X",
                source_url="https://x.com/fixture/status/perf-api",
                source_note="post:perf-api",
                posted_at="2026-01-01T09:00:00+08:00",
                symbol="600000",
                security_name="Fixture",
                direction="long",
                thesis="fixture",
                status="active",
                kol_id="9",
                kol_handle="fixture",
                source_post_id="perf-api",
            )
            app.state.event_store.register_event(event)
            app.state.event_store.freeze_checkpoints([{
                "event_id": event.event_id, "horizon": "1W", "target_days": "5", "trade_date": "2026-01-10",
                "close_raw": "10", "raw_return": "0.1", "adjusted_return": "0.1", "directional_return": "0.1",
                "benchmark_return": "0.01", "directional_excess_return": "0.09", "max_adverse_return": "-0.02",
                "max_favorable_return": "0.1", "primary_source": "fixture", "secondary_source": "",
                "secondary_close": "", "verification_status": "verified", "finalized_at": "2026-01-10",
            }])
            client = TestClient(app)
            listing = client.get("/api/kol-performance?platform=X&window=all&horizon=1W")
            self.assertEqual(200, listing.status_code)
            self.assertTrue(listing.json()["rows"])
            self.assertEqual(200, client.post("/api/kol-performance/refresh", json={"as_of": "2026-01-12"}).status_code)
            detail = client.get("/api/kol-performance/X:9")
            self.assertEqual(200, detail.status_code)
            series = client.get("/api/kol-performance/X:9/series?horizon=1W&range=all")
            self.assertEqual(200, series.status_code)
            self.assertEqual(1, series.json()["point_count"])


    def test_performance_and_leaderboard_endpoints_expose_directional_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            long_event = EventRecord(
                event_id="KOL-COUNT-LONG",
                kol_name="Fixture KOL",
                platform="X",
                source_url="https://x.com/fixture/status/count-long",
                source_note="post:count-long",
                posted_at="2026-01-01T09:00:00+08:00",
                symbol="600000",
                security_name="Fixture",
                direction="long",
                thesis="fixture",
                status="active",
                kol_id="9901",
                kol_handle="fixture-count",
                source_post_id="count-long",
            )
            short_event = EventRecord(
                event_id="KOL-COUNT-SHORT",
                kol_name="Fixture KOL",
                platform="X",
                source_url="https://x.com/fixture/status/count-short",
                source_note="post:count-short",
                posted_at="2026-01-01T09:00:00+08:00",
                symbol="600001",
                security_name="Fixture",
                direction="short",
                thesis="fixture",
                status="active",
                kol_id="9901",
                kol_handle="fixture-count",
                source_post_id="count-short",
            )
            app.state.event_store.register_event(long_event)
            app.state.event_store.register_event(short_event)
            app.state.event_store.freeze_checkpoints([{
                "event_id": long_event.event_id, "horizon": "1W", "target_days": "5", "trade_date": "2026-01-10",
                "close_raw": "10", "raw_return": "0.1", "adjusted_return": "0.1", "directional_return": "0.1",
                "benchmark_return": "0.01", "directional_excess_return": "0.09", "max_adverse_return": "-0.02",
                "max_favorable_return": "0.1", "primary_source": "fixture", "secondary_source": "",
                "secondary_close": "", "verification_status": "verified", "finalized_at": "2026-01-10",
            }])
            client = TestClient(app)

            listing = client.get("/api/kol-performance?platform=X&window=all&horizon=1W")
            self.assertEqual(200, listing.status_code)
            row = next(item for item in listing.json()["rows"] if item["kol_key"] == "X:9901")
            metrics = row["metrics"]
            self.assertEqual(1, metrics["long_event_count"])
            self.assertEqual(1, metrics["short_event_count"])
            self.assertEqual(1, metrics["executable_long_event_count"])
            self.assertEqual(2, metrics["audit_event_count"])
            self.assertEqual(1, metrics["event_count"])

            board = client.get("/api/kol-leaderboard")
            self.assertEqual(200, board.status_code)
            board_row = next(item for item in board.json()["rows"] if item["kol_key"] == "X:9901")
            self.assertEqual(1, board_row["long_event_count"])
            self.assertEqual(1, board_row["short_event_count"])
            self.assertEqual(1, board_row["executable_long_event_count"])
            self.assertEqual(2, board_row["audit_event_count"])
            self.assertIn("counting_policy", board.json()["policy"])

            events = client.get("/api/events")
            self.assertEqual(200, events.status_code)
            by_id = {item["event_id"]: item for item in events.json()}
            self.assertEqual("long", by_id["KOL-COUNT-LONG"]["direction"])
            self.assertEqual("short", by_id["KOL-COUNT-SHORT"]["direction"])


if __name__ == "__main__":
    unittest.main()
