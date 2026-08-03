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


if __name__ == "__main__":
    unittest.main()
