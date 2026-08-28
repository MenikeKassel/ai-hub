from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import KolPostStore, RuleClassifier, normalise_twitter_post  # noqa: E402
from kol_tracker import EventRecord, KolStore  # noqa: E402
from review_agent import (  # noqa: E402
    PolicyDecision,
    ReviewAgentRepository,
    ReviewAgentRunner,
    ReviewPolicy,
    rollback_decision,
)


def draft(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "symbol": "002414",
        "security_name": "高德红外",
        "direction": "long",
        "thesis": "业绩超预期，继续看多",
        "evidence_type": "original_pre_event",
        "confidence": 0.98,
        "evidence_spans": ["002414 高德红外", "业绩超预期，继续看多"],
        "evidence_source": "text",
        "conditions": [],
        "depends_on_ocr": False,
        "mention_kind": "recommendation",
    }
    value.update(overrides)
    return value


def post(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "post_id": "2076000000000000001",
        "post_type": "original",
        "posted_at": "2026-07-15T10:30:00+08:00",
        "text": "关注 002414 高德红外，业绩超预期，继续看多。",
        "article_text": "",
        "quoted_text": "",
        "reply_to_id": "",
        "provider_warning": "",
        "local_media": [],
        "content_type": "recommendation",
        "evidence_type": "original_pre_event",
        "model_status": "completed",
        "confidence": 0.99,
        "drafts": [draft()],
    }
    value.update(overrides)
    return value


def instrument(symbol: str) -> dict[str, str] | None:
    if symbol == "002414":
        return {
            "symbol": symbol,
            "name": "高德红外",
            "instrument_type": "stock",
            "status": "active",
            "exchange": "SZ",
        }
    return None


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ReviewPolicy()

    def test_clear_text_recommendation_is_auto_approved(self) -> None:
        result = self.policy.evaluate(post(), instrument)

        self.assertEqual("auto_approve", result.decision)
        self.assertEqual([], result.validator_errors)

    def test_evidence_must_be_present_in_original_text(self) -> None:
        result = self.policy.evaluate(
            post(drafts=[draft(evidence_spans=["从未出现的证据"])]),
            instrument,
        )

        self.assertEqual("needs_human", result.decision)
        self.assertIn("evidence_span_missing", result.validator_errors)

    def test_image_dependent_recommendation_needs_human(self) -> None:
        result = self.policy.evaluate(
            post(
                local_media=[{"path": "fixture.png"}],
                drafts=[draft(depends_on_ocr=True, evidence_source="ocr")],
            ),
            instrument,
        )

        self.assertEqual("needs_human", result.decision)
        self.assertIn("image_or_ocr_dependent", result.validator_errors)

    def test_any_attached_image_keeps_v1_in_human_review(self) -> None:
        result = self.policy.evaluate(
            post(local_media=[{"path": "fixture.png"}]),
            instrument,
        )

        self.assertEqual("needs_human", result.decision)
        self.assertIn("image_or_ocr_dependent", result.validator_errors)

    def test_multi_stock_and_conditional_events_need_human(self) -> None:
        multi = self.policy.evaluate(post(drafts=[draft(), draft(symbol="600519")]), instrument)
        conditional = self.policy.evaluate(
            post(drafts=[draft(conditions=["站稳五日线后再买"])]),
            instrument,
        )

        self.assertEqual("needs_human", multi.decision)
        self.assertIn("multiple_drafts", multi.validator_errors)
        self.assertEqual("needs_human", conditional.decision)
        self.assertIn("conditional_entry", conditional.validator_errors)

    def test_omitted_second_stock_and_thin_thesis_cannot_auto_approve(self) -> None:
        def lookup(symbol: str) -> dict[str, str] | None:
            if symbol == "600519":
                return {
                    "symbol": symbol,
                    "name": "贵州茅台",
                    "instrument_type": "stock",
                    "status": "active",
                    "exchange": "SH",
                }
            return instrument(symbol)

        omitted = self.policy.evaluate(
            post(text="关注 002414 高德红外和 600519 贵州茅台，业绩超预期，继续看多。"),
            lookup,
        )
        thin = self.policy.evaluate(post(drafts=[draft(thesis="看多")]), instrument)

        self.assertEqual("needs_human", omitted.decision)
        self.assertIn("multiple_stock_mentions", omitted.validator_errors)
        self.assertEqual("needs_human", thin.decision)
        self.assertIn("missing_thesis", thin.validator_errors)

    def test_short_and_source_conflict_need_human(self) -> None:
        short = self.policy.evaluate(post(drafts=[draft(direction="short")]), instrument)
        conflict = self.policy.evaluate(post(provider_warning="source_conflict"), instrument)

        self.assertEqual("needs_human", short.decision)
        self.assertIn("short_direction", short.validator_errors)
        self.assertEqual("needs_human", conflict.decision)
        self.assertIn("source_conflict", conflict.validator_errors)

    def test_retrospective_is_auto_excluded_at_high_confidence(self) -> None:
        result = self.policy.evaluate(
            post(
                content_type="recommendation",
                evidence_type="retrospective",
                confidence=0.99,
                drafts=[],
            ),
            instrument,
        )

        self.assertEqual("auto_exclude", result.decision)

    def test_high_confidence_methodology_without_drafts_is_auto_ignored(self) -> None:
        result = self.policy.evaluate(
            post(content_type="methodology", evidence_type="ambiguous", confidence=0.99, drafts=[]),
            instrument,
        )

        self.assertEqual("auto_ignore", result.decision)


class ReviewAgentRepositoryTests(unittest.TestCase):
    def test_migration_creates_shadow_settings_and_audit_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            repository = ReviewAgentRepository(store)

            self.assertEqual("shadow", repository.get_settings()["mode"])
            with store.connect() as db:
                names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'review_agent_%'"
                    ).fetchall()
                }
                version = int(db.execute("PRAGMA user_version").fetchone()[0])

            self.assertEqual(
                {"review_agent_runs", "review_agent_decisions", "review_agent_settings"},
                names,
            )
            self.assertGreaterEqual(version, 11)

    def test_interrupted_runs_are_closed_before_the_next_worker_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            repository = ReviewAgentRepository(store)
            run_id = repository.start_run("shadow")

            recovered = repository.recover_interrupted_runs()

            self.assertEqual(1, recovered)
            with store.connect() as db:
                row = db.execute(
                    "SELECT status,completed_at,errors_json FROM review_agent_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            self.assertEqual("failed", row["status"])
            self.assertTrue(row["completed_at"])
            self.assertIn("worker exited", row["errors_json"])

    def test_validation_metrics_only_count_the_current_shadow_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("示例KOL", "example", "A股")
            value = normalise_twitter_post(
                {
                    "id": "2076000000000000999",
                    "text": "关注 002414 高德红外",
                    "url": "https://x.com/example/status/2076000000000000999",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-15T02:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
            )
            store.upsert_post(value)
            repository = ReviewAgentRepository(store)
            current = store.get_post(value.post_id)
            policy = PolicyDecision("auto_approve", 0.99, ["fixture"], [], [draft()], [])
            repository.save_decision(current, repository.start_run("shadow"), "shadow", policy)
            repository.save_decision(current, repository.start_run("enabled"), "enabled", policy)

            summary = repository.summary()
            self.assertEqual(2, summary["decision_counts"]["auto_approve"])
            self.assertEqual(1, summary["validation_decision_counts"]["auto_approve"])

            with store.connect() as db:
                db.execute(
                    "UPDATE review_agent_decisions SET created_at='2020-01-01T00:00:00+08:00' WHERE mode='shadow'"
                )
            repository.set_mode("shadow", reset_validation=True)
            self.assertEqual(0, repository.summary()["validation_total_decisions"])

    def test_first_month_random_audit_sampling_is_deterministic_and_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = ReviewAgentRepository(
                KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            )
            repository.set_mode("enabled", force=True)
            first = [repository.requires_random_audit(str(value)) for value in range(100)]
            second = [repository.requires_random_audit(str(value)) for value in range(100)]

            self.assertEqual(first, second)
            self.assertGreater(sum(first), 0)
            self.assertLess(sum(first), 25)

    def test_human_symbol_correction_is_a_critical_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = KolPostStore(Path(tmp) / "posts.db", Path(tmp) / "media")
            kol_id, _ = store.add_kol("示例KOL", "example", "A股")
            value = normalise_twitter_post(
                {
                    "id": "2076000000000000777",
                    "text": "关注 002414 高德红外",
                    "url": "https://x.com/example/status/2076000000000000777",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-15T02:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
            )
            store.upsert_post(value)
            repository = ReviewAgentRepository(store)
            saved = repository.save_decision(
                store.get_post(value.post_id),
                repository.start_run("shadow"),
                "shadow",
                PolicyDecision("auto_approve", 0.99, ["fixture"], [], [draft()], []),
            )

            repository.mark_overridden_for_post(
                value.post_id,
                "approved",
                [draft(symbol="600519")],
            )

            self.assertIn(
                "human_corrected_stock_or_direction",
                repository.get_decision(saved["id"])["validator_errors"],
            )
            self.assertEqual(1, repository.summary()["critical_error_count"])


class ReviewAgentRunnerTests(unittest.TestCase):
    class FakeClassifier:
        prompt_version = "kol-post-v3"

        def classify(self, _post: dict[str, object]) -> dict[str, object]:
            return {
                "content_type": "recommendation",
                "evidence_type": "original_pre_event",
                "confidence": 0.99,
                "summary": "明确的事前文字推荐",
                "drafts": [draft()],
            }

    class FakeMarket:
        def __init__(self) -> None:
            self.queued: list[str] = []
            self.restored: list[tuple[str, str, str]] = []

        def get_instrument(self, symbol: str) -> dict[str, str] | None:
            value = instrument(symbol)
            if value:
                return {
                    **value,
                    "list_date": "2010-07-16",
                    "first_seen_at": "2026-07-01",
                }
            return None

        def upsert_instrument(self, _value: object) -> None:
            pass

        def enqueue_sync(self, symbol: str, *, reason: str) -> None:
            self.queued.append(f"{symbol}:{reason}")

        def restore_research_state(self, symbol: str, *, lifecycle: str, last_mentioned_at: str) -> None:
            self.restored.append((symbol, lifecycle, last_mentioned_at))

    class TimeoutOcr:
        timeout_seconds: float | None = None

        def __init__(self) -> None:
            self.seen_timeout = 0.0

        def classify(self, _posts: list[dict[str, object]]) -> dict[str, dict[str, str]]:
            self.seen_timeout = float(self.timeout_seconds or 0)
            raise RuntimeError("fixture OCR timeout")

    class UnexpectedClassifier:
        prompt_version = "kol-post-v3"

        def classify(self, _post: dict[str, object]) -> dict[str, object]:
            raise AssertionError("structured numbered lists must not call Codex")

    def make_runtime(self, root: Path) -> tuple[KolPostStore, KolStore, "ReviewAgentRunnerTests.FakeMarket"]:
        posts = KolPostStore(root / "posts.db", root / "media")
        kol_id, _ = posts.add_kol("示例KOL", "example", "A股")
        normalised = normalise_twitter_post(
            {
                "id": "2076000000000000001",
                "text": "关注 002414 高德红外，业绩超预期，继续看多。",
                "url": "https://x.com/example/status/2076000000000000001",
                "author": {"screenName": "example", "name": "示例KOL"},
                "createdAtISO": "2026-07-15T02:30:00+00:00",
                "media": [],
                "isRetweet": False,
            },
            {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
        )
        posts.upsert_post(normalised)
        posts.save_rule_classification(
            normalised.post_id,
            RuleClassifier({"002414": "高德红外"}).classify(asdict(normalised)),
        )
        return posts, KolStore(root / "events"), self.FakeMarket()

    def test_shadow_run_records_decision_without_applying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            runner = ReviewAgentRunner(
                posts,
                events,
                market,
                classifier=self.FakeClassifier(),
                rule_classifier=RuleClassifier({"002414": "高德红外"}),
            )

            result = runner.run(mode="shadow", max_items=1)

            self.assertEqual(1, result["counts"]["auto_approve"])
            self.assertEqual("pending", posts.get_post("2076000000000000001")["review_status"])
            self.assertEqual([], events.load_events())
            decisions = ReviewAgentRepository(posts).list_decisions()
            self.assertEqual("proposed", decisions[0]["status"])

    def test_structured_numbered_list_bypasses_codex_and_creates_three_drafts(self) -> None:
        aliases = {
            "605178": "时空科技",
            "002303": "美盈森",
            "000938": "紫光股份",
            "600988": "赤峰黄金",
            "603893": "瑞芯微",
        }

        class StructuredMarket(self.FakeMarket):
            def get_instrument(self, symbol: str) -> dict[str, str] | None:
                name = aliases.get(symbol)
                if not name:
                    return None
                return {
                    "symbol": symbol,
                    "name": name,
                    "instrument_type": "stock",
                    "status": "active",
                    "exchange": "SH" if symbol.startswith("6") else "SZ",
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            posts = KolPostStore(root / "posts.db", root / "media")
            kol_id, _ = posts.add_kol("示例KOL", "example", "A股")
            normalised = normalise_twitter_post(
                {
                    "id": "2078000000000000001",
                    "text": (
                        "马前炮：7 月 17 日个股分享\n"
                        "1.时空科技\n2.美盈森\n3.紫光股份\n"
                        "昨日内部群推的两只票赤峰黄金和瑞芯微应声大涨。"
                    ),
                    "url": "https://x.com/example/status/2078000000000000001",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-17T01:00:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                {"id": kol_id, "handle": "example", "display_name": "示例KOL"},
            )
            posts.upsert_post(normalised)
            rules = RuleClassifier(aliases)
            posts.save_rule_classification(normalised.post_id, rules.classify(asdict(normalised)))
            runner = ReviewAgentRunner(
                posts,
                KolStore(root / "events"),
                StructuredMarket(),
                classifier=self.UnexpectedClassifier(),
                rule_classifier=rules,
            )

            result = runner.run(mode="shadow", max_items=1)
            saved = posts.get_post(normalised.post_id)

            self.assertEqual(1, result["counts"]["needs_human"], result)
            self.assertEqual("structured-rules", saved["model_name"])
            self.assertEqual(
                ["605178", "002303", "000938"],
                [draft["symbol"] for draft in saved["drafts"]],
            )

    def test_run_recovers_interrupted_ocr_and_bounds_the_stage_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            with posts.connect() as db:
                db.execute(
                    "UPDATE posts SET local_media_json=? WHERE post_id=?",
                    (json.dumps([{"path": "fixture.png"}]), "2076000000000000001"),
                )
                db.execute(
                    "UPDATE classifications SET ocr_status='running' WHERE post_id=?",
                    ("2076000000000000001",),
                )
            ocr = self.TimeoutOcr()
            runner = ReviewAgentRunner(
                posts,
                events,
                market,
                classifier=self.FakeClassifier(),
                ocr_classifier=ocr,
                rule_classifier=RuleClassifier({"002414": "高德红外"}),
            )

            result = runner.run(mode="shadow", max_runtime_minutes=0.05, max_items=1)

            self.assertEqual(1, result["counts"]["needs_human"], result)
            self.assertGreater(ocr.seen_timeout, 0)
            self.assertLessEqual(ocr.seen_timeout, 3)
            self.assertEqual("failed", posts.get_post("2076000000000000001")["ocr_status"])

    def test_text_candidates_are_scheduled_before_image_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            with posts.connect() as db:
                db.execute(
                    "UPDATE posts SET local_media_json=? WHERE post_id=?",
                    (json.dumps([{"path": "slow-image.png"}]), "2076000000000000001"),
                )
            kol = posts.get_kol_by_handle("example")
            assert kol is not None
            text_post = normalise_twitter_post(
                {
                    "id": "2075000000000000001",
                    "text": "关注 002414 高德红外，业绩超预期，继续看多。",
                    "url": "https://x.com/example/status/2075000000000000001",
                    "author": {"screenName": "example", "name": "示例KOL"},
                    "createdAtISO": "2026-07-14T02:30:00+00:00",
                    "media": [],
                    "isRetweet": False,
                },
                kol,
            )
            posts.upsert_post(text_post)
            posts.save_rule_classification(
                text_post.post_id,
                RuleClassifier({"002414": "高德红外"}).classify(asdict(text_post)),
            )
            runner = ReviewAgentRunner(posts, events, market)

            ordered = runner._pending_posts()

            self.assertEqual(text_post.post_id, ordered[0]["post_id"])

    def test_single_image_ocr_timeout_is_capped_for_long_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            with posts.connect() as db:
                db.execute(
                    "UPDATE posts SET local_media_json=? WHERE post_id=?",
                    (json.dumps([{"path": "fixture.png"}]), "2076000000000000001"),
                )
                db.execute(
                    "UPDATE classifications SET ocr_status='not_requested' WHERE post_id=?",
                    ("2076000000000000001",),
                )
            ocr = self.TimeoutOcr()
            runner = ReviewAgentRunner(
                posts,
                events,
                market,
                classifier=None,
                ocr_classifier=ocr,
            )

            runner._ensure_classification(
                posts.get_post("2076000000000000001"),
                timeout_seconds=1500,
            )

            self.assertLessEqual(ocr.seen_timeout, 90)

    def test_applied_auto_approval_can_be_rolled_back_without_deleting_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            posts.save_model_classification(
                "2076000000000000001",
                self.FakeClassifier().classify({}),
                model_name="codex",
                prompt_version="kol-post-v3",
            )
            current = posts.get_post("2076000000000000001")
            repository = ReviewAgentRepository(posts)
            run_id = repository.start_run("enabled")
            saved = repository.save_decision(
                current,
                run_id,
                "enabled",
                PolicyDecision("auto_approve", 0.99, ["strict_text_recommendation"], [], [draft()], []),
            )
            repository.set_mode("enabled", force=True)
            runner = ReviewAgentRunner(posts, events, market)

            runner.apply_decision(saved["id"])
            rolled_back = rollback_decision(repository, posts, events, saved["id"], market)

            self.assertEqual("rolled_back", rolled_back["status"])
            self.assertEqual("pending", posts.get_post("2076000000000000001")["review_status"])
            self.assertEqual("excluded", events.load_events()[0].status)
            leads = posts.list_stock_leads(post_id="2076000000000000001")
            self.assertEqual("pending", leads[0]["status"])
            self.assertTrue(events.load_marks() == [])
            self.assertEqual("shadow", repository.get_settings()["mode"])
            self.assertEqual(1, len(events.pending_notifications()))
            self.assertEqual([("002414", "tracking", "")], market.restored)

    def test_rollback_does_not_exclude_a_reused_preexisting_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            existing = EventRecord(
                event_id="KOL-0099",
                kol_name="示例KOL",
                platform="X",
                source_url="https://x.com/example/status/2076000000000000001",
                source_note="post:2076000000000000001",
                posted_at="2026-07-15T10:30:00+08:00",
                symbol="002414",
                security_name="高德红外",
                direction="long",
                thesis="人工已登记的同源事件",
                status="active",
            )
            events.register_event(existing)
            posts.save_model_classification(
                "2076000000000000001",
                self.FakeClassifier().classify({}),
                model_name="codex",
                prompt_version="kol-post-v3",
            )
            repository = ReviewAgentRepository(posts)
            saved = repository.save_decision(
                posts.get_post("2076000000000000001"),
                repository.start_run("enabled"),
                "enabled",
                PolicyDecision("auto_approve", 0.99, ["strict_text_recommendation"], [], [draft()], []),
            )
            repository.set_mode("enabled", force=True)
            runner = ReviewAgentRunner(posts, events, market)

            runner.apply_decision(saved["id"])
            applied = repository.get_decision(saved["id"])
            rollback_decision(repository, posts, events, saved["id"])

            self.assertEqual(["KOL-0099"], applied["event_ids"])
            self.assertEqual([], applied["created_event_ids"])
            self.assertEqual("active", events.load_events()[0].status)

    def test_enabled_run_resumes_a_proposed_decision_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            posts.save_model_classification(
                "2076000000000000001",
                self.FakeClassifier().classify({}),
                model_name="codex",
                prompt_version="kol-post-v3",
            )
            repository = ReviewAgentRepository(posts)
            saved = repository.save_decision(
                posts.get_post("2076000000000000001"),
                repository.start_run("enabled"),
                "enabled",
                PolicyDecision("auto_approve", 0.99, ["strict_text_recommendation"], [], [draft()], []),
            )
            repository.set_mode("enabled", force=True)
            runner = ReviewAgentRunner(posts, events, market)
            runner.repository.summary = lambda: {"activation_ready": True}  # type: ignore[method-assign]

            result = runner.run(mode="enabled", max_items=1)

            self.assertEqual(1, result["counts"]["auto_approve"])
            self.assertEqual("applied", repository.get_decision(saved["id"])["status"])
            self.assertEqual("approved", posts.get_post("2076000000000000001")["review_status"])

    def test_apply_respects_a_human_decision_made_after_the_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            repository = ReviewAgentRepository(posts)
            saved = repository.save_decision(
                posts.get_post("2076000000000000001"),
                repository.start_run("enabled"),
                "enabled",
                PolicyDecision("auto_approve", 0.99, ["strict_text_recommendation"], [], [draft()], []),
            )
            posts.set_review("2076000000000000001", "excluded", "人工先行排除")
            runner = ReviewAgentRunner(posts, events, market)

            symbols = runner.apply_decision(saved["id"])

            self.assertEqual([], symbols)
            self.assertEqual("excluded", posts.get_post("2076000000000000001")["review_status"])
            self.assertEqual("overridden", repository.get_decision(saved["id"])["status"])
            self.assertEqual([], events.load_events())

    def test_enabled_mode_holds_ten_percent_sample_for_human_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posts, events, market = self.make_runtime(Path(tmp))
            runner = ReviewAgentRunner(
                posts,
                events,
                market,
                classifier=self.FakeClassifier(),
                rule_classifier=RuleClassifier({"002414": "高德红外"}),
            )
            runner.repository.set_mode("enabled", force=True)
            runner.repository.summary = lambda: {"activation_ready": True}  # type: ignore[method-assign]
            runner.repository.requires_random_audit = lambda _post_id: True  # type: ignore[method-assign]

            result = runner.run(mode="enabled", max_items=1)

            self.assertEqual(1, result["counts"]["auto_approve"])
            self.assertEqual([], events.load_events())
            self.assertEqual("pending", posts.get_post("2076000000000000001")["review_status"])
            self.assertIn(
                "random_audit_first_30_days",
                runner.repository.list_decisions()[0]["reason_codes"],
            )


if __name__ == "__main__":
    unittest.main()
