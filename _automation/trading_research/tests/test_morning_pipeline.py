from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kol_posts import (
    KolPostStore,
    ModelProviderUnavailableError,
    RuleClassifier,
    RuleResult,
    normalise_twitter_post,
)
from market_data import Instrument, MarketStore
from morning_pipeline import MorningPipeline
from recommendation_drafts import RecommendationDraftRepository


class FakeBatchClassifier:
    prompt_version = "fake-batch-v1"

    def classify_many(self, posts):
        return {
            post["post_id"]: {
                "content_type": "recommendation",
                "evidence_type": "original_pre_event",
                "confidence": 0.99,
                "summary": "Morning recommendation.",
                "drafts": [{
                    "symbol": "002414",
                    "security_name": "Gaode Infrared",
                    "direction": "long",
                    "thesis": "The author explicitly selected this stock for the morning list.",
                    "evidence_type": "original_pre_event",
                    "confidence": 0.99,
                    "evidence_spans": [post["text"]],
                    "evidence_source": "text",
                    "conditions": [],
                    "depends_on_ocr": False,
                    "mention_kind": "recommendation",
                }],
            }
            for post in posts
        }


class PartialBatchClassifier(FakeBatchClassifier):
    def classify_many(self, posts):
        if len(posts) == 1:
            return {}
        return super().classify_many(posts[:1])


class FakeOcrClassifier:
    timeout_seconds = 300

    def __init__(self):
        self.calls = 0

    def classify(self, posts):
        self.calls += 1
        return {post["post_id"]: {"text": "002414 高德红外，继续看多"} for post in posts}


class UnavailableBatchClassifier(FakeBatchClassifier):
    def __init__(self):
        self.calls = 0

    def classify_many(self, posts):
        self.calls += 1
        raise ModelProviderUnavailableError("Codex usage limit reached; try again tomorrow")


class MorningPipelineTests(unittest.TestCase):
    def test_current_window_is_materialized_before_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            market.upsert_instrument(
                Instrument("002414", "Gaode Infrared", "stock", "SZ", source="fixture")
            )
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            kol = {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"}
            for post_id, posted_at in [
                ("2078000000000001001", "2026-07-17T00:20:00+00:00"),
                ("2077000000000001002", "2026-07-10T00:20:00+00:00"),
            ]:
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": "Morning selection: 002414.",
                        "url": f"https://x.com/fixture/status/{post_id}",
                        "author": {"screenName": "fixture", "name": "Fixture KOL"},
                        "createdAtISO": posted_at,
                        "media": [],
                        "isRetweet": False,
                    },
                    kol,
                )
                posts.upsert_post(post)
                posts.save_rule_classification(
                    post_id,
                    RuleResult(80, True, ["002414"], "", ["fixture"], "ambiguous", "market_view"),
                )

            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002414": "Gaode Infrared"}),
                batch_classifier=FakeBatchClassifier(),
            )
            result = pipeline.run(
                as_of=date(2026, 7, 17),
                fetch=False,
                backlog_limit=1,
                max_runtime_minutes=2,
            )
            repository = RecommendationDraftRepository(posts)
            morning = repository.list_drafts(review_date="2026-07-17", queue_scope="morning")
            backlog = repository.list_drafts(review_date="2026-07-17", queue_scope="backlog")

            self.assertTrue(result["ok"], result)
            self.assertEqual(["2078000000000001001"], [item["post_id"] for item in morning])
            self.assertEqual(["2077000000000001002"], [item["post_id"] for item in backlog])
            self.assertEqual(2, result["reviewed_posts"])
            self.assertEqual(1, result["ready_drafts"])
            self.assertEqual(1, result["backlog_drafts"])

    def test_completed_non_recommendations_do_not_starve_unresolved_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            market.upsert_instrument(Instrument("002580", "圣阳股份", "stock", "SZ", source="fixture"))
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            kol = {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"}
            for index in range(25):
                post_id = f"2077000000000002{index:03d}"
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": "板块观察，002580 仅作记录。",
                        "url": f"https://x.com/fixture/status/{post_id}",
                        "author": {"screenName": "fixture", "name": "Fixture KOL"},
                        "createdAtISO": "2026-07-10T00:20:00+00:00",
                        "media": [],
                        "isRetweet": False,
                    },
                    kol,
                )
                posts.upsert_post(post)
                posts.save_rule_classification(
                    post_id,
                    RuleResult(80, True, ["002580"], "long", ["fixture"], "ambiguous", "market_view"),
                )
                posts.save_model_classification(
                    post_id,
                    {"content_type": "market_view", "evidence_type": "ambiguous", "confidence": 0.99, "summary": "观察", "drafts": []},
                    model_name="fixture",
                    prompt_version="fixture-v1",
                )
                posts.set_draft_generation_status(post_id, "not_applicable", version="fixture-v1")

            target = normalise_twitter_post(
                {
                    "id": "2077000000000002999",
                    "text": "周一建仓计划【圣阳股份】\n圣阳股份\n重点：算力 IDC 备电龙头。",
                    "url": "https://x.com/fixture/status/2077000000000002999",
                    "author": {"screenName": "fixture", "name": "Fixture KOL"},
                    "createdAtISO": "2026-07-10T00:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            posts.upsert_post(target)
            posts.save_rule_classification(
                target.post_id,
                RuleClassifier({"002580": "圣阳股份"}).classify(target),
            )

            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002580": "圣阳股份"}),
                batch_classifier=FakeBatchClassifier(),
            )
            result = pipeline.run(as_of=date(2026, 7, 17), fetch=False, backlog_limit=1, max_runtime_minutes=2)
            drafts = RecommendationDraftRepository(posts).list_drafts(post_id=target.post_id)

            self.assertTrue(result["ok"], result)
            self.assertEqual([target.post_id], [item["post_id"] for item in drafts])
            self.assertEqual("generated", posts.get_post(target.post_id)["draft_generation_status"])

    def test_late_catch_up_keeps_the_fixed_previous_day_window_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            market.upsert_instrument(Instrument("002414", "Gaode Infrared", "stock", "SZ", source="fixture"))
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000001010",
                    "text": "Morning selection: 002414.",
                    "url": "https://x.com/fixture/status/2078000000000001010",
                    "author": {"screenName": "fixture", "name": "Fixture KOL"},
                    "createdAtISO": "2026-07-16T02:00:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
            )
            posts.upsert_post(post)
            posts.save_rule_classification(post.post_id, RuleResult(80, True, ["002414"], "", ["fixture"], "ambiguous", "market_view"))
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002414": "Gaode Infrared"}),
                batch_classifier=FakeBatchClassifier(),
                now_provider=lambda: datetime(2026, 7, 17, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            pipeline.run(as_of=date(2026, 7, 17), fetch=False, backlog_limit=0, max_runtime_minutes=2)

            morning = RecommendationDraftRepository(posts).list_drafts(
                review_date="2026-07-17", queue_scope="morning"
            )
            self.assertEqual([post.post_id], [item["post_id"] for item in morning])

    def test_late_final_does_not_pull_posts_after_nine_into_the_closed_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            market.upsert_instrument(Instrument("002414", "Gaode Infrared", "stock", "SZ", source="fixture"))
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            for post_id, posted_at in [
                ("2078000000000001111", "2026-07-17T00:20:00+00:00"),
                ("2078000000000001112", "2026-07-17T02:20:00+00:00"),
            ]:
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": "Morning selection: 002414.",
                        "url": f"https://x.com/fixture/status/{post_id}",
                        "author": {"screenName": "fixture", "name": "Fixture KOL"},
                        "createdAtISO": posted_at,
                        "media": [],
                        "isRetweet": False,
                    },
                    {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
                )
                posts.upsert_post(post)
                posts.save_rule_classification(post.post_id, RuleResult(80, True, ["002414"], "", ["fixture"], "ambiguous", "market_view"))
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002414": "Gaode Infrared"}),
                batch_classifier=FakeBatchClassifier(),
                now_provider=lambda: datetime(2026, 7, 17, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            pipeline.run(
                as_of=date(2026, 7, 17),
                fetch=False,
                backlog_limit=0,
                max_runtime_minutes=2,
                phase="final",
            )

            morning = RecommendationDraftRepository(posts).list_drafts(
                review_date="2026-07-17", queue_scope="morning"
            )
            self.assertEqual(["2078000000000001111"], [item["post_id"] for item in morning])

    def test_missing_batch_item_does_not_overwrite_successful_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            market.upsert_instrument(Instrument("002414", "Gaode Infrared", "stock", "SZ", source="fixture"))
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            for index in range(2):
                post_id = f"207800000000000102{index}"
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": "Morning selection: 002414.",
                        "url": f"https://x.com/fixture/status/{post_id}",
                        "author": {"screenName": "fixture", "name": "Fixture KOL"},
                        "createdAtISO": f"2026-07-17T00:2{index}:00+00:00",
                        "media": [],
                        "isRetweet": False,
                    },
                    {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
                )
                posts.upsert_post(post)
                posts.save_rule_classification(post_id, RuleResult(80, True, ["002414"], "", ["fixture"], "ambiguous", "market_view"))
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002414": "Gaode Infrared"}),
                batch_classifier=PartialBatchClassifier(),
                now_provider=lambda: datetime(2026, 7, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            result = pipeline.run(as_of=date(2026, 7, 17), fetch=False, backlog_limit=0, max_runtime_minutes=2)
            statuses = {post_id: posts.get_post(post_id)["model_status"] for post_id in ["2078000000000001020", "2078000000000001021"]}

            self.assertEqual(1, result["failed_posts"])
            self.assertEqual(["completed", "failed"], sorted(statuses.values()))

    def test_failed_ocr_is_retried_up_to_three_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000001030",
                    "text": "Image recommendation.",
                    "url": "https://x.com/fixture/status/2078000000000001030",
                    "author": {"screenName": "fixture", "name": "Fixture KOL"},
                    "createdAtISO": "2026-07-17T00:20:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
            )
            posts.upsert_post(post)
            posts.save_rule_classification(post.post_id, RuleResult(80, True, [], "", ["fixture"], "ambiguous", "market_view"))
            with posts.connect() as db:
                db.execute("UPDATE posts SET local_media_json='[{\"path\":\"fixture.png\"}]' WHERE post_id=?", (post.post_id,))
            posts.save_ocr_result(post.post_id, error="temporary OCR failure")
            ocr = FakeOcrClassifier()
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002414": "Gaode Infrared"}),
                batch_classifier=FakeBatchClassifier(),
                ocr_classifier=ocr,
            )
            stages = {"ocr_completed": 0, "ocr_failed": 0}

            pipeline._run_ocr([posts.get_post(post.post_id)], float("inf"), stages, [])

            self.assertEqual(1, ocr.calls)
            self.assertEqual("completed", posts.get_post(post.post_id)["ocr_status"])

    def test_fetch_outage_requires_scheduler_alert_without_losing_queue_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({}),
                batch_classifier=FakeBatchClassifier(),
                fetcher=lambda: (_ for _ in ()).throw(RuntimeError("provider offline")),
            )

            result = pipeline.run(as_of=date(2026, 7, 17), fetch=True, backlog_limit=0, max_runtime_minutes=2)

            self.assertTrue(result["ok"])
            self.assertTrue(result["degraded"])
            self.assertTrue(result["alert_required"])

    def test_model_provider_unavailable_stops_after_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            for index in range(12):
                post_id = f"207800000000009{index:03d}"
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": "Morning market note.",
                        "url": f"https://x.com/fixture/status/{post_id}",
                        "author": {"screenName": "fixture", "name": "Fixture KOL"},
                        "createdAtISO": f"2026-07-17T00:{index:02d}:00+00:00",
                        "media": [],
                        "isRetweet": False,
                    },
                    {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
                )
                posts.upsert_post(post)
                posts.save_rule_classification(
                    post_id,
                    RuleResult(80, True, [], "", ["fixture"], "ambiguous", "market_view"),
                )
            classifier = UnavailableBatchClassifier()
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({}),
                batch_classifier=classifier,
                now_provider=lambda: datetime(2026, 7, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            result = pipeline.run(
                as_of=date(2026, 7, 17), fetch=False, backlog_limit=0, max_runtime_minutes=2
            )

            self.assertEqual(1, classifier.calls)
            self.assertTrue(result["alert_required"])
            self.assertEqual(10, result["failed_posts"])
            unavailable = [error for error in result["errors"] if error.startswith("ai unavailable:")]
            self.assertEqual(1, len(unavailable))

    def test_batch_retry_is_not_started_without_minimum_model_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            candidates = []
            for index in range(2):
                post_id = f"207800000000008{index:03d}"
                post = normalise_twitter_post(
                    {
                        "id": post_id,
                        "text": "Morning market note.",
                        "url": f"https://x.com/fixture/status/{post_id}",
                        "author": {"screenName": "fixture", "name": "Fixture KOL"},
                        "createdAtISO": f"2026-07-17T00:2{index}:00+00:00",
                        "media": [],
                        "isRetweet": False,
                    },
                    {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
                )
                posts.upsert_post(post)
                posts.save_rule_classification(
                    post_id,
                    RuleResult(80, True, [], "", ["fixture"], "ambiguous", "market_view"),
                )
                candidates.append(posts.get_post(post_id))
            clock = {"after_first_call": False}

            class DeadlineClassifier(PartialBatchClassifier):
                def __init__(self):
                    self.calls = 0

                def classify_many(self, batch):
                    self.calls += 1
                    result = super().classify_many(batch)
                    clock["after_first_call"] = True
                    return result

            classifier = DeadlineClassifier()
            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({}),
                batch_classifier=classifier,
            )
            errors: list[str] = []

            with patch(
                "morning_pipeline.time.monotonic",
                side_effect=lambda: 20 if clock["after_first_call"] else 0,
            ):
                pipeline._run_batches(candidates, 100, {}, errors)

            self.assertEqual(1, classifier.calls)
            self.assertEqual("completed", posts.get_post(candidates[0]["post_id"])["model_status"])
            self.assertEqual("failed", posts.get_post(candidates[1]["post_id"])["model_status"])
            self.assertFalse(any("timed out after 1 seconds" in error for error in errors))

    def test_completed_model_result_is_materialized_after_runtime_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            market = MarketStore(root / "market")
            market.upsert_instrument(
                Instrument("002414", "Gaode Infrared", "stock", "SZ", source="fixture")
            )
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000081000",
                    "text": "Morning selection: 002414.",
                    "url": "https://x.com/fixture/status/2078000000000081000",
                    "author": {"screenName": "fixture", "name": "Fixture KOL"},
                    "createdAtISO": "2026-07-17T00:20:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
            )
            posts.upsert_post(post)
            posts.save_rule_classification(
                post.post_id,
                RuleResult(80, True, ["002414"], "", ["fixture"], "ambiguous", "market_view"),
            )
            clock = {"expired": False}

            class DeadlineCrossingClassifier(FakeBatchClassifier):
                def classify_many(self, batch):
                    result = super().classify_many(batch)
                    clock["expired"] = True
                    return result

            pipeline = MorningPipeline(
                posts,
                market,
                rule_classifier=RuleClassifier({"002414": "Gaode Infrared"}),
                batch_classifier=DeadlineCrossingClassifier(),
            )
            stages = {
                "reviewed_posts": 0,
                "failed_posts": 0,
                "ocr_completed": 0,
                "ocr_failed": 0,
            }

            with patch(
                "morning_pipeline.time.monotonic",
                side_effect=lambda: 101 if clock["expired"] else 0,
            ):
                pipeline._process_scope(
                    [posts.get_post(post.post_id)],
                    "morning",
                    date(2026, 7, 17),
                    100,
                    stages,
                    [],
                )

            drafts = RecommendationDraftRepository(posts).list_drafts(
                review_date="2026-07-17", queue_scope="morning"
            )
            self.assertEqual("completed", posts.get_post(post.post_id)["model_status"])
            self.assertEqual([post.post_id], [item["post_id"] for item in drafts])
            self.assertEqual(1, stages["reviewed_posts"])

    def test_legacy_non_candidates_are_archived_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            kol_id, _ = posts.add_kol("Fixture KOL", "fixture", "A-share")
            post = normalise_twitter_post(
                {
                    "id": "2078000000000001003",
                    "text": "Pure promotion.",
                    "url": "https://x.com/fixture/status/2078000000000001003",
                    "author": {"screenName": "fixture", "name": "Fixture KOL"},
                    "createdAtISO": "2026-07-17T00:20:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "fixture", "display_name": "Fixture KOL"},
            )
            posts.upsert_post(post)
            posts.save_rule_classification(
                post.post_id,
                RuleResult(0, False, [], "", [], "ambiguous", "other"),
            )

            result = RecommendationDraftRepository(posts).migrate_legacy_review_queue()

            self.assertEqual({"screened_non_candidates": 1}, result)
            self.assertEqual("ignored", posts.get_post(post.post_id)["review_status"])
            self.assertEqual("legacy_non_candidate_migration", posts.latest_review(post.post_id)["detail"]["reason"])


if __name__ == "__main__":
    unittest.main()
