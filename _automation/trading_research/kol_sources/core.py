from __future__ import annotations
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from kol_tracker import SHANGHAI, now_iso


SEED_KOLS = [
    ("A股点金手", "agudianjinshou", "A股、AI硬件、半导体主题"),
    ("林哥-深研A股", "WwQQ129146", "A股深研、业绩预告、事件驱动"),
    ("擒龙捉妖-泰戈", "sszcw", "个股推荐"),
    ("大道无形我有型", "bafeite1234", "市场周期、流动性、市场情绪"),
    ("Mistery / Mimiwftt", "Mimiwftt", "交易心理、市场人性"),
    ("Hoyooyoo", "Hoyooyoo", "待补"),
]


LONG_WORDS = {
    "看多",
    "买入",
    "建仓",
    "低吸",
    "推荐",
    "布局",
    "机会",
    "继续看好",
    "明天关注",
    "可以进场",
    "马前炮",
    "个股分享",
}


SHORT_WORDS = {"看空", "卖出", "减仓", "清仓", "回避", "见顶", "离场"}


RETROSPECTIVE_WORDS = {"昨天推荐", "此前推荐", "已经涨停", "成功涨停", "回顾", "复盘"}


FINANCE_WORDS = {"A股", "股票", "个股", "板块", "涨停", "跌停", "K线", "指数", "业绩", "估值", "资金", "成交量", "主力"}


STRUCTURED_REVIEW_VERSION = "structured-text-v2"


RECOMMENDATION_ACTIONS = {"buy", "add", "hold", "watch", "reduce", "sell", "avoid"}


RECOMMENDATION_HORIZONS = {"intraday", "short", "swing", "medium_long", "unspecified"}


RECOMMENDATION_STRENGTHS = {"explicit", "moderate", "weak", "unspecified"}


STRUCTURED_RECOMMENDATION_HEADER = re.compile(
    r"(?:马前炮|个股分享|今日分享|今日个股|今日关注|股票分享|股票池|"
    r"建仓计划|计划建仓|准备建仓|明日建仓|周一建仓|下周建仓|买入计划|低吸计划|可建仓)"
)


NUMBERED_STOCK_LINE = re.compile(r"^\s*(?:\d{1,2}\s*[.、．)]|[（(]\d{1,2}[）)])\s*(?P<body>.+?)\s*$")


RETROSPECTIVE_CLAIM = re.compile(r"(?:昨日|昨天|此前|前天).*(?:推荐|推的|分享|买入|关注|大涨|涨停)")


PROSPECTIVE_PLAN_MARKERS = (
    "建仓计划", "计划建仓", "准备买入", "准备建仓", "明日建仓", "周一建仓",
    "下周建仓", "买入计划", "低吸计划", "可建仓", "明日关注", "明天关注",
)


REALIZED_POSITION_MARKERS = ("已建仓", "已经建仓", "刚建仓", "当前持仓", "持仓", "买入了", "我的仓位")


class TwitterProviderError(RuntimeError):
    def __init__(self, message: str, attempts: list["ProviderAttempt"] | None = None):
        super().__init__(message)
        self.attempts = list(attempts or [])


class CredentialValidationError(ValueError):
    pass


class CredentialStorageError(RuntimeError):
    pass


class ModelWorkerBusyError(RuntimeError):
    pass


class ModelProviderUnavailableError(RuntimeError):
    """Raised when the configured model cannot accept more work this run."""


def _raise_codex_failure(detail: str, fallback: str) -> None:
    message = (detail or fallback).strip()
    lowered = message.lower()
    quota_markers = (
        "you've hit your usage limit",
        "usage limit reached",
        "purchase more credits",
    )
    if any(marker in lowered for marker in quota_markers):
        retry = re.search(r"try again at\s+([^\r\n.]+)", message, re.IGNORECASE)
        suffix = f"; try again at {retry.group(1).strip()}" if retry else ""
        raise ModelProviderUnavailableError(f"Codex usage limit reached{suffix}")
    raise RuntimeError(message[-2000:])


def validate_twitter_credentials(auth_token: str, ct0: str) -> tuple[str, str]:
    values = {"auth_token": auth_token.strip(), "ct0": ct0.strip()}
    for name, value in values.items():
        if len(value) < 10:
            raise CredentialValidationError(f"{name} 长度异常，请重新复制 Cookie 值")
        if len(value) > 512:
            raise CredentialValidationError(
                f"{name} 过长。必须只填写单个 Cookie 值，不要粘贴整段 Cookie 或请求头"
            )
        if any(character.isspace() for character in value) or ";" in value or "=" in value:
            raise CredentialValidationError(
                f"{name} 必须只填写单个 Cookie 值，不要包含 {name}=、分号、空格或请求头"
            )
    return values["auth_token"], values["ct0"]


class TwitterAuthenticationError(TwitterProviderError):
    pass


class TwitterRateLimitError(TwitterProviderError):
    pass


class XSessionUnavailableError(TwitterProviderError):
    """The guarded X session pool cannot issue a request right now."""


class XBudgetDeferredError(TwitterProviderError):
    """A request was deferred by the local global/session budget gate."""


class ZhihuProviderError(TwitterProviderError):
    pass


@dataclass(frozen=True)
class PostRecord:
    post_id: str
    kol_id: int
    platform: str
    handle: str
    author_name: str
    url: str
    text: str
    article_title: str
    article_text: str
    quoted_id: str
    quoted_text: str
    quoted_author: str
    reply_to_id: str
    reply_to_author: str
    posted_at: str
    posted_at_utc: str
    post_type: str
    language: str
    media: list[dict[str, Any]]
    metrics: dict[str, Any]
    raw_payload: dict[str, Any]
    content_hash: str
    fetched_at: str
    canonical_provider: str = "twitter-cli"
    metrics_provider: str = "twitter-cli"
    provider_warning: str = ""


@dataclass(frozen=True)
class RuleResult:
    score: int
    is_candidate: bool
    symbols: list[str]
    direction: str
    reasons: list[str]
    evidence_type: str
    content_type: str


@dataclass(frozen=True)
class FetchSummary:
    run_id: str
    successful_kols: int
    failed_kols: int
    new_posts: int
    candidate_posts: int
    pending_reviews: int
    auth_status: str
    gap_kols: list[str]
    fallback_kols: list[str]
    shadow_failed_kols: list[str]
    errors: list[str]
    queue_total: int = 0
    queue_completed: int = 0
    queue_pending: int = 0
    rate_limit_paused: bool = False
    blocked_platforms: list[str] = field(default_factory=list)
    platform_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    status: str
    post_count: int = 0
    duration_ms: int = 0
    error_code: str = ""
    error: str = ""


@dataclass(frozen=True)
class ProviderFetchResult:
    provider: str
    posts: list[dict[str, Any]]
    attempts: list[ProviderAttempt]
    warnings: list[str]
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str = ""
    user_id: str = ""
    exhausted: bool = False


class XPostProvider(Protocol):
    name: str

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult: ...


PROVIDER_PRIORITY = {
    "nitter": 10,
    "twitter-cli-legacy": 20,
    "vxtwitter": 30,
    "fxtwitter": 30,
    "twitter-cli": 40,
    "zhihu-local": 40,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _normalise_attributed_name(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _zhihu_profile_handle(value: str) -> str:
    match = re.fullmatch(r"https://www\.zhihu\.com/people/([A-Za-z0-9_-]{1,100})/?", value.strip())
    if not match:
        raise ValueError("invalid Zhihu profile URL")
    return match.group(1)


def _utc_and_local(value: str) -> tuple[str, str]:
    if not value:
        raise ValueError("twitter post is missing an exact timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("twitter post timestamp has no timezone")
    utc = parsed.astimezone(timezone.utc)
    local = parsed.astimezone(SHANGHAI)
    return utc.isoformat(timespec="seconds"), local.isoformat(timespec="seconds")


DEFAULT_OUTBOUND_PROXY = "http://127.0.0.1:7897"


def proxy_opener() -> urllib.request.OpenerDirector:
    """urllib opener that carries the configured outbound proxy.

    Media and public-backup hosts that are only reachable through the local
    proxy otherwise stall every request until the socket timeout, which shows
    up as multi-minute fetches and mass ``url: timeout`` media errors.
    ``KOL_X_PROXY`` overrides the default; set it to an empty string to force
    direct connections.
    """
    proxy = os.environ.get("KOL_X_PROXY", DEFAULT_OUTBOUND_PROXY).strip()
    if not proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
