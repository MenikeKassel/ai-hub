from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_posts import KolPostStore, RuleClassifier, normalise_twitter_post  # noqa: E402
from stock_leads import extract_stock_leads, reconcile_exact_stock_leads  # noqa: E402


def post_payload(post_id: str, text: str) -> dict:
    return {
        "id": post_id,
        "text": text,
        "url": f"https://x.com/example/status/{post_id}",
        "author": {"screenName": "example", "name": "Example"},
        "metrics": {},
        "createdAtISO": "2026-07-14T08:30:00+00:00",
        "media": [],
        "isRetweet": False,
    }


class StockLeadExtractionTests(unittest.TestCase):
    def make_store(self, root: Path) -> tuple[KolPostStore, dict]:
        store = KolPostStore(root / "posts.db", root / "media")
        kol_id, _ = store.add_kol("Example", "example")
        kol = store.get_kol(kol_id)
        assert kol is not None
        return store, kol

    def add_post(self, store: KolPostStore, kol: dict, post_id: str, text: str) -> None:
        post = normalise_twitter_post(post_payload(post_id, text), kol)
        store.upsert_post(post)
        store.save_rule_classification(
            post.post_id,
            RuleClassifier({"600900": "长江电力", "600519": "贵州茅台"}).classify(post),
        )

    def test_exact_codes_create_confirmed_data_leads_without_approving_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "2077000000000000001", "关注 600900 长江电力，继续看多。")

            result = extract_stock_leads(
                store,
                instruments={"600900": {"name": "长江电力", "instrument_type": "stock"}},
                aliases={"600900": "长江电力"},
            )
            leads = store.list_stock_leads()

            self.assertEqual(1, result.created)
            self.assertEqual(1, len(leads))
            self.assertEqual("600900", leads[0]["symbol"])
            self.assertEqual("confirmed", leads[0]["status"])
            self.assertEqual("exact_code", leads[0]["extraction_method"])
            self.assertEqual("pending", store.get_post("2077000000000000001")["review_status"])

    def test_name_only_and_model_only_leads_require_human_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "2077000000000000002", "长江电力值得继续观察。")
            self.add_post(store, kol, "2077000000000000003", "图里这家公司可能受益。")
            store.save_model_classification(
                "2077000000000000003",
                {
                    "content_type": "recommendation",
                    "evidence_type": "ambiguous",
                    "confidence": 0.72,
                    "summary": "图片可能提到贵州茅台。",
                    "drafts": [
                        {
                            "symbol": "600519",
                            "security_name": "贵州茅台",
                            "direction": "long",
                            "thesis": "图片型观点，仍需人工核对。",
                            "evidence_type": "ambiguous",
                            "confidence": 0.72,
                        }
                    ],
                },
                model_name="codex",
                prompt_version="fixture",
            )

            result = extract_stock_leads(
                store,
                instruments={
                    "600900": {"name": "长江电力", "instrument_type": "stock"},
                    "600519": {"name": "贵州茅台", "instrument_type": "stock"},
                },
                aliases={"600900": "长江电力", "600519": "贵州茅台"},
            )
            leads = {item["post_id"]: item for item in store.list_stock_leads()}

            self.assertEqual(2, result.created)
            self.assertEqual("pending", leads["2077000000000000002"]["status"])
            self.assertEqual("name_match", leads["2077000000000000002"]["extraction_method"])
            self.assertEqual("pending", leads["2077000000000000003"]["status"])
            self.assertEqual("codex", leads["2077000000000000003"]["extraction_method"])

    def test_multi_symbol_extraction_and_review_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(
                store,
                kol,
                "2077000000000000004",
                "600900 长江电力与 600519 贵州茅台都在观察池，不代表推荐。",
            )
            instruments = {
                "600900": {"name": "长江电力", "instrument_type": "stock"},
                "600519": {"name": "贵州茅台", "instrument_type": "stock"},
            }

            first = extract_stock_leads(store, instruments=instruments, aliases={})
            second = extract_stock_leads(store, instruments=instruments, aliases={})
            leads = store.list_stock_leads()
            store.review_stock_lead(leads[0]["id"], "confirmed", "人工确认")
            store.review_stock_lead(leads[0]["id"], "confirmed", "人工确认")

            self.assertEqual(2, first.created)
            self.assertEqual(0, second.created)
            self.assertEqual(2, len(leads))
            self.assertEqual("confirmed", store.get_stock_lead(leads[0]["id"])["status"])

    def test_structured_list_does_not_turn_retrospective_names_into_new_leads(self) -> None:
        aliases = {
            "605178": "时空科技",
            "002303": "美盈森",
            "000938": "紫光股份",
            "600988": "赤峰黄金",
            "603893": "瑞芯微",
        }
        instruments = {
            symbol: {"name": name, "instrument_type": "stock"}
            for symbol, name in aliases.items()
        }
        text = (
            "马前炮：7 月 17 日个股分享\n"
            "1.时空科技\n2.美盈森\n3.紫光股份\n"
            "昨日内部群推的两只票赤峰黄金和瑞芯微应声大涨。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            post = normalise_twitter_post(post_payload("2077000000000000008", text), kol)
            store.upsert_post(post)
            classifier = RuleClassifier(aliases)
            store.save_rule_classification(post.post_id, classifier.classify(post))
            payload = classifier.classify_structured_text(post)
            assert payload is not None
            store.save_model_classification(
                post.post_id,
                payload,
                model_name="structured-rules",
                prompt_version="structured-text-v1",
            )

            result = extract_stock_leads(store, instruments=instruments, aliases=aliases)
            leads = store.list_stock_leads(post_id=post.post_id)

            self.assertEqual(3, result.created)
            self.assertEqual(
                {"605178", "002303", "000938"},
                {lead["symbol"] for lead in leads},
            )
            self.assertTrue(all(lead["mention_kind"] == "recommendation" for lead in leads))

    def test_construction_plan_with_bracketed_stocks_and_sections_creates_drafts(self) -> None:
        aliases = {
            "002580": "圣阳股份",
            "002131": "利欧股份",
        }
        text = (
            "周一建仓计划【圣阳股份】【利欧股份】\n"
            "圣阳股份\n"
            "重点：算力 IDC 备电龙头，海外储能订单落地，国资加持，电池多路线布局。\n"
            "利欧股份\n"
            "重点：英伟达、华为液冷泵核心供应商，AI 散热刚需。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            post = normalise_twitter_post(post_payload("2077000000000000010", text), kol)
            store.upsert_post(post)
            classifier = RuleClassifier(aliases)

            payload = classifier.classify_structured_text(post)

            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual("recommendation", payload["content_type"])
            self.assertEqual({"002580", "002131"}, {item["symbol"] for item in payload["drafts"]})
            drafts = {item["symbol"]: item for item in payload["drafts"]}
            self.assertEqual("buy", drafts["002580"]["action"])
            self.assertIn("算力 IDC", drafts["002580"]["thesis"])
            self.assertIn("英伟达", drafts["002131"]["thesis"])
            self.assertTrue(all(text.find(span) >= 0 for item in payload["drafts"] for span in item["evidence_spans"]))

    def test_prospective_construction_plan_is_not_classified_as_holding(self) -> None:
        aliases = {"002580": "圣阳股份"}
        classifier = RuleClassifier(aliases)
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            planned = normalise_twitter_post(
                post_payload("2077000000000000011", "周一建仓计划【圣阳股份】"), kol
            )
            held = normalise_twitter_post(
                post_payload("2077000000000000012", "当前持仓圣阳股份，已经建仓。"), kol
            )
            store.upsert_post(planned)
            store.upsert_post(held)
            store.save_rule_classification(planned.post_id, classifier.classify(planned))
            store.save_rule_classification(held.post_id, classifier.classify(held))

            extract_stock_leads(store, instruments={"002580": {"name": "圣阳股份", "instrument_type": "stock"}}, aliases=aliases)
            leads = {lead["post_id"]: lead for lead in store.list_stock_leads()}

            self.assertEqual("recommendation", leads[planned.post_id]["mention_kind"])
            self.assertEqual("holding", leads[held.post_id]["mention_kind"])

    def test_exact_code_is_reconciled_after_instrument_master_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "2077000000000000005", "观察 601991 的走势，仅作示例。")
            extract_stock_leads(store, instruments={}, aliases={})

            symbols = reconcile_exact_stock_leads(
                store,
                {"601991": {"name": "大唐发电", "instrument_type": "stock"}},
            )
            lead = store.list_stock_leads()[0]

            self.assertEqual(["601991"], symbols)
            self.assertEqual("confirmed", lead["status"])
            self.assertTrue(lead["auto_confirmed"])

    def test_any_six_digit_code_can_match_the_instrument_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "2077000000000000006", "观察 430047，仅作研究线索。")

            result = extract_stock_leads(
                store,
                instruments={"430047": {"name": "诺思兰德", "instrument_type": "stock"}},
                aliases={},
            )

            self.assertEqual(1, result.created)
            self.assertEqual("confirmed", store.list_stock_leads()[0]["status"])

    def test_failed_extraction_is_retried_for_the_same_source_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, kol = self.make_store(Path(tmp))
            self.add_post(store, kol, "2077000000000000007", "关注 600900 长江电力。")

            with patch.object(store, "upsert_stock_lead", side_effect=RuntimeError("temporary failure")):
                first = extract_stock_leads(
                    store,
                    instruments={"600900": {"name": "长江电力", "instrument_type": "stock"}},
                    aliases={},
                )
            second = extract_stock_leads(
                store,
                instruments={"600900": {"name": "长江电力", "instrument_type": "stock"}},
                aliases={},
            )

            self.assertEqual(1, first.failed)
            self.assertEqual(1, second.created)


if __name__ == "__main__":
    unittest.main()
