from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import (  # noqa: E402
    CodexPostClassifier,
    FallbackXPostProvider,
    KolPostStore,
    ProviderFetchResult,
    ReaderCredentialStore,
    TwitterAuthenticationError,
    TwitterProviderError,
    XtfNitterProvider,
    _post_provider_warning,
    normalise_twitter_post,
    run_post_fetch,
)


def twitter_payload(*, text: str = "primary text", created_at: str = "2026-07-13T08:30:00+00:00") -> dict:
    return {
        "id": "2076000000000000001",
        "text": text,
        "url": "https://x.com/example/status/2076000000000000001",
        "author": {"screenName": "example", "name": "Example"},
        "metrics": {"likes": 10, "retweets": 2, "replies": 1, "views": 1000},
        "createdAtISO": created_at,
        "media": [],
        "isRetweet": False,
    }


class MultiSourceProviderTests(unittest.TestCase):
    def test_reader_credentials_are_isolated_from_sealed_primary_store(self) -> None:
        reader = ReaderCredentialStore()
        self.assertEqual("ai-hub/twitter-reader", reader.service_name)

        class Primary:
            name = "twitter-cli"
            credentials = reader

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("twitter-cli", [], [], [])

        fallback = FallbackXPostProvider(Primary(), XtfNitterProvider("xtf", "http://127.0.0.1:9377"))
        self.assertIs(reader, fallback.credentials)

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_codex_classifier_resolves_an_executable_windows_shim(self) -> None:
        classifier = CodexPostClassifier(Path("schema.json"), Path.cwd())

        self.assertIn(Path(classifier.command).suffix.lower(), {".cmd", ".exe"})

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_codex_classifier_finds_npm_shim_when_gateway_path_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "npm" / "codex.cmd"
            shim.parent.mkdir()
            shim.write_text("@echo off\r\n", encoding="ascii")
            with patch.dict(os.environ, {"APPDATA": tmp}), patch("kol_posts.shutil.which", return_value=None):
                classifier = CodexPostClassifier(Path("schema.json"), Path.cwd())

        self.assertEqual(shim, Path(classifier.command))

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_codex_classifier_runs_npm_javascript_through_node_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim = root / "codex.cmd"
            node = root / "node.exe"
            javascript = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            javascript.parent.mkdir(parents=True)
            shim.write_text("@echo off\r\n", encoding="ascii")
            node.write_bytes(b"fixture")
            javascript.write_text("", encoding="ascii")

            classifier = CodexPostClassifier(
                Path("schema.json"), Path.cwd(), command=str(shim)
            )

        self.assertEqual([str(node), str(javascript)], classifier.command_prefix)

    def test_only_canonical_nitter_snowflake_time_becomes_a_post_warning(self) -> None:
        payload = twitter_payload() | {"timestampSource": "snowflake"}

        self.assertEqual("", _post_provider_warning(payload, "twitter-cli"))
        self.assertEqual(
            "timestamp_recovered_from_snowflake",
            _post_provider_warning(payload, "nitter"),
        )

    def test_xtf_nitter_provider_maps_exact_timestamp_and_media(self) -> None:
        calls = []

        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps(
                {
                    "username": "example",
                    "backend": "nitter",
                    "tweets": [
                        {
                            "tweet_id": "2076000000000000001",
                            "author": "@example",
                            "author_name": "Example",
                            "text": "nitter text",
                            "time_ago": "Jul 13, 2026 \u00b7 8:30 AM UTC",
                            "likes": 12,
                            "retweets": 3,
                            "replies": 2,
                            "views": 1200,
                            "media": ["https://pbs.twimg.com/media/example.jpg"],
                        }
                    ],
                }
            )

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return Result()

        result = XtfNitterProvider("xtf", "http://127.0.0.1:9377", runner).fetch_user_posts("example", 50)

        self.assertEqual("nitter", result.provider)
        self.assertEqual("2026-07-13T08:30:00+00:00", result.posts[0]["createdAtISO"])
        self.assertEqual("photo", result.posts[0]["media"][0]["type"])
        self.assertEqual("http://127.0.0.1:9377", calls[0][1]["env"]["XTF_NITTER"])
        self.assertEqual("utf-8", calls[0][1]["env"]["PYTHONIOENCODING"])
        self.assertEqual(30, calls[0][1]["timeout"])
        self.assertIn("--backend", calls[0][0])
        self.assertIn("nitter", calls[0][0])

    def test_xtf_nitter_provider_accepts_windows_replacement_separator(self) -> None:
        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps(
                {
                    "tweets": [
                        {
                            "tweet_id": "2076000000000000001",
                            "author": "@example",
                            "text": "nitter text",
                            "time_ago": "Jul 14, 2026 \ufffd\ufffd 2:23 AM UTC",
                        }
                    ]
                }
            )

        result = XtfNitterProvider("xtf", runner=lambda *args, **kwargs: Result()).fetch_user_posts(
            "example", 1
        )

        self.assertEqual("2026-07-14T02:23:00+00:00", result.posts[0]["createdAtISO"])

    def test_xtf_nitter_provider_recovers_timestamp_from_snowflake(self) -> None:
        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps(
                {
                    "tweets": [
                        {
                            "tweet_id": "2076675438580248923",
                            "author": "@public_kol_2",
                            "text": "nitter text",
                            "time_ago": "@public_kol_2",
                        }
                    ]
                }
            )

        result = XtfNitterProvider("xtf", runner=lambda *args, **kwargs: Result()).fetch_user_posts(
            "public_kol_2", 1
        )

        self.assertEqual("2026-07-13T14:29:41+00:00", result.posts[0]["createdAtISO"])
        self.assertEqual("snowflake", result.posts[0]["timestampSource"])
        self.assertIn("timestamp_recovered_from_snowflake", result.warnings)

    def test_fallback_opens_auth_circuit_for_the_rest_of_the_run(self) -> None:
        class Primary:
            name = "twitter-cli"

            def __init__(self) -> None:
                self.calls = 0

            def fetch_user_posts(self, handle, max_count):
                self.calls += 1
                raise TwitterAuthenticationError("expired")

        class Fallback:
            name = "nitter"

            def __init__(self) -> None:
                self.calls = 0

            def fetch_user_posts(self, handle, max_count):
                self.calls += 1
                return ProviderFetchResult("nitter", [twitter_payload()], [], [])

        primary = Primary()
        fallback = Fallback()
        provider = FallbackXPostProvider(primary, fallback)

        first = provider.fetch_user_posts("example", 50)
        second = provider.fetch_user_posts("another", 50)

        self.assertEqual("nitter", first.provider)
        self.assertEqual("nitter", second.provider)
        self.assertEqual(1, primary.calls)
        self.assertEqual(2, fallback.calls)
        self.assertTrue(provider.primary_auth_failed)
        self.assertIn("primary_authentication_failed", first.warnings)

    def test_suspicious_empty_primary_uses_fallback(self) -> None:
        class EmptyPrimary:
            name = "twitter-cli"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("twitter-cli", [], [], [])

        class Fallback:
            name = "nitter"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("nitter", [twitter_payload()], [], [])

        result = FallbackXPostProvider(EmptyPrimary(), Fallback()).fetch_user_posts("example", 50)

        self.assertEqual("nitter", result.provider)
        self.assertIn("primary_suspicious_empty", result.warnings)

    def test_shadow_mode_compares_ids_but_returns_primary(self) -> None:
        class Primary:
            name = "twitter-cli"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("twitter-cli", [twitter_payload(text="primary")], [], [])

        class Fallback:
            name = "nitter"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("nitter", [twitter_payload(text="backup")], [], [])

        result = FallbackXPostProvider(Primary(), Fallback(), mode="shadow").fetch_user_posts("example", 50)

        self.assertEqual("twitter-cli", result.provider)
        self.assertEqual("primary", result.posts[0]["text"])
        self.assertEqual(1.0, result.comparisons[0]["coverage"])
        self.assertIn("shadow_compared", result.warnings)

    def test_shadow_mode_opens_fallback_circuit_after_first_failure(self) -> None:
        class Primary:
            name = "twitter-cli"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("twitter-cli", [twitter_payload()], [], [])

        class Fallback:
            name = "nitter"

            def __init__(self) -> None:
                self.calls = 0

            def fetch_user_posts(self, handle, max_count):
                self.calls += 1
                raise TwitterProviderError("nitter unavailable")

        fallback = Fallback()
        provider = FallbackXPostProvider(Primary(), fallback, mode="shadow")

        first = provider.fetch_user_posts("example", 50)
        second = provider.fetch_user_posts("another", 50)

        self.assertIn("shadow_fallback_failed", first.warnings)
        self.assertIn("shadow_fallback_circuit_open", second.warnings)
        self.assertEqual(1, fallback.calls)

    def test_shadow_mode_does_not_promote_fallback_when_primary_fails(self) -> None:
        class Primary:
            name = "twitter-cli"

            def fetch_user_posts(self, handle, max_count):
                raise TwitterAuthenticationError("expired")

        class Fallback:
            name = "nitter"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("nitter", [twitter_payload()], [], [])

        provider = FallbackXPostProvider(Primary(), Fallback(), mode="shadow")
        with self.assertRaises(TwitterAuthenticationError):
            provider.fetch_user_posts("example", 50)


class MultiSourceStoreTests(unittest.TestCase):
    def test_fresh_first_page_does_not_expand_stale_cursor_to_history_depth(self) -> None:
        calls: list[int] = []

        class TrackingProvider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                calls.append(max_count)
                return ProviderFetchResult("fixture", [], [], [])

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            with store.connect() as db:
                db.execute(
                    "UPDATE kols SET last_post_id=?,last_fetched_at=? WHERE id=?",
                    ("2076000000000000000", "2026-08-01T10:00:00+08:00", kol_id),
                )
            run_post_fetch(
                store,
                TrackingProvider(),
                max_count=20,
                fresh_first_page=True,
                batch_key="fresh-fixture",
                sleep_seconds=0,
                retry_delays=(),
                download_media=False,
            )

        self.assertEqual([20], calls)

    def test_store_migration_removes_run_level_warnings_from_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolPostStore(root / "posts.db", root / "media")
            kol_id, _ = store.add_kol("Example", "example", "A股")
            kol = store.get_kol(kol_id)
            assert kol is not None
            post = normalise_twitter_post(
                twitter_payload(),
                kol,
                provider_warning="timestamp_recovered_from_snowflake;shadow_coverage_below_threshold",
            )
            store.upsert_post(post)

            reopened = KolPostStore(root / "posts.db", root / "media")

            self.assertEqual("", reopened.get_post(post.post_id)["provider_warning"])

    def test_lower_priority_source_does_not_replace_primary_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            kol = store.get_kol(kol_id)
            assert kol is not None
            primary = normalise_twitter_post(twitter_payload(text="primary text"), kol, provider="twitter-cli")
            nitter = normalise_twitter_post(
                {**twitter_payload(text="nitter text"), "metrics": {"likes": 99}},
                kol,
                provider="nitter",
            )

            self.assertTrue(store.upsert_post(primary, run_id="run-primary"))
            self.assertFalse(store.upsert_post(nitter, run_id="run-nitter"))
            saved = store.get_post(primary.post_id)
            sources = store.list_post_sources(primary.post_id)

            self.assertEqual("primary text", saved["text"])
            self.assertEqual("twitter-cli", saved["canonical_provider"])
            self.assertEqual("nitter", saved["metrics_provider"])
            self.assertEqual(99, saved["metrics"]["likes"])
            self.assertEqual({"nitter", "twitter-cli"}, {row["provider"] for row in sources})

    def test_failed_fetch_does_not_advance_kol_cursor(self) -> None:
        class Broken:
            name = "broken"

            def fetch_user_posts(self, handle, max_count):
                raise TwitterAuthenticationError("fixture")

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            result = run_post_fetch(store, Broken(), sleep_seconds=0, retry_delays=())
            kol = store.get_kol(kol_id)
            assert kol is not None

            self.assertEqual(0, result.successful_kols)
            self.assertEqual("", kol["last_post_id"])
            self.assertEqual("", kol["last_fetched_at"])
            self.assertEqual("failed", kol["fetch_status"])

    def test_unusable_posts_are_not_saved_or_treated_as_a_success(self) -> None:
        class EmptyContent:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                return [{**twitter_payload(text=""), "media": []}]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            result = run_post_fetch(store, EmptyContent(), sleep_seconds=0, retry_delays=())
            kol = store.get_kol(kol_id)
            assert kol is not None

            self.assertEqual(0, result.successful_kols)
            self.assertEqual(0, store.count_posts())
            self.assertEqual("", kol["last_fetched_at"])

    def test_three_complete_shadow_runs_unlock_fallback_rollout(self) -> None:
        class Primary:
            name = "twitter-cli"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("twitter-cli", [twitter_payload()], [], [])

        class Fallback:
            name = "nitter"

            def fetch_user_posts(self, handle, max_count):
                return ProviderFetchResult("nitter", [twitter_payload()], [], [])

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("Example", "example")
            for index in range(3):
                provider = FallbackXPostProvider(Primary(), Fallback(), mode="shadow")
                result = run_post_fetch(
                    store,
                    provider,
                    sleep_seconds=0,
                    retry_delays=(),
                    download_media=False,
                )
                self.assertEqual(1, result.successful_kols)
                with store.connect() as db:
                    db.execute(
                        "UPDATE fetch_runs SET started_at=? WHERE run_id=?",
                        (f"2026-07-{10 + index:02d}T19:00:00+08:00", result.run_id),
                    )

            rollout = store.shadow_rollout_status()
            self.assertTrue(rollout["ready"])
            self.assertEqual(3, len(rollout["runs"]))


if __name__ == "__main__":
    unittest.main()
