from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import (  # noqa: E402
    KolPostStore,
    RuleClassifier,
    RuleResult,
    classify_pending_in_batches,
    normalise_twitter_post,
    process_pending_ocr_with_budget,
)
from model_budget import ModelDailyBudget, OcrDailyBudget  # noqa: E402


class ModelDailyBudgetTests(unittest.TestCase):
    def test_budget_is_persistent_and_never_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            budget = ModelDailyBudget(store, daily_limit=2)

            self.assertTrue(budget.reserve())
            budget.finish(success=True)
            self.assertTrue(ModelDailyBudget(store, daily_limit=2).reserve())
            budget.finish(success=False)
            self.assertFalse(budget.reserve())

            status = budget.status()
            self.assertEqual(2, status["attempted"])
            self.assertEqual(1, status["completed"])
            self.assertEqual(1, status["failed"])
            self.assertEqual(0, status["remaining"])

    def test_batch_classifier_respects_shared_daily_item_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("fixture", "fixture")
            kol = store.get_kol(kol_id)
            assert kol is not None
            for index in range(2):
                post = normalise_twitter_post(
                    {
                        "id": f"209800000000000000{index}",
                        "text": "这家公司值得继续研究",
                        "url": f"https://x.com/fixture/status/209800000000000000{index}",
                        "author": {"screenName": "fixture", "name": "fixture"},
                        "createdAtISO": f"2026-08-2{index + 5}T01:00:00+00:00",
                        "media": [],
                        "isRetweet": False,
                    },
                    kol,
                )
                store.upsert_post(post)
                store.save_rule_classification(
                    post.post_id,
                    RuleResult(70, True, [], "", ["fixture"], "ambiguous", "analysis"),
                )

            class BatchClassifier:
                model_name = "fixture-batch"
                prompt_version = "fixture-v1"

                def classify_many(self, posts):
                    return {
                        post["post_id"]: {
                            "content_type": "analysis",
                            "evidence_type": "original_pre_event",
                            "confidence": 0.8,
                            "summary": "fixture",
                            "drafts": [],
                        }
                        for post in posts
                    }

            completed, failed = classify_pending_in_batches(
                store,
                BatchClassifier(),
                daily_limit=1,
                batch_size=10,
            )

            self.assertEqual((1, 0), (completed, failed))
            self.assertEqual(1, store.model_queue_summary(daily_limit=1)["not_requested"])
            self.assertEqual(0, ModelDailyBudget(store, daily_limit=1).status()["remaining"])

    def test_ocr_unlimited_mode_keeps_audit_counts_without_remaining_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            budget = OcrDailyBudget(store, daily_limit=1, limit_mode="unlimited")

            for _ in range(3):
                self.assertTrue(budget.reserve())
                budget.finish(success=True)

            status = budget.status()
            self.assertEqual("unlimited", status["limit_mode"])
            self.assertIsNone(status["daily_limit"])
            self.assertIsNone(status["remaining"])
            self.assertEqual(3, status["attempted"])
            self.assertEqual(3, status["completed"])

    def test_ocr_unlimited_run_can_resume_a_legacy_exhausted_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            bounded = OcrDailyBudget(store, daily_limit=1, limit_mode="bounded")
            self.assertTrue(bounded.reserve())
            bounded.finish(success=False)

            unlimited = OcrDailyBudget(store, daily_limit=1, limit_mode="unlimited")
            self.assertTrue(unlimited.reserve())
            unlimited.finish(success=True)
            status = unlimited.status()
            self.assertEqual("unlimited", status["limit_mode"])
            self.assertEqual(2, status["attempted"])

    def test_ocr_timeout_batch_is_split_and_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolPostStore(root / "posts.db", root / "media")
            kol_id, _ = store.add_kol("fixture", "fixture")
            kol = store.get_kol(kol_id)
            assert kol is not None
            for index in range(2):
                post = normalise_twitter_post(
                    {
                        "id": f"209810000000000000{index}",
                        "text": "图片中的推荐",
                        "url": f"https://x.com/fixture/status/209810000000000000{index}",
                        "author": {"screenName": "fixture", "name": "fixture"},
                        "createdAtISO": f"2026-08-2{index + 5}T01:00:00+00:00",
                        "media": [{"type": "photo", "url": "https://example.invalid/a.jpg"}],
                        "isRetweet": False,
                    },
                    kol,
                )
                store.upsert_post(post)
                path = root / f"{post.post_id}.jpg"
                path.write_bytes(b"fixture")
                store.save_local_media(post.post_id, [{"path": str(path), "source_url": "https://example.invalid/a.jpg"}])
                store.save_rule_classification(
                    post.post_id,
                    RuleResult(70, True, [], "", ["fixture"], "ambiguous", "analysis"),
                )

            class TimeoutClassifier:
                provider_name = "fixture-ocr"

                def classify(self, posts):
                    if len(posts) > 1:
                        raise RuntimeError("RapidOCR batch timed out after 90 seconds")
                    return {posts[0]["post_id"]: {"text": "fixture OCR"}}

            completed, failed = process_pending_ocr_with_budget(
                store,
                TimeoutClassifier(),
                RuleClassifier({}),
                daily_limit=1,
                batch_size=2,
            )

            self.assertEqual((2, 0), (completed, failed))
            with store.connect() as db:
                self.assertEqual(
                    [("completed", 2)],
                    [tuple(row) for row in db.execute("SELECT ocr_status,COUNT(*) FROM classifications GROUP BY ocr_status").fetchall()],
                )


if __name__ == "__main__":
    unittest.main()
