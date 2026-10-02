from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_api import ApiSettings, create_app
from kol_posts import KolPostStore, RuleClassifier, normalise_twitter_post
from theme_leads import extract_theme_leads, load_theme_catalog, query_theme_leads, theme_evidence


class ThemeLeadTests(unittest.TestCase):
    def make_store(self, root: Path) -> tuple[KolPostStore, dict]:
        store = KolPostStore(root / "posts.db", root / "media")
        kol_id, _ = store.add_kol("原作者", "original")
        return store, store.get_kol(kol_id)

    def add_post(
        self, store: KolPostStore, kol: dict, post_id: str, text: str,
        posted_at: str = "2026-09-19T10:00:00+08:00", **extra,
    ):
        payload = {
            "id": post_id, "text": text, "createdAtISO": posted_at,
            "author": {"screenName": kol["handle"], "name": kol["display_name"]},
            "url": f"https://x.com/{kol['handle']}/status/{post_id}", **extra,
        }
        post = replace(normalise_twitter_post(payload, kol), fetched_at="2026-09-25T10:00:00+08:00")
        store.upsert_post(post)
        store.save_rule_classification(post_id, RuleClassifier({}).classify(post))
        return post

    def test_catalog_limits_product_aliases_and_has_research_only_mappings(self):
        catalog = load_theme_catalog()
        post = {"text": "兰亭集序很好。金枫是一个人名。白酒景气度。泛酒消费。", "posted_at": "2026-09-19"}
        hits = theme_evidence(post, catalog)
        self.assertEqual(["baijiu"], [value["theme_id"] for value in hits])
        hits = theme_evidence({"text": "兰庭黄酒口感不错。金枫股票估值仅作研究。微盘与大消费板块。"}, catalog)
        self.assertEqual({"huangjiu", "microcap", "consumption"}, {value["theme_id"] for value in hits})
        mapped = next(value for value in hits if value["theme_id"] == "huangjiu")["mapped_symbols"]
        self.assertEqual({("601579", "会稽山"), ("600059", "古越龙山"), ("600616", "金枫酒业")},
                         {(value["symbol"], value["name"]) for value in mapped})
        self.assertTrue(all(value["association"] == "research_only" for value in mapped))
        self.assertEqual([], theme_evidence({"text": "订单号16000591"}, catalog))
        self.assertEqual([], theme_evidence({"text": "把会稽山封给范蠡。", "article_title": "中国古代有名的商人有哪些？"}, catalog))
        self.assertEqual("huangjiu", theme_evidence({"text": "研究600059"}, catalog)[0]["theme_id"])

    def test_historical_claims_do_not_backdate_real_source_or_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            product = self.add_post(store, kol, "90001", "买了一瓶会稽山兰亭黄酒，口感不错。", "2026-09-15T10:00:00+08:00")
            retrospective = self.add_post(
                store, kol, "90002", "9月初反复提示过黄酒机会。8/19推荐黄酒板块。",
                articleTitle="为什么9月初推荐黄酒",
            )
            with patch("kol_sources.repository.now_iso", return_value="2026-10-02T14:00:00+08:00"):
                result = extract_theme_leads(store)
            self.assertEqual(2, result.created)
            response = query_theme_leads(store, theme_id="huangjiu", sort="oldest")
            first, recap = response["items"]
            self.assertEqual("product", first["kind"])
            self.assertEqual("retrospective", recap["kind"])
            self.assertIn("9月初", recap["claimed_timing"])
            self.assertIn("8/19", recap["claimed_timing"])
            self.assertEqual(product.posted_at, response["summary"][0]["first_posted_at"])
            self.assertEqual(retrospective.posted_at, recap["posted_at"])
            self.assertEqual("2026-10-02T14:00:00+08:00", response["summary"][0]["first_detected_at"])
            self.assertEqual("90001", response["summary"][0]["first_post_id"])
            self.assertEqual(product.url, response["summary"][0]["first_url"])
            self.assertEqual([], store.list_stock_leads())
            self.assertEqual("pending", store.get_post("90002")["review_status"])
            for item in response["items"]:
                for span in item["evidence"]:
                    self.assertEqual(item[span["field"]][span["start"]:span["end"]], span["text"])

    def test_local_future_clause_is_not_downgraded_by_other_theme_recap(self):
        hits = theme_evidence({
            "text": "白酒复盘，黄酒下周机会。9月初计划布局微盘。消费板块估值8.19倍。",
            "posted_at": "2026-09-19T10:00:00+08:00",
        }, load_theme_catalog())
        kinds = {value["theme_id"]: value["kind"] for value in hits}
        self.assertEqual("retrospective", kinds["baijiu"])
        self.assertEqual("prospective", kinds["huangjiu"])
        self.assertEqual("retrospective", kinds["microcap"])
        self.assertEqual("analysis", kinds["consumption"])

    def test_long_recap_frame_survives_later_macro_future_language(self):
        body = (
            "我于九月初反复提示过黄酒机会。Q1:为什么9月初推荐黄酒？\n"
            "白酒营销资源迅速向黄酒重置。\n" + "黄酒的产业属性需要研究。\n" * 30 +
            "未来黄酒有望完成全国化，黄酒的产业机会可以继续验证。"
        )
        hit = next(value for value in theme_evidence({
            "text": body, "article_title": "一个人的核心能力是什么？", "post_type": "answer",
            "posted_at": "2026-09-19T23:51:50+08:00",
        }, load_theme_catalog()) if value["theme_id"] == "huangjiu")
        self.assertEqual("retrospective", hit["kind"])
        self.assertEqual({"九月初", "9月初"}, set(hit["claimed_timing"]))
        self.assertIn("prospective", {value["kind"] for value in hit["evidence"]})

    def test_answer_question_context_does_not_become_author_recommendation(self):
        hit = theme_evidence({
            "text": "未研究，不知道。", "article_title": "你会推荐黄酒板块吗？",
            "post_type": "answer", "posted_at": "2026-09-19T10:00:00+08:00",
        }, load_theme_catalog())[0]
        self.assertEqual("quoted", hit["source_role"])
        self.assertEqual("secondhand", hit["kind"])
        self.assertEqual("question", hit["evidence"][0]["context_role"])

    def test_live_recap_is_not_a_new_topic_call(self):
        hit = theme_evidence({
            "text": "8.13号直播复盘总结\n几个嘉宾的直播观点。新消费黄酒加个会稽山。",
            "evidence_type": "retrospective", "posted_at": "2026-08-13T23:35:02+08:00",
        }, load_theme_catalog())[0]
        self.assertEqual("retrospective", hit["kind"])
        self.assertEqual("analysis", hit["evidence"][0]["kind"])

    def test_withdrawals_are_not_forward_calls_and_keep_exact_local_evidence(self):
        samples = (
            "某些人认知真低，黄酒炒的是铺货行情，白酒大商手里有高端客户，不过这个位置也不推荐了。有人认为还是赌博。",
            "黄酒后期不再推荐，我自己设置了止盈线，一旦跌破就走。不格局。后期就是刷量化数据，根据量化指导操作就好，减少主观判断",
            "明天别买入黄酒股票。", "黄酒没有机会。", "不看好黄酒。",
        )
        for text in samples:
            with self.subTest(text=text):
                hit = next(value for value in theme_evidence({"text": text, "posted_at": "2026-09-22T21:36:52+08:00"}, load_theme_catalog()) if value["theme_id"] == "huangjiu")
                self.assertEqual("analysis", hit["kind"])
                self.assertTrue(any(span.get("context_role") == "withdrawal" for span in hit["evidence"]))
                for span in hit["evidence"]:
                    self.assertEqual(text[span["start"]:span["end"]], span["text"])

    def test_withdrawal_does_not_contaminate_another_theme_and_past_claim_stays_retrospective(self):
        for text in ("不再推荐白酒，明天关注黄酒。", "明天关注黄酒，不再推荐白酒。"):
            with self.subTest(text=text):
                hits = {value["theme_id"]: value for value in theme_evidence({"text": text}, load_theme_catalog())}
                self.assertEqual("prospective", hits["huangjiu"]["kind"])
                self.assertEqual("analysis", hits["baijiu"]["kind"])
                self.assertFalse(any(span.get("context_role") == "withdrawal" for span in hits["huangjiu"]["evidence"]))
        hit = theme_evidence({"text": "9月初推荐过黄酒，如今黄酒不再推荐。", "posted_at": "2026-09-22T21:36:52+08:00"}, load_theme_catalog())[0]
        self.assertEqual("retrospective", hit["kind"])

    def test_batch_savepoint_isolates_a_bad_post_and_keeps_successful_signatures(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            for post_id in ("90001", "90002", "90003"):
                self.add_post(store, kol, post_id, "研究黄酒板块。")
            catalog = load_theme_catalog()
            posts = store.list_posts_for_theme_extraction(catalog.version)
            values = [(post, theme_evidence(post, catalog)) for post in posts]
            # A valid insert is attempted before the invalid insert for this
            # post; neither may survive the per-post rollback.
            invalid = {**values[1][1][0], "theme_id": "invalid", "kind": "bad"}
            values[1][1].append(invalid)
            with patch.object(store, "connect", wraps=store.connect) as connections:
                result = store.replace_theme_leads_batch(values, catalog.version)
                self.assertEqual(1, connections.call_count)
            self.assertEqual({"created": 2, "updated": 0, "removed": 0, "failed": 1}, result)
            self.assertEqual({"90001", "90003"}, {value["post_id"] for value in store.query_theme_leads()["items"]})
            pending = store.list_posts_for_theme_extraction(catalog.version)
            self.assertEqual(["90002"], [value["post_id"] for value in pending])
            self.assertEqual(1, extract_theme_leads(store, catalog=catalog).created)

    def test_source_metadata_retains_legacy_limits_and_edit_time_without_raw_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "90001", "研究黄酒板块。")
            self.add_post(store, kol, "90002", "研究黄酒板块。")
            with store.connect() as db:
                db.execute("UPDATE posts SET platform='Zhihu',post_type='answer' WHERE post_id IN ('90001','90002')")
                db.execute("UPDATE posts SET raw_json=? WHERE post_id='90002'", (json.dumps({
                    "surface": "answers", "updatedAtISO": "2026-10-01T15:00:00+08:00",
                    "coverage": {"status": "partial", "historical_complete": False,
                                 "requested_surfaces": ["answers", "ideas", "articles"],
                                 "failed_surfaces": ["ideas"], "posts": ["do not expose"]},
                }),))
            extract_theme_leads(store)
            items = {value["post_id"]: value for value in store.query_theme_leads()["items"]}
            self.assertEqual("legacy_answers_only", items["90001"]["source_coverage"]["status"])
            self.assertEqual("partial", items["90002"]["source_coverage"]["status"])
            self.assertEqual(["ideas"], items["90002"]["source_coverage"]["failed_surfaces"])
            self.assertEqual("2026-10-01T15:00:00+08:00", items["90002"]["source_updated_at"])
            self.assertNotIn("posts", items["90002"]["source_coverage"])
            self.assertNotIn("raw_json", items["90002"])

    def test_source_coverage_sanitizes_real_surface_shape_and_normalizes_progress(self):
        surfaces = {
            "answers": {"status": "success", "bounded": True, "exhausted": False, "pages": 2, "partial": False, "warnings": ["bounded_limit_reached"]},
            "articles": {"status": "success", "bounded": False, "exhausted": True, "pages": 1, "partial": False, "warnings": []},
            "ideas": {"status": "partial", "bounded": False, "exhausted": False, "pages": 3, "partial": True, "warnings": ["row_missing_id_text_or_question"]},
        }
        for detail in surfaces.values():
            detail.update({"posts": [{"text": "do-not-expose"}], "raw": "do-not-expose", "credentials": {"auth_token": "do-not-expose"}})
        value = KolPostStore._theme_lead_row({
            "matched_terms_json": "[]", "evidence_json": "[]", "claimed_timing_json": "[]", "mapped_symbols_json": "[]",
            "platform": "Zhihu", "post_type": "idea", "raw_json": json.dumps({
                "coverage": {"status": "partial", "historical_complete": False}, "surfaces": surfaces,
                "credentials": {"auth_token": "do-not-expose"},
            }),
        })
        progress = value["source_coverage"]["surfaces"]
        self.assertEqual({"answers": "bounded", "articles": "complete", "ideas": "partial"},
                         {name: summary["status"] for name, summary in progress.items()})
        self.assertEqual(["bounded_limit_reached"], progress["answers"]["warnings"])
        self.assertEqual(3, progress["ideas"]["pages"])
        self.assertFalse(value["source_coverage"]["historical_complete"])
        self.assertNotIn("do-not-expose", json.dumps(value))
        self.assertNotIn("raw_json", value)
        surfaces["articles"]["status"] = "failed"
        failed = KolPostStore._theme_lead_row({
            "matched_terms_json": "[]", "evidence_json": "[]", "claimed_timing_json": "[]", "mapped_symbols_json": "[]",
            "platform": "Zhihu", "post_type": "idea", "raw_json": json.dumps({"surfaces": surfaces}),
        })
        self.assertEqual("failed", failed["source_coverage"]["surfaces"]["articles"]["status"])

    def test_quoted_and_aggregation_evidence_are_not_independent_original_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "90001", "关注黄酒板块。")
            self.add_post(store, kol, "90002", "继续研究黄酒。")
            other_id, _ = store.add_kol("转述账号", "secondhand")
            other = store.get_kol(other_id)
            self.add_post(store, other, "90003", "引用一段", quotedTweet={
                "id": "89001", "text": "黄酒下周机会。", "author": {"screenName": "another-author"},
            })
            aggregate = self.add_post(store, other, "90004", "黄酒机会摘录。")
            with store.connect() as db:
                db.execute("UPDATE posts SET post_type='aggregation',raw_json=? WHERE post_id=?",
                           (json.dumps({"source_kind": "aggregation"}), aggregate.post_id))
            extract_theme_leads(store)
            result = query_theme_leads(store, theme_id="huangjiu")
            self.assertEqual(4, result["summary"][0]["post_count"])
            self.assertEqual(1, result["summary"][0]["source_count"])
            self.assertEqual(2, result["summary"][0]["secondhand_post_count"])
            items = {value["post_id"]: value for value in result["items"]}
            self.assertEqual("quoted", items["90003"]["source_role"])
            self.assertEqual("another-author", items["90003"]["quoted_author"])
            self.assertEqual("aggregation", items["90004"]["source_role"])
            self.assertEqual("secondhand", items["90004"]["kind"])

    def test_inventory_summary_is_global_while_dates_and_pagination_filter_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "90001", "研究黄酒板块。", "2026-05-01T10:00:00+08:00")
            self.add_post(store, kol, "90002", "黄酒值得关注。")
            self.add_post(store, kol, "90003", "继续研究黄酒。", "2026-09-20T10:00:00+08:00")
            extract_theme_leads(store, batch_size=1)
            result = query_theme_leads(store, query="会稽山", date_from="2026-09-01", page_size=1, page=2)
            self.assertEqual(2, result["total"])
            self.assertEqual("90002", result["items"][0]["post_id"])
            self.assertEqual(3, result["summary"][0]["post_count"])
            self.assertEqual("2026-05-01T10:00:00+08:00", result["summary"][0]["first_posted_at"])
            self.assertEqual(3, query_theme_leads(store, query="原作者")["total"])

    def test_incremental_ocr_refresh_catalog_change_and_stale_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "90001", "研究黄酒板块。")
            self.add_post(store, kol, "90002", "正文无主题，待核对图片。")
            catalog = load_theme_catalog()
            with patch("kol_sources.repository.now_iso", return_value="2026-10-02T14:00:00+08:00"):
                first = extract_theme_leads(store, catalog=catalog)
            self.assertEqual(2, first.processed_posts)
            self.assertEqual(0, extract_theme_leads(store, catalog=catalog).processed_posts)
            with store.connect() as db:
                # Deliberately leave classification updated_at unchanged: OCR
                # may complete more than once within the same clock second.
                db.execute("UPDATE classifications SET ocr_text='会稽山板块机会' WHERE post_id='90002'")
            refreshed = extract_theme_leads(store, catalog=catalog)
            self.assertEqual(1, refreshed.processed_posts)
            self.assertEqual("ocr_text", query_theme_leads(store, query="会稽山")["items"][0]["evidence"][0]["field"])
            bumped = replace(catalog, version=catalog.version + ":changed")
            self.assertEqual(2, extract_theme_leads(store, catalog=bumped).processed_posts)
            self.assertEqual("2026-10-02T14:00:00+08:00", store.query_theme_leads(sort="oldest")["items"][0]["first_detected_at"])
            with store.connect() as db:
                db.execute("UPDATE posts SET text='主题已撤去',content_hash=? WHERE post_id='90001'",
                           (hashlib.sha256(b"removed").hexdigest(),))
                db.execute("UPDATE classifications SET ocr_text='' WHERE post_id='90002'")
            result = extract_theme_leads(store, catalog=bumped)
            self.assertEqual(2, result.removed)
            self.assertEqual([], store.query_theme_leads()["items"])

    def test_theme_api_is_read_only_and_extract_never_routes_to_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(ApiSettings(
                runtime_root=root / "runtime", frontend_dist=root / "dist",
                codex_schema=Path(__file__).resolve().parents[1] / "kol_classifier_schema.json",
            ))
            store = app.state.post_store
            kol = store.list_kols()[0]
            self.add_post(store, kol, "90001", "只研究黄酒板块。")
            client = TestClient(app)
            with (
                patch.object(app.state.market_store, "instrument_map", side_effect=AssertionError("market read")),
                patch.object(app.state.market_store, "touch_mention", side_effect=AssertionError("market write")),
                patch.object(app.state.market_store, "enqueue_sync", side_effect=AssertionError("market queue")),
            ):
                before = client.get("/api/theme-leads")
                self.assertEqual(0, before.json()["total"])
                extracted = client.post("/api/theme-leads/extract")
                self.assertEqual(200, extracted.status_code, extracted.text)
                with patch.object(store, "replace_theme_leads_batch", side_effect=AssertionError("GET write")):
                    result = client.get("/api/theme-leads", params={"q": "会稽山", "sort": "oldest"})
                self.assertEqual(200, result.status_code, result.text)
                self.assertEqual(1, result.json()["total"])
                self.assertIn("未验证", result.json()["coverage"]["zhihu"])
                self.assertEqual(422, client.get("/api/theme-leads", params={"sort": "bad"}).status_code)
                self.assertEqual(422, client.get("/api/theme-leads", params={"date_from": "2026-02-31"}).status_code)
            self.assertEqual([], store.list_stock_leads())
            self.assertEqual([], app.state.event_store.load_events())


if __name__ == "__main__":
    unittest.main()
