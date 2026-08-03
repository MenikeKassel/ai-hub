from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import KolPostStore, RuleClassifier, RuleResult, normalise_twitter_post  # noqa: E402
from recommendation_drafts import (  # noqa: E402
    RecommendationDraftRepository,
    _attention_reasons,
    _is_reviewable_recommendation_draft,
)
from recommendation_processing import materialize_recommendation_drafts  # noqa: E402


ALIASES = {
    "002414": "高德红外",
    "605178": "时空科技",
    "002303": "美盈森",
    "000938": "紫光股份",
    "600988": "赤峰黄金",
    "603893": "瑞芯微",
}


def add_mixed_post(store: KolPostStore) -> str:
    kol_id, _ = store.add_kol("示例KOL", "example", "A股")
    post = normalise_twitter_post(
        {
            "id": "2078000000000000001",
            "text": (
                "马前炮：7 月 17 日个股分享\n"
                "1.时空科技\n2.美盈森\n3.紫光股份\n"
                "昨日内部群推的两只票赤峰黄金和瑞芯微应声大涨，"
                "内部学习群已移至 Discord 平台，私聊狙哥 388 一个月。"
            ),
            "url": "https://x.com/example/status/2078000000000000001",
            "author": {"screenName": "example", "name": "示例KOL"},
            "createdAtISO": "2026-07-17T00:30:00+00:00",
            "media": [],
            "isRetweet": False,
        },
        {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
    )
    store.upsert_post(post)
    classifier = RuleClassifier(ALIASES)
    store.save_rule_classification(post.post_id, classifier.classify(post))
    payload = classifier.classify_structured_text(post)
    assert payload is not None
    store.save_model_classification(
        post.post_id,
        payload,
        model_name="structured-rules",
        prompt_version="structured-text-v1",
    )
    return post.post_id


class RecommendationDraftRepositoryTests(unittest.TestCase):
    def test_rules_only_marks_clear_non_recommendation_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("示例KOL", "example", "A股")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000098",
                    "text": "行业观察：算力板块继续震荡。",
                    "url": "https://x.com/example/status/2078000000000000098",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-31T14:09:43+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
            )
            store.upsert_post(post)
            repository = RecommendationDraftRepository(store)

            result = materialize_recommendation_drafts(
                store,
                repository,
                RuleClassifier({"002580": "圣阳股份"}),
                post.post_id,
                instruments={},
                queue_scope="backlog",
                review_date="2026-08-03",
            )

            self.assertEqual("not_applicable", result["draft_generation_status"])
            self.assertEqual([], repository.list_drafts(post_id=post.post_id))

    def test_retweet_recommendation_is_visible_but_not_approvable_as_direct_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("示例KOL", "example", "A股")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000097",
                    "text": "002580 圣阳股份，今日重点关注。",
                    "url": "https://x.com/example/status/2078000000000000097",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-31T14:09:43+00:00",
                    "media": [],
                    "isRetweet": True,
                },
                {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
            )
            store.upsert_post(post)
            store.save_rule_classification(
                post.post_id,
                RuleClassifier({"002580": "圣阳股份"}).classify(post),
            )
            store.save_model_classification(
                post.post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "original_pre_event",
                    "confidence": 0.99,
                    "drafts": [{
                        "symbol": "002580",
                        "security_name": "圣阳股份",
                        "direction": "long",
                        "action": "watch",
                        "horizon": "unspecified",
                        "strength": "explicit",
                        "thesis": "今日重点关注。",
                        "evidence_type": "original_pre_event",
                        "evidence_spans": ["002580 圣阳股份，今日重点关注。"],
                        "evidence_source": "text",
                        "conditions": [],
                        "depends_on_ocr": False,
                        "mention_kind": "recommendation",
                    }],
                },
                model_name="fixture",
                prompt_version="fixture-v1",
            )
            repository = RecommendationDraftRepository(store)
            repository.sync_post(
                post.post_id,
                {"002580": {"instrument_type": "stock", "status": "active"}},
                queue_scope="backlog",
                review_date="2026-08-03",
            )

            draft = repository.list_drafts(post_id=post.post_id)[0]
            self.assertEqual("needs_attention", draft["status"])
            self.assertIn("secondhand_source", draft["attention_reasons"])

    def test_deterministic_repair_materializes_bracketed_construction_plan_idempotently(self) -> None:
        aliases = {"002580": "圣阳股份", "002131": "利欧股份"}
        instruments = {
            symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
            for symbol, name in aliases.items()
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("示例KOL", "example", "A股")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000000099",
                    "text": (
                        "周一建仓计划【圣阳股份】【利欧股份】\n"
                        "圣阳股份\n重点：算力 IDC 备电龙头，海外储能订单落地。\n"
                        "利欧股份\n重点：英伟达、华为液冷泵核心供应商。"
                    ),
                    "url": "https://x.com/example/status/2078000000000000099",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-31T14:09:43+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
            )
            store.upsert_post(post)
            store.save_rule_classification(post.post_id, RuleClassifier(aliases).classify(post))
            repository = RecommendationDraftRepository(store)

            first = materialize_recommendation_drafts(
                store, repository, RuleClassifier(aliases), post.post_id,
                instruments=instruments, queue_scope="backlog", review_date="2026-08-03",
            )
            second = materialize_recommendation_drafts(
                store, repository, RuleClassifier(aliases), post.post_id,
                instruments=instruments, queue_scope="backlog", review_date="2026-08-03",
            )

            self.assertEqual("generated", first["draft_generation_status"])
            self.assertEqual(2, first["sync"]["created"])
            self.assertEqual(0, second["sync"]["created"])
            self.assertEqual({"002580", "002131"}, {item["symbol"] for item in second["drafts"]})

    def test_migration_creates_draft_audit_and_morning_run_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            with store.connect() as db:
                names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                        "('recommendation_drafts','recommendation_draft_reviews','draft_revisions','morning_runs')"
                    )
                }
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                draft_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(recommendation_drafts)").fetchall()
                }
                classification_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(classifications)").fetchall()
                }

            self.assertEqual(
                {"recommendation_drafts", "recommendation_draft_reviews", "draft_revisions", "morning_runs"},
                names,
            )
            self.assertTrue(
                {"evidence_source", "depends_on_ocr", "action", "horizon", "strength"}.issubset(draft_columns)
            )
            self.assertTrue(
                {"ocr_provider", "ocr_confidence", "ocr_details_json"}.issubset(classification_columns)
            )
            self.assertGreaterEqual(version, 17)

    def test_stale_running_morning_run_is_interrupted_without_touching_fresh_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            repository = RecommendationDraftRepository(store)
            with store.connect() as db:
                db.executemany(
                    """
                    INSERT INTO morning_runs(
                        run_id,review_date,window_start,window_end,started_at,status,phase,errors_json
                    ) VALUES(?,?,?,?,?,'running','initial','[]')
                    """,
                    [
                        ("stale", "2026-07-18", "", "", "2026-07-18T08:00:00+08:00"),
                        ("fresh", "2026-07-18", "", "", "2026-07-18T10:55:00+08:00"),
                    ],
                )

            interrupted = repository.interrupt_stale_runs(
                now=datetime(2026, 7, 18, 11, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
                max_age_minutes=70,
            )

            runs = {item["run_id"]: item for item in repository.recent_morning_runs()}
            self.assertEqual(1, interrupted)
            self.assertEqual("interrupted", runs["stale"]["status"])
            self.assertEqual("running", runs["fresh"]["status"])

    def test_final_morning_run_reports_on_time_delivery_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            repository = RecommendationDraftRepository(store)
            run_id = repository.start_morning_run(
                "2026-07-17",
                window_start="2026-07-16T09:00:00+08:00",
                window_end="2026-07-17T09:00:00+08:00",
                phase="final",
            )
            repository.finish_morning_run(
                run_id,
                status="completed",
                stages={"active_kols": 12, "successful_kols": 12, "failed_kols": 0},
                errors=[],
            )
            with store.connect() as db:
                db.execute(
                    "UPDATE morning_runs SET completed_at='2026-07-17T08:55:00+08:00' WHERE run_id=?",
                    (run_id,),
                )

            result = repository.morning_delivery(
                "2026-07-17",
                now=datetime(2026, 7, 17, 8, 56, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            self.assertEqual("ready", result["status"])
            self.assertEqual(1.0, result["coverage"])
            self.assertEqual(12, result["successful_kols"])

    def test_recovery_run_reuses_prior_fetch_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            repository = RecommendationDraftRepository(store)
            with store.connect() as db:
                db.executemany(
                    """
                    INSERT INTO morning_runs(
                        run_id,review_date,window_start,window_end,started_at,completed_at,status,phase,
                        active_kols,successful_kols,failed_kols,errors_json
                    ) VALUES(?,?,?,?,?,?,'completed','final',?,?,?,'[]')
                    """,
                    [
                        (
                            "fetched", "2026-07-17", "", "", "2026-07-17T08:00:00+08:00",
                            "2026-07-17T08:30:00+08:00", 12, 11, 1,
                        ),
                        (
                            "recovery", "2026-07-17", "", "", "2026-07-17T08:40:00+08:00",
                            "2026-07-17T08:50:00+08:00", 12, 0, 0,
                        ),
                    ],
                )

            result = repository.morning_delivery(
                "2026-07-17",
                now=datetime(2026, 7, 17, 8, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            self.assertEqual(11, result["successful_kols"])
            self.assertEqual(1, result["failed_kols"])
            self.assertAlmostEqual(11 / 12, result["coverage"])

    def test_pending_delivery_exposes_latest_pipeline_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            repository = RecommendationDraftRepository(store)
            run_id = repository.start_morning_run(
                "2026-07-17",
                window_start="2026-07-16T09:00:00+08:00",
                window_end="2026-07-17T09:00:00+08:00",
                phase="refresh",
            )
            repository.update_morning_run(
                run_id,
                stage="ai_review",
                progress_current=7,
                progress_total=20,
                stages={"active_kols": 12, "successful_kols": 8, "failed_kols": 0},
            )

            result = repository.morning_delivery(
                "2026-07-17",
                now=datetime(2026, 7, 17, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            self.assertEqual("pending", result["status"])
            self.assertEqual("refresh", result["latest_phase"])
            self.assertEqual("ai_review", result["stage"])
            self.assertEqual(7, result["progress_current"])
            self.assertEqual(20, result["progress_total"])
            self.assertEqual(12, result["active_kols"])

    def test_structured_mixed_post_creates_only_three_review_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }

            result = repository.sync_post(
                post_id,
                instruments,
                queue_scope="morning",
                review_date="2026-07-17",
            )
            drafts = repository.list_drafts(review_date="2026-07-17")

            self.assertEqual(3, result["created"])
            self.assertEqual(["605178", "002303", "000938"], [draft["symbol"] for draft in drafts])
            self.assertTrue(all(draft["status"] == "ready" for draft in drafts))
            self.assertTrue(all(draft["action"] == "watch" for draft in drafts))
            self.assertTrue(all(draft["horizon"] == "unspecified" for draft in drafts))
            self.assertTrue(all(draft["strength"] == "explicit" for draft in drafts))
            self.assertNotIn("388", " ".join(draft["thesis"] for draft in drafts))

    def test_missing_instrument_mapping_requires_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)

            repository.sync_post(
                post_id,
                {},
                queue_scope="morning",
                review_date="2026-07-17",
            )
            drafts = repository.list_drafts(review_date="2026-07-17")

            self.assertTrue(all(draft["status"] == "needs_attention" for draft in drafts))
            self.assertTrue(all("instrument_not_found" in draft["attention_reasons"] for draft in drafts))

    def test_existing_attention_draft_becomes_ready_after_instrument_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)
            repository.sync_post(post_id, {}, queue_scope="morning", review_date="2026-07-17")
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }

            repository.sync_post(
                post_id,
                instruments,
                queue_scope="morning",
                review_date="2026-07-17",
            )

            self.assertEqual(
                {"ready"},
                {item["status"] for item in repository.list_drafts(review_date="2026-07-17")},
            )

    def test_one_exact_evidence_span_is_enough_when_other_span_is_imprecise(self) -> None:
        post = {
            "url": "https://x.com/example/status/1",
            "posted_at": "2026-07-17T08:30:00+08:00",
            "text": "明日观察：天赐材料 特变电工\n特高压 + 电网设备 + AI电力",
        }
        draft = {
            "symbol": "600089",
            "security_name": "特变电工",
            "direction": "long",
            "action": "watch",
            "horizon": "short",
            "strength": "explicit",
            "thesis": "明日观察",
            "evidence_type": "original_pre_event",
            "mention_kind": "recommendation",
            "evidence_spans": ["明日观察：特变电工", "特高压 + 电网设备 + AI电力"],
        }
        instruments = {
            "600089": {"instrument_type": "stock", "status": "active"},
        }

        self.assertNotIn("evidence_not_found", _attention_reasons(post, draft, instruments))

    def test_review_gate_rejects_non_recommendation_quote_retrospective_and_hallucinated_stock(self) -> None:
        draft = {
            "symbol": "601179",
            "security_name": "中国西电",
            "evidence_source": "text",
        }
        instruments = {"601179": {"instrument_type": "stock", "status": "active"}}
        self.assertFalse(_is_reviewable_recommendation_draft(
            {"content_type": "other", "text": "中国西电，继续持有"},
            draft,
            instruments,
        ))
        self.assertFalse(_is_reviewable_recommendation_draft(
            {
                "content_type": "recommendation",
                "text": "大盘正在扭转乾坤",
                "quoted_text": "中国西电，继续持有",
            },
            {**draft, "evidence_source": "quoted_text"},
            instruments,
        ))
        self.assertFalse(_is_reviewable_recommendation_draft(
            {
                "content_type": "recommendation",
                "text": "收盘了，市场都给出了答案，中国西电涨停了",
            },
            draft,
            instruments,
        ))
        self.assertFalse(_is_reviewable_recommendation_draft(
            {"content_type": "recommendation", "text": "ALL in 电力设备出口"},
            draft,
            instruments,
        ))

    def test_theme_indexes_do_not_create_stock_recommendation_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            store.save_model_classification(
                post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "original_pre_event",
                    "confidence": 0.99,
                    "summary": "五个热点题材指数，不是个股推荐。",
                    "drafts": [{
                        "symbol": "880612",
                        "security_name": "镍金属",
                        "direction": "long",
                        "thesis": "可关注热点题材。",
                        "evidence_type": "original_pre_event",
                        "confidence": 0.99,
                        "evidence_spans": ["可关注热点题材"],
                        "evidence_source": "text",
                        "conditions": [],
                        "depends_on_ocr": False,
                        "mention_kind": "recommendation",
                    }],
                },
                model_name="fixture",
                prompt_version="theme-index-v1",
            )
            repository = RecommendationDraftRepository(store)

            result = repository.sync_post(
                post_id,
                {},
                queue_scope="morning",
                review_date="2026-07-17",
            )

            self.assertEqual(0, result["created"])
            self.assertEqual([], repository.list_drafts(review_date="2026-07-17"))

    def test_editing_an_ocr_draft_keeps_its_evidence_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            ocr_evidence = "大盘环境较差，锚定低吸瑞芯微"
            store.save_ocr_result(post_id, ocr_evidence)
            store.save_model_classification(
                post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "original_pre_event",
                    "confidence": 0.99,
                    "summary": "Recommendation visible in the image.",
                    "drafts": [{
                        "symbol": "603893",
                        "security_name": "瑞芯微",
                        "direction": "long",
                        "thesis": "图片明确建议低吸瑞芯微",
                        "evidence_type": "original_pre_event",
                        "confidence": 0.99,
                        "evidence_spans": [ocr_evidence],
                        "evidence_source": "ocr",
                        "conditions": [],
                        "depends_on_ocr": True,
                        "mention_kind": "recommendation",
                    }],
                },
                model_name="fixture",
                prompt_version="ocr-v1",
            )
            repository = RecommendationDraftRepository(store)
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }
            repository.sync_post(post_id, instruments, queue_scope="morning", review_date="2026-07-17")
            draft = repository.list_drafts(review_date="2026-07-17")[0]
            with store.connect() as db:
                db.execute(
                    "UPDATE recommendation_drafts SET evidence_source='text',depends_on_ocr=0 WHERE id=?",
                    (draft["id"],),
                )

            edited = repository.update_draft(
                draft["id"],
                {"thesis": "人工核对图片后确认低吸瑞芯微"},
                instruments,
            )

            self.assertNotIn("evidence_not_found", edited["attention_reasons"])
            self.assertIn("image_dependency", edited["attention_reasons"])
            self.assertEqual("ocr", edited["evidence_source"])
            self.assertTrue(edited["depends_on_ocr"])

    def test_edit_and_reject_are_audited_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }
            repository.sync_post(post_id, instruments, queue_scope="morning", review_date="2026-07-17")
            draft = repository.list_drafts(review_date="2026-07-17")[0]

            edited = repository.update_draft(
                draft["id"],
                {"thesis": "列入7月17日个股分享，原帖未提供具体个股理由"},
                instruments,
                note="人工修订",
            )
            first = repository.reject(draft["id"], "不纳入收益审计")
            second = repository.reject(draft["id"], "重复操作")

            self.assertEqual("ready", edited["status"])
            self.assertEqual("rejected", first["status"])
            self.assertEqual("rejected", second["status"])
            with store.connect() as db:
                actions = [row[0] for row in db.execute(
                    "SELECT action FROM recommendation_draft_reviews WHERE draft_id=? ORDER BY id",
                    (draft["id"],),
                )]
            self.assertEqual(["edited", "rejected"], actions)

    def test_human_correction_and_missed_stock_draft_are_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }
            repository.sync_post(post_id, instruments, queue_scope="morning", review_date="2026-07-17")
            draft = repository.list_drafts()[0]

            duplicate_of_ai = repository.create_manual_draft(
                post_id,
                {
                    "symbol": draft["symbol"],
                    "security_name": draft["security_name"],
                    "direction": "long",
                    "thesis": "不应生成重复草稿",
                    "evidence_spans": ["马前炮：7 月 17 日个股分享"],
                },
                instruments,
                review_date="2026-07-17",
                correction_type="missed_stock",
                note="重复补建",
            )
            self.assertEqual(draft["id"], duplicate_of_ai["id"])

            repository.update_draft(
                draft["id"],
                {"direction": "short"},
                instruments,
                note="AI方向识别错误",
                correction_type="wrong_direction",
            )
            manual = repository.create_manual_draft(
                post_id,
                {
                    "symbol": "002414",
                    "security_name": "高德红外",
                    "direction": "long",
                    "action": "watch",
                    "horizon": "unspecified",
                    "strength": "explicit",
                    "thesis": "人工补建漏掉的股票",
                    "evidence_spans": ["马前炮：7 月 17 日个股分享"],
                },
                instruments,
                review_date="2026-07-17",
                correction_type="missed_stock",
                note="AI漏识别",
            )
            repeated = repository.create_manual_draft(
                post_id,
                {
                    "symbol": "002414",
                    "security_name": "高德红外",
                    "direction": "long",
                    "thesis": "重复提交",
                    "evidence_spans": ["马前炮：7 月 17 日个股分享"],
                },
                instruments,
                review_date="2026-07-17",
                correction_type="missed_stock",
                note="重复",
            )

            self.assertEqual(manual["id"], repeated["id"])
            revisions = repository.list_revisions(draft["id"])
            self.assertEqual("wrong_direction", revisions[0]["correction_type"])
            self.assertEqual("missed_stock", repository.list_revisions(manual["id"])[0]["correction_type"])

    def test_human_symbol_correction_survives_same_model_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }
            repository.sync_post(post_id, instruments, queue_scope="morning", review_date="2026-07-17")
            original = next(item for item in repository.list_drafts() if item["symbol"] == "605178")
            corrected = repository.update_draft(
                original["id"],
                {"symbol": "002414", "security_name": "高德红外"},
                instruments,
                note="人工修正映射",
            )

            result = repository.sync_post(
                post_id,
                instruments,
                queue_scope="morning",
                review_date="2026-07-17",
            )
            active = [item for item in repository.list_drafts() if item["status"] in {"ready", "needs_attention"}]

            self.assertEqual(0, result["created"])
            self.assertEqual("002414", repository.get_draft(corrected["id"])["symbol"])
            self.assertNotIn("605178", [item["symbol"] for item in active])
            self.assertEqual(3, len(active))

    def test_reanalysis_without_recommendations_supersedes_pending_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            post_id = add_mixed_post(store)
            repository = RecommendationDraftRepository(store)
            instruments = {
                symbol: {"symbol": symbol, "name": name, "instrument_type": "stock", "status": "active"}
                for symbol, name in ALIASES.items()
            }
            repository.sync_post(post_id, instruments, queue_scope="morning", review_date="2026-07-17")
            store.save_model_classification(
                post_id,
                {
                    "content_type": "other",
                    "evidence_type": "ambiguous",
                    "confidence": 0.99,
                    "summary": "Promotion only.",
                    "drafts": [],
                },
                model_name="fixture",
                prompt_version="morning-v2",
            )

            result = repository.sync_post(
                post_id,
                instruments,
                queue_scope="morning",
                review_date="2026-07-17",
            )

            self.assertEqual(3, result["superseded"])
            self.assertEqual(
                {"superseded"},
                {item["status"] for item in repository.list_drafts(review_date="2026-07-17")},
            )


if __name__ == "__main__":
    unittest.main()
