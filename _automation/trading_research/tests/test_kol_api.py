from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_api import ApiSettings, _read_process_pid, create_app  # noqa: E402
from kol_posts import RuleClassifier, RuleResult, normalise_twitter_post  # noqa: E402
from kol_tracker import SHANGHAI, EventRecord  # noqa: E402
from event_context import compute_event_technical_context, failed_event_technical_context  # noqa: E402
from market_data import Instrument  # noqa: E402
from recommendation_drafts import RecommendationDraftRepository  # noqa: E402
from review_agent import PolicyDecision  # noqa: E402


class ApiTests(unittest.TestCase):
    def test_gateway_pid_reader_supports_legacy_and_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy.pid"
            structured = root / "structured.pid"
            invalid = root / "invalid.pid"
            legacy.write_text("12345\n", encoding="utf-8")
            structured.write_text('{"pid": 67890, "kind": "hermes-gateway"}', encoding="utf-8")
            invalid.write_text('{"pid": "not-a-number"}', encoding="utf-8")

            self.assertEqual(_read_process_pid(legacy), "12345")
            self.assertEqual(_read_process_pid(structured), "67890")
            self.assertEqual(_read_process_pid(invalid), "")
            self.assertEqual(_read_process_pid(root / "missing.pid"), "")

    def test_pipeline_status_is_lightweight_and_scopes_pending_ai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            store = app.state.post_store
            kol = store.get_kol_by_handle("public_kol_2")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000001",
                    "text": "关注 002414 高德红外",
                    "url": "https://x.com/public_kol_2/status/0000000000000000000",
                    "author": {"screenName": "public_kol_2", "name": "fixture"},
                    "createdAtISO": datetime.now(SHANGHAI).astimezone(timezone.utc).isoformat(),
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            store.upsert_post(post)
            store.save_rule_classification(
                post.post_id,
                RuleResult(80, True, ["002414"], "long", ["fixture"], "original_pre_event", "recommendation"),
            )
            store.set_review(post.post_id, "ignored", "already handled")

            response = TestClient(app).get("/api/pipeline/status")

            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(0, response.json()["pending_ai"])
            self.assertEqual(0, response.json()["pending_human_review"])
            self.assertIn("queue_review_date", response.json())
            self.assertIn("next_preview", response.json())

    def test_recommendation_reprocess_materializes_historical_bracketed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            store = app.state.post_store
            market = app.state.market_store
            market.upsert_instrument(Instrument("002580", "圣阳股份", "stock", "SZ", source="fixture"))
            market.upsert_instrument(Instrument("002131", "利欧股份", "stock", "SZ", source="fixture"))
            kol_id, _ = store.add_kol("Fixture KOL", "fixture", "A股")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000055",
                    "text": "周一建仓计划【圣阳股份】【利欧股份】\n圣阳股份\n重点：算力 IDC 备电龙头。\n利欧股份\n重点：英伟达液冷泵供应商。",
                    "url": "https://x.com/fixture/status/2078000000000000055",
                    "author": {"screenName": "fixture", "name": "Fixture KOL"},
                    "createdAtISO": "2026-07-31T14:09:43+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
            )
            store.upsert_post(post)
            store.save_rule_classification(
                post.post_id,
                RuleClassifier({"002580": "圣阳股份", "002131": "利欧股份"}).classify(post),
            )

            client = TestClient(app)
            response = client.post(f"/api/posts/{post.post_id}/recommendation-reprocess")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({"002580", "002131"}, {item["symbol"] for item in response.json()["drafts"]})
            with store.connect() as db:
                db.execute(
                    "UPDATE recommendation_drafts SET review_date=? WHERE post_id=?",
                    ("2026-01-01", post.post_id),
                )
            morning = client.get(f"/api/morning-review?review_date={date.today().isoformat()}&include_history=true")
            self.assertEqual(200, morning.status_code, morning.text)
            self.assertIn(post.post_id, {item["post_id"] for item in morning.json()["history_drafts"]})

    def test_pipeline_status_degrades_when_market_database_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            with patch.object(
                app.state.market_store,
                "health",
                side_effect=OSError("Cannot open file: already open in another process"),
            ):
                response = TestClient(app).get("/api/pipeline/status")

            self.assertEqual(200, response.status_code, response.text)
            payload = response.json()
            self.assertEqual("market_locked", payload["market_status"])
            self.assertEqual([], payload["lagging_symbols"])

    @patch("kol_api._start_scheduled_task", return_value={"ok": True, "task": "KOL_Morning_Pipeline"})
    def test_operator_can_start_full_morning_pipeline(self, start_task) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))

            response = TestClient(app).post("/api/tasks/morning/start")

            self.assertEqual(202, response.status_code, response.text)
            self.assertEqual("KOL_Morning_Pipeline", response.json()["task"])
            start_task.assert_called_once_with("KOL_Morning_Pipeline")

    def test_morning_review_uses_nine_to_nine_delivery_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            client = TestClient(app)
            store = app.state.post_store
            kol = store.get_kol_by_handle("public_kol_2")
            values = [
                ("2078000000000000801", "2026-07-17T02:00:00+00:00"),
                ("2078000000000000802", "2026-07-18T00:00:00+00:00"),
                ("2078000000000000803", "2026-07-18T02:00:00+00:00"),
            ]
            for post_id, posted_at in values:
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": f"fixture {post_id}",
                        "url": f"https://x.com/public_kol_2/status/{post_id}",
                        "author": {"screenName": "public_kol_2", "name": "fixture"},
                        "createdAtISO": posted_at,
                        "media": [],
                        "isRetweet": False,
                    },
                    kol,
                )
                store.upsert_post(post)
                store.save_rule_classification(post.post_id, RuleClassifier().classify(post))

            response = client.get("/api/morning-review?review_date=2026-07-18")

            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(2, response.json()["summary"]["new_posts"])
            self.assertEqual(
                {values[0][0], values[1][0]},
                {item["post_id"] for item in response.json()["posts"]},
            )

    def test_manual_draft_and_revision_endpoints_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            store = app.state.post_store
            app.state.market_store.upsert_instrument(
                Instrument("002414", "高德红外", "stock", "SZ", source="fixture")
            )
            kol = store.get_kol_by_handle("public_kol_2")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000999",
                    "text": "今日补充关注 002414 高德红外。",
                    "url": "https://x.com/public_kol_2/status/0000000000000000000",
                    "author": {"screenName": "public_kol_2", "name": "fixture"},
                    "createdAtISO": "2026-07-18T00:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            store.upsert_post(post)
            store.save_rule_classification(
                post.post_id,
                RuleResult(80, True, ["002414"], "long", ["fixture"], "original_pre_event", "recommendation"),
            )
            payload = {
                "symbol": "002414",
                "security_name": "高德红外",
                "direction": "long",
                "action": "watch",
                "horizon": "short",
                "strength": "explicit",
                "thesis": "原帖明确补充关注高德红外。",
                "evidence_spans": ["今日补充关注 002414 高德红外。"],
                "conditions": [],
                "evidence_source": "text",
                "depends_on_ocr": False,
                "review_date": "2026-07-18",
                "correction_type": "missed_stock",
                "note": "AI 漏识别股票",
            }

            created = client.post(f"/api/posts/{post.post_id}/recommendation-drafts", json=payload)
            self.assertEqual(201, created.status_code, created.text)
            repeated = client.post(f"/api/posts/{post.post_id}/recommendation-drafts", json=payload)
            revisions = client.get(f"/api/recommendation-drafts/{created.json()['id']}/revisions")

            self.assertEqual(created.json()["id"], repeated.json()["id"])
            self.assertEqual("ready", created.json()["status"])
            self.assertEqual(1, len(revisions.json()))
            self.assertEqual("missed_stock", revisions.json()[0]["correction_type"])

    def test_event_amendment_endpoint_audits_and_queues_core_recalculation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            event = EventRecord(
                event_id="KOL-T900",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/2078000000000000900",
                source_note="post:2078000000000000900",
                posted_at="2026-07-17T08:30:00+08:00",
                symbol="002414",
                security_name="高德红外",
                direction="long",
                thesis="原理由",
                status="active",
            )
            self.assertTrue(app.state.event_store.register_event(event))

            metadata = client.post(
                f"/api/events/{event.event_id}/amendments",
                json={"reason": "补充原帖理由", "thesis": "修正后的推荐理由"},
            )
            with patch("kol_api.run_post_approval_refresh") as refresh:
                core = client.post(
                    f"/api/events/{event.event_id}/amendments",
                    json={"reason": "修正股票映射", "symbol": "600900", "security_name": "长江电力"},
                )
            revisions = client.get(f"/api/events/{event.event_id}/revisions")

            self.assertEqual(200, metadata.status_code, metadata.text)
            self.assertEqual("not_required", metadata.json()["refresh_status"])
            self.assertEqual(200, core.status_code, core.text)
            self.assertEqual("queued", core.json()["refresh_status"])
            self.assertTrue(core.json()["revision"]["recalculation_required"])
            self.assertEqual(1, refresh.call_count)
            self.assertEqual(2, len(revisions.json()))

    def test_event_dossier_is_explicit_about_missing_sections_and_refreshes_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            event = EventRecord(
                event_id="KOL-DOSSIER-1",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/2078000000000000990",
                source_note="post:2078000000000000990",
                posted_at="2026-07-17T08:30:00+08:00",
                symbol="002414",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            app.state.event_store.register_event(event)
            client = TestClient(app)

            dossier = client.get(f"/api/events/{event.event_id}/dossier")
            research = client.get(f"/api/events/{event.event_id}/research")
            research_history = client.get(
                f"/api/events/{event.event_id}/research/history"
            )
            research_refresh = client.post(
                f"/api/events/{event.event_id}/research/refresh"
            )
            refreshed = client.post(f"/api/events/{event.event_id}/data-refresh")
            exported = client.get(f"/api/events/{event.event_id}/dossier/export")

            self.assertEqual(200, dossier.status_code, dossier.text)
            self.assertIn("recommendation", dossier.json()["sections"])
            self.assertNotIn("method_research", dossier.json()["sections"])
            self.assertEqual(200, research.status_code, research.text)
            self.assertIn("short_term_leader", research.json()["data"]["lenses"])
            self.assertEqual(200, research_history.status_code, research_history.text)
            self.assertEqual(202, research_refresh.status_code, research_refresh.text)
            self.assertTrue(research_refresh.json()["snapshot_id"])
            self.assertEqual("unavailable", dossier.json()["sections"]["event_market"]["status"])
            self.assertEqual(202, refreshed.status_code, refreshed.text)
            self.assertTrue(refreshed.json()["ok"])
            self.assertEqual(200, exported.status_code, exported.text)
            self.assertEqual(1, len(exported.json()["snapshots"]))

    def test_legacy_evidence_partial_preserves_metadata_without_inventing_post_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            event = EventRecord(
                event_id="KOL-LEGACY",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/2078000000000001990",
                source_note="post:2078000000000001990",
                posted_at="2026-07-17T08:30:00+08:00",
                symbol="002414",
                security_name="fixture",
                direction="long",
                thesis="human-confirmed legacy thesis",
                status="active",
                execution_warning="legacy_evidence_partial",
            )
            app.state.event_store.register_event(event)

            response = TestClient(app).get(f"/api/events/{event.event_id}/dossier")

            self.assertEqual(200, response.status_code, response.text)
            evidence = response.json()["sections"]["evidence"]
            self.assertEqual("partial", evidence["status"])
            self.assertEqual(event.source_url, evidence["legacy_metadata"]["source_url"])
            self.assertNotIn("text", evidence["legacy_metadata"])
            self.assertIn("source_post_snapshot_missing", evidence["warnings"])

    def test_event_series_preserves_open_date_gaps_without_fake_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            event = EventRecord(
                event_id="KOL-SERIES",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/2078000000000002990",
                source_note="post:2078000000000002990",
                posted_at="2026-07-20T08:30:00+08:00",
                symbol="002414",
                security_name="fixture",
                direction="long",
                thesis="fixture",
                status="active",
                baseline_date="2026-07-20",
                baseline_price_raw="10.00",
            )
            app.state.event_store.register_event(event)
            app.state.market_store.replace_calendar(
                [
                    date(2026, 7, 20),
                    date(2026, 7, 21),
                    date(2026, 7, 22),
                ],
                provider="fixture",
            )
            app.state.event_store.upsert_marks(
                [
                    {
                        "event_id": event.event_id,
                        "trade_date": "2026-07-20",
                        "close_raw": "10.00",
                        "directional_return": "0.00000000",
                        "tracking_days": "0",
                        "data_status": "ok",
                    },
                    {
                        "event_id": event.event_id,
                        "trade_date": "2026-07-21",
                        "close_raw": "10.50",
                        "directional_return": "0.05000000",
                        "tracking_days": "1",
                        "data_status": "ok",
                    },
                ]
            )

            response = TestClient(app).get(
                f"/api/events/{event.event_id}/series?range=all"
            )

            self.assertEqual(200, response.status_code, response.text)
            rows = response.json()
            self.assertEqual(3, len(rows))
            self.assertEqual("suspended_or_missing", rows[-1]["data_status"])
            self.assertEqual("", rows[-1]["close_raw"])
            self.assertEqual("2026-07-22", rows[-1]["trade_date"])

    def test_event_dossier_auxiliary_data_is_point_in_time_and_snapshots_include_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            event = EventRecord(
                event_id="KOL-DOSSIER-PTI",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/2078000000000000991",
                source_note="post:2078000000000000991",
                posted_at="2026-07-17T08:30:00+08:00",
                symbol="002414",
                security_name="fixture",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            app.state.event_store.register_event(event)
            timestamp = "2026-07-24T09:00:00+08:00"
            with app.state.market_store.connect() as db:
                db.execute(
                    "INSERT INTO board_catalog VALUES (?,?,?,?,?,?,?,?,?)",
                    ["industry:BKPTI", "BKPTI", "point in time board", "industry", "active", "fixture", timestamp, timestamp, timestamp],
                )
                db.executemany(
                    "INSERT INTO board_memberships VALUES (?,?,?,?,?,?)",
                    [
                        ["industry:BKPTI", event.symbol, "fixture", "2026-07-16", "fixture", timestamp],
                        ["industry:BKPTI", event.symbol, "fixture", "2026-07-17", "fixture", timestamp],
                    ],
                )
                db.executemany(
                    "INSERT INTO board_rps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        ["industry:BKPTI", "2026-07-16", 0.1, 0.2, 0.3, 70, 65, 60, 0.5, 1.0, "neutral", "board-rps-v1", 10, 10, 10, 1.0, "[]", "old", timestamp],
                        ["industry:BKPTI", "2026-07-17", 0.2, 0.3, 0.4, 80, 75, 70, 0.5, 1.0, "neutral", "board-rps-v1", 10, 10, 10, 1.0, "[]", "new", timestamp],
                    ],
                )
            daily = pd.DataFrame([
                {"trade_date": "2026-07-15", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
                {"trade_date": "2026-07-20", "open": 12, "high": 13, "low": 11, "close": 12.5, "volume": 120},
            ])
            for adjustment in ("raw", "qfq"):
                daily_directory = app.state.market_store.warehouse_root / "daily" / event.symbol / adjustment
                daily_directory.mkdir(parents=True, exist_ok=True)
                daily.to_parquet(daily_directory / "2026.parquet", index=False)
            directory = app.state.market_store.warehouse_root / "financial_summary" / event.symbol
            directory.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([
                {"snapshot_date": "2026-07-15", "source": "before", "net_profit": 10},
            ]).to_parquet(directory / "2026-07-15.parquet", index=False)
            pd.DataFrame([
                {"snapshot_date": "2026-07-20", "source": "after", "net_profit": 99},
            ]).to_parquet(directory / "2026-07-20.parquet", index=False)

            client = TestClient(app)
            first = client.get(f"/api/events/{event.event_id}/dossier")
            self.assertEqual(200, first.status_code, first.text)
            financial = first.json()["sections"]["fundamentals"]["datasets"]["financial_summary"]
            self.assertEqual(1, financial["row_count"])
            self.assertEqual("before", financial["rows"][0]["source"])
            self.assertEqual("2026-07-16", first.json()["sections"]["fundamentals"]["cutoff_date"])
            board_rows = first.json()["sections"]["board_context"]["rows"]
            self.assertEqual("2026-07-16", board_rows[0]["snapshot_date"])
            self.assertEqual("2026-07-16", board_rows[0]["trade_date"])
            point_in_time_series = client.get(
                f"/api/events/{event.event_id}/market-series?adjustment=raw"
            )
            self.assertEqual(200, point_in_time_series.status_code, point_in_time_series.text)
            self.assertEqual("2026-07-15", point_in_time_series.json()[-1]["trade_date"])
            explicit_performance_series = client.get(
                f"/api/events/{event.event_id}/market-series?adjustment=raw&end=2026-07-20"
            )
            self.assertEqual(200, explicit_performance_series.status_code, explicit_performance_series.text)
            self.assertEqual("2026-07-20", explicit_performance_series.json()[-1]["trade_date"])

            first_refresh = client.post(f"/api/events/{event.event_id}/data-refresh")
            self.assertEqual(202, first_refresh.status_code, first_refresh.text)
            pd.DataFrame([
                {"snapshot_date": "2026-07-14", "source": "older", "net_profit": 8},
            ]).to_parquet(directory / "2026-07-14.parquet", index=False)
            second_refresh = client.post(f"/api/events/{event.event_id}/data-refresh")
            self.assertEqual(202, second_refresh.status_code, second_refresh.text)
            exported = client.get(f"/api/events/{event.event_id}/dossier/export")
            self.assertEqual(200, exported.status_code, exported.text)
            self.assertEqual(2, len(exported.json()["snapshots"]))

    def test_stock_mentions_are_server_paginated_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            store = app.state.post_store
            kol = store.get_kol_by_handle("public_kol_2")
            timestamp = "2026-07-18T08:00:00+08:00"
            with store.connect() as db:
                posts = []
                classifications = []
                leads = []
                for index in range(1500):
                    post_id = str(3000000000000000000 + index)
                    symbol = "002414" if index % 2 == 0 else "600000"
                    posts.append((
                        post_id, kol["id"], "X", kol["handle"], kol["display_name"],
                        f"https://x.com/{kol['handle']}/status/{post_id}", f"证据 {symbol} 第{index}条",
                        timestamp, timestamp, "original", "{}", "hash", timestamp, timestamp,
                    ))
                    classifications.append((post_id, timestamp))
                    leads.append((post_id, kol["id"], symbol, "高德红外" if symbol == "002414" else "浦发银行", f"证据 {symbol}", timestamp, timestamp))
                db.executemany(
                    """
                    INSERT INTO posts(
                        post_id,kol_id,platform,handle,author_name,url,text,posted_at,posted_at_utc,
                        post_type,raw_json,content_hash,fetched_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    posts,
                )
                db.executemany(
                    "INSERT INTO classifications(post_id,updated_at) VALUES(?,?)",
                    classifications,
                )
                db.executemany(
                    """
                    INSERT INTO stock_leads(
                        post_id,kol_id,symbol,security_name,evidence_text,extraction_method,
                        first_seen_at,updated_at
                    ) VALUES(?,?,?,?,?,'fixture',?,?)
                    """,
                    leads,
                )

            first = client.get("/api/stock-mentions?page=1&page_size=50")
            filtered = client.get("/api/stock-mentions?q=002414&page=2&page_size=50")

            self.assertEqual(200, first.status_code, first.text)
            self.assertEqual(1500, first.json()["total"])
            self.assertEqual(50, len(first.json()["items"]))
            self.assertEqual(30, first.json()["total_pages"])
            self.assertEqual(750, filtered.json()["total"])
            self.assertEqual(2, filtered.json()["page"])

    def test_morning_review_approves_each_stock_without_stock_lead_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            post_store = app.state.post_store
            market_store = app.state.market_store
            instruments = [
                Instrument("605178", "Space-Time Technology", "stock", "SH", source="fixture"),
                Instrument("002303", "Meiyingsen", "stock", "SZ", source="fixture"),
                Instrument("000938", "Unisplendour", "stock", "SZ", source="fixture"),
            ]
            market_store.upsert_instruments(instruments)
            kol = post_store.get_kol_by_handle("public_kol_2")
            text = "Morning picks: 605178, 002303, 000938. No individual thesis was provided."
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000123",
                    "text": text,
                    "url": "https://x.com/public_kol_2/status/0000000000000000000",
                    "author": {"screenName": "public_kol_2", "name": "fixture"},
                    "createdAtISO": "2026-07-17T00:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            post_store.upsert_post(post)
            post_store.save_rule_classification(
                post.post_id,
                RuleResult(90, True, [item.symbol for item in instruments], "long", ["fixture"], "original_pre_event", "recommendation"),
            )
            post_store.save_model_classification(
                post.post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "original_pre_event",
                    "confidence": 0.99,
                    "summary": "Three morning picks.",
                    "drafts": [
                        {
                            "symbol": item.symbol,
                            "security_name": item.name,
                            "direction": "long",
                            "thesis": "Listed as a morning pick; no stock-specific thesis was provided.",
                            "evidence_type": "original_pre_event",
                            "confidence": 0.99,
                            "evidence_spans": [text],
                            "evidence_source": "text",
                            "conditions": [],
                            "depends_on_ocr": False,
                            "mention_kind": "recommendation",
                        }
                        for item in instruments
                    ],
                },
                model_name="fixture",
                prompt_version="morning-v1",
            )
            RecommendationDraftRepository(post_store).sync_post(
                post.post_id,
                market_store.instrument_map(),
                queue_scope="morning",
                review_date="2026-07-17",
            )

            morning = client.get("/api/morning-review?review_date=2026-07-17")

            self.assertEqual(200, morning.status_code, morning.text)
            self.assertEqual(3, morning.json()["summary"]["waiting_review"])
            self.assertEqual([post.post_id], [item["post_id"] for item in morning.json()["posts"]])
            self.assertEqual([], morning.json()["approved_drafts"])
            drafts = morning.json()["drafts"]
            first, second, third = drafts
            edited = client.patch(
                f"/api/recommendation-drafts/{first['id']}",
                json={"thesis": "Edited human-reviewed thesis.", "note": "clarified"},
            )
            self.assertEqual(200, edited.status_code, edited.text)

            with patch("kol_api.run_post_approval_refresh") as refresh:
                approved_first = client.post(
                    f"/api/recommendation-drafts/{first['id']}/approve",
                    json={"note": "approve first"},
                )
                approved_second = client.post(
                    f"/api/recommendation-drafts/{second['id']}/approve",
                    json={"note": "approve second"},
                )
                repeated = client.post(
                    f"/api/recommendation-drafts/{first['id']}/approve",
                    json={"note": "duplicate click"},
                )
                self.assertEqual(2, refresh.call_count)
            rejected = client.post(
                f"/api/recommendation-drafts/{third['id']}/reject",
                json={"note": "exclude this stock"},
            )

            self.assertEqual(200, approved_first.status_code, approved_first.text)
            self.assertEqual(200, approved_second.status_code, approved_second.text)
            self.assertEqual(200, repeated.status_code, repeated.text)
            self.assertEqual(200, rejected.status_code, rejected.text)
            self.assertEqual(2, len(app.state.event_store.load_events()))
            self.assertEqual(
                [approved_first.json()["event_id"], approved_second.json()["event_id"]],
                [item.event_id for item in app.state.event_store.load_events()],
            )
            self.assertEqual([], post_store.list_stock_leads(post_id=post.post_id))
            self.assertEqual("approved", post_store.get_post(post.post_id)["review_status"])
            self.assertEqual("tracking", market_store.get_instrument(first["symbol"])["lifecycle"])
            self.assertEqual("rejected", rejected.json()["status"])

    def test_review_agent_api_defaults_to_shadow_and_records_human_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            store = app.state.post_store
            kol = store.get_kol_by_handle("public_kol_2")
            post = normalise_twitter_post(
                {
                    "id": "2076000000000000888",
                    "text": "普通市场观察",
                    "url": "https://x.com/public_kol_2/status/0000000000000000000",
                    "author": {"screenName": "public_kol_2", "name": "fixture"},
                    "createdAtISO": "2026-07-15T02:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            store.upsert_post(post)
            repository = app.state.review_agent
            run_id = repository.start_run("shadow")
            decision = repository.save_decision(
                store.get_post(post.post_id),
                run_id,
                "shadow",
                PolicyDecision("auto_ignore", 0.99, ["high_confidence_other"], [], [], []),
            )

            summary = client.get("/api/review-agent/summary")
            blocked = client.patch("/api/review-agent/settings", json={"mode": "enabled"})
            values = client.get("/api/review-agent/decisions")
            ignored = client.post(
                f"/api/posts/{post.post_id}/review",
                json={"action": "ignore", "note": "人工确认", "drafts": []},
            )

            self.assertEqual("shadow", summary.json()["settings"]["mode"])
            self.assertEqual(409, blocked.status_code)
            self.assertEqual(decision["id"], values.json()[0]["id"])
            self.assertEqual(200, ignored.status_code)
            self.assertEqual("overridden", repository.get_decision(decision["id"])["status"])

    def test_unified_review_confirms_lead_registers_event_and_queues_market_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            post_store = app.state.post_store
            market_store = app.state.market_store
            market_store.upsert_instrument(
                Instrument("002414", "Gaode Infrared", "stock", "SZ", lifecycle="archived", source="fixture")
            )
            kol = post_store.get_kol_by_handle("public_kol_2")
            post = normalise_twitter_post(
                {
                    "id": "2077000000000000999",
                    "text": "002414 long thesis",
                    "url": "https://x.com/public_kol_2/status/0000000000000000000",
                    "author": {"screenName": "public_kol_2", "name": "fixture"},
                    "metrics": {},
                    "createdAtISO": "2026-07-14T08:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            post_store.upsert_post(post)
            post_store.save_rule_classification(
                post.post_id,
                RuleResult(80, True, ["002414"], "long", ["stock_code"], "original_pre_event", "recommendation"),
            )

            response = client.post(
                f"/api/posts/{post.post_id}/review",
                json={
                    "action": "approve",
                    "note": "fixture review",
                    "confirm_leads": True,
                    "refresh_returns": False,
                    "drafts": [{
                        "symbol": "002414",
                        "security_name": "Gaode Infrared",
                        "direction": "long",
                        "thesis": "earnings expectation",
                        "evidence_type": "original_pre_event",
                        "confidence": 0.9,
                    }],
                },
            )

            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(["002414"], response.json()["queued_symbols"])
            self.assertEqual("approved", post_store.get_post(post.post_id)["review_status"])
            self.assertEqual("confirmed", post_store.list_stock_leads(post_id=post.post_id)[0]["status"])
            self.assertEqual("pinned", market_store.get_instrument("002414")["lifecycle"])
            self.assertEqual(1, len(app.state.event_store.load_events()))

    def test_event_timeline_links_retrospective_quote_without_creating_an_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            post_store = app.state.post_store
            event_store = app.state.event_store
            kol_id, _ = post_store.add_kol(
                "Public KOL 10",
                "public_kol_10",
                "A股技术复盘",
                status="paused",
            )
            kol = post_store.get_kol(kol_id)
            original = normalise_twitter_post(
                {
                    "id": "2077011102911852883",
                    "text": "明日参考：603127 昭衍新药。逻辑：医疗服务+创新药。",
                    "url": "https://x.com/public_kol_10/status/0000000000000000000",
                    "author": {"screenName": "public_kol_10", "name": "Public KOL 10"},
                    "metrics": {},
                    "createdAtISO": "2026-07-14T12:43:30+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
                provider="vxtwitter",
            )
            retrospective = normalise_twitter_post(
                {
                    "id": "2077210207818764707",
                    "text": "涨停，逻辑预判正确。大资金都去医药了。",
                    "url": "https://x.com/public_kol_10/status/0000000000000000000",
                    "author": {"screenName": "public_kol_10", "name": "Public KOL 10"},
                    "quotedTweet": {
                        "id": original.post_id,
                        "text": original.text,
                        "author": {"screenName": "public_kol_10"},
                    },
                    "metrics": {},
                    "createdAtISO": "2026-07-15T01:54:40+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
                provider="vxtwitter",
            )
            pending_retrospective = normalise_twitter_post(
                {
                    "id": "2077210207818764708",
                    "text": "尚未人工审核的复盘。",
                    "url": "https://x.com/public_kol_10/status/0000000000000000000",
                    "author": {"screenName": "public_kol_10", "name": "Public KOL 10"},
                    "quotedTweet": {
                        "id": original.post_id,
                        "text": original.text,
                        "author": {"screenName": "public_kol_10"},
                    },
                    "metrics": {},
                    "createdAtISO": "2026-07-15T02:54:40+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
                provider="vxtwitter",
            )
            for post in (original, retrospective, pending_retrospective):
                post_store.upsert_post(post)
            post_store.save_rule_classification(
                original.post_id,
                RuleResult(80, True, ["603127"], "long", ["stock_code"], "original_pre_event", "recommendation"),
            )
            post_store.save_rule_classification(
                retrospective.post_id,
                RuleResult(60, True, ["603127"], "long", ["retrospective"], "retrospective", "recommendation"),
            )
            post_store.save_rule_classification(
                pending_retrospective.post_id,
                RuleResult(60, True, ["603127"], "long", ["retrospective"], "retrospective", "recommendation"),
            )
            post_store.set_review(retrospective.post_id, "excluded", "涨后复盘，不重复注册事件")
            event = EventRecord(
                event_id="KOL-T001",
                kol_name="Public KOL 10",
                platform="X",
                source_url=original.url,
                source_note=f"post:{original.post_id}",
                posted_at=original.posted_at,
                symbol="603127",
                security_name="昭衍新药",
                direction="long",
                thesis="医疗服务+创新药；满足盘中条件后回踩五日线低吸。",
                status="active",
                execution_warning="conditional_intraday_entry_unverified",
            )
            self.assertTrue(event_store.register_event(event))
            self.assertFalse(event_store.register_event(event))

            response = client.get("/api/events")

            self.assertEqual(200, response.status_code)
            saved = next(item for item in response.json() if item["event_id"] == event.event_id)
            self.assertEqual(1, len(saved["followups"]))
            self.assertEqual(retrospective.post_id, saved["followups"][0]["post_id"])
            self.assertEqual("retrospective", saved["followups"][0]["evidence_type"])
            self.assertEqual("paused", post_store.get_kol(kol_id)["status"])
            self.assertEqual(
                1,
                sum(
                    item.symbol == "603127" and item.source_url == original.url
                    for item in event_store.load_events()
                ),
            )

    def test_market_daily_endpoint_filters_the_history_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            store = app.state.market_store
            store.upsert_instrument(
                Instrument("002414", "高德红外", "stock", "SZ", lifecycle="tracking", source="fixture")
            )
            store.write_daily(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2026-05-29", "2026-06-01", "2026-06-02"]).date,
                        "open": [10.0, 10.2, 10.4],
                        "high": [10.3, 10.5, 10.7],
                        "low": [9.9, 10.1, 10.3],
                        "close": [10.2, 10.4, 10.6],
                    }
                ),
                symbol="002414",
                adjustment="raw",
            )

            response = client.get(
                "/api/market/instruments/002414/daily?adjustment=raw&start=2026-06-01&end=2026-06-01"
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual(["2026-06-01"], [row["trade_date"] for row in response.json()])

    def test_market_indicator_endpoint_returns_versioned_qfq_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            store = app.state.market_store
            dates = pd.bdate_range("2026-01-05", periods=80)
            store.write_daily(
                pd.DataFrame(
                    {
                        "symbol": ["002414"] * 80,
                        "trade_date": dates.date,
                        "open": [10 + index * 0.1 for index in range(80)],
                        "high": [10.2 + index * 0.1 for index in range(80)],
                        "low": [9.8 + index * 0.1 for index in range(80)],
                        "close": [10.1 + index * 0.1 for index in range(80)],
                        "volume": [1000 + index * 10 for index in range(80)],
                        "amount": [10000 + index * 100 for index in range(80)],
                        "adjustment": ["qfq"] * 80,
                        "provider": ["fixture"] * 80,
                    }
                ),
                symbol="002414",
                adjustment="qfq",
            )

            response = TestClient(app).get(
                "/api/market/daily/002414/indicators?start=2026-03-01"
            )

            self.assertEqual(200, response.status_code, response.text)
            payload = response.json()
            self.assertEqual("daily-technical-v1", payload["formula_version"])
            self.assertEqual("qfq", payload["adjustment"])
            self.assertTrue(payload["rows"])
            self.assertIn("ma60", payload["rows"][-1])
            self.assertIn("macd_hist", payload["rows"][-1])
            self.assertIn("rsi14", payload["rows"][-1])

    def test_events_include_current_technical_context_and_feature_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            client = TestClient(app)
            dates = pd.bdate_range("2026-03-02", periods=80)
            prices = pd.DataFrame(
                {
                    "trade_date": dates.date,
                    "open": [10 + index * 0.1 for index in range(80)],
                    "high": [10.2 + index * 0.1 for index in range(80)],
                    "low": [9.8 + index * 0.1 for index in range(80)],
                    "close": [10.1 + index * 0.1 for index in range(80)],
                    "volume": [1000 + index * 10 for index in range(80)],
                }
            )
            posted_at = f"{dates[-1].date().isoformat()}T16:00:00+08:00"
            event = EventRecord(
                event_id="KOL-CONTEXT-1",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/10001",
                source_note="post:10001",
                posted_at=posted_at,
                symbol="600900",
                security_name="fixture stock",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            app.state.event_store.register_event(event)
            context = compute_event_technical_context(
                event_id=event.event_id,
                symbol=event.symbol,
                posted_at=event.posted_at,
                qfq_prices=prices,
            )
            app.state.market_store.save_event_technical_context(context.to_record())

            events_response = client.get("/api/events")
            feature_response = client.get(f"/api/events/{event.event_id}/features")

            self.assertEqual(200, events_response.status_code, events_response.text)
            saved_event = next(item for item in events_response.json() if item["event_id"] == event.event_id)
            self.assertEqual("complete", saved_event["technical_context"]["status"])
            self.assertEqual(dates[-1].date().isoformat(), saved_event["technical_context"]["as_of_trade_date"])
            self.assertEqual(200, feature_response.status_code, feature_response.text)
            self.assertEqual("technical-context-v1", feature_response.json()["feature_version"])

    def test_missing_event_context_is_explicitly_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            event = EventRecord(
                event_id="KOL-CONTEXT-2",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/10002",
                source_note="post:10002",
                posted_at="2026-07-17T10:00:00+08:00",
                symbol="600900",
                security_name="fixture stock",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            app.state.event_store.register_event(event)

            response = TestClient(app).get(f"/api/events/{event.event_id}/features")

            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("pending", response.json()["status"])
            self.assertIsNone(response.json()["rsi14"])

    def test_event_context_api_preserves_partial_and_failed_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            client = TestClient(app)
            event_store = app.state.event_store
            market_store = app.state.market_store
            dates = pd.bdate_range("2026-07-01", periods=10)
            prices = pd.DataFrame(
                {
                    "trade_date": dates.date,
                    "open": [10 + index * 0.1 for index in range(10)],
                    "high": [10.2 + index * 0.1 for index in range(10)],
                    "low": [9.8 + index * 0.1 for index in range(10)],
                    "close": [10.1 + index * 0.1 for index in range(10)],
                    "volume": [1000 + index * 10 for index in range(10)],
                }
            )
            partial_event = EventRecord(
                event_id="KOL-CONTEXT-PARTIAL",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/10003",
                source_note="post:10003",
                posted_at=f"{dates[-1].date().isoformat()}T16:00:00+08:00",
                symbol="600900",
                security_name="fixture stock",
                direction="long",
                thesis="fixture thesis",
                status="active",
            )
            failed_event = EventRecord(
                event_id="KOL-CONTEXT-FAILED",
                kol_name="fixture",
                platform="X",
                source_url="https://x.com/fixture/status/10004",
                source_note="post:10004",
                posted_at="2026-07-17T16:00:00+08:00",
                symbol="600519",
                security_name="fixture stock 2",
                direction="long",
                thesis="fixture thesis 2",
                status="active",
            )
            event_store.register_event(partial_event)
            event_store.register_event(failed_event)
            partial = compute_event_technical_context(
                event_id=partial_event.event_id,
                symbol=partial_event.symbol,
                posted_at=partial_event.posted_at,
                qfq_prices=prices,
            )
            failed = failed_event_technical_context(
                event_id=failed_event.event_id,
                symbol=failed_event.symbol,
                posted_at=failed_event.posted_at,
                error="fixture calculation failure",
            )
            market_store.save_event_technical_context(partial.to_record())
            market_store.save_event_technical_context(failed.to_record())

            partial_response = client.get(f"/api/events/{partial_event.event_id}/features")
            failed_response = client.get(f"/api/events/{failed_event.event_id}/features")

            self.assertEqual("partial", partial_response.json()["status"])
            self.assertIsNone(partial_response.json()["rsi14"])
            self.assertEqual("failed", failed_response.json()["status"])
            self.assertEqual("fixture calculation failure", failed_response.json()["error"])

    def test_stock_lead_extraction_and_confirmation_queue_market_data_without_approving_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            post_store = app.state.post_store
            market_store = app.state.market_store
            market_store.upsert_instrument(
                Instrument("002414", "高德红外", "stock", "SZ", lifecycle="tracking", source="fixture")
            )
            kol = post_store.get_kol_by_handle("public_kol_2")
            payload = {
                "id": "2077000000000000101",
                "text": "关注 002414 高德红外，继续看多。",
                "url": "https://x.com/public_kol_2/status/0000000000000000000",
                "author": {"screenName": "public_kol_2", "name": "林哥"},
                "metrics": {},
                "createdAtISO": "2026-07-14T08:30:00+00:00",
                "media": [],
                "isRetweet": False,
            }
            post = normalise_twitter_post(payload, kol)
            post_store.upsert_post(post)
            post_store.save_rule_classification(post.post_id, RuleClassifier().classify(post))

            extracted = client.post("/api/stock-leads/extract")
            leads = client.get("/api/stock-leads").json()
            queued = market_store.pending_sync()

            self.assertEqual(200, extracted.status_code)
            self.assertEqual(1, len(leads))
            self.assertEqual("confirmed", leads[0]["status"])
            self.assertEqual("pending", post_store.get_post(post.post_id)["review_status"])
            self.assertEqual("002414", queued[0]["symbol"])

    def test_pending_stock_lead_can_be_corrected_and_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)
            post_store = app.state.post_store
            app.state.market_store.upsert_instrument(
                Instrument("002414", "高德红外", "stock", "SZ", lifecycle="archived", source="fixture_master")
            )
            kol = post_store.get_kol_by_handle("public_kol_2")
            payload = {
                "id": "2077000000000000102",
                "text": "高德红外值得继续研究。",
                "url": "https://x.com/public_kol_2/status/0000000000000000000",
                "author": {"screenName": "public_kol_2", "name": "林哥"},
                "metrics": {},
                "createdAtISO": "2026-07-14T08:30:00+00:00",
                "media": [],
                "isRetweet": False,
            }
            post = normalise_twitter_post(payload, kol)
            post_store.upsert_post(post)
            post_store.save_rule_classification(
                post.post_id, RuleClassifier({"002414": "高德红外"}).classify(post)
            )
            client.post("/api/stock-leads/extract")
            lead = client.get("/api/stock-leads").json()[0]

            response = client.post(
                f"/api/stock-leads/{lead['id']}/review",
                json={
                    "action": "confirmed",
                    "note": "人工核对公司名称",
                    "symbol": "002414",
                    "security_name": "高德红外",
                },
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual("confirmed", response.json()["status"])
            self.assertEqual("tracking", app.state.market_store.get_instrument("002414")["lifecycle"])
            self.assertEqual(1, len(app.state.market_store.pending_sync()))

            rejected = client.post(
                f"/api/stock-leads/{lead['id']}/review",
                json={"action": "confirmed", "symbol": "777777", "security_name": "不存在"},
            )
            self.assertEqual(422, rejected.status_code)
            self.assertIn("不能直接改代码", rejected.json()["detail"])

            app.state.market_store.restore_research_state(
                "002414", lifecycle="archived", last_mentioned_at=""
            )
            post_store.review_stock_lead(
                lead["id"], "pending", "准备测试补偿", symbol="002414", security_name="高德红外"
            )
            with patch.object(
                app.state.market_store,
                "enqueue_sync",
                side_effect=RuntimeError("queue unavailable"),
            ):
                failed_queue = client.post(
                    f"/api/stock-leads/{lead['id']}/review",
                    json={"action": "confirmed", "symbol": "002414", "security_name": "高德红外"},
                )
            self.assertEqual(502, failed_queue.status_code)
            self.assertEqual("pending", post_store.get_stock_lead(lead["id"])["status"])
            self.assertEqual("archived", app.state.market_store.get_instrument("002414")["lifecycle"])

    def test_credential_endpoint_returns_actionable_validation_error(self) -> None:
        class RecordingCredentials:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def save(self, auth_token: str, ct0: str) -> None:
                self.calls.append((auth_token, ct0))

            def configured(self) -> bool:
                return False

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            credentials = RecordingCredentials()
            app = create_app(settings, credential_store=credentials)
            client = TestClient(app)

            response = client.post(
                "/api/system/twitter-credentials",
                json={"auth_token": "auth_token=" + "a" * 40, "ct0": "c" * 160},
            )

            self.assertEqual(422, response.status_code)
            self.assertIn("单个 Cookie 值", response.json()["detail"])
            self.assertEqual([], credentials.calls)

    def test_nitter_credentials_use_a_separate_store(self) -> None:
        class RecordingCredentials:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def save(self, auth_token: str, ct0: str) -> None:
                self.calls.append((auth_token, ct0))

            def configured(self) -> bool:
                return bool(self.calls)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            primary = RecordingCredentials()
            backup = RecordingCredentials()
            app = create_app(
                settings,
                credential_store=primary,
                nitter_credential_store=backup,
            )
            client = TestClient(app)

            response = client.post(
                "/api/system/nitter-credentials",
                json={"auth_token": "a" * 40, "ct0": "c" * 64},
            )

            self.assertEqual(200, response.status_code)
            self.assertEqual([], primary.calls)
            self.assertEqual([("a" * 40, "c" * 64)], backup.calls)

    def test_summary_kol_management_and_review_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = ApiSettings(
                runtime_root=root / "runtime",
                frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            )
            app = create_app(settings)
            client = TestClient(app)

            summary = client.get("/api/summary")
            self.assertEqual(200, summary.status_code)
            self.assertEqual(6, summary.json()["active_kols"])
            leaderboard = client.get("/api/kol-leaderboard")
            self.assertEqual(200, leaderboard.status_code)
            self.assertEqual(6, len(leaderboard.json()["rows"]))
            self.assertTrue(all(row["tier"] == "collecting" for row in leaderboard.json()["rows"]))
            self.assertEqual(400, client.get("/api/summary", headers={"Host": "attacker.example"}).status_code)
            shadow_nitter = client.post(
                "/api/fetch",
                json={"provider": "nitter", "max_count": 1, "classify": False},
            )
            self.assertEqual(409, shadow_nitter.status_code)
            self.assertIn("shadow-only", shadow_nitter.json()["detail"])

            created = client.post(
                "/api/kols",
                json={"display_name": "新增KOL", "handle": "new_kol", "domain": "A股"},
            )
            self.assertEqual(201, created.status_code)
            kol_id = created.json()["id"]
            paused = client.patch(f"/api/kols/{kol_id}", json={"status": "paused"})
            self.assertEqual("paused", paused.json()["status"])
            queued = client.post(f"/api/kols/{kol_id}/backfill", json={"count": 250})
            self.assertEqual(200, queued.status_code)
            self.assertEqual(250, queued.json()["backfill_requested"])
            self.assertEqual("queued", queued.json()["backfill_status"])

            store = app.state.post_store
            kol = store.get_kol_by_handle("public_kol_2")
            payload = {
                "id": "2076000000000000001",
                "text": "关注 002414 高德红外，继续看多。",
                "url": "https://x.com/public_kol_2/status/0000000000000000000",
                "author": {"screenName": "public_kol_2", "name": "林哥"},
                "metrics": {},
                "createdAtISO": "2026-07-13T08:30:00+00:00",
                "media": [],
                "isRetweet": False,
            }
            post = normalise_twitter_post(payload, kol)
            store.upsert_post(post)
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))

            queue = client.get("/api/posts", params={"candidate_only": True})
            self.assertEqual(1, len(queue.json()))
            ignored = client.post(
                f"/api/posts/{post.post_id}/review",
                json={"action": "ignore", "note": "仅用于接口测试", "drafts": []},
            )
            self.assertEqual(200, ignored.status_code)
            self.assertEqual("ignored", ignored.json()["review_status"])
            ignored_again = client.post(
                f"/api/posts/{post.post_id}/review",
                json={"action": "ignore", "note": "重复点击不应重复记账", "drafts": []},
            )
            self.assertEqual(200, ignored_again.status_code)
            with store.connect() as db:
                ignored_actions = db.execute(
                    "SELECT COUNT(*) FROM reviews WHERE post_id=? AND action='ignored'",
                    (post.post_id,),
                ).fetchone()[0]
            self.assertEqual(1, ignored_actions)

            store.set_review(post.post_id, "approved", "fixture", {"event_ids": ["KOL-9999"]})
            rejected = client.post(
                f"/api/posts/{post.post_id}/review",
                json={"action": "exclude", "note": "cannot silently deactivate", "drafts": []},
            )
            self.assertEqual(409, rejected.status_code)


if __name__ == "__main__":
    unittest.main()
