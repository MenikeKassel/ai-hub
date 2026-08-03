from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_research_service import EventMethodResearchService  # noqa: E402
from kol_tracker import EventRecord, KolStore  # noqa: E402
from market_data import Instrument, MarketStore  # noqa: E402


LENSES = [
    "short_term_leader",
    "dow_wave_gann",
    "price_action",
    "ict",
    "wyckoff_orderflow",
]


class FakeInterpreter:
    model_name = "fixture-ai"
    prompt_version = "fixture-v1"

    def interpret_many(self, items):
        return {
            item["event"]["event_id"]: {
                "interpretations": [
                    {
                        "lens": lens,
                        "hypothesis": f"{lens} hypothesis",
                        "confidence": 0.6,
                        "evidence_refs": [f"lenses.{lens}.facts"],
                        "counter_evidence": [],
                        "invalidation": [],
                    }
                    for lens in LENSES
                ]
            }
            for item in items
        }


class FailingInterpreter:
    model_name = "fixture-ai"
    prompt_version = "fixture-v1"

    def interpret_many(self, items):
        del items
        raise ValueError("evidence ref does not exist")


class EventResearchServiceTests(unittest.TestCase):
    def test_current_provider_replaces_legacy_ready_interpretation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_store = KolStore(root / "kol")
            market = MarketStore(root / "market")
            event = EventRecord(
                event_id="KOL-PROVIDER-MIGRATION",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/provider",
                source_note="post:provider",
                posted_at="2026-07-01T16:00:00+08:00",
                symbol="600900",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            event_store.register_event(event)
            market.save_event_method_research(
                {
                    "snapshot_id": "research-provider",
                    "event_id": event.event_id,
                    "method_version": "fixture-v1",
                    "input_hash": "fixture-input",
                    "symbol": event.symbol,
                    "posted_at": event.posted_at,
                    "as_of_trade_date": "2026-07-01",
                    "status": "partial",
                    "payload": {
                        "version": "fixture-v1",
                        "lenses": {
                            lens: {"facts": {"fixture": True}}
                            for lens in LENSES
                        },
                    },
                    "warnings": [],
                    "computed_at": "2026-07-01T16:01:00+08:00",
                }
            )
            market.save_event_method_interpretation(
                {
                    "interpretation_id": "legacy-ready",
                    "event_id": event.event_id,
                    "research_snapshot_id": "research-provider",
                    "provider": "legacy-ai",
                    "model": "legacy-ai",
                    "prompt_version": "legacy-v1",
                    "input_hash": "legacy-input",
                    "status": "ready",
                    "payload": {"interpretations": []},
                    "validation": {"ok": True},
                    "error": "",
                    "created_at": "2026-07-01T16:02:00+08:00",
                }
            )
            service = EventMethodResearchService(
                event_store,
                market,
                interpreter=FakeInterpreter(),
            )

            self.assertFalse(
                service.has_ready_interpretation(
                    event.event_id,
                    "research-provider",
                )
            )
            result = service.interpret_pending(event_ids=[event.event_id])

            self.assertTrue(result["ok"])
            self.assertTrue(
                service.has_ready_interpretation(
                    event.event_id,
                    "research-provider",
                )
            )
            self.assertEqual(
                "fixture-ai",
                service.get_section(event.event_id)["interpretation"]["provider"],
            )

    def test_current_prompt_replaces_ready_interpretation_from_old_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_store = KolStore(root / "kol")
            market = MarketStore(root / "market")
            event = EventRecord(
                event_id="KOL-PROMPT-MIGRATION",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/prompt",
                source_note="post:prompt",
                posted_at="2026-07-01T16:00:00+08:00",
                symbol="600900",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            event_store.register_event(event)
            market.save_event_method_research(
                {
                    "snapshot_id": "research-prompt",
                    "event_id": event.event_id,
                    "method_version": "fixture-v1",
                    "input_hash": "fixture-input",
                    "symbol": event.symbol,
                    "posted_at": event.posted_at,
                    "as_of_trade_date": "2026-07-01",
                    "status": "partial",
                    "payload": {
                        "version": "fixture-v1",
                        "lenses": {
                            lens: {"facts": {"fixture": True}}
                            for lens in LENSES
                        },
                    },
                    "warnings": [],
                    "computed_at": "2026-07-01T16:01:00+08:00",
                }
            )
            market.save_event_method_interpretation(
                {
                    "interpretation_id": "old-policy-ready",
                    "event_id": event.event_id,
                    "research_snapshot_id": "research-prompt",
                    "provider": "fixture-ai",
                    "model": "fixture-ai",
                    "prompt_version": "fixture-v0",
                    "input_hash": "legacy-input",
                    "status": "ready",
                    "payload": {"interpretations": []},
                    "validation": {"ok": True},
                    "error": "",
                    "created_at": "2026-07-01T16:02:00+08:00",
                }
            )
            service = EventMethodResearchService(
                event_store,
                market,
                interpreter=FakeInterpreter(),
            )

            self.assertFalse(
                service.has_ready_interpretation(
                    event.event_id,
                    "research-prompt",
                )
            )
            self.assertIsNone(
                service.get_section(event.event_id)["interpretation"]
            )

            result = service.interpret_pending(event_ids=[event.event_id])
            repeated = service.interpret_pending(event_ids=[event.event_id])

            self.assertTrue(result["ok"])
            self.assertEqual(0, repeated["processed"])
            self.assertEqual(
                "fixture-v1",
                service.get_section(event.event_id)["interpretation"][
                    "prompt_version"
                ],
            )

            event_store.update_event(
                replace(event, thesis="revised fixture thesis"),
                action="fixture_thesis_amendment",
            )

            self.assertFalse(
                service.has_ready_interpretation(
                    event.event_id,
                    "research-prompt",
                )
            )
            self.assertIsNone(
                service.get_section(event.event_id)["interpretation"]
            )
            revised = service.interpret_pending(event_ids=[event.event_id])
            self.assertEqual(1, revised["processed"])

    def test_empty_board_membership_does_not_scan_the_rps_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_store = KolStore(root / "kol")
            market = MarketStore(root / "market")
            event = EventRecord(
                event_id="KOL-NO-BOARD",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/3",
                source_note="post:3",
                posted_at="2026-07-01T16:00:00+08:00",
                symbol="600900",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            event_store.register_event(event)
            service = EventMethodResearchService(event_store, market)

            with patch.object(
                market,
                "connect",
                wraps=market.connect,
            ) as connect:
                rows, members = service._board_context(event)

            self.assertEqual([], rows)
            self.assertEqual({}, members)
            self.assertEqual(1, connect.call_count)

    def test_refresh_is_append_only_and_ai_attaches_to_same_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_store = KolStore(root / "kol")
            market = MarketStore(root / "market")
            event = EventRecord(
                event_id="KOL-RESEARCH-1",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/1",
                source_note="post:1",
                posted_at="2026-07-01T16:00:00+08:00",
                symbol="600900",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            event_store.register_event(event)
            market.upsert_instrument(
                Instrument("600900", "fixture", "stock", "SH")
            )
            dates = pd.bdate_range("2026-06-01", periods=23)
            daily = pd.DataFrame(
                [
                    {
                        "trade_date": current.date().isoformat(),
                        "open": 10 + index * 0.1,
                        "high": 10.5 + index * 0.1,
                        "low": 9.5 + index * 0.1,
                        "close": 10.2 + index * 0.1,
                        "volume": 1000 + index * 10,
                    }
                    for index, current in enumerate(dates)
                ]
            )
            directory = market.warehouse_root / "daily" / event.symbol / "qfq"
            directory.mkdir(parents=True, exist_ok=True)
            daily.to_parquet(directory / "2026.parquet", index=False)
            raw_directory = (
                market.warehouse_root / "daily" / event.symbol / "raw"
            )
            raw_directory.mkdir(parents=True, exist_ok=True)
            raw_daily = daily.copy()
            for column in ("open", "high", "low", "close"):
                raw_daily[column] = raw_daily[column] * 2
            raw_daily.to_parquet(raw_directory / "2026.parquet", index=False)
            for current in dates[-21:]:
                market.save_cross_section_snapshot(
                    provider="fixture",
                    as_of=current.date(),
                    frame=pd.DataFrame(
                        [
                            {
                                "trade_date": current.date().isoformat(),
                                "symbol": "600900",
                                "close": 10.0 + current.day / 100,
                                "preclose": 9.9,
                                "amount": 1000000,
                                "pct_change_pct": 1.0,
                                "is_st": False,
                            },
                            {
                                "trade_date": current.date().isoformat(),
                                "symbol": "600901",
                                "close": 9.0,
                                "preclose": 9.0,
                                "amount": 500000,
                                "pct_change_pct": 0.0,
                                "is_st": False,
                            },
                        ]
                    ),
                )
            service = EventMethodResearchService(
                event_store,
                market,
                interpreter=FakeInterpreter(),
                cache_market_data=True,
            )

            first = service.refresh(event.event_id)
            second = service.refresh(event.event_id)
            ai = service.interpret_pending(event_ids=[event.event_id])
            section = service.get_section(event.event_id)

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertTrue(ai["ok"])
            self.assertEqual(
                "event_cutoff_raw_close",
                first["research"]["data_lineage"]["daily_price_scale_basis"],
            )
            self.assertEqual(
                2.0,
                first["research"]["data_lineage"]["daily_price_scale"],
            )
            self.assertEqual(first["snapshot_id"], section["snapshot_id"])
            self.assertEqual("ready", section["interpretation"]["status"])
            self.assertEqual(1, len(service.history(event.event_id)))

            amended = replace(
                event,
                posted_at="2026-07-01T16:01:00+08:00",
            )
            event_store.update_event(amended, action="fixture_amendment")
            revised = service.refresh(event.event_id)
            revised_section = service.get_section(event.event_id)

            self.assertTrue(revised["created"])
            self.assertNotEqual(first["snapshot_id"], revised["snapshot_id"])
            self.assertEqual(revised["snapshot_id"], revised_section["snapshot_id"])
            self.assertIsNone(revised_section["interpretation"])
            self.assertEqual(2, len(service.history(event.event_id)))

    def test_ai_validation_failure_is_kept_as_an_append_only_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_store = KolStore(root / "kol")
            market = MarketStore(root / "market")
            event = EventRecord(
                event_id="KOL-RESEARCH-FAILED",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/2",
                source_note="post:2",
                posted_at="2026-07-01T16:00:00+08:00",
                symbol="600900",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            event_store.register_event(event)
            market.save_event_method_research(
                {
                    "snapshot_id": "research-failed",
                    "event_id": event.event_id,
                    "method_version": "fixture-v1",
                    "input_hash": "fixture-input",
                    "symbol": event.symbol,
                    "posted_at": event.posted_at,
                    "as_of_trade_date": "2026-07-01",
                    "status": "partial",
                    "payload": {
                        "version": "fixture-v1",
                        "lenses": {
                            lens: {"facts": {"fixture": True}}
                            for lens in LENSES
                        },
                    },
                    "warnings": [],
                    "computed_at": "2026-07-01T16:01:00+08:00",
                }
            )
            service = EventMethodResearchService(
                event_store,
                market,
                interpreter=FailingInterpreter(),
            )

            first = service.interpret_pending(event_ids=[event.event_id])
            second = service.interpret_pending(event_ids=[event.event_id])
            records = market.list_event_method_interpretations(event.event_id)
            section = service.get_section(event.event_id)

            self.assertFalse(first["ok"])
            self.assertEqual(1, len(first["failed"]))
            self.assertFalse(second["failed"][0]["created"])
            self.assertEqual(1, len(records))
            self.assertEqual("failed", records[0]["status"])
            self.assertIn("evidence ref", records[0]["error"])
            self.assertEqual("failed", section["interpretation"]["status"])


if __name__ == "__main__":
    unittest.main()
