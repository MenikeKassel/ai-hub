from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import (  # noqa: E402
    DeepSeekBatchPostClassifier,
    FallbackBatchPostClassifier,
    FallbackPostClassifier,
    DeepSeekPostClassifier,
    ModelProviderUnavailableError,
)


PAYLOAD = {
    "content_type": "other",
    "evidence_type": "ambiguous",
    "confidence": 0.99,
    "summary": "No recommendation.",
    "drafts": [],
}


class FakeClassifier:
    prompt_version = "fake-v1"
    model_name = "fake"

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def classify(self, post):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def classify_many(self, posts):
        self.calls += 1
        if self.error:
            raise self.error
        return {post["post_id"]: self.result for post in posts}


class AiProviderFallbackTests(unittest.TestCase):
    def test_deepseek_uses_json_mode_and_non_thinking_review(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            self.assertEqual("Bearer fixture-key", request.headers["Authorization"])
            self.assertEqual(
                "https://opencode.ai/zen/go/v1/chat/completions",
                str(request.url),
            )
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(PAYLOAD)}}],
            })

        class Credentials:
            @staticmethod
            def load() -> str:
                return "fixture-key"

        classifier = DeepSeekPostClassifier(
            Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            Credentials(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        post = {
            "post_id": "1",
            "platform": "X",
            "display_name": "Fixture",
            "handle": "fixture",
            "posted_at": "2026-07-23T08:00:00+08:00",
            "post_type": "original",
            "text": "market note",
        }

        self.assertEqual(PAYLOAD, classifier.classify(post))
        self.assertEqual("deepseek-v4-flash", captured["model"])
        self.assertEqual({"type": "json_object"}, captured["response_format"])
        self.assertEqual({"type": "disabled"}, captured["thinking"])

    def test_deepseek_accepts_json_inside_a_markdown_fence(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            content = "```json\n" + json.dumps(PAYLOAD) + "\n```"
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        class Credentials:
            @staticmethod
            def load() -> str:
                return "fixture-key"

        classifier = DeepSeekPostClassifier(
            Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            Credentials(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        self.assertEqual(PAYLOAD, classifier.classify({"post_id": "1", "text": "market note"}))

    def test_deepseek_batch_recovers_omitted_items_with_smaller_requests(self) -> None:
        batch_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user_content = body["messages"][1]["content"].split("\n", 1)[1]
            posts = json.loads(user_content)["posts"]
            batch_sizes.append(len(posts))
            selected = posts[:1]
            results = [{"post_id": post["post_id"], **PAYLOAD} for post in selected]
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({"results": results})}}],
            })

        class Credentials:
            @staticmethod
            def load() -> str:
                return "fixture-key"

        classifier = DeepSeekBatchPostClassifier(
            Path(__file__).resolve().parents[1] / "kol_batch_classifier_schema.json",
            Credentials(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        posts = [
            {"post_id": str(index), "text": f"market note {index}"}
            for index in range(1, 5)
        ]

        result = classifier.classify_many(posts)

        self.assertEqual({"1", "2", "3", "4"}, set(result))
        self.assertTrue(all(size <= 3 for size in batch_sizes))
        self.assertIn(1, batch_sizes)

    def test_single_classifier_falls_back_when_primary_is_unavailable(self) -> None:
        primary = FakeClassifier(error=ModelProviderUnavailableError("quota"))
        backup = FakeClassifier(result=PAYLOAD)
        classifier = FallbackPostClassifier(primary, backup)

        result = classifier.classify({"post_id": "1"})

        self.assertEqual(PAYLOAD, result)
        self.assertEqual("fake", classifier.last_provider)
        self.assertEqual(1, backup.calls)

    def test_schema_error_does_not_silently_switch_providers(self) -> None:
        primary = FakeClassifier(error=ValueError("invalid schema"))
        backup = FakeClassifier(result=PAYLOAD)
        classifier = FallbackPostClassifier(primary, backup)

        with self.assertRaisesRegex(ValueError, "invalid schema"):
            classifier.classify({"post_id": "1"})

        self.assertEqual(0, backup.calls)

    def test_batch_classifier_falls_back_for_the_whole_batch(self) -> None:
        primary = FakeClassifier(error=ModelProviderUnavailableError("quota"))
        backup = FakeClassifier(result=PAYLOAD)
        classifier = FallbackBatchPostClassifier(primary, backup)

        result = classifier.classify_many([{"post_id": "1"}, {"post_id": "2"}])
        classifier.classify_many([{"post_id": "3"}])

        self.assertEqual({"1", "2"}, set(result))
        self.assertEqual(1, primary.calls)
        self.assertEqual(2, backup.calls)


if __name__ == "__main__":
    unittest.main()
