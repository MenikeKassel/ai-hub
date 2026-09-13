from __future__ import annotations
from pathlib import Path
from opencode_go import OPENCODE_GO_API_URL, OPENCODE_GO_MODEL, load_opencode_go_api_key
from .core import CredentialStorageError, CredentialValidationError, ModelProviderUnavailableError, TwitterAuthenticationError, validate_twitter_credentials


class KeyringCredentialStore:
    service_name = "ai-hub/twitter-cli"

    def __init__(self, service_name: str | None = None):
        if service_name:
            self.service_name = service_name

    def save(self, auth_token: str, ct0: str) -> None:
        import keyring  # type: ignore

        auth_token, ct0 = validate_twitter_credentials(auth_token, ct0)
        usernames = ("auth_token", "ct0")
        previous = {name: keyring.get_password(self.service_name, name) for name in usernames}
        try:
            keyring.set_password(self.service_name, "auth_token", auth_token)
            keyring.set_password(self.service_name, "ct0", ct0)
        except Exception as exc:
            for name in usernames:
                try:
                    if previous[name] is None:
                        keyring.delete_password(self.service_name, name)
                    else:
                        keyring.set_password(self.service_name, name, previous[name])
                except Exception:
                    pass
            raise CredentialStorageError(
                "Windows Credential Manager 保存失败，本次输入未生效"
            ) from exc

    def load(self) -> dict[str, str]:
        auth_token, ct0 = self.load_values()
        return {"TWITTER_AUTH_TOKEN": auth_token, "TWITTER_CT0": ct0}

    def load_values(self) -> tuple[str, str]:
        import keyring  # type: ignore

        auth_token = keyring.get_password(self.service_name, "auth_token") or ""
        ct0 = keyring.get_password(self.service_name, "ct0") or ""
        if not auth_token or not ct0:
            raise TwitterAuthenticationError("Twitter credentials are not configured")
        return auth_token, ct0

    def configured(self) -> bool:
        try:
            self.load()
            return True
        except Exception:
            return False


class NitterCredentialStore(KeyringCredentialStore):
    service_name = "ai-hub/nitter"

    def session(self) -> dict[str, str]:
        auth_token, ct0 = self.load_values()
        return {"kind": "cookie", "auth_token": auth_token, "ct0": ct0}


class ReaderCredentialStore(KeyringCredentialStore):
    """Low-frequency reader identity, kept separate from the user's main X session."""

    service_name = "ai-hub/twitter-reader"


class OpenCodeGoCredentialStore:
    service_name = "ai-hub/opencode-go"

    def __init__(
        self,
        service_name: str | None = None,
        *,
        config_path: Path | None = None,
    ):
        if service_name:
            self.service_name = service_name
        self.config_path = config_path

    @staticmethod
    def validate(api_key: str) -> str:
        value = str(api_key or "").strip()
        if not 12 <= len(value) <= 1024:
            raise CredentialValidationError("OpenCode Go API key format is invalid")
        if any(character.isspace() for character in value):
            raise CredentialValidationError("OpenCode Go API key cannot contain whitespace")
        return value

    def save(self, api_key: str) -> None:
        import keyring  # type: ignore

        value = self.validate(api_key)
        try:
            previous = keyring.get_password(self.service_name, "api_key")
        except Exception as exc:
            raise CredentialStorageError(
                "Windows Credential Manager is unavailable for OpenCode Go"
            ) from exc
        try:
            keyring.set_password(self.service_name, "api_key", value)
        except Exception as exc:
            try:
                if previous is None:
                    keyring.delete_password(self.service_name, "api_key")
                else:
                    keyring.set_password(self.service_name, "api_key", previous)
            except Exception:
                pass
            raise CredentialStorageError(
                "Windows Credential Manager could not save the OpenCode Go API key"
            ) from exc

    def load(self) -> str:
        import keyring  # type: ignore

        try:
            value = keyring.get_password(self.service_name, "api_key") or ""
        except Exception:
            value = ""
        if value:
            return self.validate(value)
        try:
            return self.validate(
                load_opencode_go_api_key(config_path=self.config_path)
            )
        except Exception as exc:
            raise ModelProviderUnavailableError(
                "OpenCode Go / DeepSeek V4 Flash is not configured"
            ) from exc

    def configured(self) -> bool:
        try:
            self.load()
            return True
        except Exception:
            return False


DeepSeekCredentialStore = OpenCodeGoCredentialStore
