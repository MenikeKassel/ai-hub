from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import (  # noqa: E402
    KolPostStore,
    RuleResult,
    classify_pending_in_batches,
    normalise_twitter_post,
)
from model_budget import ModelDailyBudget  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
