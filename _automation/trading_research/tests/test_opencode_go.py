from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencode_go import (  # noqa: E402
    OPENCODE_GO_API_URL,
    OPENCODE_GO_MODEL,
    load_opencode_go_api_key,
)
from kol_posts import (  # noqa: E402
    CredentialStorageError,
    OpenCodeGoCredentialStore,
)


class OpenCodeGoTests(unittest.TestCase):
    def test_public_contract_uses_go_deepseek_v4_flash(self) -> None:
        self.assertEqual(
            "https://opencode.ai/zen/go/v1/chat/completions",
            OPENCODE_GO_API_URL,
        )
        self.assertEqual("deepseek-v4-flash", OPENCODE_GO_MODEL)

    def test_rejects_inline_key_even_for_the_matching_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "opencode.json"
            config.write_text(
                json.dumps(
                    {
                        "provider": {
                            "custom-name": {
                                "options": {
                                    "baseURL": "https://opencode.ai/zen/go/v1",
                                    "apiKey": "fixture-opencode-go-key",
                                },
                                "models": {"deepseek-v4-flash": {}},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Inline OpenCode Go API keys"):
                load_opencode_go_api_key(config_path=config)

    def test_resolves_env_reference_without_exposing_other_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "opencode.json"
            config.write_text(
                json.dumps(
                    {
                        "provider": {
                            "not-go": {
                                "options": {
                                    "baseURL": "https://example.invalid/v1",
                                    "apiKey": "fixture-must-not-be-used",
                                }
                            },
                            "go": {
                                "options": {
                                    "baseURL": "https://opencode.ai/zen/go/v1/",
                                    "apiKey": "{env:FIXTURE_OPENCODE_GO_KEY}",
                                },
                                "models": {"deepseek-v4-flash": {}},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"FIXTURE_OPENCODE_GO_KEY": "fixture-env-go-key"},
                clear=False,
            ):
                self.assertEqual(
                    "fixture-env-go-key",
                    load_opencode_go_api_key(config_path=config),
                )

    def test_credential_store_uses_environment_when_keyring_is_unavailable(self) -> None:
        with patch("keyring.get_password", side_effect=RuntimeError("unavailable")):
            with patch.dict(
                os.environ,
                {"OPENCODE_GO_API_KEY": "fixture-environment-go-key"},
                clear=False,
            ):
                self.assertEqual(
                    "fixture-environment-go-key",
                    OpenCodeGoCredentialStore().load(),
                )

    def test_credential_save_normalizes_keyring_backend_failure(self) -> None:
        with patch("keyring.get_password", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(
                CredentialStorageError,
                "Credential Manager is unavailable",
            ):
                OpenCodeGoCredentialStore().save("fixture-environment-go-key")


if __name__ == "__main__":
    unittest.main()
