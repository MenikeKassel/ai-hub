"""Private capture adapters and AI scoring for the public KOL workbench core.

The public package owns schemas, review state, and the HTTP API. This module only
maps private/local capture output into that contract and keeps credentials out of
the public repository.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import httpx
from foundation_market_client import FoundationBackedMarketStore
from kol_audit.api.app import ApiSettings, create_app
from kol_audit.discovery.models import (
    AccountContentResult,
    DiscoveredAccount,
    DiscoveryResult,
    PlatformCapabilities,
    ResolvedAccount,
    content_id,
)
from kol_audit.discovery.scoring import CandidateScorer
from kol_audit.discovery.service import PLATFORMS, ProviderRegistry
from kol_audit.events.store import KolStore
from kol_audit.market.store import MarketStore
from kol_audit.posts.store import KolPostStore
from opencode_go import OPENCODE_GO_API_URL, OPENCODE_GO_MODEL, load_opencode_go_api_key

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_ROOT = ROOT / "_runtime" / "trading"
DEFAULT_CAPTURE_ROOT = ROOT / "_runtime" / "trading" / "kol-discovery" / "captures"
DEFAULT_FOUNDATION_ROOT = Path(
    os.environ.get("ASHARE_FOUNDATION_ROOT", r"F:\ai-data\ashare")
)

PLATFORM_CAPABILITIES = {
    "x": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, historical_backfill=True
    ),
    "zhihu": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, ocr=True, historical_backfill=True
    ),
    "xiaohongshu": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, ocr=True, historical_backfill=True
    ),
    "douyin": PlatformCapabilities(
        discover=True,
        resolve=True,
        fetch=True,
        ocr=True,
        transcript=True,
        historical_backfill=True,
    ),
    "bilibili": PlatformCapabilities(
        discover=True,
        resolve=True,
        fetch=True,
        ocr=True,
        transcript=True,
        historical_backfill=True,
    ),
    "weibo": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, ocr=True, historical_backfill=True
    ),
    "wechat_rss": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, historical_backfill=True
    ),
    "xueqiu": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, ocr=True, historical_backfill=True
    ),
    "taoguba": PlatformCapabilities(
        discover=True, resolve=True, fetch=True, ocr=True, historical_backfill=True
    ),
}

# JSON capture files are useful for fixtures and manual imports, but they are
# not evidence that a live platform adapter works.  Keep unverified platforms
# visible in the registry while reporting them as manual_only instead of
# advertising a false live capability.
for _platform in {
    "xiaohongshu",
    "douyin",
    "bilibili",
    "weibo",
    "wechat_rss",
    "xueqiu",
    "taoguba",
}:
    PLATFORM_CAPABILITIES[_platform] = PlatformCapabilities()


def _read_items(path: Path, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError(f"capture file must contain an array: {path}")
    return [dict(item) for item in payload if isinstance(item, dict)]


class LocalCaptureAccountProvider:
    """Read deterministic capture exports without embedding platform credentials."""

    name = "ai-hub-local-capture-v1"

    def __init__(self, platform: str, capture_root: Path):
        if platform not in PLATFORM_CAPABILITIES:
            raise ValueError(f"unsupported KOL platform: {platform}")
        self.platform = platform
        self.root = Path(capture_root) / platform

    @property
    def accounts_path(self) -> Path:
        return self.root / "accounts.json"

    @property
    def content_path(self) -> Path:
        return self.root / "content.json"

    def capabilities(self) -> PlatformCapabilities:
        return PLATFORM_CAPABILITIES[self.platform]

    def health(self) -> dict[str, Any]:
        return {
            "configured": self.accounts_path.is_file(),
            "accounts_capture": self.accounts_path.is_file(),
            "content_capture": self.content_path.is_file(),
            "mode": "manual_only" if self.platform not in {"x", "zhihu"} else "local_capture",
            "live_adapter": False,
        }

    def _accounts(self) -> list[dict[str, Any]]:
        return _read_items(self.accounts_path, ("accounts", "items"))

    def _resolved(self, value: dict[str, Any]) -> ResolvedAccount:
        external_id = str(
            value.get("external_account_id") or value.get("id") or ""
        ).strip()
        handle = (
            str(value.get("handle") or value.get("screen_name") or external_id)
            .strip()
            .lstrip("@")
        )
        if not external_id or not handle:
            raise ValueError(f"{self.platform} account is missing a stable identity")
        return ResolvedAccount(
            platform=self.platform,
            external_account_id=external_id,
            handle=handle,
            display_name=str(
                value.get("display_name") or value.get("name") or handle
            ).strip(),
            profile_url=str(value.get("profile_url") or value.get("url") or "").strip(),
            bio=str(value.get("bio") or value.get("description") or "").strip(),
            follower_count=(
                int(value["follower_count"])
                if value.get("follower_count") not in {None, ""}
                else None
            ),
            raw=value,
        )

    def resolve(self, reference: str) -> ResolvedAccount:
        needle = reference.strip().lstrip("@").casefold()
        for value in self._accounts():
            account = self._resolved(value)
            if needle in {
                account.external_account_id.casefold(),
                account.handle.casefold(),
                account.profile_url.casefold(),
            }:
                return account
        raise KeyError(f"{self.platform} account not found: {reference}")

    def discover(self, query: str, cursor: str | None, limit: int) -> DiscoveryResult:
        if not self.accounts_path.is_file():
            return DiscoveryResult(
                [], warnings=[f"missing capture: {self.accounts_path}"]
            )
        needle = query.strip().casefold()
        matches: list[DiscoveredAccount] = []
        for value in self._accounts():
            account = self._resolved(value)
            evidence = (
                value.get("evidence") if isinstance(value.get("evidence"), list) else []
            )
            haystack = "\n".join(
                (
                    account.handle,
                    account.display_name,
                    account.bio,
                    json.dumps(evidence, ensure_ascii=False),
                )
            ).casefold()
            if needle and needle not in haystack:
                continue
            matches.append(
                DiscoveredAccount(
                    **account.__dict__,
                    evidence=[
                        dict(item) for item in evidence if isinstance(item, dict)
                    ],
                )
            )
        start = max(0, int(cursor or 0))
        selected = matches[start : start + max(1, limit)]
        next_offset = start + len(selected)
        return DiscoveryResult(
            selected,
            cursor=str(next_offset) if next_offset < len(matches) else None,
        )

    def fetch(
        self,
        account: ResolvedAccount,
        cursor: str | None,
        limit: int,
    ) -> AccountContentResult:
        if account.platform != self.platform:
            raise ValueError("account platform does not match provider")
        if not self.content_path.is_file():
            return AccountContentResult(
                [], warnings=[f"missing capture: {self.content_path}"]
            )
        values = []
        for item in _read_items(self.content_path, ("content", "posts", "items")):
            owner = str(item.get("external_account_id") or item.get("account_id") or "")
            if owner != account.external_account_id:
                continue
            external_item_id = str(
                item.get("external_item_id")
                or item.get("item_id")
                or item.get("id")
                or ""
            ).strip()
            if not external_item_id:
                raise ValueError(f"{self.platform} content is missing external_item_id")
            values.append(
                {
                    **item,
                    "post_id": content_id(self.platform, external_item_id),
                    "platform": self.platform,
                }
            )
        values.sort(
            key=lambda item: str(item.get("posted_at") or item.get("created_at") or ""),
            reverse=True,
        )
        start = max(0, int(cursor or 0))
        selected = values[start : start + max(1, limit)]
        next_offset = start + len(selected)
        return AccountContentResult(
            selected,
            cursor=str(next_offset) if next_offset < len(values) else None,
        )


class OpenCodeGoCandidateScoreProvider:
    """Score evidence only; the public core keeps admission human-controlled."""

    name = f"opencode-go/{OPENCODE_GO_MODEL}"

    def __init__(
        self,
        *,
        key_loader: Callable[[], str] = load_opencode_go_api_key,
        client: httpx.Client | None = None,
        timeout_seconds: float = 90,
    ):
        self.key_loader = key_loader
        self.client = client
        self.timeout_seconds = max(1, timeout_seconds)

    def score(
        self, candidate: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        instruction = (
            "Score a social-media research-account candidate using only the supplied evidence. "
            "Return JSON with expertise, track_record, relevance, activity, and identity_quality "
            "as numbers from 0 to 1, plus a reasons string array. Scores are triage aids only: "
            "never accept, reject, rank securities, or make trading recommendations."
        )
        body = {
            "model": OPENCODE_GO_MODEL,
            "messages": [
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"candidate": candidate, "evidence": evidence},
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.key_loader()}"}
        if self.client is None:
            response = httpx.post(
                OPENCODE_GO_API_URL,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
        else:
            response = self.client.post(
                OPENCODE_GO_API_URL,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, str):
            result = json.loads(content)
        elif isinstance(content, dict):
            result = content
        else:
            raise ValueError("OpenCode Go candidate score is not a JSON object")
        if not isinstance(result, dict):
            raise ValueError("OpenCode Go candidate score is not a JSON object")
        return result


def build_provider_registry(
    capture_root: Path = DEFAULT_CAPTURE_ROOT,
) -> ProviderRegistry:
    return ProviderRegistry(
        LocalCaptureAccountProvider(platform, capture_root)
        for platform, _name in PLATFORMS
    )


def create_private_app(
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    capture_root: Path = DEFAULT_CAPTURE_ROOT,
    foundation_root: Path = DEFAULT_FOUNDATION_ROOT,
    score_provider: OpenCodeGoCandidateScoreProvider | None = None,
):
    base = ApiSettings.default()
    settings = ApiSettings(
        runtime_root=Path(runtime_root),
        frontend_dist=base.frontend_dist,
        codex_schema=base.codex_schema,
        twitter_command=base.twitter_command,
        ai_provider=base.ai_provider,
    )
    trading_root = Path(runtime_root)
    kol_root = trading_root / "kol"
    market_store = FoundationBackedMarketStore(
        MarketStore(trading_root / "market"),
        foundation_root,
    )
    market_store.bootstrap_reference_data()
    app = create_app(
        settings,
        provider_registry=build_provider_registry(capture_root),
        post_store_override=KolPostStore(kol_root / "posts.db", kol_root / "media"),
        event_store_override=KolStore(kol_root),
        market_store_override=market_store,
    )
    app.state.candidate_scorer = CandidateScorer(
        app.state.discovery_store,
        score_provider or OpenCodeGoCandidateScoreProvider(),
    )

    @app.middleware("http")
    async def pin_foundation_release_for_request(request, call_next):
        if not market_store.foundation.health()["ok"]:
            return await call_next(request)
        with market_store.foundation.pinned_release():
            return await call_next(request)

    return app
