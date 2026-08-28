from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping


OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_API_URL = f"{OPENCODE_GO_BASE_URL}/chat/completions"
OPENCODE_GO_MODEL = "deepseek-v4-flash"
_ENV_REFERENCE = re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")


def default_opencode_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ or os.environ
    explicit = str(env.get("OPENCODE_CONFIG") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg_root = str(env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg_root:
        return Path(xdg_root).expanduser() / "opencode" / "opencode.json"
    return Path.home() / ".config" / "opencode" / "opencode.json"


def load_opencode_go_api_key(
    *,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read the Go credential without returning provider configuration or logging it."""
    env = environ or os.environ
    direct = str(env.get("OPENCODE_GO_API_KEY") or "").strip()
    if direct:
        return direct

    path = Path(config_path) if config_path is not None else default_opencode_config_path(env)
    payload = json.loads(path.read_text(encoding="utf-8"))
    providers = payload.get("provider") if isinstance(payload, dict) else None
    if not isinstance(providers, dict):
        raise KeyError("OpenCode provider configuration is missing")

    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        options = provider.get("options")
        if not isinstance(options, dict):
            continue
        base_url = str(options.get("baseURL") or "").strip().rstrip("/")
        if base_url != OPENCODE_GO_BASE_URL:
            continue
        models = provider.get("models")
        if isinstance(models, dict) and OPENCODE_GO_MODEL not in models:
            continue
        value = str(options.get("apiKey") or "").strip()
        match = _ENV_REFERENCE.fullmatch(value)
        if match:
            value = str(env.get(match.group(1)) or "").strip()
            if value:
                return value
            raise KeyError("OpenCode Go API key environment variable is empty")
        if value:
            raise ValueError(
                "Inline OpenCode Go API keys are not accepted; use an environment "
                "reference or Windows Credential Manager"
            )
        raise KeyError("OpenCode Go API key is empty")
    raise KeyError("OpenCode Go provider is not configured")
