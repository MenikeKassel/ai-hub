from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_research_ai import (  # noqa: E402
    DeepSeekEventResearchInterpreter,
    build_event_research_interpreter,
    validate_interpretation_payload,
)


LENSES = [
    "short_term_leader",
    "dow_wave_gann",
    "price_action",
    "ict",
    "wyckoff_orderflow",
]


def research() -> dict:
    return {
        "lenses": {
            lens: {
                "facts": {"status": f"{lens}-fact"},
                "warnings": [],
            }
            for lens in LENSES
        }
    }


def payload() -> dict:
    return {
        "interpretations": [
            {
                "lens": lens,
                "hypothesis": f"{lens} 假设",
                "confidence": 0.6,
                "evidence_refs": [f"lenses.{lens}.facts.status"],
                "counter_evidence": [],
                "invalidation": ["新证据与当前结构冲突"],
            }
            for lens in LENSES
        ]
    }


class EventResearchAITests(unittest.TestCase):
    def test_default_builder_uses_deepseek_without_codex_fallback(self) -> None:
        provider = build_event_research_interpreter(
            Path("schema.json"),
            Path("."),
            deepseek_credentials=object(),
        )

        self.assertIsInstance(provider, DeepSeekEventResearchInterpreter)
        self.assertEqual("deepseek-v4-flash", provider.model_name)

    def test_deepseek_retries_once_with_the_validation_error(self) -> None:
        class Credentials:
            @staticmethod
            def load() -> str:
                return "test-key"

        class Response:
            status_code = 200

            def __init__(self, content: dict):
                self.content = content

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": json.dumps(self.content)}}
                    ]
                }

        class Client:
            def __init__(self):
                self.requests = []

            def post(self, *args, **kwargs):
                del args
                self.requests.append(kwargs)
                value = payload()
                if len(self.requests) == 1:
                    value["interpretations"][0]["hypothesis"] = "建议买入"
                return Response(
                    {
                        "results": [
                            {
                                "event_id": "KOL-1",
                                "interpretations": value["interpretations"],
                            }
                        ]
                    }
                )

        client = Client()
        provider = DeepSeekEventResearchInterpreter(
            Path(__file__).resolve().parents[1]
            / "event_research_schema.json",
            Credentials(),
            client=client,
        )
        result = provider.interpret_many(
            [
                {
                    "event": {
                        "event_id": "KOL-1",
                        "symbol": "600900",
                        "posted_at": "2026-07-01T16:00:00+08:00",
                    },
                    "research": research(),
                }
            ]
        )

        self.assertIn("KOL-1", result)
        self.assertEqual(2, len(client.requests))
        self.assertIn(
            "trading directive",
            client.requests[1]["json"]["messages"][-1]["content"],
        )

    def test_validates_all_lenses_and_real_evidence_paths(self) -> None:
        result = validate_interpretation_payload(payload(), research())
        self.assertEqual(5, len(result["interpretations"]))

    def test_normalizes_research_prefixed_evidence_paths(self) -> None:
        value = payload()
        value["interpretations"][0]["evidence_refs"] = [
            "research.lenses.short_term_leader.facts.status"
        ]

        result = validate_interpretation_payload(value, research())

        refs = next(
            item["evidence_refs"]
            for item in result["interpretations"]
            if item["lens"] == "short_term_leader"
        )
        self.assertEqual(
            ["lenses.short_term_leader.facts.status"],
            refs,
        )

    def test_repairs_a_unique_shortened_path_inside_the_same_lens(self) -> None:
        value = payload()
        source = research()
        source["lenses"]["wyckoff_orderflow"]["facts"]["wyckoff"] = {
            "volume_ratio_5": 1.4
        }
        wyckoff = next(
            item
            for item in value["interpretations"]
            if item["lens"] == "wyckoff_orderflow"
        )
        wyckoff["evidence_refs"] = [
            "lenses.wyckoff_orderflow.facts.volume_ratio_5"
        ]

        result = validate_interpretation_payload(value, source)

        repaired = next(
            item
            for item in result["interpretations"]
            if item["lens"] == "wyckoff_orderflow"
        )
        self.assertEqual(
            ["lenses.wyckoff_orderflow.facts.wyckoff.volume_ratio_5"],
            repaired["evidence_refs"],
        )

    def test_maps_an_undetermined_candidate_to_its_explicit_status(self) -> None:
        value = payload()
        source = research()
        source["lenses"]["short_term_leader"]["facts"]["candidate_types"] = {
            "trend_leader": {
                "candidate": None,
                "status": "not_determined",
            }
        }
        leader = next(
            item
            for item in value["interpretations"]
            if item["lens"] == "short_term_leader"
        )
        leader["evidence_refs"] = [
            "lenses.short_term_leader.facts.candidate_types.trend_leader.candidate"
        ]

        result = validate_interpretation_payload(value, source)

        repaired = next(
            item
            for item in result["interpretations"]
            if item["lens"] == "short_term_leader"
        )
        self.assertEqual(
            [
                "lenses.short_term_leader.facts.candidate_types."
                "trend_leader.status"
            ],
            repaired["evidence_refs"],
        )

    def test_rejects_nonexistent_evidence_path(self) -> None:
        value = payload()
        value["interpretations"][0]["evidence_refs"] = [
            "lenses.short_term_leader.facts.not_real"
        ]
        with self.assertRaisesRegex(ValueError, "does not exist"):
            validate_interpretation_payload(value, research())

    def test_rejects_empty_evidence_even_when_path_exists(self) -> None:
        value = payload()
        source = research()
        source["lenses"]["short_term_leader"]["facts"]["empty_context"] = {}
        value["interpretations"][0]["evidence_refs"] = [
            "lenses.short_term_leader.facts.empty_context"
        ]

        with self.assertRaisesRegex(ValueError, "empty"):
            validate_interpretation_payload(value, source)

    def test_rejects_cross_lens_evidence(self) -> None:
        value = payload()
        value["interpretations"][0]["evidence_refs"] = [
            "lenses.price_action.facts.status"
        ]

        with self.assertRaisesRegex(ValueError, "cross-lens evidence"):
            validate_interpretation_payload(value, research())

    def test_rejects_missing_lens(self) -> None:
        value = payload()
        value["interpretations"].pop()
        with self.assertRaisesRegex(ValueError, "omitted lenses"):
            validate_interpretation_payload(value, research())

    def test_rejects_future_outcome_leakage(self) -> None:
        for leakage in (
            "后来上涨20%，说明判断正确",
            "发帖后一周收益10%",
        ):
            with self.subTest(leakage=leakage):
                value = payload()
                value["interpretations"][0]["hypothesis"] = leakage

                with self.assertRaisesRegex(ValueError, "future outcome leakage"):
                    validate_interpretation_payload(value, research())

    def test_allows_conditional_future_language_in_invalidation(self) -> None:
        value = payload()
        value["interpretations"][0]["invalidation"] = [
            "后续上涨并突破前高时，该结构假设失效"
        ]

        result = validate_interpretation_payload(value, research())

        self.assertEqual(5, len(result["interpretations"]))

    def test_rejects_realized_future_outcome_inside_invalidation(self) -> None:
        value = payload()
        value["interpretations"][0]["invalidation"] = [
            "发帖后一周收益10%"
        ]

        with self.assertRaisesRegex(ValueError, "future outcome leakage"):
            validate_interpretation_payload(value, research())

    def test_rejects_trading_directives(self) -> None:
        value = payload()
        value["interpretations"][0]["hypothesis"] = "结构改善，建议买入"

        with self.assertRaisesRegex(ValueError, "trading directive"):
            validate_interpretation_payload(value, research())

    def test_rejects_disguised_action_advice(self) -> None:
        for advice in (
            "当前需观望",
            "短线不宜追高",
            "需要关注后续信号",
            "买入",
            "建议满仓",
        ):
            with self.subTest(advice=advice):
                value = payload()
                value["interpretations"][0]["hypothesis"] = advice

                with self.assertRaisesRegex(ValueError, "trading directive"):
                    validate_interpretation_payload(value, research())

    def test_rejects_order_flow_claims_from_bar_proxies(self) -> None:
        for overclaim in ("量能放大说明主力介入", "收于高位说明买盘主导"):
            with self.subTest(overclaim=overclaim):
                value = payload()
                value["interpretations"][0]["hypothesis"] = overclaim

                with self.assertRaisesRegex(ValueError, "proxy overclaim"):
                    validate_interpretation_payload(value, research())

    def test_rejects_predictive_price_signals(self) -> None:
        for signal in (
            "分钟结构看涨，价格将上涨",
            "这是看涨信号",
            "形成明确买入信号",
        ):
            with self.subTest(signal=signal):
                value = payload()
                value["interpretations"][0]["hypothesis"] = signal

                with self.assertRaisesRegex(ValueError, "predictive trading signal"):
                    validate_interpretation_payload(value, research())


if __name__ == "__main__":
    unittest.main()
