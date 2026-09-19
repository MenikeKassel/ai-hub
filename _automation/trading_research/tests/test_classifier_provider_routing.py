from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import (  # noqa: E402
    DeepSeekBatchPostClassifier,
    DeepSeekPostClassifier,
    build_batch_post_classifier,
    build_post_classifier,
)


class ClassifierProviderRoutingTests(unittest.TestCase):
    """2026-09-19 user decision: ALL classification runs on OpenCode Go.

    The builders must return OpenCode Go (DeepSeek V4 Flash) classifiers
    directly — never a Codex-backed chain, and never a fallback wrapper that
    could silently switch to Codex.
    """

    def test_post_builder_is_opencode_only(self) -> None:
        classifier = build_post_classifier(Path("schema.json"), Path("."))
        self.assertIsInstance(classifier, DeepSeekPostClassifier)
        self.assertEqual(classifier.model_name, "deepseek-v4-flash")
        self.assertEqual(classifier.prompt_version, "kol-post-opencode-go-v1")

    def test_batch_builder_is_opencode_only(self) -> None:
        classifier = build_batch_post_classifier(Path("schema.json"), Path("."))
        self.assertIsInstance(classifier, DeepSeekBatchPostClassifier)
        self.assertEqual(classifier.model_name, "deepseek-v4-flash")
        self.assertEqual(
            classifier.prompt_version, "kol-morning-opencode-go-batch-v1"
        )

    def test_no_fallback_wrapper_is_returned(self) -> None:
        from kol_posts import FallbackBatchPostClassifier, FallbackPostClassifier

        self.assertNotIsInstance(
            build_post_classifier(Path("schema.json"), Path(".")),
            FallbackPostClassifier,
        )
        self.assertNotIsInstance(
            build_batch_post_classifier(Path("schema.json"), Path(".")),
            FallbackBatchPostClassifier,
        )


if __name__ == "__main__":
    unittest.main()
