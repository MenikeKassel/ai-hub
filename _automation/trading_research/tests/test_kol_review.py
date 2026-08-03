from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import KolPostStore, RuleClassifier, initialize_seed_kols, normalise_twitter_post  # noqa: E402
from kol_review import approve_post, approve_recommendation_draft  # noqa: E402
from kol_tracker import KolStore  # noqa: E402


def payload(post_id: str = "2076000000000000001") -> dict:
    return {
        "id": post_id,
        "text": "看多 002414 高德红外和 601888 中国中免，分别受益于业绩和消费修复。",
        "url": f"https://x.com/example/status/{post_id}",
        "author": {"screenName": "example", "name": "示例KOL"},
        "metrics": {},
        "createdAtISO": "2026-07-13T08:30:00+00:00",
        "media": [],
        "isRetweet": False,
        "lang": "zh",
    }


class ApprovalTests(unittest.TestCase):
    def _stores(self, root: Path) -> tuple[KolPostStore, KolStore, str]:
        posts = KolPostStore(root / "posts.db", root / "media")
        initialize_seed_kols(posts)
        kol = posts.get_kol_by_handle("WwQQ129146")
        assert kol is not None
        post = normalise_twitter_post(payload(), kol)
        posts.upsert_post(post)
        posts.save_rule_classification(post.post_id, RuleClassifier().classify(post))
        return posts, KolStore(root / "events"), post.post_id

    def test_approval_stays_local_and_splits_multiple_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            drafts = [
                {
                    "symbol": "002414",
                    "security_name": "高德红外",
                    "direction": "long",
                    "thesis": "业绩超预期",
                    "evidence_type": "original_pre_event",
                },
                {
                    "symbol": "601888",
                    "security_name": "中国中免",
                    "direction": "long",
                    "thesis": "消费修复",
                    "evidence_type": "original_pre_event",
                },
            ]
            first = approve_post(
                posts,
                events,
                post_id,
                drafts,
            )
            posts.record_review_action(post_id, "media_download_failed", {"error": "later retry"})
            second = approve_post(
                posts,
                events,
                post_id,
                drafts,
            )

            self.assertEqual(2, first.created_events)
            self.assertEqual(0, second.created_events)
            self.assertEqual(first.event_ids, second.event_ids)
            self.assertEqual(2, len(events.load_events()))
            self.assertEqual(f"post:{post_id}", first.source_ref)
            self.assertFalse((root / "vault").exists())
            self.assertEqual("", posts.get_post(post_id)["notion_url"])
            self.assertEqual("approved", posts.get_post(post_id)["review_status"])

    def test_retrospective_draft_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            draft = {
                "symbol": "002414",
                "security_name": "高德红外",
                "direction": "long",
                "thesis": "已经涨停",
                "evidence_type": "retrospective",
            }

            with self.assertRaises(ValueError):
                approve_post(
                    posts,
                    events,
                    post_id,
                    [draft],
                )

            self.assertEqual([], events.load_events())
            self.assertEqual("pending", posts.get_post(post_id)["review_status"])

    def test_source_timestamp_conflict_blocks_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            kol = posts.get_kol_by_handle("WwQQ129146")
            assert kol is not None
            conflicting = normalise_twitter_post(
                payload() | {"createdAtISO": "2026-07-13T08:32:01+00:00"},
                kol,
                provider="nitter",
            )
            posts.upsert_post(conflicting)
            draft = {
                "symbol": "002414",
                "security_name": "高德红外",
                "direction": "long",
                "thesis": "业绩超预期",
                "evidence_type": "original_pre_event",
            }

            with self.assertRaisesRegex(ValueError, "timestamps conflict"):
                approve_post(
                    posts,
                    events,
                    post_id,
                    [draft],
                )

    def test_retrospective_post_cannot_be_relabelled_by_a_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            initialize_seed_kols(posts)
            kol = posts.get_kol_by_handle("WwQQ129146")
            assert kol is not None
            post = normalise_twitter_post(
                payload() | {"text": "昨天推荐 002414 高德红外，今天已经涨停。"},
                kol,
            )
            posts.upsert_post(post)
            posts.save_rule_classification(post.post_id, RuleClassifier().classify(post))
            draft = {
                "symbol": "002414",
                "security_name": "高德红外",
                "direction": "long",
                "thesis": "已经涨停",
                "evidence_type": "original_pre_event",
            }

            with self.assertRaises(ValueError):
                approve_post(
                    posts,
                    KolStore(root / "events"),
                    post.post_id,
                    [draft],
                )

    def test_single_clear_draft_can_be_approved_from_an_ambiguous_mixed_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            posts.save_model_classification(
                post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "ambiguous",
                    "confidence": 0.98,
                    "summary": "The post mixes holdings with a clear future action.",
                    "drafts": [],
                },
                model_name="fixture",
                prompt_version="fixture-v1",
            )
            draft = {
                "symbol": "002414",
                "security_name": "Test Security",
                "direction": "long",
                "thesis": "Explicit future entry plan",
                "evidence_type": "original_pre_event",
                "conditions": ["enter when the stated condition is met"],
            }

            result = approve_recommendation_draft(posts, events, post_id, draft)

            self.assertEqual(1, result.created_events)
            self.assertEqual("approved", posts.get_post(post_id)["review_status"])

    def test_single_draft_still_cannot_override_retrospective_post_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            posts.save_model_classification(
                post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "retrospective",
                    "confidence": 0.99,
                    "summary": "A retrospective recap.",
                    "drafts": [],
                },
                model_name="fixture",
                prompt_version="fixture-v1",
            )
            draft = {
                "symbol": "002414",
                "security_name": "Test Security",
                "direction": "long",
                "thesis": "Already rose",
                "evidence_type": "original_pre_event",
            }

            with self.assertRaisesRegex(ValueError, "retrospective"):
                approve_recommendation_draft(posts, events, post_id, draft)

            self.assertEqual([], events.load_events())

    def test_legacy_capture_failure_can_retry_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            draft = {
                "symbol": "002414",
                "security_name": "高德红外",
                "direction": "long",
                "thesis": "业绩超预期",
                "evidence_type": "original_pre_event",
            }
            posts.record_review_action(
                post_id,
                "approval_started",
                {"attempt_id": "legacy-attempt", "drafts": [draft], "note": ""},
            )
            posts.set_review(
                post_id,
                "capture_failed",
                "Notion/Obsidian capture failed: network unavailable",
                {"error": "network unavailable"},
            )

            result = approve_post(posts, events, post_id, [draft])

            self.assertEqual(1, result.created_events)
            self.assertEqual(f"post:{post_id}", result.source_ref)
            self.assertEqual("approved", posts.get_post(post_id)["review_status"])
            self.assertEqual(1, len(events.load_events()))

    def test_interrupted_approval_cannot_change_its_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts, events, post_id = self._stores(root)
            original = [{
                "symbol": "002414", "security_name": "高德红外", "direction": "long",
                "thesis": "业绩超预期", "evidence_type": "original_pre_event",
            }]
            posts.record_review_action(
                post_id,
                "approval_started",
                {"attempt_id": "attempt-1", "drafts": original},
            )
            changed = [{**original[0], "symbol": "601888", "security_name": "中国中免"}]

            with self.assertRaises(ValueError):
                approve_post(
                    posts,
                    events,
                    post_id,
                    changed,
                )


if __name__ == "__main__":
    unittest.main()
