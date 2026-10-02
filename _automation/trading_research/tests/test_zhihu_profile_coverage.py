from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_sources.normalization import normalise_zhihu_post  # noqa: E402
from kol_sources.collection import run_post_fetch  # noqa: E402
from kol_sources.core import ProviderAttempt, ProviderFetchResult  # noqa: E402
from kol_sources.providers import ZhihuProfileProvider  # noqa: E402
from kol_sources.repository import KolPostStore  # noqa: E402


class _Completed:
    returncode = 0
    stderr = ""

    def __init__(self, payload: dict):
        self.stdout = json.dumps(payload, ensure_ascii=False)


def _answer(answer_id: str = "10001", *, author: str = "target") -> dict:
    return {
        "id": answer_id,
        "type": "answer",
        "text": "回答内容",
        "articleTitle": "问题",
        "url": f"https://www.zhihu.com/question/2001/answer/{answer_id}",
        "author": {"name": "真实作者", "screenName": author},
        "createdAtISO": "2026-09-01T01:02:03Z",
    }


class ZhihuProfileProviderTests(TestCase):
    def test_partial_surface_and_duplicate_rows_are_retained_safely(self) -> None:
        calls: list[list[str]] = []

        def runner(command, **_kwargs):
            calls.append(list(command))
            return _Completed(
                {
                    "ok": True,
                    "posts": [_answer(), _answer()],
                    "surfaces": {
                        "answers": {"status": "success"},
                        "articles": {"status": "failed", "warnings": ["http_403"]},
                        "ideas": {"status": "empty", "warnings": ["empty"]},
                    },
                    "coverage": {"status": "partial"},
                    "warnings": ["surface:articles:http_403", "surface:ideas:empty"],
                }
            )

        provider = ZhihuProfileProvider(Path("capture.py"), runner=runner, timeout_seconds=120)
        # The command is constructed even when the fixture script is not on disk.
        provider.script_path = Path(__file__)
        result = provider.fetch_user_posts("target", 5)

        self.assertEqual(1, len(result.posts))
        self.assertIn("surface:articles:http_403", result.warnings)
        self.assertIn("coverage:partial", result.warnings)
        self.assertEqual("partial", result.posts[0]["coverage"]["status"])
        self.assertIn("surface:articles:http_403", result.posts[0]["profile_warnings"])
        self.assertNotIn("posts", result.posts[0]["surfaces"]["articles"])
        self.assertIn("--surfaces", calls[0])
        self.assertIn("answers,articles,ideas", calls[0])
        self.assertIn("--wait", calls[0])

    def test_empty_success_is_not_provider_failure(self) -> None:
        def runner(command, **_kwargs):
            return _Completed(
                {
                    "ok": True,
                    "posts": [],
                    "surfaces": {
                        "answers": {"status": "empty"},
                        "articles": {"status": "empty"},
                        "ideas": {"status": "empty"},
                    },
                    "coverage": {"status": "complete"},
                    "warnings": ["surface:answers:empty"],
                }
            )

        provider = ZhihuProfileProvider(Path(__file__), runner=runner)
        result = provider.fetch_user_posts("target", 3)
        self.assertEqual([], result.posts)
        self.assertEqual(["surface:answers:empty"], result.warnings)

    def test_bounded_limit_is_metadata_without_a_provider_warning(self) -> None:
        def runner(command, **_kwargs):
            return _Completed(
                {
                    "ok": True,
                    "posts": [_answer()],
                    "surfaces": {
                        "answers": {
                            "status": "success",
                            "bounded": True,
                            "warnings": ["bounded_limit_reached"],
                        },
                        "articles": {"status": "empty", "warnings": ["empty"]},
                        "ideas": {"status": "empty", "warnings": ["empty"]},
                    },
                    "coverage": {"status": "bounded", "bounded": True},
                    "warnings": ["surface:answers:bounded_limit_reached", "coverage:bounded"],
                }
            )

        provider = ZhihuProfileProvider(Path(__file__), runner=runner)
        result = provider.fetch_user_posts("target", 3)
        self.assertEqual([], result.warnings)
        self.assertEqual("bounded", result.posts[0]["coverage"]["status"])
        self.assertEqual([], result.posts[0]["profile_warnings"])


class ZhihuNormalizationTests(TestCase):
    def setUp(self) -> None:
        self.kol = {
            "id": 1,
            "handle": "target",
            "display_name": "目标作者",
            "tracking_mode": "direct_profile",
        }

    def test_article_and_idea_ids_are_namespaced_but_answer_id_stays_legacy(self) -> None:
        answer = normalise_zhihu_post(_answer(), self.kol)
        article = normalise_zhihu_post(
            {
                "id": "1001",
                "type": "article",
                "text": "文章正文",
                "articleTitle": "文章标题",
                "url": "https://zhuanlan.zhihu.com/p/1001",
                "author": {"name": "真实作者", "screenName": "target"},
                "createdAtISO": "2026-09-02T01:02:03Z",
            },
            self.kol,
        )
        idea = normalise_zhihu_post(
            {
                "id": "1001",
                "type": "idea",
                "text": "想法正文",
                "url": "https://www.zhihu.com/pin/1001",
                "author": {"name": "真实作者", "screenName": "target"},
                "createdAtISO": "2026-09-03T01:02:03Z",
            },
            self.kol,
        )
        self.assertEqual("10001", answer.post_id)
        self.assertEqual("zhihu:article:1001", article.post_id)
        self.assertEqual("zhihu:idea:1001", idea.post_id)
        self.assertEqual("article", article.post_type)
        self.assertEqual("idea", idea.post_type)

    def test_missing_date_is_rejected_without_synthesizing_now(self) -> None:
        payload = _answer()
        payload.pop("createdAtISO")
        with self.assertRaisesRegex(ValueError, "missing an exact timestamp"):
            normalise_zhihu_post(payload, self.kol)

    def test_foreign_profile_author_is_rejected_for_direct_feed(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match requested profile"):
            normalise_zhihu_post(_answer(author="other"), self.kol)

    def test_foreign_author_is_retained_for_explicit_aggregation(self) -> None:
        aggregation = dict(self.kol, tracking_mode="aggregation")
        post = normalise_zhihu_post(_answer(author="other"), aggregation)
        self.assertEqual("真实作者", post.author_name)
        self.assertIn("author_mismatch_retained", post.provider_warning)

    def test_explicit_complete_coverage_is_preserved_as_current_state(self) -> None:
        payload = _answer()
        payload["coverage"] = {"status": "complete", "historical_complete": False}
        post = normalise_zhihu_post(payload, self.kol)
        self.assertEqual("coverage:complete", post.provider_warning)
        self.assertEqual("complete", post.raw_payload["coverage"]["status"])

    def test_idea_content_array_is_normalized(self) -> None:
        post = normalise_zhihu_post(
            {
                "id": "1001",
                "type": "idea",
                "text": [{"type": "text", "content": "<p>数组想法</p>"}],
                "url": "https://www.zhihu.com/pin/1001",
                "author": {"name": "真实作者", "url_token": "target"},
                "created_time": 1_756_000_000,
            },
            self.kol,
        )
        self.assertEqual("数组想法", post.text)

    def test_numeric_article_created_field_is_real_source_timestamp(self) -> None:
        post = normalise_zhihu_post(
            {
                "id": "1002",
                "type": "article",
                "text": "带时间文章",
                "url": "https://zhuanlan.zhihu.com/p/1002",
                "author": {"name": "真实作者", "url_token": "target"},
                "created": 1_756_000_000,
            },
            self.kol,
        )
        self.assertEqual("2025-08-24T09:46:40+08:00", post.posted_at)


class ZhihuCollectionTests(TestCase):
    def _run(self, posts: list[dict]) -> tuple[KolPostStore, object, int]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = KolPostStore(Path(tmp.name) / "posts.db", Path(tmp.name) / "media")
        kol_id, _ = store.add_kol(
            "目标作者",
            "target",
            tracking_mode="direct_profile",
            platform="Zhihu",
        )

        class Provider:
            name = "zhihu-local"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult(
                    provider=self.name,
                    posts=posts,
                    attempts=[ProviderAttempt(self.name, "success", post_count=len(posts))],
                    warnings=[],
                )

        result = run_post_fetch(
            store,
            Provider(),
            platforms={"zhihu"},
            handles={"target"},
            max_count=3,
            sleep_seconds=0,
            download_media=False,
            reconcile_zhihu=False,
        )
        return store, result, kol_id

    def test_all_zhihu_rows_rejected_is_failed_without_cursor_advance(self) -> None:
        payload = _answer(author="other")
        store, result, kol_id = self._run([payload])
        kol = store.get_kol(kol_id)
        self.assertEqual(0, result.successful_kols)
        self.assertEqual(1, result.failed_kols)
        self.assertEqual("failed", kol["fetch_status"])
        self.assertEqual("", kol["last_post_id"])
        self.assertEqual(0, store.count_posts())
        self.assertTrue(any("none had usable content" in error for error in result.errors))

    def test_valid_zhihu_rows_keep_partial_coverage_in_raw_json(self) -> None:
        valid = _answer()
        valid["coverage"] = {"status": "partial", "historical_complete": False}
        valid["surfaces"] = {
            "answers": {"status": "success", "pages": 1},
            "articles": {"status": "failed", "warnings": ["http_403"]},
        }
        valid["profile_warnings"] = ["coverage:partial", "surface:articles:http_403"]
        rejected = _answer("10002", author="other")
        store, result, kol_id = self._run([valid, rejected])
        self.assertEqual(1, result.successful_kols)
        self.assertEqual(0, result.failed_kols)
        self.assertEqual("success", store.get_kol(kol_id)["fetch_status"])
        post = store.get_post("10001")
        raw = json.loads(post["raw_json"])
        self.assertEqual("partial", raw["coverage"]["status"])
        self.assertIn("surface:articles:http_403", post["provider_warning"])
        self.assertNotIn("posts", raw["surfaces"]["answers"])


if __name__ == "__main__":
    import unittest

    unittest.main()
