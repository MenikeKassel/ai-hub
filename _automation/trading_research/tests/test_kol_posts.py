from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import (  # noqa: E402
    CredentialStorageError,
    CredentialValidationError,
    KeyringCredentialStore,
    KolPostStore,
    ModelWorkerBusyError,
    ModelProviderUnavailableError,
    RapidOcrBatchClassifier,
    RuleClassifier,
    TwitterAuthenticationError,
    TwitterCliProvider,
    TwitterProviderError,
    TwitterRateLimitError,
    extract_zhihu_digest_attributions,
    initialize_seed_kols,
    load_stock_aliases,
    normalise_twitter_post,
    normalise_zhihu_answer,
    process_pending_with_ocr,
    _raise_codex_failure,
    run_post_fetch,
    validate_event_draft,
    validate_model_payload,
    _run_command_with_tree_timeout,
)
from rapid_ocr_batch import _rapidocr_rows  # noqa: E402


def tweet_payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "2076000000000000001",
        "text": "关注 002414 高德红外，业绩超预期，明天继续看多。",
        "url": "https://x.com/example/status/2076000000000000001",
        "author": {"screenName": "example", "name": "示例KOL"},
        "metrics": {"likes": 10, "retweets": 2, "replies": 1, "views": 1000},
        "createdAtISO": "2026-07-13T08:30:00+00:00",
        "media": [
            {
                "type": "photo",
                "url": "https://pbs.twimg.com/media/example.jpg",
                "width": 1200,
                "height": 800,
            }
        ],
        "urls": [],
        "isRetweet": False,
        "retweetedBy": None,
        "lang": "zh",
    }
    value.update(overrides)
    return value


class StoreTests(unittest.TestCase):
    def test_fetch_run_progress_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            run_id = store.start_fetch_run(50, total_kols=12)

            store.update_fetch_progress(
                run_id,
                processed_kols=5,
                successful_kols=4,
                failed_kols=1,
                new_posts=17,
                candidate_posts=3,
                stage="fetching",
            )

            run = store.recent_fetch_runs(1)[0]
            self.assertEqual(12, run["total_kols"])
            self.assertEqual(5, run["processed_kols"])
            self.assertEqual(17, run["new_posts"])
            self.assertEqual("fetching", run["stage"])

    def test_codex_usage_limit_is_reported_as_provider_unavailable(self) -> None:
        with self.assertRaisesRegex(
            ModelProviderUnavailableError,
            "Codex usage limit reached; try again at Jul 25th, 2026 6:50 PM",
        ):
            _raise_codex_failure(
                "You've hit your usage limit. Try again at Jul 25th, 2026 6:50 PM.",
                "Codex failed",
            )

    def test_zhihu_digest_is_secondhand_and_keeps_attributed_authors(self) -> None:
        text = (
            "Public KOL 32\n本周主题：看好能源。\n本周操作\n买入某能源股。\n"
            "Public KOL 50\n无\nPublic KOL 20\n本周主题：半导体对冲。\n本周观点\n控制风险。"
        )
        attributions = extract_zhihu_digest_attributions(text)
        self.assertEqual(["Public KOL 32", "Public KOL 20", "Public KOL 50"], [item["author_name"] for item in attributions])

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol(
                "Public KOL 9",
                "public_kol_9",
                platform="Zhihu",
                tracking_mode="aggregation",
            )
            kol = store.get_kol(kol_id)
            assert kol is not None
            post = normalise_zhihu_answer(
                {
                    "id": "2062243986040001481",
                    "text": text,
                    "articleTitle": "如何评价今日A股行情？",
                    "url": "https://www.zhihu.com/question/2060450831401497712/answer/2062243986040001481",
                    "author": {"name": "Public KOL 9", "screenName": "public_kol_9"},
                    "createdAtISO": "2026-07-20T01:00:00Z",
                    "metrics": {"likes": 12},
                },
                kol,
            )
            rule = RuleClassifier({"600900": "长江电力"}).classify(post)
            store.upsert_post(post)
            store.replace_digest_attributions(
                post.post_id,
                post.raw_payload["attributions"] + [
                    {"author_name": "public_kol_32", "section_text": "旧摘要使用主页 handle。"},
                    {
                        "author_name": "Public KOL 20的一周操作总结",
                        "section_text": "旧摘要使用固定标题后缀。",
                    },
                ],
            )

            self.assertEqual("aggregation", post.post_type)
            self.assertEqual("secondhand", rule.evidence_type)
            self.assertFalse(rule.is_candidate)
            self.assertEqual(5, len(store.list_digest_authors()))
            updated = store.upsert_digest_author_profiles([
                {
                    "display_name": "Public KOL 32",
                    "profile_url": "https://www.zhihu.com/people/public_kol_32",
                },
                {
                    "display_name": "Public KOL 20",
                    "profile_url": "https://www.zhihu.com/people/public_kol_20",
                },
            ])
            authors = {item["author_name"]: item for item in store.list_digest_authors()}
            self.assertEqual(2, len(updated))
            self.assertEqual(3, len(authors))
            self.assertEqual("public_kol_32", authors["Public KOL 32"]["profile_handle"])
            self.assertEqual(2, authors["Public KOL 32"]["summary_count"])
            self.assertEqual("linked_only", authors["Public KOL 20"]["tracking_status"])
            self.assertEqual(2, authors["Public KOL 20"]["summary_count"])
            self.assertEqual("", authors["Public KOL 50"]["profile_url"])

    def test_zhihu_direct_profile_answer_can_be_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol(
                "Public KOL 20",
                "public_kol_20",
                platform="Zhihu",
                tracking_mode="direct_profile",
            )
            kol = store.get_kol(kol_id)
            assert kol is not None
            post = normalise_zhihu_answer(
                {
                    "id": "2063202890978693514",
                    "text": str(tweet_payload()["text"]),
                    "articleTitle": "A share morning plan",
                    "url": "https://www.zhihu.com/question/2060450831401497712/answer/2063202890978693514",
                    "author": {"name": "Public KOL 20", "screenName": "public_kol_20"},
                    "createdAtISO": "2026-07-22T02:05:01Z",
                    "updatedAtISO": "2026-07-22T02:05:01Z",
                },
                kol,
            )
            rule = RuleClassifier().classify(post)

            self.assertEqual("answer", post.post_type)
            self.assertEqual("", post.provider_warning)
            self.assertEqual([], post.raw_payload["attributions"])
            self.assertEqual("original_pre_event", rule.evidence_type)
            self.assertTrue(rule.is_candidate)

    def test_zhihu_pre_onboarding_pending_answers_are_archived_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol(
                "Public KOL 20",
                "public_kol_20",
                platform="Zhihu",
                tracking_mode="direct_profile",
            )
            kol = store.get_kol(kol_id)
            assert kol is not None
            post = normalise_zhihu_answer(
                {
                    "id": "2063202890978693514",
                    "text": str(tweet_payload()["text"]),
                    "articleTitle": "Historical answer",
                    "url": "https://www.zhihu.com/question/2060450831401497712/answer/2063202890978693514",
                    "author": {"name": "Public KOL 20", "screenName": "public_kol_20"},
                    "createdAtISO": "2026-07-01T02:05:01Z",
                    "updatedAtISO": "2026-07-01T02:05:01Z",
                },
                kol,
            )
            store.upsert_post(post)
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))

            self.assertEqual(1, store.reconcile_zhihu_historical_backfill())
            self.assertEqual(0, store.reconcile_zhihu_historical_backfill())
            self.assertEqual("ignored", store.get_post(post.post_id)["review_status"])
            self.assertEqual(
                "zhihu_direct_profile_backfill_archive_only",
                store.latest_review(post.post_id, "ignored")["detail"]["policy"],
            )

    def test_kol_identity_is_platform_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            x_id, x_created = store.add_kol("Same X", "same")
            z_id, z_created = store.add_kol("Same Zhihu", "same", platform="Zhihu")

            self.assertTrue(x_created)
            self.assertTrue(z_created)
            self.assertNotEqual(x_id, z_id)
            self.assertEqual(x_id, store.get_kol_by_handle("same", "X")["id"])
            self.assertEqual(z_id, store.get_kol_by_handle("same", "Zhihu")["id"])

    def test_x_auth_failure_does_not_block_zhihu_provider(self) -> None:
        class BrokenX:
            name = "twitter-cli"

            def fetch_user_posts(self, handle, max_count):
                raise TwitterAuthenticationError("expired")

        class Zhihu:
            name = "zhihu-local"

            def fetch_user_posts(self, handle, max_count):
                return [{
                    "id": "2062243986040001481",
                    "text": "Public KOL 32\n本周主题：能源。",
                    "articleTitle": "A股行情",
                    "url": "https://www.zhihu.com/question/2060450831401497712/answer/2062243986040001481",
                    "author": {"name": "Public KOL 9"},
                    "createdAtISO": "2026-07-20T01:00:00Z",
                }]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("X one", "x_one")
            store.add_kol("X two", "x_two")
            store.add_kol("Public KOL 9", "public_kol_9", platform="Zhihu", tracking_mode="aggregation")
            result = run_post_fetch(
                store,
                BrokenX(),
                platform_providers={"zhihu": Zhihu()},
                sleep_seconds=0,
                retry_delays=(),
                download_media=False,
            )

        self.assertEqual(1, result.successful_kols)
        self.assertEqual(2, result.failed_kols)
        self.assertEqual(1, result.new_posts)
        self.assertEqual("failed", result.auth_status)

    def test_stale_fetch_run_is_closed_without_touching_fresh_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            stale = store.start_fetch_run(50)
            fresh = store.start_fetch_run(50)
            current = datetime.now(ZoneInfo("Asia/Shanghai"))
            with store.connect() as db:
                db.execute(
                    "UPDATE fetch_runs SET started_at=? WHERE run_id=?",
                    ((current - timedelta(hours=2)).isoformat(timespec="seconds"), stale),
                )

            recovered = store.interrupt_stale_fetch_runs(now=current, max_age_minutes=60)
            rows = {item["run_id"]: item for item in store.recent_fetch_runs()}

            self.assertEqual(1, recovered)
            self.assertEqual("failed", rows[stale]["status"])
            self.assertIn("stale_run_recovered", rows[stale]["errors"])
            self.assertEqual("running", rows[fresh]["status"])

    def test_rapid_ocr_batch_adapter_uses_isolated_runtime_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / "runner.py"
            runner.write_text(
                """
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--manifest'); p.add_argument('--output'); a=p.parse_args()
items=json.loads(Path(a.manifest).read_text(encoding='utf-8'))
Path(a.output).write_text(json.dumps({items[0]['post_id']:{'text':'002414 高德红外 看多','error':'','average_confidence':0.98,'lines':[{'text':'002414 高德红外 看多','confidence':0.98,'box':[[0,0],[1,0],[1,1],[0,1]]}]}},ensure_ascii=False),encoding='utf-8')
""".strip(),
                encoding="utf-8",
            )
            image = root / "1.jpg"
            image.write_bytes(b"fixture")
            classifier = RapidOcrBatchClassifier(
                Path(sys.executable),
                runner,
                timeout_seconds=10,
            )

            result = classifier.classify([{
                "post_id": "2076000000000000001",
                "local_media": [{"path": str(image)}],
            }])

            self.assertTrue(classifier.available())
            self.assertEqual("002414 高德红外 看多", result["2076000000000000001"]["text"])
            self.assertEqual(0.98, result["2076000000000000001"]["average_confidence"])
            self.assertEqual("002414 高德红外 看多", result["2076000000000000001"]["lines"][0]["text"])

    def test_command_timeout_terminates_the_process_tree(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            _run_command_with_tree_timeout(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                input_text="",
                timeout_seconds=0.1,
                operation="fixture",
            )

    def test_model_worker_lock_is_global_to_the_posts_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posts.db"
            first = KolPostStore(path, Path(tmp) / "media")
            second = KolPostStore(path, Path(tmp) / "media")

            with first.model_worker():
                with self.assertRaises(ModelWorkerBusyError):
                    with second.model_worker():
                        self.fail("second classifier acquired the global worker lock")

    def test_seed_watchlist_contains_six_enabled_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            created = initialize_seed_kols(store)
            handles = {row["handle"] for row in store.list_kols(status="active")}

            self.assertEqual(6, created)
            self.assertEqual(
                {"public_kol_1", "public_kol_2", "public_kol_3", "public_kol_4", "public_kol_5", "Public KOL 6"},
                handles,
            )
            self.assertNotIn("aleabitoreddit", handles)
            self.assertNotIn("artinmemes", handles)

    def test_post_upsert_is_idempotent_and_preserves_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            initialize_seed_kols(store)
            kol = store.get_kol_by_handle("public_kol_2")
            assert kol is not None
            post = normalise_twitter_post(tweet_payload(), kol)

            self.assertTrue(store.upsert_post(post))
            store.set_review(post.post_id, "ignored", "不是荐股")
            self.assertFalse(store.upsert_post(post))
            saved = store.get_post(post.post_id)

            self.assertEqual("ignored", saved["review_status"])
            self.assertEqual(1, store.count_posts())


class NormalisationTests(unittest.TestCase):
    def test_quote_and_media_are_preserved(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        payload = tweet_payload(
            quotedTweet={
                "id": "100",
                "text": "上游材料供给紧张",
                "author": {"screenName": "source", "name": "原作者"},
            }
        )

        post = normalise_twitter_post(payload, kol)

        self.assertEqual("quote", post.post_type)
        self.assertEqual("上游材料供给紧张", post.quoted_text)
        self.assertEqual("photo", post.media[0]["type"])
        self.assertEqual("2026-07-13T16:30:00+08:00", post.posted_at)

    def test_pure_retweet_is_marked_secondhand(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        post = normalise_twitter_post(
            tweet_payload(isRetweet=True, retweetedBy="example"),
            kol,
        )

        self.assertEqual("retweet", post.post_type)

    def test_reply_metadata_is_preserved_when_provider_exposes_it(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        post = normalise_twitter_post(
            tweet_payload(inReplyToStatusId="2075000000000000000", inReplyToScreenName="source"),
            kol,
        )

        self.assertEqual("reply", post.post_type)
        self.assertEqual("2075000000000000000", post.reply_to_id)
        self.assertEqual("source", post.reply_to_author)

    def test_non_numeric_post_id_is_rejected_before_path_use(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        with self.assertRaises(ValueError):
            normalise_twitter_post(tweet_payload(id="../../outside"), kol)


class ClassificationTests(unittest.TestCase):
    def test_rapidocr_rows_treats_missing_arrays_as_empty(self) -> None:
        class EmptyOutput:
            txts = None
            scores = None
            boxes = None

        self.assertEqual([], list(_rapidocr_rows(EmptyOutput())))

    def test_rapidocr_rows_accepts_array_like_values_without_truth_testing(self) -> None:
        class ArrayLike:
            def __iter__(self):
                return iter(["value"])

            def __bool__(self):
                raise ValueError("ambiguous")

        class Output:
            txts = ArrayLike()
            scores = ArrayLike()
            boxes = ArrayLike()

        self.assertEqual([("value", "value", "value")], list(_rapidocr_rows(Output())))

    def test_numbered_recommendation_list_is_split_from_retrospective_claims(self) -> None:
        text = (
            "马前炮：7 月 17 日个股分享\n"
            "1.时空科技\n"
            "2.美盈森\n"
            "3.紫光股份\n"
            "昨日在如此极端行情下内部群推的两只票赤峰黄金和瑞芯微应声大涨，"
            "内部学习群已移至 Discord 平台，私聊狙哥 388 一个月，国内平台不做开放，"
            "同行内鬼太多。"
        )
        classifier = RuleClassifier(
            {
                "605178": "时空科技",
                "002303": "美盈森",
                "000938": "紫光股份",
                "600988": "赤峰黄金",
                "603893": "瑞芯微",
            }
        )

        rule_result = classifier.classify({"text": text})
        payload = classifier.classify_structured_text({"text": text})

        self.assertTrue(rule_result.is_candidate)
        self.assertEqual("long", rule_result.direction)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("recommendation", payload["content_type"])
        self.assertEqual("original_pre_event", payload["evidence_type"])
        self.assertEqual(
            ["605178", "002303", "000938"],
            [draft["symbol"] for draft in payload["drafts"]],
        )
        self.assertTrue(all(draft["mention_kind"] == "recommendation" for draft in payload["drafts"]))
        self.assertIn("赤峰黄金、瑞芯微属于昨日推荐的复盘声称", payload["summary"])

    def test_rule_classifier_extracts_symbol_and_long_direction(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        post = normalise_twitter_post(tweet_payload(), kol)

        result = RuleClassifier().classify(post)

        self.assertTrue(result.is_candidate)
        self.assertGreaterEqual(result.score, 60)
        self.assertEqual("002414", result.symbols[0])
        self.assertEqual("long", result.direction)
        self.assertIn("stock_code", result.reasons)

    def test_rule_classifier_neutral_risk_text_has_no_direction(self) -> None:
        # "风险" is a neutral word; it must not flip a post to short, and a
        # bare "关注" without bullish context must not force long.
        for text in ("注意风险", "关注该股风险", "该股值得关注"):
            result = RuleClassifier().classify(
                {"text": text, "post_type": "original", "media": []}
            )
            self.assertEqual("", result.direction, f"neutral text misclassified: {text}")

    def test_rule_classifier_tie_between_directional_words_yields_no_direction(self) -> None:
        # One bullish and one bearish token is a tie -> no direction.
        result = RuleClassifier().classify(
            {"text": "有机会但建议卖出", "post_type": "original", "media": []}
        )
        self.assertEqual("", result.direction)

    def test_rule_classifier_clear_long_and_short_directions(self) -> None:
        long_result = RuleClassifier().classify(
            {"text": "推荐买入，布局机会很大", "post_type": "original", "media": []}
        )
        self.assertEqual("long", long_result.direction)
        short_result = RuleClassifier().classify(
            {"text": "该股风险很大，建议回避离场", "post_type": "original", "media": []}
        )
        self.assertEqual("short", short_result.direction)

    def test_retweet_cannot_become_direct_candidate(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        post = normalise_twitter_post(
            tweet_payload(isRetweet=True, retweetedBy="example"),
            kol,
        )

        result = RuleClassifier().classify(post)

        self.assertFalse(result.is_candidate)
        self.assertEqual("secondhand", result.evidence_type)

    def test_financial_image_enters_manual_codex_queue_without_symbol_text(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        post = normalise_twitter_post(tweet_payload(text=""), kol)

        result = RuleClassifier().classify(post)

        self.assertTrue(result.is_candidate)
        self.assertIn("financial_image_review", result.reasons)

    def test_known_company_alias_maps_back_to_a_share_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            aliases_path = Path(tmp) / "watchlist.csv"
            aliases_path.write_text("symbol,name\n600900,长江电力\n", encoding="utf-8")
            aliases = load_stock_aliases(aliases_path)
        kol = {"id": 1, "handle": "example", "display_name": "示例KOL"}
        post = normalise_twitter_post(tweet_payload(text="继续看多长江电力。", media=[]), kol)

        result = RuleClassifier(aliases).classify(post)

        self.assertEqual(["600900"], result.symbols)
        self.assertTrue(result.is_candidate)

    def test_ocr_six_digit_amount_must_exist_in_instrument_aliases(self) -> None:
        kol = {"id": 1, "handle": "example", "display_name": "KOL"}
        post = normalise_twitter_post(tweet_payload(text="看多", media=[]), kol)

        result = RuleClassifier({"600900": "长江电力"}).classify(
            post,
            supplemental_text="成交额 115421.00 亿；长江电力 600900",
        )

        self.assertEqual(["600900"], result.symbols)
        self.assertNotIn("115421", result.symbols)

    def test_event_draft_requires_one_symbol_and_pre_event_evidence(self) -> None:
        valid = {
            "symbol": "002414",
            "security_name": "高德红外",
            "direction": "long",
            "thesis": "业绩预告超预期",
            "evidence_type": "original_pre_event",
        }
        self.assertEqual([], validate_event_draft(valid))
        self.assertIn("one_symbol_per_event", validate_event_draft({**valid, "symbol": "002414,601888"}))
        self.assertIn("evidence_type", validate_event_draft({**valid, "evidence_type": "retrospective"}))

    def test_invalid_codex_payload_is_rejected_after_cli_schema_check(self) -> None:
        with self.assertRaises(ValueError):
            validate_model_payload({"content_type": "recommendation", "drafts": []})
        with self.assertRaises(ValueError):
            validate_model_payload({
                "content_type": "recommendation",
                "evidence_type": "original_pre_event",
                "confidence": 0.9,
                "summary": "fixture",
                "drafts": [],
                "unexpected": True,
            })

    def test_local_ocr_enriches_rules_before_codex_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolPostStore(root / "posts.db", root / "media")
            initialize_seed_kols(store)
            kol = store.get_kol_by_handle("public_kol_2")
            post = normalise_twitter_post(tweet_payload(text=""), kol)
            store.upsert_post(post)
            image = root / "media" / post.post_id / "1.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fixture")
            store.save_local_media(
                post.post_id,
                [{"type": "image", "path": str(image), "sha256": "fixture", "bytes": 7}],
            )
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))

            class FakeOcr:
                provider_name = "fixture-ocr"

                def classify(self, posts):
                    return {posts[0]["post_id"]: {
                        "text": str(tweet_payload()["text"]),
                        "error": "",
                        "average_confidence": 0.97,
                        "lines": [{"text": "002414 高德红外", "confidence": 0.97, "box": []}],
                    }}

            completed, failed = process_pending_with_ocr(
                store,
                FakeOcr(),
                RuleClassifier(),
                limit=8,
            )
            store.prepare_model_queue()
            saved = store.get_post(post.post_id)

            self.assertEqual((1, 0), (completed, failed))
            self.assertEqual("completed", saved["ocr_status"])
            self.assertEqual("fixture-ocr", saved["ocr_provider"])
            self.assertEqual(0.97, saved["ocr_confidence"])
            self.assertEqual("002414 高德红外", saved["ocr_details"][0]["text"])
            self.assertIn("002414", saved["rule_symbols"])
            self.assertEqual("not_needed", saved["model_status"])

    def test_non_candidate_stale_ocr_request_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KolPostStore(root / "posts.db", root / "media")
            initialize_seed_kols(store)
            kol = store.get_kol_by_handle("public_kol_2")
            post = normalise_twitter_post(tweet_payload(text="普通闲聊"), kol)
            store.upsert_post(post)
            image = root / "media" / post.post_id / "1.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"fixture")
            store.save_local_media(
                post.post_id,
                [{"type": "image", "path": str(image), "sha256": "fixture", "bytes": 7}],
            )
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))
            with store.connect() as db:
                db.execute(
                    "UPDATE classifications SET is_candidate=0,ocr_status='not_requested' WHERE post_id=?",
                    (post.post_id,),
                )

            self.assertEqual([], store.claim_posts_for_ocr(1))
            self.assertEqual("not_needed", store.get_post(post.post_id)["ocr_status"])


class ProviderAndFetchTests(unittest.TestCase):
    def test_credential_store_rejects_cookie_headers_before_keyring_write(self) -> None:
        store = KeyringCredentialStore()

        with self.assertRaises(CredentialValidationError):
            store.save("auth_token=" + "a" * 40, "c" * 160)
        with self.assertRaises(CredentialValidationError):
            store.save("a" * 40, "ct0=" + "c" * 160 + "; lang=zh-CN")
        with self.assertRaises(CredentialValidationError):
            store.save("a" * 40, "c" * 2048)

    def test_credential_store_rolls_back_a_partial_write(self) -> None:
        store = KeyringCredentialStore()

        with (
            patch("keyring.get_password", return_value=None),
            patch("keyring.set_password", side_effect=[None, OSError("fixture failure")]),
            patch("keyring.delete_password") as delete_password,
        ):
            with self.assertRaises(CredentialStorageError):
                store.save("a" * 40, "c" * 160)

        self.assertEqual(2, delete_password.call_count)

    def test_twitter_cli_provider_uses_credentials_without_putting_them_in_command(self) -> None:
        calls = []

        class Credentials:
            def load(self):
                return {"TWITTER_AUTH_TOKEN": "secret-auth", "TWITTER_CT0": "secret-ct0"}

        class Result:
            returncode = 0
            stdout = "[" + __import__("json").dumps(tweet_payload(), ensure_ascii=False) + "]"
            stderr = ""

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return Result()

        provider = TwitterCliProvider("twitter", Credentials(), runner, proxy_url="http://127.0.0.1:7897")
        result = provider.fetch_user_posts("example", 50)

        self.assertEqual(1, len(result.posts))
        self.assertEqual("twitter-cli", result.provider)
        self.assertNotIn("secret-auth", " ".join(calls[0][0]))
        self.assertEqual("secret-auth", calls[0][1]["env"]["TWITTER_AUTH_TOKEN"])
        self.assertEqual("http://127.0.0.1:7897", calls[0][1]["env"]["TWITTER_PROXY"])

    def test_daily_fetch_is_idempotent(self) -> None:
        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                return [tweet_payload()]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            initialize_seed_kols(store)
            first = run_post_fetch(store, Provider(), sleep_seconds=0, download_media=False)
            second = run_post_fetch(store, Provider(), sleep_seconds=0, download_media=False)

            self.assertEqual(1, first.new_posts)
            self.assertEqual(0, second.new_posts)
            self.assertEqual(1, store.count_posts())

    def test_morning_batch_queue_does_not_refetch_completed_accounts(self) -> None:
        calls: list[str] = []

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                calls.append(handle)
                return [
                    tweet_payload(
                        id=str(2076000000000001000 + len(calls)),
                        url=f"https://x.com/{handle}/status/{2076000000000001000 + len(calls)}",
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("First", "first")
            store.add_kol("Second", "second")
            run_post_fetch(
                store,
                Provider(),
                batch_key="morning:2026-07-24:x",
                sleep_seconds=0,
                download_media=False,
            )
            second = run_post_fetch(
                store,
                Provider(),
                batch_key="morning:2026-07-24:x",
                sleep_seconds=0,
                download_media=False,
            )

            self.assertEqual(2, len(calls))
            self.assertEqual(0, second.successful_kols)
            self.assertEqual(0, second.failed_kols)
            self.assertEqual(0, store.fetch_queue_status("morning:2026-07-24:x")["pending"])

    def test_rate_limit_leaves_current_and_remaining_accounts_for_resume(self) -> None:
        first_calls: list[str] = []
        resumed_calls: list[str] = []

        class LimitedProvider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                first_calls.append(handle)
                if len(first_calls) == 18:
                    raise TwitterRateLimitError("429")
                return [
                    tweet_payload(
                        id=str(2076000000000010000 + len(first_calls)),
                        url=f"https://x.com/{handle}/status/{2076000000000010000 + len(first_calls)}",
                    )
                ]

        class HealthyProvider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                resumed_calls.append(handle)
                return [
                    tweet_payload(
                        id=str(2076000000000020000 + len(resumed_calls)),
                        url=f"https://x.com/{handle}/status/{2076000000000020000 + len(resumed_calls)}",
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            for index in range(20):
                store.add_kol(f"Example {index}", f"example_{index:02d}")
            first = run_post_fetch(
                store,
                LimitedProvider(),
                batch_key="morning:2026-07-24:x",
                sleep_seconds=0,
                retry_delays=(),
                rate_limit_cooldown_seconds=0,
                download_media=False,
            )
            second = run_post_fetch(
                store,
                HealthyProvider(),
                batch_key="morning:2026-07-24:x",
                sleep_seconds=0,
                retry_delays=(),
                rate_limit_cooldown_seconds=0,
                download_media=False,
            )

            self.assertEqual(18, len(first_calls))
            self.assertEqual(17, first.successful_kols)
            self.assertEqual(1, first.failed_kols)
            self.assertEqual(3, len(resumed_calls))
            self.assertEqual(3, second.successful_kols)
            self.assertEqual(0, store.fetch_queue_status("morning:2026-07-24:x")["pending"])

    def test_gap_search_expands_timeline_until_the_saved_cursor_is_found(self) -> None:
        requested: list[int] = []
        ids = [str(2076000000000030000 + index) for index in range(120)]
        saved_cursor = ids[79]

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                requested.append(max_count)
                return [
                    tweet_payload(
                        id=post_id,
                        url=f"https://x.com/{handle}/status/{post_id}",
                        media=[],
                    )
                    for post_id in ids[:max_count]
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            with store.connect() as db:
                db.execute(
                    "UPDATE kols SET last_post_id=?,last_fetched_at=? WHERE id=?",
                    (saved_cursor, "2099-07-01T19:00:00+08:00", kol_id),
                )
            result = run_post_fetch(
                store,
                Provider(),
                max_count=50,
                sleep_seconds=0,
                download_media=False,
            )

            self.assertEqual([50, 100], requested)
            self.assertEqual([], result.gap_kols)
            self.assertEqual("success", store.get_kol(kol_id)["fetch_status"])

    def test_short_first_page_still_searches_for_saved_cursor(self) -> None:
        requested: list[int] = []
        saved_cursor = "2076000000000030999"

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                requested.append(max_count)
                ids = ["2076000000000031001", saved_cursor] if max_count > 50 else ["2076000000000031001"]
                return [
                    tweet_payload(
                        id=post_id,
                        url=f"https://x.com/{handle}/status/{post_id}",
                        media=[],
                    )
                    for post_id in ids
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            with store.connect() as db:
                db.execute(
                    "UPDATE kols SET last_post_id=?,last_fetched_at=? WHERE id=?",
                    (saved_cursor, "2099-07-01T19:00:00+08:00", kol_id),
                )

            result = run_post_fetch(
                store,
                Provider(),
                max_count=50,
                sleep_seconds=0,
                download_media=False,
            )

            self.assertEqual([50, 100], requested)
            self.assertEqual([], result.gap_kols)

    def test_interrupted_cursor_search_remains_in_persistent_queue(self) -> None:
        saved_cursor = "2076000000000031999"

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                if max_count > 50:
                    raise TwitterRateLimitError("429")
                return [
                    tweet_payload(
                        id="2076000000000032001",
                        url=f"https://x.com/{handle}/status/2076000000000032001",
                        media=[],
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            with store.connect() as db:
                db.execute(
                    "UPDATE kols SET last_post_id=?,last_fetched_at=? WHERE id=?",
                    (saved_cursor, "2099-07-01T19:00:00+08:00", kol_id),
                )

            result = run_post_fetch(
                store,
                Provider(),
                max_count=50,
                batch_key="morning:2026-07-24:x",
                sleep_seconds=0,
                retry_delays=(),
                download_media=False,
            )

            self.assertEqual(["example"], result.gap_kols)
            self.assertEqual(1, store.fetch_queue_status("morning:2026-07-24:x")["pending"])
            self.assertEqual(saved_cursor, store.get_kol(kol_id)["last_post_id"])

    def test_targeted_fetch_only_visits_requested_handles(self) -> None:
        calls: list[str] = []

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                calls.append(handle)
                return [tweet_payload(id=f"20760000000000000{len(calls)}")]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("First", "first")
            store.add_kol("Second", "second")
            result = run_post_fetch(
                store,
                Provider(),
                handles={"@SECOND"},
                sleep_seconds=0,
                download_media=False,
            )

        self.assertEqual(["second"], calls)
        self.assertEqual(1, result.successful_kols)
        self.assertEqual(0, result.failed_kols)

    def test_first_fetch_backfills_100_then_uses_daily_limit(self) -> None:
        requested = []

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                requested.append(max_count)
                return []

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("Example", "example")
            run_post_fetch(store, Provider(), max_count=50, sleep_seconds=0, download_media=False)
            run_post_fetch(store, Provider(), max_count=50, sleep_seconds=0, download_media=False)

        self.assertEqual([100, 50], requested)

    def test_rate_limit_uses_finite_retry(self) -> None:
        calls = 0

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise TwitterRateLimitError("429")
                return [tweet_payload()]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("Example", "example")
            result = run_post_fetch(
                store,
                Provider(),
                sleep_seconds=0,
                download_media=False,
                retry_delays=(0, 0),
            )

        self.assertEqual(3, calls)
        self.assertEqual(1, result.successful_kols)
        self.assertEqual(0, result.failed_kols)

    def test_authentication_failure_counts_all_unfetched_accounts(self) -> None:
        calls = 0

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                nonlocal calls
                calls += 1
                raise TwitterAuthenticationError("expired")

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            for index in range(3):
                store.add_kol(f"Example {index}", f"example_{index}")
            result = run_post_fetch(store, Provider(), sleep_seconds=0, retry_delays=(0, 0))

        self.assertEqual(1, calls)
        self.assertEqual(3, result.failed_kols)
        self.assertEqual("failed", result.auth_status)

    def test_missing_last_post_after_three_days_marks_gap(self) -> None:
        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                return [
                    tweet_payload(
                        id=str(2076000000000000100 + index),
                        url=f"https://x.com/example/status/{2076000000000000100 + index}",
                        media=[],
                    )
                    for index in range(max_count)
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            with store.connect() as db:
                db.execute(
                    "UPDATE kols SET last_post_id=?,last_fetched_at=? WHERE id=?",
                    ("2075000000000000000", "2026-07-01T19:00:00+08:00", kol_id),
                )
            result = run_post_fetch(store, Provider(), max_count=50, sleep_seconds=0, download_media=False)

            self.assertEqual(["example"], result.gap_kols)
            self.assertEqual("gap_detected", store.get_kol(kol_id)["fetch_status"])

    def test_model_summary_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("Example", "example")
            kol = store.get_kol_by_handle("example")
            assert kol is not None
            post = normalise_twitter_post(tweet_payload(), kol)
            store.upsert_post(post)
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))
            store.save_model_classification(
                post.post_id,
                {
                    "content_type": "recommendation",
                    "evidence_type": "original_pre_event",
                    "confidence": 0.9,
                    "summary": "高德红外的事前看多观点。",
                    "drafts": [],
                },
                model_name="codex",
                prompt_version="test",
            )

            self.assertEqual("高德红外的事前看多观点。", store.get_post(post.post_id)["model_summary"])

            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))
            self.assertEqual("original_pre_event", store.get_post(post.post_id)["evidence_type"])

            store.save_model_classification(
                post.post_id,
                {
                    "content_type": "other",
                    "evidence_type": "secondhand",
                    "confidence": 0.95,
                    "summary": "二手转述。",
                    "drafts": [],
                },
                model_name="codex",
                prompt_version="test",
            )
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))
            self.assertEqual("secondhand", store.get_post(post.post_id)["evidence_type"])

    def test_failed_image_download_is_audited_and_retried(self) -> None:
        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                return [tweet_payload()]

        saved = [{"type": "image", "path": "fixture.jpg", "sha256": "abc", "bytes": 3}]
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("Example", "example")
            with patch("kol_posts.download_images", side_effect=[([], ["network error"]), (saved, [])]) as downloader:
                run_post_fetch(store, Provider(), sleep_seconds=0)
                first_review = store.latest_review("2076000000000000001")
                run_post_fetch(store, Provider(), sleep_seconds=0)

            self.assertEqual(2, downloader.call_count)
            self.assertEqual("media_download_failed", first_review["action"])
            self.assertEqual(saved, store.get_post("2076000000000000001")["local_media"])

    def test_partial_image_retries_merge_prior_successes(self) -> None:
        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                return [tweet_payload(media=[
                    {"type": "photo", "url": "https://img.example/1.jpg"},
                    {"type": "photo", "url": "https://img.example/2.jpg"},
                ])]

        first = {"type": "image", "source_url": "https://img.example/1.jpg", "path": "1.jpg", "sha256": "one", "bytes": 3}
        second = {"type": "image", "source_url": "https://img.example/2.jpg", "path": "2.jpg", "sha256": "two", "bytes": 3}
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            store.add_kol("Example", "example")
            with patch("kol_posts.download_images", side_effect=[([first], ["second failed"]), ([second], [])]):
                run_post_fetch(store, Provider(), sleep_seconds=0)
                run_post_fetch(store, Provider(), sleep_seconds=0)

            media = store.get_post("2076000000000000001")["local_media"]
            self.assertEqual({"one", "two"}, {item["sha256"] for item in media})

    def test_queued_backfill_uses_requested_depth_and_clears_only_after_success(self) -> None:
        requested: list[int] = []

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                requested.append(max_count)
                return [
                    tweet_payload(
                        id=str(2076000000000000001 + index),
                        url=f"https://x.com/example/status/{2076000000000000001 + index}",
                    )
                    for index in range(max_count)
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            store.queue_backfill(kol_id, 250)
            run_post_fetch(store, Provider(), sleep_seconds=0, download_media=False)
            with store.connect() as db:
                db.execute(
                    "UPDATE kols SET last_fetched_at='2020-01-01T00:00:00+08:00' WHERE id=?",
                    (kol_id,),
                )
            run_post_fetch(store, Provider(), sleep_seconds=0, download_media=False)
            run_post_fetch(store, Provider(), sleep_seconds=0, download_media=False)
            kol = store.get_kol(kol_id)

            self.assertEqual([100, 200, 250], requested)
            self.assertEqual(0, kol["backfill_requested"])
            self.assertEqual("completed", kol["backfill_status"])
            self.assertEqual(250, kol["backfill_completed_depth"])
            self.assertTrue(kol["last_success_at"])

    def test_short_backfill_result_is_visible_and_not_repeated_automatically(self) -> None:
        requested: list[int] = []

        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                requested.append(max_count)
                return [tweet_payload()]

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            store.queue_backfill(kol_id, 250)
            run_post_fetch(store, Provider(), sleep_seconds=0, download_media=False)
            run_post_fetch(store, Provider(), max_count=50, sleep_seconds=0, download_media=False)
            kol = store.get_kol(kol_id)

            self.assertEqual([100, 50], requested)
            self.assertEqual(250, kol["backfill_requested"])
            self.assertEqual("needs_review", kol["backfill_status"])
            self.assertEqual(1, kol["backfill_result_count"])
            self.assertIn("returned 1 of 100", kol["backfill_warning"])

    def test_failed_backfill_remains_queued_and_increments_failure_count(self) -> None:
        class Provider:
            name = "fixture"

            def fetch_user_posts(self, handle, max_count):
                raise TwitterProviderError("offline")

        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            store.queue_backfill(kol_id, 300)
            run_post_fetch(store, Provider(), sleep_seconds=0, retry_delays=())
            kol = store.get_kol(kol_id)

            self.assertEqual(300, kol["backfill_requested"])
            self.assertEqual("queued", kol["backfill_status"])
            self.assertEqual(1, kol["consecutive_failures"])

    def test_gap_preserves_cursor_without_counting_as_an_account_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            store.update_fetch_state(
                kol_id, "2076000000000000001", "success", fetched_count=1, requested_count=1
            )
            before = store.get_kol(kol_id)
            store.mark_fetch_failed(kol_id)
            store.update_fetch_state(
                kol_id, "2076000000000009999", "gap_detected", fetched_count=50, requested_count=50
            )
            after = store.get_kol(kol_id)

            self.assertEqual(before["last_post_id"], after["last_post_id"])
            self.assertEqual(before["last_success_at"], after["last_success_at"])
            self.assertEqual(0, after["consecutive_failures"])
            self.assertTrue(after["last_gap_at"])

    def test_model_candidate_is_claimed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("Example", "example")
            kol = store.get_kol(kol_id)
            post = normalise_twitter_post(tweet_payload(text=""), kol)
            store.upsert_post(post)
            store.save_rule_classification(post.post_id, RuleClassifier().classify(post))

            first = store.claim_posts_for_model(1)
            second = store.claim_posts_for_model(1)

            self.assertEqual([post.post_id], [item["post_id"] for item in first])
            self.assertEqual([], second)
            self.assertEqual("running", store.get_post(post.post_id)["model_status"])


if __name__ == "__main__":
    unittest.main()
