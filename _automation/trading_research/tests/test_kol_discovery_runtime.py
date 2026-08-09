from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
PUBLIC_SRC = HERE.parents[2] / "kol-audit-workbench" / "src"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PUBLIC_SRC))

from kol_audit.discovery.models import ResolvedAccount  # noqa: E402
from kol_discovery_runtime import (  # noqa: E402
    LocalCaptureAccountProvider,
    OpenCodeGoCandidateScoreProvider,
    build_provider_registry,
    create_private_app,
)


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "expertise": 0.9,
                                "track_record": 0.8,
                                "relevance": 1.0,
                                "activity": 0.7,
                                "identity_quality": 0.9,
                                "reasons": ["持续发布公司研究", "身份线索充分"],
                            }
                        )
                    }
                }
            ]
        }


class FakeClient:
    def __init__(self) -> None:
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse()


class KolDiscoveryRuntimeTests(unittest.TestCase):
    def test_registry_exposes_all_nine_platforms_with_capture_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = build_provider_registry(Path(tmp))
            platforms = registry.platforms()

        self.assertEqual(9, len(platforms))
        self.assertTrue(all(item["available"] for item in platforms))
        self.assertTrue(
            all(item["health"]["configured"] is False for item in platforms)
        )
        self.assertTrue(
            next(item for item in platforms if item["platform"] == "douyin")[
                "capabilities"
            ]["transcript"]
        )

    def test_local_capture_has_stable_identity_and_platform_content_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            platform_root = root / "bilibili"
            platform_root.mkdir()
            (platform_root / "accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "external_account_id": "uid-42",
                                "handle": "value-research",
                                "display_name": "价值研究所",
                                "bio": "A股 基本面研究",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (platform_root / "content.json").write_text(
                json.dumps(
                    [
                        {
                            "external_account_id": "uid-42",
                            "external_item_id": "BV1TEST",
                            "posted_at": "2026-08-09T10:00:00+08:00",
                            "text": "示例内容",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            provider = LocalCaptureAccountProvider("bilibili", root)
            discovered = provider.discover("基本面", None, 10)
            account = provider.resolve("value-research")
            content = provider.fetch(account, None, 100)

        self.assertEqual("uid-42", discovered.accounts[0].external_account_id)
        self.assertEqual("bilibili:BV1TEST", content.items[0]["post_id"])
        self.assertIsInstance(account, ResolvedAccount)

    def test_opencode_go_scoring_uses_fixed_model_and_has_no_admission_field(
        self,
    ) -> None:
        client = FakeClient()
        provider = OpenCodeGoCandidateScoreProvider(
            key_loader=lambda: "fixture-key-not-a-secret",
            client=client,
        )
        result = provider.score(
            {"candidate_id": "cand-1", "platform": "bilibili"},
            [{"excerpt": "公司研究"}],
        )

        self.assertEqual(0.9, result["expertise"])
        self.assertNotIn("accepted", result)
        body = client.request[1]["json"]
        self.assertEqual("deepseek-v4-flash", body["model"])
        self.assertIn("never accept", body["messages"][0]["content"])

    def test_private_app_reuses_legacy_kol_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_private_app(
                runtime_root=root,
                capture_root=root / "captures",
                foundation_root=root / "missing-foundation",
                score_provider=OpenCodeGoCandidateScoreProvider(
                    key_loader=lambda: "fixture-key-not-a-secret",
                    client=FakeClient(),
                ),
            )

            self.assertEqual(root / "kol" / "posts.db", app.state.post_store.path)
            self.assertEqual(root / "kol", app.state.event_store.root)
            self.assertIsNotNone(app.state.candidate_scorer)


if __name__ == "__main__":
    unittest.main()
