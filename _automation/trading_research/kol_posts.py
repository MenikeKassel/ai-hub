from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from filelock import FileLock, Timeout as FileLockTimeout

from kol_tracker import SHANGHAI, now_iso
from opencode_go import (
    OPENCODE_GO_API_URL,
    OPENCODE_GO_MODEL,
    load_opencode_go_api_key,
)


SEED_KOLS = [
    ("Public KOL 1", "public_kol_1", "A股、AI硬件、半导体主题"),
    ("Public KOL 2", "public_kol_2", "A股深研、业绩预告、事件驱动"),
    ("Public KOL 3", "public_kol_3", "个股推荐"),
    ("Public KOL 4", "public_kol_4", "市场周期、流动性、市场情绪"),
    ("Mistery / public_kol_5", "public_kol_5", "交易心理、市场人性"),
    ("Public KOL 6", "Public KOL 6", "待补"),
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


def normalise_twitter_post(
    payload: dict[str, Any],
    kol: dict[str, Any],
    *,
    provider: str = "twitter-cli",
    provider_warning: str = "",
) -> PostRecord:
    post_id = str(payload.get("id") or "").strip()
    if not re.fullmatch(r"\d{5,25}", post_id):
        raise ValueError("twitter post has an invalid id")
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    quoted = payload.get("quotedTweet") if isinstance(payload.get("quotedTweet"), dict) else {}
    quoted_author_data = quoted.get("author") if isinstance(quoted.get("author"), dict) else {}
    reply_to_id = str(
        payload.get("inReplyToStatusId")
        or payload.get("inReplyToTweetId")
        or payload.get("replyToId")
        or ""
    )
    utc_value, local_value = _utc_and_local(str(payload.get("createdAtISO") or ""))
    is_retweet = bool(payload.get("isRetweet"))
    post_type = "retweet" if is_retweet else "quote" if quoted else "reply" if reply_to_id else "original"
    text = str(payload.get("text") or "").strip()
    article_text = str(payload.get("articleText") or "").strip()
    quoted_text = str(quoted.get("text") or "").strip()
    media = list(payload.get("media") or [])
    if not any([text, article_text, quoted_text, media]):
        raise ValueError("twitter post has no usable text or media")
    hash_input = "\n".join([post_id, text, article_text, quoted_text]).encode("utf-8")
    return PostRecord(
        post_id=post_id,
        kol_id=int(kol["id"]),
        platform="X",
        handle=str(kol["handle"]),
        author_name=str(author.get("name") or kol.get("display_name") or kol["handle"]),
        url=str(payload.get("url") or f"https://x.com/{kol['handle']}/status/{post_id}"),
        text=text,
        article_title=str(payload.get("articleTitle") or ""),
        article_text=article_text,
        quoted_id=str(quoted.get("id") or ""),
        quoted_text=quoted_text,
        quoted_author=str(quoted_author_data.get("screenName") or ""),
        reply_to_id=reply_to_id,
        reply_to_author=str(payload.get("inReplyToScreenName") or payload.get("replyToAuthor") or ""),
        posted_at=local_value,
        posted_at_utc=utc_value,
        post_type=post_type,
        language=str(payload.get("lang") or ""),
        media=media,
        metrics=dict(payload.get("metrics") or {}),
        raw_payload=payload,
        content_hash=hashlib.sha256(hash_input).hexdigest(),
        fetched_at=now_iso(),
        canonical_provider=provider,
        metrics_provider=provider,
        provider_warning=provider_warning,
    )


ZHIHU_DIGEST_AUTHOR_RE = re.compile(
    r"(?:^|(?<=[。！？\n]))"
    r"(?P<name>[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _./·-]{0,30})"
    r"\s*本周主题\s*[:：]",
    re.MULTILINE,
)


def extract_zhihu_digest_attributions(text: str) -> list[dict[str, Any]]:
    clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(ZHIHU_DIGEST_AUTHOR_RE.finditer(clean))
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        name = re.sub(r"\s+", " ", match.group("name")).strip(" ：:。；;")
        names = [name]
        if name == "Public KOL 50无Public KOL 38":
            names = ["Public KOL 50", "Public KOL 38"]
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        section = clean[match.start():section_end].strip()[:20000]
        for attributed_name in names:
            normalized = re.sub(r"\s+", "", attributed_name).casefold()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            values.append(
                {
                    "author_name": attributed_name,
                    "section_text": "无" if attributed_name == "Public KOL 50" else section,
                    "symbols": sorted(set(re.findall(r"(?<!\d)\d{6}(?!\d)", section))),
                    "status": "secondhand_aggregation",
                }
            )
    for match in re.finditer(
        r"(?m)^(?P<name>[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff _./·-]{0,30})\n无(?:\n|$)",
        clean,
    ):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        if name in {"本周主题", "本周操作", "本周观点", "本周风险"}:
            continue
        normalized = re.sub(r"\s+", "", name).casefold()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(
                {
                    "author_name": name,
                    "section_text": "无",
                    "symbols": [],
                    "status": "secondhand_aggregation",
                }
            )
    return values


def normalise_zhihu_answer(
    payload: dict[str, Any],
    kol: dict[str, Any],
    *,
    provider: str = "zhihu-local",
) -> PostRecord:
    post_id = str(payload.get("id") or "").strip()
    if not re.fullmatch(r"\d{5,25}", post_id):
        raise ValueError("Zhihu answer has an invalid id")
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    utc_value, local_value = _utc_and_local(str(payload.get("createdAtISO") or ""))
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("Zhihu answer has no usable text")
    url = str(payload.get("url") or "").strip()
    if not re.fullmatch(r"https://www\.zhihu\.com/question/\d+/answer/\d+", url):
        raise ValueError("Zhihu answer has no canonical URL")
    tracking_mode = str(kol.get("tracking_mode") or "").strip()
    is_aggregation = tracking_mode == "aggregation"
    raw_payload = dict(payload)
    raw_payload["source_kind"] = "aggregation" if is_aggregation else "direct_profile"
    raw_payload["attributions"] = extract_zhihu_digest_attributions(text) if is_aggregation else []
    hash_input = "\n".join([post_id, text, str(payload.get("articleTitle") or "")]).encode("utf-8")
    return PostRecord(
        post_id=post_id,
        kol_id=int(kol["id"]),
        platform="Zhihu",
        handle=str(kol["handle"]),
        author_name=str(author.get("name") or kol.get("display_name") or kol["handle"]),
        url=url,
        text=text,
        article_title=str(payload.get("articleTitle") or ""),
        article_text="",
        quoted_id="",
        quoted_text="",
        quoted_author="",
        reply_to_id="",
        reply_to_author="",
        posted_at=local_value,
        posted_at_utc=utc_value,
        post_type="aggregation" if is_aggregation else "answer",
        language=str(payload.get("lang") or "zh-CN"),
        media=[],
        metrics=dict(payload.get("metrics") or {}),
        raw_payload=raw_payload,
        content_hash=hashlib.sha256(hash_input).hexdigest(),
        fetched_at=now_iso(),
        canonical_provider=provider,
        metrics_provider=provider,
        provider_warning="secondhand_aggregation" if is_aggregation else "",
    )


class RuleClassifier:
    def __init__(self, stock_aliases: dict[str, str] | None = None):
        self.stock_aliases = stock_aliases or {}

    def classify(self, post: PostRecord | dict[str, Any], supplemental_text: str = "") -> RuleResult:
        def value(name: str, default: Any = "") -> Any:
            return post.get(name, default) if isinstance(post, dict) else getattr(post, name, default)

        source_content = "\n".join(
            [
                str(value("text")),
                str(value("article_title")),
                str(value("article_text")),
                str(value("quoted_text")),
            ]
        )
        content = "\n".join([source_content, supplemental_text])
        reasons: list[str] = []
        source_symbols = set(re.findall(r"(?<!\d)(?:\d{6})(?!\d)", source_content))
        ocr_symbols = set(re.findall(r"(?<!\d)(?:\d{6})(?!\d)", supplemental_text))
        if self.stock_aliases:
            # OCR commonly turns prices and turnover values into six-digit tokens.
            # Only accept image-derived codes that exist in the instrument master.
            ocr_symbols = {symbol for symbol in ocr_symbols if symbol in self.stock_aliases}
        symbols = sorted(source_symbols | ocr_symbols)
        if symbols:
            reasons.append("stock_code")
        for symbol, name in self.stock_aliases.items():
            if name and name in content and symbol not in symbols:
                symbols.append(symbol)
                reasons.append("stock_name")
        long_hits = sorted(word for word in LONG_WORDS if word in content)
        short_hits = sorted(word for word in SHORT_WORDS if word in content)
        if long_hits:
            reasons.append("long_language")
        if short_hits:
            reasons.append("short_language")
        if value("media", []):
            reasons.append("has_media")
        finance_hits = sorted(word for word in FINANCE_WORDS if word in content)
        if finance_hits:
            reasons.append("finance_language")
        # Direction needs a strict majority of directional words; a tie
        # (e.g. one bullish and one bearish token) yields no direction
        # rather than defaulting to short.
        if long_hits and not short_hits:
            direction = "long"
        elif short_hits and not long_hits:
            direction = "short"
        elif long_hits and len(long_hits) > len(short_hits):
            direction = "long"
        elif short_hits and len(short_hits) > len(long_hits):
            direction = "short"
        else:
            direction = ""
        retrospective = any(word in content for word in RETROSPECTIVE_WORDS)
        evidence_type = "retrospective" if retrospective else "original_pre_event"
        if value("post_type") in {"retweet", "aggregation"}:
            evidence_type = "secondhand"
        score = min(
            100,
            len(symbols) * 35
            + bool(long_hits or short_hits) * 35
            + bool(value("media", [])) * 10,
        )
        if retrospective:
            score = max(0, score - 30)
            reasons.append("retrospective_language")
        image_candidate = bool(value("media", []))
        is_candidate = bool(
            (symbols and direction or image_candidate)
            and value("post_type") not in {"retweet", "aggregation"}
        )
        if image_candidate and not (symbols and direction):
            reasons.append("financial_image_review")
        content_type = "recommendation" if symbols and direction else "market_view" if long_hits or short_hits or finance_hits else "other"
        return RuleResult(
            score=score,
            is_candidate=is_candidate,
            symbols=symbols,
            direction=direction,
            reasons=reasons,
            evidence_type=evidence_type,
            content_type=content_type,
        )

    def classify_structured_text(self, post: PostRecord | dict[str, Any]) -> dict[str, Any] | None:
        def value(name: str, default: Any = "") -> Any:
            return post.get(name, default) if isinstance(post, dict) else getattr(post, name, default)

        if value("post_type", "original") not in {"", "original", "answer"} or not self.stock_aliases:
            return None
        text = str(value("text") or "").strip()
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        header_index = next(
            (index for index, line in enumerate(lines) if STRUCTURED_RECOMMENDATION_HEADER.search(line)),
            -1,
        )
        if header_index < 0:
            return None

        aliases = sorted(
            ((str(symbol), str(name)) for symbol, name in self.stock_aliases.items() if name),
            key=lambda item: len(item[1]),
            reverse=True,
        )
        def unique_matches(value: str, *, used: set[str] | None = None) -> list[tuple[str, str]]:
            found: list[tuple[str, str]] = []
            for symbol in re.findall(r"(?<!\d)(?:\d{6})(?!\d)", value):
                name = dict(aliases).get(symbol)
                if name and (not used or symbol not in used):
                    found.append((symbol, name))
            for symbol, name in aliases:
                if name and name in value and (not used or symbol not in used):
                    found.append((symbol, name))
            result: list[tuple[str, str]] = []
            seen: set[str] = set()
            for symbol, name in found:
                if symbol not in seen:
                    result.append((symbol, name))
                    seen.add(symbol)
            return result

        def heading_match(value: str, name: str) -> tuple[bool, str]:
            clean = value.strip(" ：:－-·")
            if clean == name:
                return True, ""
            match = re.match(rf"^{re.escape(name)}(?:\s*[:：\-—]\s*|\s+)(?P<rest>.+)$", value)
            return (True, str(match.group("rest") or "").strip()) if match else (False, "")

        # A title can enumerate several securities with full-width brackets.
        # Later standalone headings attach the reason text to the matching item.
        recommendations: list[dict[str, Any]] = []
        by_symbol: dict[str, dict[str, Any]] = {}
        used_symbols: set[str] = set()
        headline = lines[header_index]
        for bracket in re.findall(r"【([^】]+)】", headline):
            matches = unique_matches(bracket, used=used_symbols)
            if len(matches) == 1:
                symbol, name = matches[0]
                item = {"symbol": symbol, "name": name, "start": header_index, "inline": "", "evidence": [headline]}
                recommendations.append(item)
                by_symbol[symbol] = item
                used_symbols.add(symbol)

        stop_index = len(lines)
        last_item_index = header_index
        for index, line in enumerate(lines[header_index + 1 :], start=header_index + 1):
            if RETROSPECTIVE_CLAIM.search(line):
                stop_index = index
                break
            numbered = NUMBERED_STOCK_LINE.match(line)
            body = numbered.group("body") if numbered else line
            matches = unique_matches(body, used=used_symbols if numbered else None)
            if numbered:
                if len(matches) != 1:
                    return None
                symbol, name = matches[0]
                item = by_symbol.get(symbol)
                if item is None:
                    item = {"symbol": symbol, "name": name, "start": index, "inline": "", "evidence": []}
                    recommendations.append(item)
                    by_symbol[symbol] = item
                    used_symbols.add(symbol)
                item["start"] = index
                item["inline"] = body.split(name, 1)[1].strip(" ：:－-·") if name in body else ""
                item["evidence"].append(line)
                last_item_index = index
                continue

            if len(matches) != 1:
                continue
            symbol, name = matches[0]
            is_heading, inline = heading_match(line, name)
            if not is_heading:
                continue
            item = by_symbol.get(symbol)
            if item is None:
                item = {"symbol": symbol, "name": name, "start": index, "inline": inline, "evidence": [line]}
                recommendations.append(item)
                by_symbol[symbol] = item
                used_symbols.add(symbol)
            elif item["start"] == header_index:
                item["start"] = index
                item["evidence"].append(line)
            item["inline"] = inline
            last_item_index = index

        if not recommendations:
            return None

        # Build one exact evidence/thesis block per security. A section ends at
        # the next parsed security heading, so reasons cannot leak across stocks.
        recommendations.sort(key=lambda item: int(item["start"]))
        for position, item in enumerate(recommendations):
            start = int(item["start"])
            end = int(recommendations[position + 1]["start"]) if position + 1 < len(recommendations) else stop_index
            section = lines[start:end]
            reason_lines = [line for line in section[1:] if line and not line.startswith("#")]
            inline = str(item.get("inline") or "").strip()
            if inline:
                reason_lines.insert(0, inline)
            item["reason_lines"] = list(dict.fromkeys(reason_lines))
            item["evidence"].extend(reason_lines)

        # Include the boundary line itself so a trailing retrospective claim
        # remains visible in the summary without becoming a recommendation.
        tail_end = min(len(lines), stop_index + 1) if stop_index < len(lines) else stop_index
        tail = "\n".join(lines[last_item_index + 1 : tail_end])
        retrospective_names = [
            name
            for symbol, name in sorted(aliases, key=lambda item: tail.find(item[1]) if item[1] in tail else len(tail))
            if symbol not in used_symbols and name in tail
        ]
        recommendation_names = "、".join(item["name"] for item in recommendations)
        summary = f"事前分享：{recommendation_names}。"
        if retrospective_names:
            summary += (
                f"{'、'.join(retrospective_names)}属于昨日推荐的复盘声称，"
                "未据此生成新事件草稿。"
            )
        intent_text = "\n".join(lines[header_index:stop_index])
        explicit_plan = any(marker in intent_text for marker in PROSPECTIVE_PLAN_MARKERS)
        action = "buy" if explicit_plan else "watch"
        drafts = [
            {
                "symbol": item["symbol"],
                "security_name": item["name"],
                "direction": "long",
                "action": action,
                "horizon": "unspecified",
                "strength": "explicit",
                "thesis": "；".join(item["reason_lines"]) or f"作者将{item['name']}列入“{headline}”清单；原帖未给出个股理由。",
                "evidence_type": "original_pre_event",
                "confidence": 0.99,
                "evidence_spans": list(dict.fromkeys(item["evidence"] or [headline])),
                "evidence_source": "text",
                "conditions": [],
                "depends_on_ocr": False,
                "mention_kind": "recommendation",
            }
            for item in recommendations
        ]
        return validate_model_payload(
            {
                "content_type": "recommendation",
                "evidence_type": "original_pre_event",
                "confidence": 0.99,
                "summary": summary,
                "drafts": drafts,
            }
        )


def validate_event_draft(draft: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    symbol = str(draft.get("symbol") or "")
    if not re.fullmatch(r"\d{6}", symbol):
        errors.append("one_symbol_per_event")
    if str(draft.get("direction") or "") not in {"long", "short"}:
        errors.append("direction")
    if not str(draft.get("thesis") or "").strip():
        errors.append("thesis")
    if str(draft.get("evidence_type") or "") != "original_pre_event":
        errors.append("evidence_type")
    return sorted(set(errors))


def validate_model_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Codex classification must be an object")
    required = {"content_type", "evidence_type", "confidence", "summary", "drafts"}
    if set(value) != required:
        raise ValueError("Codex classification has missing or additional fields")
    if value["content_type"] not in {"recommendation", "methodology", "market_view", "other"}:
        raise ValueError("invalid Codex content_type")
    if value["evidence_type"] not in {"original_pre_event", "retrospective", "secondhand", "ambiguous"}:
        raise ValueError("invalid Codex evidence_type")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("invalid Codex confidence")
    if not isinstance(value["summary"], str) or not isinstance(value["drafts"], list):
        raise ValueError("invalid Codex summary or drafts")
    draft_fields = {
        "symbol",
        "security_name",
        "direction",
        "thesis",
        "evidence_type",
        "confidence",
        "evidence_spans",
        "evidence_source",
        "conditions",
        "depends_on_ocr",
        "mention_kind",
        "action",
        "horizon",
        "strength",
    }
    for draft in value["drafts"]:
        if not isinstance(draft, dict):
            raise ValueError("invalid Codex draft fields")
        draft.setdefault("action", "watch")
        draft.setdefault("horizon", "unspecified")
        draft.setdefault("strength", "unspecified")
        if set(draft) != draft_fields:
            raise ValueError("invalid Codex draft fields")
        if not re.fullmatch(r"\d{6}", str(draft["symbol"])):
            raise ValueError("invalid Codex draft symbol")
        if draft["direction"] not in {"long", "short"}:
            raise ValueError("invalid Codex draft direction")
        if draft["action"] not in RECOMMENDATION_ACTIONS:
            raise ValueError("invalid Codex draft action")
        if draft["horizon"] not in RECOMMENDATION_HORIZONS:
            raise ValueError("invalid Codex draft horizon")
        if draft["strength"] not in RECOMMENDATION_STRENGTHS:
            raise ValueError("invalid Codex draft strength")
        if draft["evidence_type"] not in {"original_pre_event", "retrospective", "secondhand", "ambiguous"}:
            raise ValueError("invalid Codex draft evidence_type")
        draft_confidence = draft["confidence"]
        if isinstance(draft_confidence, bool) or not isinstance(draft_confidence, (int, float)) or not 0 <= draft_confidence <= 1:
            raise ValueError("invalid Codex draft confidence")
        if not isinstance(draft["security_name"], str) or not isinstance(draft["thesis"], str):
            raise ValueError("invalid Codex draft text")
        if (
            not isinstance(draft["evidence_spans"], list)
            or not all(isinstance(item, str) for item in draft["evidence_spans"])
            or not isinstance(draft["conditions"], list)
            or not all(isinstance(item, str) for item in draft["conditions"])
        ):
            raise ValueError("invalid Codex evidence or conditions")
        if draft["evidence_source"] not in {"text", "article_text", "quoted_text", "ocr", "image"}:
            raise ValueError("invalid Codex evidence_source")
        if not isinstance(draft["depends_on_ocr"], bool):
            raise ValueError("invalid Codex depends_on_ocr")
        if draft["mention_kind"] not in {"recommendation", "holding", "retrospective", "analysis"}:
            raise ValueError("invalid Codex mention_kind")
    return value


class KolPostStore:
    def __init__(self, path: Path, media_root: Path):
        self.path = Path(path)
        self.media_root = Path(media_root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'X',
                    handle TEXT NOT NULL COLLATE NOCASE,
                    profile_url TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
                    tracking_mode TEXT NOT NULL DEFAULT 'all',
                    last_post_id TEXT NOT NULL DEFAULT '',
                    last_fetched_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_gap_at TEXT NOT NULL DEFAULT '',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    backfill_requested INTEGER NOT NULL DEFAULT 0,
                    backfill_status TEXT NOT NULL DEFAULT 'idle',
                    backfill_completed_depth INTEGER NOT NULL DEFAULT 0,
                    backfill_result_count INTEGER NOT NULL DEFAULT 0,
                    backfill_warning TEXT NOT NULL DEFAULT '',
                    fetch_status TEXT NOT NULL DEFAULT 'never',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform,handle)
                );
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL DEFAULT '',
                    article_title TEXT NOT NULL DEFAULT '',
                    article_text TEXT NOT NULL DEFAULT '',
                    quoted_id TEXT NOT NULL DEFAULT '',
                    quoted_text TEXT NOT NULL DEFAULT '',
                    quoted_author TEXT NOT NULL DEFAULT '',
                    reply_to_id TEXT NOT NULL DEFAULT '',
                    reply_to_author TEXT NOT NULL DEFAULT '',
                    posted_at TEXT NOT NULL,
                    posted_at_utc TEXT NOT NULL,
                    post_type TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    media_json TEXT NOT NULL DEFAULT '[]',
                    local_media_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    raw_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    canonical_provider TEXT NOT NULL DEFAULT 'twitter-cli-legacy',
                    metrics_provider TEXT NOT NULL DEFAULT 'twitter-cli-legacy',
                    provider_warning TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(review_status IN ('pending','approved','excluded','ignored','capture_failed')),
                    review_note TEXT NOT NULL DEFAULT '',
                    source_note TEXT NOT NULL DEFAULT '',
                    notion_url TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_posts_kol_date ON posts(kol_id, posted_at DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_review ON posts(review_status, posted_at DESC);
                CREATE TABLE IF NOT EXISTS digest_attributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    attributed_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    section_text TEXT NOT NULL DEFAULT '',
                    symbols_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'secondhand_aggregation',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(source_post_id,normalized_name)
                );
                CREATE INDEX IF NOT EXISTS idx_digest_attributions_name
                    ON digest_attributions(normalized_name,last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS digest_author_profiles (
                    normalized_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    handle TEXT NOT NULL UNIQUE,
                    profile_url TEXT NOT NULL UNIQUE,
                    tracking_status TEXT NOT NULL DEFAULT 'linked_only'
                        CHECK(tracking_status IN ('linked_only','paused','active')),
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS classifications (
                    post_id TEXT PRIMARY KEY REFERENCES posts(post_id),
                    rule_score INTEGER NOT NULL DEFAULT 0,
                    rule_reasons_json TEXT NOT NULL DEFAULT '[]',
                    rule_symbols_json TEXT NOT NULL DEFAULT '[]',
                    rule_direction TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'other',
                    evidence_type TEXT NOT NULL DEFAULT 'ambiguous',
                    is_candidate INTEGER NOT NULL DEFAULT 0,
                    model_status TEXT NOT NULL DEFAULT 'not_requested',
                    model_attempts INTEGER NOT NULL DEFAULT 0,
                    model_next_retry_at TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    model_summary TEXT NOT NULL DEFAULT '',
                    drafts_json TEXT NOT NULL DEFAULT '[]',
                    draft_generation_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(draft_generation_status IN ('pending','generated','not_applicable','needs_attention','failed')),
                    draft_generation_version TEXT NOT NULL DEFAULT '',
                    draft_generation_error TEXT NOT NULL DEFAULT '',
                    draft_generated_at TEXT NOT NULL DEFAULT '',
                    model_error TEXT NOT NULL DEFAULT '',
                    ocr_status TEXT NOT NULL DEFAULT 'not_needed',
                    ocr_attempts INTEGER NOT NULL DEFAULT 0,
                    ocr_next_retry_at TEXT NOT NULL DEFAULT '',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    ocr_provider TEXT NOT NULL DEFAULT '',
                    ocr_confidence REAL NOT NULL DEFAULT 0,
                    ocr_details_json TEXT NOT NULL DEFAULT '[]',
                    ocr_error TEXT NOT NULL DEFAULT '',
                    ocr_updated_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id),
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fetch_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    requested_count INTEGER NOT NULL,
                    total_kols INTEGER NOT NULL DEFAULT 0,
                    processed_kols INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    successful_kols INTEGER NOT NULL DEFAULT 0,
                    failed_kols INTEGER NOT NULL DEFAULT 0,
                    new_posts INTEGER NOT NULL DEFAULT 0,
                    candidate_posts INTEGER NOT NULL DEFAULT 0,
                    auth_status TEXT NOT NULL DEFAULT 'unknown',
                    codex_completed INTEGER NOT NULL DEFAULT 0,
                    codex_failed INTEGER NOT NULL DEFAULT 0,
                    gap_kols_json TEXT NOT NULL DEFAULT '[]',
                    fallback_kols_json TEXT NOT NULL DEFAULT '[]',
                    shadow_failed_kols_json TEXT NOT NULL DEFAULT '[]',
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS fetch_queue (
                    batch_key TEXT NOT NULL,
                    kol_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    requested_count INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued'
                        CHECK(state IN ('queued','running','cooldown','completed')),
                    not_before TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_key,kol_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_queue_pending
                    ON fetch_queue(batch_key,state,not_before,position);
                CREATE TABLE IF NOT EXISTS post_sources (
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(post_id, provider)
                );
                CREATE TABLE IF NOT EXISTS post_content_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_post_content_revisions_post
                    ON post_content_revisions(post_id,id DESC);
                CREATE TABLE IF NOT EXISTS fetch_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES fetch_runs(run_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    handle TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    post_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_comparisons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES fetch_runs(run_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    handle TEXT NOT NULL,
                    primary_count INTEGER NOT NULL,
                    fallback_count INTEGER NOT NULL,
                    matching_count INTEGER NOT NULL,
                    coverage REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id,kol_id)
                );
                CREATE TABLE IF NOT EXISTS stock_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    symbol TEXT NOT NULL,
                    security_name TEXT NOT NULL DEFAULT '',
                    instrument_type TEXT NOT NULL DEFAULT 'stock',
                    direction TEXT NOT NULL DEFAULT '',
                    mention_kind TEXT NOT NULL DEFAULT 'analysis',
                    evidence_text TEXT NOT NULL DEFAULT '',
                    extraction_method TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','confirmed','ignored')),
                    auto_confirmed INTEGER NOT NULL DEFAULT 0,
                    review_note TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(post_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS stock_lead_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL REFERENCES stock_leads(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lead_extractions (
                    post_id TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
                    source_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    extracted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_agent_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    mode TEXT NOT NULL DEFAULT 'shadow' CHECK(mode IN ('shadow','enabled')),
                    policy_version TEXT NOT NULL DEFAULT 'review-agent-v1',
                    shadow_started_at TEXT NOT NULL DEFAULT '',
                    enabled_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_agent_runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    auto_approved INTEGER NOT NULL DEFAULT 0,
                    auto_excluded INTEGER NOT NULL DEFAULT 0,
                    auto_ignored INTEGER NOT NULL DEFAULT 0,
                    needs_human INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS review_agent_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    input_signature TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN (
                        'auto_approve','auto_exclude','auto_ignore','needs_human','failed'
                    )),
                    confidence REAL NOT NULL DEFAULT 0,
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    drafts_json TEXT NOT NULL DEFAULT '[]',
                    validator_errors_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN (
                        'proposed','applied','overridden','rolled_back','failed'
                    )),
                    event_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_event_ids_json TEXT NOT NULL DEFAULT '[]',
                    side_effects_json TEXT NOT NULL DEFAULT '{}',
                    model_name TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT '',
                    overridden_at TEXT NOT NULL DEFAULT '',
                    rolled_back_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(post_id,input_signature,policy_version,mode)
                );
                CREATE TABLE IF NOT EXISTS recommendation_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    security_name TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT 'watch',
                    horizon TEXT NOT NULL DEFAULT 'unspecified',
                    strength TEXT NOT NULL DEFAULT 'unspecified',
                    thesis TEXT NOT NULL DEFAULT '',
                    evidence_type TEXT NOT NULL DEFAULT 'ambiguous',
                    evidence_spans_json TEXT NOT NULL DEFAULT '[]',
                    evidence_source TEXT NOT NULL DEFAULT 'text',
                    depends_on_ocr INTEGER NOT NULL DEFAULT 0,
                    conditions_json TEXT NOT NULL DEFAULT '[]',
                    mention_kind TEXT NOT NULL DEFAULT 'recommendation',
                    confidence REAL NOT NULL DEFAULT 0,
                    model_name TEXT NOT NULL DEFAULT '',
                    extraction_version TEXT NOT NULL,
                    source_signature TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN (
                        'ready','needs_attention','approved','rejected','superseded'
                    )),
                    attention_reasons_json TEXT NOT NULL DEFAULT '[]',
                    queue_scope TEXT NOT NULL DEFAULT 'morning' CHECK(queue_scope IN ('morning','backlog')),
                    review_date TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(post_id,symbol,extraction_version)
                );
                CREATE TABLE IF NOT EXISTS recommendation_draft_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES recommendation_drafts(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES recommendation_drafts(id) ON DELETE CASCADE,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    correction_type TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS morning_runs (
                    run_id TEXT PRIMARY KEY,
                    review_date TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    fetched_posts INTEGER NOT NULL DEFAULT 0,
                    reviewed_posts INTEGER NOT NULL DEFAULT 0,
                    ready_drafts INTEGER NOT NULL DEFAULT 0,
                    attention_drafts INTEGER NOT NULL DEFAULT 0,
                    failed_posts INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'legacy',
                    active_kols INTEGER NOT NULL DEFAULT 0,
                    successful_kols INTEGER NOT NULL DEFAULT 0,
                    failed_kols INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_attempts_run ON fetch_attempts(run_id,id);
                CREATE INDEX IF NOT EXISTS idx_provider_comparisons_run ON provider_comparisons(run_id,id);
                CREATE INDEX IF NOT EXISTS idx_stock_leads_status ON stock_leads(status,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_stock_leads_symbol ON stock_leads(symbol,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_review_agent_decisions_post
                    ON review_agent_decisions(post_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_review_agent_decisions_queue
                    ON review_agent_decisions(decision,status,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_recommendation_drafts_queue
                    ON recommendation_drafts(review_date,queue_scope,status,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_recommendation_drafts_post
                    ON recommendation_drafts(post_id,status,id);
                CREATE INDEX IF NOT EXISTS idx_draft_revisions_draft
                    ON draft_revisions(draft_id,created_at DESC);
                """
            )
            table_sql = str(
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='kols'"
                ).fetchone()["sql"]
            )
            if "handle TEXT NOT NULL COLLATE NOCASE UNIQUE" in table_sql:
                db.commit()
                db.execute("PRAGMA foreign_keys=OFF")
                db.executescript(
                    """
                    ALTER TABLE kols RENAME TO kols_legacy_unique_handle;
                    CREATE TABLE kols (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        display_name TEXT NOT NULL,
                        platform TEXT NOT NULL DEFAULT 'X',
                        handle TEXT NOT NULL COLLATE NOCASE,
                        profile_url TEXT NOT NULL,
                        domain TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
                        tracking_mode TEXT NOT NULL DEFAULT 'all',
                        last_post_id TEXT NOT NULL DEFAULT '',
                        last_fetched_at TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        last_gap_at TEXT NOT NULL DEFAULT '',
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        backfill_requested INTEGER NOT NULL DEFAULT 0,
                        backfill_status TEXT NOT NULL DEFAULT 'idle',
                        backfill_completed_depth INTEGER NOT NULL DEFAULT 0,
                        backfill_result_count INTEGER NOT NULL DEFAULT 0,
                        backfill_warning TEXT NOT NULL DEFAULT '',
                        fetch_status TEXT NOT NULL DEFAULT 'never',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform,handle)
                    );
                    INSERT INTO kols(
                        id,display_name,platform,handle,profile_url,domain,status,tracking_mode,
                        last_post_id,last_fetched_at,last_success_at,last_gap_at,consecutive_failures,
                        backfill_requested,backfill_status,backfill_completed_depth,backfill_result_count,
                        backfill_warning,fetch_status,created_at,updated_at
                    )
                    SELECT
                        id,display_name,platform,handle,profile_url,domain,status,tracking_mode,
                        last_post_id,last_fetched_at,last_success_at,last_gap_at,consecutive_failures,
                        backfill_requested,backfill_status,backfill_completed_depth,backfill_result_count,
                        backfill_warning,fetch_status,created_at,updated_at
                    FROM kols_legacy_unique_handle;
                    DROP TABLE kols_legacy_unique_handle;
                    """
                )
                db.execute("PRAGMA foreign_keys=ON")
            legacy_fk_needed = False
            for table in ("posts", "fetch_attempts", "provider_comparisons", "stock_leads"):
                for row in db.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                    if str(row["table"]) == "kols_legacy_unique_handle":
                        legacy_fk_needed = True
            if legacy_fk_needed:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS kols_legacy_unique_handle (
                        id INTEGER PRIMARY KEY
                    );
                    INSERT OR IGNORE INTO kols_legacy_unique_handle(id)
                    SELECT id FROM kols;
                    CREATE TRIGGER IF NOT EXISTS trg_kols_legacy_insert
                    AFTER INSERT ON kols
                    BEGIN
                        INSERT OR IGNORE INTO kols_legacy_unique_handle(id) VALUES (NEW.id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_kols_legacy_delete
                    AFTER DELETE ON kols
                    BEGIN
                        DELETE FROM kols_legacy_unique_handle WHERE id=OLD.id;
                    END;
                    """
                )
            migrations = {
                "posts": {
                    "reply_to_id": "TEXT NOT NULL DEFAULT ''",
                    "reply_to_author": "TEXT NOT NULL DEFAULT ''",
                    "canonical_provider": "TEXT NOT NULL DEFAULT 'twitter-cli-legacy'",
                    "metrics_provider": "TEXT NOT NULL DEFAULT 'twitter-cli-legacy'",
                    "provider_warning": "TEXT NOT NULL DEFAULT ''",
                },
                "fetch_runs": {
                    "auth_status": "TEXT NOT NULL DEFAULT 'unknown'",
                    "codex_completed": "INTEGER NOT NULL DEFAULT 0",
                    "codex_failed": "INTEGER NOT NULL DEFAULT 0",
                    "fallback_kols_json": "TEXT NOT NULL DEFAULT '[]'",
                    "shadow_failed_kols_json": "TEXT NOT NULL DEFAULT '[]'",
                    "total_kols": "INTEGER NOT NULL DEFAULT 0",
                    "processed_kols": "INTEGER NOT NULL DEFAULT 0",
                    "stage": "TEXT NOT NULL DEFAULT 'queued'",
                },
                "kols": {
                    "last_success_at": "TEXT NOT NULL DEFAULT ''",
                    "last_gap_at": "TEXT NOT NULL DEFAULT ''",
                    "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_requested": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_status": "TEXT NOT NULL DEFAULT 'idle'",
                    "backfill_completed_depth": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_result_count": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_warning": "TEXT NOT NULL DEFAULT ''",
                },
                "classifications": {
                    "model_summary": "TEXT NOT NULL DEFAULT ''",
                    "model_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "model_next_retry_at": "TEXT NOT NULL DEFAULT ''",
                    "draft_generation_status": "TEXT NOT NULL DEFAULT 'pending'",
                    "draft_generation_version": "TEXT NOT NULL DEFAULT ''",
                    "draft_generation_error": "TEXT NOT NULL DEFAULT ''",
                    "draft_generated_at": "TEXT NOT NULL DEFAULT ''",
                    "ocr_status": "TEXT NOT NULL DEFAULT 'not_needed'",
                    "ocr_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "ocr_next_retry_at": "TEXT NOT NULL DEFAULT ''",
                    "ocr_text": "TEXT NOT NULL DEFAULT ''",
                    "ocr_provider": "TEXT NOT NULL DEFAULT ''",
                    "ocr_confidence": "REAL NOT NULL DEFAULT 0",
                    "ocr_details_json": "TEXT NOT NULL DEFAULT '[]'",
                    "ocr_error": "TEXT NOT NULL DEFAULT ''",
                    "ocr_updated_at": "TEXT NOT NULL DEFAULT ''",
                },
                "review_agent_decisions": {
                    "created_event_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                    "side_effects_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "recommendation_drafts": {
                    "evidence_source": "TEXT NOT NULL DEFAULT 'text'",
                    "depends_on_ocr": "INTEGER NOT NULL DEFAULT 0",
                    "action": "TEXT NOT NULL DEFAULT 'watch'",
                    "horizon": "TEXT NOT NULL DEFAULT 'unspecified'",
                    "strength": "TEXT NOT NULL DEFAULT 'unspecified'",
                },
                "morning_runs": {
                    "phase": "TEXT NOT NULL DEFAULT 'legacy'",
                    "active_kols": "INTEGER NOT NULL DEFAULT 0",
                    "successful_kols": "INTEGER NOT NULL DEFAULT 0",
                    "failed_kols": "INTEGER NOT NULL DEFAULT 0",
                    "stage": "TEXT NOT NULL DEFAULT 'queued'",
                    "progress_current": "INTEGER NOT NULL DEFAULT 0",
                    "progress_total": "INTEGER NOT NULL DEFAULT 0",
                },
            }
            for table, columns in migrations.items():
                existing_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, definition in columns.items():
                    if column not in existing_columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            db.execute(
                """
                INSERT OR IGNORE INTO post_sources(
                    post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json
                )
                SELECT post_id,'twitter-cli-legacy',fetched_at,content_hash,raw_json,metrics_json,'[]'
                FROM posts
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO review_agent_settings(
                    id,mode,policy_version,shadow_started_at,updated_at
                ) VALUES(1,'shadow','review-agent-v1',?,?)
                """,
                (now_iso(), now_iso()),
            )
            if db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
                db.execute("INSERT INTO schema_meta(version) VALUES (16)")
            else:
                db.execute("UPDATE schema_meta SET version=16")
            db.execute(
                """
                UPDATE classifications
                SET draft_generation_status=CASE
                    WHEN EXISTS(
                        SELECT 1 FROM recommendation_drafts d
                        WHERE d.post_id=classifications.post_id
                          AND d.status IN ('ready','needs_attention','approved','rejected')
                    ) THEN 'generated'
                    WHEN model_status='failed' THEN 'failed'
                    WHEN model_status='completed' AND content_type<>'recommendation' THEN 'not_applicable'
                    ELSE 'pending'
                END,
                draft_generation_error=CASE WHEN model_status='failed' THEN model_error ELSE '' END,
                draft_generation_version=CASE WHEN prompt_version<>'' THEN prompt_version ELSE 'pending-migration' END
                WHERE draft_generation_version=''
                """
            )
            db.execute(
                """
                UPDATE classifications SET ocr_status=CASE
                    WHEN EXISTS(
                        SELECT 1 FROM posts p WHERE p.post_id=classifications.post_id
                        AND p.local_media_json NOT IN ('','[]')
                        AND p.review_status='pending'
                    ) THEN CASE WHEN ocr_status='completed' THEN ocr_status ELSE 'not_requested' END
                    ELSE 'not_needed' END
                WHERE ocr_status IN ('','not_needed','not_requested')
                """
            )
            run_level_warnings = {
                "shadow_coverage_below_threshold",
                "shadow_fallback_failed",
                "shadow_compared",
                "fallback_used",
            }
            for row in db.execute(
                "SELECT post_id,canonical_provider,provider_warning FROM posts WHERE provider_warning<>''"
            ).fetchall():
                warnings = [
                    warning
                    for warning in str(row["provider_warning"] or "").split(";")
                    if warning and warning not in run_level_warnings
                ]
                if str(row["canonical_provider"] or "") != "nitter":
                    warnings = [warning for warning in warnings if warning != "timestamp_recovered_from_snowflake"]
                cleaned = ";".join(dict.fromkeys(warnings))
                if cleaned != row["provider_warning"]:
                    db.execute("UPDATE posts SET provider_warning=? WHERE post_id=?", (cleaned, row["post_id"]))
            db.execute("PRAGMA user_version=21")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for source, target, default in [
            ("media_json", "media", []),
            ("local_media_json", "local_media", []),
            ("metrics_json", "metrics", {}),
            ("rule_reasons_json", "rule_reasons", []),
            ("rule_symbols_json", "rule_symbols", []),
            ("drafts_json", "drafts", []),
            ("ocr_details_json", "ocr_details", []),
            ("gap_kols_json", "gap_kols", []),
            ("fallback_kols_json", "fallback_kols", []),
            ("shadow_failed_kols_json", "shadow_failed_kols", []),
            ("errors_json", "errors", []),
            ("warnings_json", "warnings", []),
        ]:
            if source in value:
                value[target] = _loads(value.pop(source), default)
        return value

    def add_kol(
        self,
        display_name: str,
        handle: str,
        domain: str = "",
        status: str = "active",
        tracking_mode: str = "all",
        platform: str = "X",
        profile_url: str = "",
    ) -> tuple[int, bool]:
        clean_handle = handle.strip().lstrip("@")
        clean_platform = platform.strip().title()
        if clean_platform == "X":
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", clean_handle):
                raise ValueError("invalid X handle")
            canonical_profile = profile_url.strip() or f"https://x.com/{clean_handle}"
        elif clean_platform == "Zhihu":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", clean_handle):
                raise ValueError("invalid Zhihu url_token")
            canonical_profile = profile_url.strip() or f"https://www.zhihu.com/people/{clean_handle}"
        else:
            raise ValueError("unsupported KOL platform")
        if status not in {"active", "paused"}:
            raise ValueError("invalid KOL status")
        timestamp = now_iso()
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM kols WHERE platform=? AND handle=? COLLATE NOCASE",
                (clean_platform, clean_handle),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = db.execute(
                """
                INSERT INTO kols(
                    display_name,platform,handle,profile_url,domain,status,tracking_mode,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    display_name.strip() or clean_handle,
                    clean_platform,
                    clean_handle,
                    canonical_profile,
                    domain.strip(),
                    status,
                    tracking_mode,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid), True

    def update_kol(self, kol_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"display_name", "domain", "status", "tracking_mode"}
        updates = {key: str(value).strip() for key, value in changes.items() if key in allowed}
        if not updates:
            raise ValueError("no editable KOL fields supplied")
        if "status" in updates and updates["status"] not in {"active", "paused"}:
            raise ValueError("invalid KOL status")
        updates["updated_at"] = now_iso()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE kols SET {assignments} WHERE id=?",  # noqa: S608 - field names are allowlisted.
                (*updates.values(), kol_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"KOL not found: {kol_id}")
        result = self.get_kol(kol_id)
        assert result is not None
        return result

    def list_kols(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM kols"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY display_name COLLATE NOCASE"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def get_kol(self, kol_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM kols WHERE id=?", (kol_id,)).fetchone()
        return self._row(row)

    def get_kol_by_handle(self, handle: str, platform: str | None = None) -> dict[str, Any] | None:
        with self.connect() as db:
            clean_handle = handle.strip().lstrip("@")
            if platform:
                row = db.execute(
                    "SELECT * FROM kols WHERE platform=? AND handle=? COLLATE NOCASE",
                    (platform.strip().title(), clean_handle),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM kols WHERE handle=? COLLATE NOCASE ORDER BY platform='X' DESC LIMIT 1",
                    (clean_handle,),
                ).fetchone()
        return self._row(row)

    @contextmanager
    def model_worker(self, timeout: float = 0):
        lock = FileLock(str(self.path.with_suffix(".model-worker.lock")))
        try:
            lock.acquire(timeout=timeout)
        except FileLockTimeout as exc:
            raise ModelWorkerBusyError("another Codex classifier is already running") from exc
        try:
            yield
        finally:
            lock.release()

    def queue_backfill(self, kol_id: int, count: int) -> dict[str, Any]:
        if count < 1:
            raise ValueError("backfill count must be positive")
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE kols
                SET backfill_requested=?,backfill_status='queued',backfill_completed_depth=0,
                    backfill_result_count=0,backfill_warning='',updated_at=?
                WHERE id=?
                """,
                (int(count), timestamp, kol_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"KOL not found: {kol_id}")
        result = self.get_kol(kol_id)
        assert result is not None
        return result

    def update_fetch_state(
        self,
        kol_id: int,
        post_id: str,
        status: str,
        *,
        fetched_count: int = 0,
        requested_count: int = 0,
    ) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT last_post_id,last_success_at,consecutive_failures,
                    backfill_requested,backfill_status,backfill_completed_depth,
                    backfill_result_count,backfill_warning
                FROM kols WHERE id=?
                """,
                (kol_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"KOL not found: {kol_id}")
            backfill_requested = int(row["backfill_requested"] or 0)
            backfill_status = str(row["backfill_status"] or "idle")
            backfill_completed_depth = int(row["backfill_completed_depth"] or 0)
            backfill_result_count = int(row["backfill_result_count"] or 0)
            backfill_warning = str(row["backfill_warning"] or "")
            if backfill_status == "queued" and backfill_requested > 0:
                backfill_result_count = max(0, fetched_count)
                if status == "success" and fetched_count >= max(1, requested_count):
                    backfill_completed_depth = max(
                        backfill_completed_depth,
                        min(requested_count, backfill_requested),
                    )
                    if backfill_completed_depth >= backfill_requested:
                        backfill_requested = 0
                        backfill_status = "completed"
                elif status == "success":
                    backfill_status = "needs_review"
                    backfill_warning = (
                        f"provider returned {fetched_count} of {requested_count}; "
                        "timeline may be exhausted or provider-limited"
                    )
            clean_success = status == "success"
            non_failure = status in {"success", "gap_detected"}
            stored_post_id = post_id if clean_success else str(row["last_post_id"] or "")
            last_success_at = timestamp if clean_success else str(row["last_success_at"] or "")
            consecutive_failures = (
                0
                if non_failure
                else int(row["consecutive_failures"] or 0) + 1
            )
            db.execute(
                """
                UPDATE kols
                SET last_post_id=?,last_fetched_at=?,last_success_at=?,fetch_status=?,
                    last_gap_at=CASE WHEN ?='gap_detected' THEN ? ELSE last_gap_at END,
                    consecutive_failures=?,backfill_requested=?,backfill_status=?,
                    backfill_completed_depth=?,backfill_result_count=?,backfill_warning=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    stored_post_id,
                    timestamp,
                    last_success_at,
                    status,
                    status,
                    timestamp,
                    consecutive_failures,
                    backfill_requested,
                    backfill_status,
                    backfill_completed_depth,
                    backfill_result_count,
                    backfill_warning,
                    timestamp,
                    kol_id,
                ),
            )

    def mark_fetch_failed(self, kol_id: int, status: str = "failed") -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE kols
                SET fetch_status=?,consecutive_failures=consecutive_failures+1,updated_at=?
                WHERE id=?
                """,
                (status, now_iso(), kol_id),
            )

    def upsert_post(self, post: PostRecord, *, run_id: str = "") -> bool:
        timestamp = now_iso()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM posts WHERE post_id=?", (post.post_id,)).fetchone()
            warning = post.provider_warning
            if existing is not None:
                if str(existing["content_hash"] or "") != post.content_hash:
                    db.execute(
                        """
                        INSERT INTO post_content_revisions(
                            post_id,provider,fetched_at,previous_hash,content_hash,raw_json,created_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            post.post_id,
                            post.canonical_provider,
                            post.fetched_at,
                            str(existing["content_hash"] or ""),
                            post.content_hash,
                            _json(post.raw_payload),
                            timestamp,
                        ),
                    )
                try:
                    existing_time = datetime.fromisoformat(str(existing["posted_at_utc"]).replace("Z", "+00:00"))
                    incoming_time = datetime.fromisoformat(post.posted_at_utc.replace("Z", "+00:00"))
                    if abs((incoming_time - existing_time).total_seconds()) > 60:
                        warning = "source_conflict"
                except ValueError:
                    warning = "source_conflict"
                if "source_conflict" in str(existing["provider_warning"] or "").split(";"):
                    warning = "source_conflict"

            db.execute(
                """
                INSERT INTO post_sources(
                    post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(post_id,provider) DO UPDATE SET
                    fetched_at=excluded.fetched_at,content_hash=excluded.content_hash,
                    raw_json=excluded.raw_json,metrics_json=excluded.metrics_json,
                    warnings_json=excluded.warnings_json
                """,
                (
                    post.post_id,
                    post.canonical_provider,
                    post.fetched_at,
                    post.content_hash,
                    _json(post.raw_payload),
                    _json(post.metrics),
                    _json([warning] if warning else []),
                ),
            ) if existing is not None else None

            if existing is None:
                db.execute(
                    """
                    INSERT INTO posts(
                        post_id,kol_id,platform,handle,author_name,url,text,article_title,article_text,
                        quoted_id,quoted_text,quoted_author,reply_to_id,reply_to_author,
                        posted_at,posted_at_utc,post_type,language,media_json,metrics_json,raw_json,
                        content_hash,fetched_at,canonical_provider,metrics_provider,provider_warning,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        post.post_id, post.kol_id, post.platform, post.handle, post.author_name,
                        post.url, post.text, post.article_title, post.article_text, post.quoted_id,
                        post.quoted_text, post.quoted_author, post.reply_to_id, post.reply_to_author,
                        post.posted_at, post.posted_at_utc, post.post_type, post.language,
                        _json(post.media), _json(post.metrics), _json(post.raw_payload),
                        post.content_hash, post.fetched_at, post.canonical_provider,
                        post.metrics_provider, warning, timestamp,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO post_sources(
                        post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        post.post_id, post.canonical_provider, post.fetched_at, post.content_hash,
                        _json(post.raw_payload), _json(post.metrics), _json([warning] if warning else []),
                    ),
                )
                return True

            existing_provider = str(existing["canonical_provider"] or "twitter-cli-legacy")
            incoming_priority = PROVIDER_PRIORITY.get(post.canonical_provider, 0)
            existing_priority = PROVIDER_PRIORITY.get(existing_provider, 0)
            if incoming_priority >= existing_priority:
                db.execute(
                    """
                    UPDATE posts SET author_name=?,url=?,text=?,article_title=?,article_text=?,
                        quoted_id=?,quoted_text=?,quoted_author=?,reply_to_id=?,reply_to_author=?,
                        posted_at=?,posted_at_utc=?,post_type=?,language=?,media_json=?,raw_json=?,
                        content_hash=?,canonical_provider=?,metrics_json=?,metrics_provider=?,
                        provider_warning=?,fetched_at=?,updated_at=? WHERE post_id=?
                    """,
                    (
                        post.author_name, post.url, post.text, post.article_title, post.article_text,
                        post.quoted_id, post.quoted_text, post.quoted_author, post.reply_to_id,
                        post.reply_to_author, post.posted_at, post.posted_at_utc, post.post_type,
                        post.language, _json(post.media), _json(post.raw_payload), post.content_hash,
                        post.canonical_provider, _json(post.metrics), post.metrics_provider, warning,
                        post.fetched_at, timestamp, post.post_id,
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE posts SET metrics_json=?,metrics_provider=?,provider_warning=?,
                        fetched_at=?,updated_at=? WHERE post_id=?
                    """,
                    (
                        _json(post.metrics), post.metrics_provider,
                        warning or str(existing["provider_warning"] or ""),
                        post.fetched_at, timestamp, post.post_id,
                    ),
                )
            return False

    def list_post_sources(self, post_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM post_sources WHERE post_id=? ORDER BY fetched_at DESC",
                (post_id,),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def replace_digest_attributions(
        self,
        post_id: str,
        attributions: list[dict[str, Any]],
    ) -> None:
        timestamp = now_iso()
        normalized_values: set[str] = set()
        with self.connect() as db:
            for attribution in attributions:
                name = re.sub(r"\s+", " ", str(attribution.get("author_name") or "")).strip()
                normalized = _normalise_attributed_name(name)
                if not normalized or normalized in normalized_values:
                    continue
                normalized_values.add(normalized)
                db.execute(
                    """
                    INSERT INTO digest_attributions(
                        source_post_id,attributed_name,normalized_name,section_text,
                        symbols_json,status,first_seen_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_post_id,normalized_name) DO UPDATE SET
                        attributed_name=excluded.attributed_name,
                        section_text=excluded.section_text,
                        symbols_json=excluded.symbols_json,
                        status=excluded.status,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        post_id,
                        name,
                        normalized,
                        str(attribution.get("section_text") or "")[:20000],
                        _json(list(attribution.get("symbols") or [])),
                        "secondhand_aggregation",
                        timestamp,
                        timestamp,
                    ),
                )
            if normalized_values:
                placeholders = ",".join("?" for _ in normalized_values)
                db.execute(
                    f"DELETE FROM digest_attributions WHERE source_post_id=? "  # noqa: S608 - placeholders only.
                    f"AND normalized_name NOT IN ({placeholders})",
                    (post_id, *sorted(normalized_values)),
                )
            else:
                db.execute("DELETE FROM digest_attributions WHERE source_post_id=?", (post_id,))

    def list_digest_authors(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT d.*,p.url AS source_url,p.posted_at,p.article_title
                FROM digest_attributions d
                JOIN posts p ON p.post_id=d.source_post_id
                ORDER BY p.posted_at DESC,d.id DESC
                """
            ).fetchall()
            profile_rows = db.execute("SELECT * FROM digest_author_profiles").fetchall()
        profiles_by_name = {str(row["normalized_name"]): row for row in profile_rows}
        profiles_by_handle = {
            _normalise_attributed_name(str(row["handle"])): row for row in profile_rows
        }
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw_normalized = str(row["normalized_name"])
            aliases = [raw_normalized]
            for suffix in ("的一周操作总结", "的一周总结"):
                if raw_normalized.endswith(suffix):
                    aliases.append(raw_normalized[: -len(suffix)])
            profile = next(
                (
                    profiles_by_name.get(alias) or profiles_by_handle.get(alias)
                    for alias in aliases
                    if profiles_by_name.get(alias) or profiles_by_handle.get(alias)
                ),
                None,
            )
            normalized = str(profile["normalized_name"]) if profile is not None else raw_normalized
            value = grouped.get(normalized)
            if value is None:
                value = {
                    "author_name": (
                        str(profile["display_name"])
                        if profile is not None
                        else str(row["attributed_name"])
                    ),
                    "normalized_name": normalized,
                    "status": "attributed_only",
                    "source_kind": "secondhand_aggregation",
                    "summary_count": 0,
                    "latest_posted_at": str(row["posted_at"]),
                    "latest_source_url": str(row["source_url"]),
                    "latest_title": str(row["article_title"]),
                    "latest_summary": str(row["section_text"]),
                    "symbols": [],
                    "profile_handle": str(profile["handle"] if profile is not None else ""),
                    "profile_url": str(profile["profile_url"] if profile is not None else ""),
                    "tracking_status": str(
                        profile["tracking_status"] if profile is not None else "unresolved"
                    ),
                    "profile_updated_at": str(
                        profile["updated_at"] if profile is not None else ""
                    ),
                }
                grouped[normalized] = value
            value["summary_count"] += 1
            value["symbols"] = sorted(
                set(value["symbols"]) | set(_loads(str(row["symbols_json"]), []))
            )
        return list(grouped.values())[: max(1, min(int(limit), 500))]

    def upsert_digest_author_profiles(
        self,
        profiles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not profiles:
            raise ValueError("at least one Zhihu author profile is required")
        prepared: list[tuple[str, str, str, str]] = []
        names: set[str] = set()
        handles: set[str] = set()
        for profile in profiles:
            display_name = re.sub(r"\s+", " ", str(profile.get("display_name") or "")).strip()
            if not display_name or len(display_name) > 100:
                raise ValueError("invalid attributed author name")
            normalized = _normalise_attributed_name(display_name)
            profile_url = str(profile.get("profile_url") or "").strip()
            handle = _zhihu_profile_handle(profile_url)
            if normalized in names or handle.casefold() in handles:
                raise ValueError("duplicate attributed author profile in request")
            names.add(normalized)
            handles.add(handle.casefold())
            prepared.append((normalized, display_name, handle, profile_url.rstrip("/")))
        timestamp = now_iso()
        with self.connect() as db:
            for normalized, display_name, handle, profile_url in prepared:
                db.execute(
                    """
                    INSERT INTO digest_author_profiles(
                        normalized_name,display_name,handle,profile_url,
                        tracking_status,source,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(normalized_name) DO UPDATE SET
                        display_name=excluded.display_name,
                        handle=excluded.handle,
                        profile_url=excluded.profile_url,
                        updated_at=excluded.updated_at
                    """,
                    (
                        normalized,
                        display_name,
                        handle,
                        profile_url,
                        "linked_only",
                        "manual",
                        timestamp,
                        timestamp,
                    ),
                )
        by_name = {item["normalized_name"]: item for item in self.list_digest_authors(500)}
        return [by_name[normalized] for normalized, _, _, _ in prepared if normalized in by_name]

    def list_digest_author_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM digest_author_profiles ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_digest_author_profile_status(self, handle: str, status: str) -> None:
        if status not in {"linked_only", "paused", "active"}:
            raise ValueError("invalid digest author profile status")
        with self.connect() as db:
            db.execute(
                """
                UPDATE digest_author_profiles
                SET tracking_status=?,updated_at=?
                WHERE handle=? COLLATE NOCASE
                """,
                (status, now_iso(), handle.strip()),
            )

    def record_fetch_attempts(
        self,
        run_id: str,
        kol_id: int,
        handle: str,
        attempts: list[ProviderAttempt],
    ) -> None:
        if not attempts:
            return
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO fetch_attempts(
                    run_id,kol_id,handle,provider,status,post_count,duration_ms,
                    error_code,error,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id, kol_id, handle, attempt.provider, attempt.status,
                        attempt.post_count, attempt.duration_ms, attempt.error_code,
                        attempt.error[:1000], now_iso(),
                    )
                    for attempt in attempts
                ],
            )

    def recent_fetch_attempts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM fetch_attempts ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_provider_comparisons(
        self,
        run_id: str,
        kol_id: int,
        comparisons: list[dict[str, Any]],
    ) -> None:
        if not comparisons:
            return
        with self.connect() as db:
            for comparison in comparisons:
                db.execute(
                    """
                    INSERT INTO provider_comparisons(
                        run_id,kol_id,handle,primary_count,fallback_count,
                        matching_count,coverage,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,kol_id) DO UPDATE SET
                        primary_count=excluded.primary_count,
                        fallback_count=excluded.fallback_count,
                        matching_count=excluded.matching_count,
                        coverage=excluded.coverage,
                        created_at=excluded.created_at
                    """,
                    (
                        run_id,
                        kol_id,
                        str(comparison.get("handle") or ""),
                        int(comparison.get("primary_count") or 0),
                        int(comparison.get("fallback_count") or 0),
                        int(comparison.get("matching_count") or 0),
                        float(comparison.get("coverage") or 0),
                        now_iso(),
                    ),
                )

    def shadow_rollout_status(self, required_runs: int = 3, threshold: float = 0.95) -> dict[str, Any]:
        active_ids = [int(item["id"]) for item in self.list_kols("active")]
        active_count = len(active_ids)
        with self.connect() as db:
            candidates = db.execute(
                "SELECT run_id,started_at FROM fetch_runs WHERE status='success' "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (max(required_runs * 10, 30),),
            ).fetchall()
            runs = []
            seen_dates: set[str] = set()
            for run in candidates:
                run_date = str(run["started_at"])[:10]
                if run_date in seen_dates:
                    continue
                seen_dates.add(run_date)
                runs.append(run)
                if len(runs) == required_runs:
                    break
            details = []
            for run in runs:
                placeholders = ",".join("?" for _ in active_ids)
                row = db.execute(
                    "SELECT COUNT(*) count,MIN(coverage) min_coverage,AVG(coverage) avg_coverage "
                    f"FROM provider_comparisons WHERE run_id=? "
                    + (f"AND kol_id IN ({placeholders})" if active_ids else "AND 1=0"),
                    (run["run_id"], *active_ids),
                ).fetchone()
                details.append(
                    {
                        "run_id": run["run_id"],
                        "started_at": run["started_at"],
                        "comparison_count": int(row["count"] or 0),
                        "min_coverage": float(row["min_coverage"] or 0),
                        "avg_coverage": float(row["avg_coverage"] or 0),
                    }
                )
        ready = active_count > 0 and len(details) == required_runs and all(
            item["comparison_count"] >= active_count and item["min_coverage"] >= threshold
            for item in details
        )
        return {
            "ready": ready,
            "required_runs": required_runs,
            "required_distinct_dates": required_runs,
            "threshold": threshold,
            "active_kols": active_count,
            "runs": details,
        }

    def save_local_media(self, post_id: str, local_media: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            row = db.execute("SELECT local_media_json FROM posts WHERE post_id=?", (post_id,)).fetchone()
            if row is None:
                raise KeyError(f"post not found: {post_id}")
            existing = _loads(row["local_media_json"], [])
            merged: dict[str, dict[str, Any]] = {}
            for item in [*existing, *local_media]:
                key = str(item.get("source_url") or item.get("sha256") or item.get("path") or "")
                if key:
                    merged[key] = item
            db.execute(
                "UPDATE posts SET local_media_json=?,updated_at=? WHERE post_id=?",
                (_json(list(merged.values())), now_iso(), post_id),
            )

    def save_rule_classification(self, post_id: str, result: RuleResult) -> None:
        with self.connect() as db:
            media_row = db.execute(
                "SELECT local_media_json FROM posts WHERE post_id=?",
                (post_id,),
            ).fetchone()
            has_local_media = bool(media_row and _loads(media_row["local_media_json"], []))
            ocr_status = "not_requested" if has_local_media and result.is_candidate else "not_needed"
            db.execute(
                """
                INSERT INTO classifications(
                    post_id,rule_score,rule_reasons_json,rule_symbols_json,rule_direction,
                    content_type,evidence_type,is_candidate,ocr_status,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET
                    rule_score=excluded.rule_score,rule_reasons_json=excluded.rule_reasons_json,
                    rule_symbols_json=excluded.rule_symbols_json,rule_direction=excluded.rule_direction,
                    content_type=CASE WHEN classifications.model_status='completed'
                        THEN classifications.content_type ELSE excluded.content_type END,
                    evidence_type=CASE WHEN classifications.model_status='completed'
                        THEN classifications.evidence_type ELSE excluded.evidence_type END,
                    is_candidate=excluded.is_candidate,
                    ocr_status=CASE
                        WHEN classifications.ocr_status='completed' THEN classifications.ocr_status
                        ELSE excluded.ocr_status END,
                    updated_at=excluded.updated_at
                """,
                (
                    post_id,
                    result.score,
                    _json(result.reasons),
                    _json(result.symbols),
                    result.direction,
                    result.content_type,
                    result.evidence_type,
                    int(result.is_candidate),
                    ocr_status,
                    now_iso(),
                ),
            )

    def upsert_stock_lead(self, value: dict[str, Any]) -> bool:
        timestamp = now_iso()
        symbol = str(value.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("invalid stock lead symbol")
        status = str(value.get("status") or "pending")
        if status not in {"pending", "confirmed", "ignored"}:
            raise ValueError("invalid stock lead status")
        with self.connect() as db:
            existing = db.execute(
                "SELECT id,status FROM stock_leads WHERE post_id=? AND symbol=?",
                (value["post_id"], symbol),
            ).fetchone()
            db.execute(
                """
                INSERT INTO stock_leads(
                    post_id,kol_id,symbol,security_name,instrument_type,direction,mention_kind,
                    evidence_text,extraction_method,confidence,status,auto_confirmed,
                    first_seen_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_id,symbol) DO UPDATE SET
                    security_name=CASE WHEN excluded.security_name<>'' THEN excluded.security_name ELSE stock_leads.security_name END,
                    instrument_type=excluded.instrument_type,
                    direction=CASE WHEN excluded.direction<>'' THEN excluded.direction ELSE stock_leads.direction END,
                    mention_kind=excluded.mention_kind,
                    evidence_text=CASE WHEN excluded.evidence_text<>'' THEN excluded.evidence_text ELSE stock_leads.evidence_text END,
                    extraction_method=CASE
                        WHEN stock_leads.extraction_method='exact_code' THEN stock_leads.extraction_method
                        WHEN excluded.extraction_method='exact_code' THEN excluded.extraction_method
                        WHEN stock_leads.extraction_method='name_match' THEN stock_leads.extraction_method
                        ELSE excluded.extraction_method END,
                    confidence=MAX(stock_leads.confidence,excluded.confidence),
                    status=CASE
                        WHEN stock_leads.status IN ('confirmed','ignored') THEN stock_leads.status
                        ELSE excluded.status END,
                    auto_confirmed=MAX(stock_leads.auto_confirmed,excluded.auto_confirmed),
                    updated_at=excluded.updated_at
                """,
                (
                    value["post_id"],
                    int(value["kol_id"]),
                    symbol,
                    str(value.get("security_name") or "").strip(),
                    str(value.get("instrument_type") or "stock"),
                    str(value.get("direction") or ""),
                    str(value.get("mention_kind") or "analysis"),
                    str(value.get("evidence_text") or "")[:2000],
                    str(value.get("extraction_method") or "rule"),
                    float(value.get("confidence") or 0),
                    status,
                    int(bool(value.get("auto_confirmed"))),
                    timestamp,
                    timestamp,
                ),
            )
        return existing is None

    def get_stock_lead(self, lead_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT l.*,p.url,p.text,p.posted_at,p.review_status,k.display_name,k.handle
                FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                WHERE l.id=?
                """,
                (lead_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"stock lead not found: {lead_id}")
        value = dict(row)
        value["auto_confirmed"] = bool(value["auto_confirmed"])
        return value

    def list_stock_leads(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
        kol_id: int | None = None,
        post_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("l.status=?")
            params.append(status)
        if symbol:
            clauses.append("l.symbol=?")
            params.append(symbol)
        if kol_id:
            clauses.append("l.kol_id=?")
            params.append(kol_id)
        if post_id:
            clauses.append("l.post_id=?")
            params.append(post_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT l.*,p.url,p.text,p.posted_at,p.review_status,k.display_name,k.handle
                FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                {where}
                ORDER BY p.posted_at DESC,l.id DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["auto_confirmed"] = bool(value["auto_confirmed"])
            values.append(value)
        return values

    def query_stock_mentions(
        self,
        *,
        query: str = "",
        kind: str = "",
        kol_id: int | None = None,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.strip():
            term = f"%{query.strip().lower()}%"
            clauses.append(
                "(LOWER(l.symbol) LIKE ? OR LOWER(l.security_name) LIKE ? "
                "OR LOWER(k.display_name) LIKE ? OR LOWER(k.handle) LIKE ? "
                "OR LOWER(p.text) LIKE ? OR LOWER(l.evidence_text) LIKE ?)"
            )
            params.extend([term] * 6)
        if kind:
            clauses.append("l.mention_kind=?")
            params.append(kind)
        if kol_id is not None:
            clauses.append("l.kol_id=?")
            params.append(kol_id)
        if symbol:
            clauses.append("l.symbol=?")
            params.append(symbol)
        if date_from:
            clauses.append("substr(p.posted_at,1,10)>=?")
            params.append(date_from)
        if date_to:
            clauses.append("substr(p.posted_at,1,10)<=?")
            params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_page_size
        with self.connect() as db:
            total = int(db.execute(
                f"""
                SELECT COUNT(*) FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                {where}
                """,
                params,
            ).fetchone()[0])
            rows = db.execute(
                f"""
                SELECT l.*,p.url,p.text,p.article_title,p.article_text,p.posted_at,
                    p.review_status,c.ocr_text,k.display_name,k.handle
                FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                LEFT JOIN classifications c ON c.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                {where}
                ORDER BY p.posted_at DESC,l.id DESC LIMIT ? OFFSET ?
                """,
                [*params, safe_page_size, offset],
            ).fetchall()
        items = []
        for row in rows:
            value = dict(row)
            value["auto_confirmed"] = bool(value["auto_confirmed"])
            items.append(value)
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "total_pages": (total + safe_page_size - 1) // safe_page_size,
        }

    def review_stock_lead(
        self,
        lead_id: int,
        action: str,
        note: str = "",
        *,
        symbol: str | None = None,
        security_name: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"confirmed", "ignored", "pending"}:
            raise ValueError("invalid stock lead review action")
        updates = ["status=?", "review_note=?", "reviewed_at=?", "updated_at=?"]
        timestamp = now_iso()
        params: list[Any] = [action, note.strip(), timestamp if action != "pending" else "", timestamp]
        if symbol is not None:
            clean_symbol = symbol.strip()
            if not re.fullmatch(r"\d{6}", clean_symbol):
                raise ValueError("invalid stock lead symbol")
            updates.append("symbol=?")
            params.append(clean_symbol)
        if security_name is not None:
            updates.append("security_name=?")
            params.append(security_name.strip())
        params.append(lead_id)
        try:
            with self.connect() as db:
                cursor = db.execute(
                    f"UPDATE stock_leads SET {','.join(updates)} WHERE id=?",
                    params,
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"stock lead not found: {lead_id}")
                db.execute(
                    "INSERT INTO stock_lead_reviews(lead_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                    (lead_id, action, _json({"note": note, "symbol": symbol, "security_name": security_name}), timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("a stock lead for this post and symbol already exists") from exc
        return self.get_stock_lead(lead_id)

    def auto_confirm_stock_lead(self, lead_id: int, note: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE stock_leads SET status='confirmed',auto_confirmed=1,
                    review_note=?,reviewed_at=?,updated_at=?
                WHERE id=? AND status='pending' AND extraction_method='exact_code'
                """,
                (note, timestamp, timestamp, lead_id),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "INSERT INTO stock_lead_reviews(lead_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                    (lead_id, "auto_confirmed", _json({"note": note}), timestamp),
                )
        return self.get_stock_lead(lead_id)

    def lead_extraction_signature(self, post_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT source_signature FROM lead_extractions WHERE post_id=? AND status='completed'",
                (post_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def mark_lead_extraction(self, post_id: str, signature: str, *, error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO lead_extractions(post_id,source_signature,status,error,extracted_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET source_signature=excluded.source_signature,
                    status=excluded.status,error=excluded.error,extracted_at=excluded.extracted_at
                """,
                (post_id, signature, "failed" if error else "completed", error[:2000], now_iso()),
            )

    def save_model_classification(
        self,
        post_id: str,
        payload: dict[str, Any] | None,
        *,
        model_name: str,
        prompt_version: str,
        error: str = "",
    ) -> None:
        status = "failed" if error else "completed"
        value = payload or {}
        retry_at = (
            (datetime.now(SHANGHAI) + timedelta(hours=6)).isoformat(timespec="seconds")
            if error
            else ""
        )
        with self.connect() as db:
            db.execute(
                """
                UPDATE classifications SET model_status=?,model_name=?,prompt_version=?,
                    model_attempts=model_attempts+1,model_next_retry_at=?,
                    content_type=COALESCE(NULLIF(?,''),content_type),
                    evidence_type=COALESCE(NULLIF(?,''),evidence_type),confidence=?,
                    model_summary=?,drafts_json=?,model_error=?,
                    draft_generation_status=CASE
                        WHEN EXISTS(
                            SELECT 1 FROM recommendation_drafts d
                            WHERE d.post_id=classifications.post_id
                              AND d.status IN ('ready','needs_attention','approved','rejected')
                        ) THEN 'generated'
                        WHEN ?<>'' THEN 'failed'
                        ELSE 'pending'
                    END,
                    draft_generation_error=?,draft_generation_version=?,updated_at=? WHERE post_id=?
                """,
                (
                    status,
                    model_name,
                    prompt_version,
                    retry_at,
                    str(value.get("content_type") or ""),
                    str(value.get("evidence_type") or ""),
                    float(value.get("confidence") or 0),
                    str(value.get("summary") or "")[:2000],
                    _json(value.get("drafts") or []),
                    error[:2000],
                    error,
                    error[:2000],
                    prompt_version,
                    now_iso(),
                    post_id,
                ),
            )

    def set_draft_generation_status(
        self,
        post_id: str,
        status: str,
        *,
        version: str = "",
        error: str = "",
    ) -> None:
        if status not in {"pending", "generated", "not_applicable", "needs_attention", "failed"}:
            raise ValueError("invalid draft generation status")
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE classifications
                SET draft_generation_status=?,draft_generation_version=?,draft_generation_error=?,
                    draft_generated_at=CASE WHEN ? IN ('generated','not_applicable') THEN ? ELSE draft_generated_at END,
                    updated_at=?
                WHERE post_id=?
                """,
                (status, version[:120], error[:2000], status, timestamp, timestamp, post_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"classification not found: {post_id}")

    def get_post(self, post_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                    c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                    c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                    c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                    c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                    c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                    c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
                FROM posts p JOIN kols k ON k.id=p.kol_id
                LEFT JOIN classifications c ON c.post_id=p.post_id
                WHERE p.post_id=?
                """,
                (post_id,),
            ).fetchone()
        value = self._row(row)
        if value is None:
            raise KeyError(f"post not found: {post_id}")
        value["is_candidate"] = bool(value.get("is_candidate"))
        return value

    def list_posts(
        self,
        *,
        review_status: str | None = None,
        kol_id: int | None = None,
        candidate_only: bool = False,
        posted_date: str | None = None,
        posted_from_utc: str | None = None,
        posted_to_utc: str | None = None,
        model_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if review_status:
            clauses.append("p.review_status=?")
            params.append(review_status)
        if kol_id:
            clauses.append("p.kol_id=?")
            params.append(kol_id)
        if candidate_only:
            clauses.append("c.is_candidate=1")
        if posted_date:
            clauses.append("substr(p.posted_at,1,10)=?")
            params.append(posted_date)
        if posted_from_utc:
            clauses.append("p.posted_at_utc>=?")
            params.append(posted_from_utc)
        if posted_to_utc:
            clauses.append("p.posted_at_utc<?")
            params.append(posted_to_utc)
        if model_status:
            clauses.append("c.model_status=?")
            params.append(model_status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"""
            SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
            FROM posts p JOIN kols k ON k.id=p.kol_id
            LEFT JOIN classifications c ON c.post_id=p.post_id
            {where} ORDER BY p.posted_at DESC LIMIT ? OFFSET ?
        """
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self.connect() as db:
            values = [self._row(row) for row in db.execute(query, params).fetchall()]
        return [{**value, "is_candidate": bool(value.get("is_candidate"))} for value in values if value]

    def reconcile_zhihu_historical_backfill(self) -> int:
        """Archive pre-onboarding Zhihu answers left pending by interrupted backfills."""
        timestamp = now_iso()
        note = "Zhihu historical backfill is archive-only."
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT p.post_id
                FROM posts p
                JOIN kols k ON k.id=p.kol_id
                JOIN classifications c ON c.post_id=p.post_id
                WHERE p.platform='Zhihu'
                    AND p.post_type='answer'
                    AND k.tracking_mode='direct_profile'
                    AND datetime(p.posted_at_utc) < datetime(k.created_at)
                    AND p.review_status='pending'
                    AND c.model_status='not_requested'
                    AND NOT EXISTS (
                        SELECT 1 FROM recommendation_drafts d
                        WHERE d.post_id=p.post_id
                            AND d.status IN ('ready','needs_attention','approved')
                    )
                """
            ).fetchall()
            post_ids = [str(row["post_id"]) for row in rows]
            if not post_ids:
                return 0
            placeholders = ",".join("?" for _ in post_ids)
            db.execute(
                f"UPDATE posts SET review_status='ignored',review_note=?,updated_at=? "
                f"WHERE post_id IN ({placeholders})",
                (note, timestamp, *post_ids),
            )
            detail = _json({"policy": "zhihu_direct_profile_backfill_archive_only"})
            db.executemany(
                "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,'ignored',?,?)",
                [(post_id, detail, timestamp) for post_id in post_ids],
            )
        return len(post_ids)

    def list_retrospective_followups(self, source_post_id: str) -> list[dict[str, Any]]:
        if not re.fullmatch(r"\d{5,25}", source_post_id):
            return []
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.post_id,p.url,p.text,p.posted_at,p.review_status,
                    k.display_name,c.evidence_type
                FROM posts p
                JOIN kols k ON k.id=p.kol_id
                JOIN classifications c ON c.post_id=p.post_id
                WHERE p.quoted_id=? AND c.evidence_type='retrospective'
                    AND p.review_status IN ('excluded','ignored')
                ORDER BY p.posted_at,p.post_id
                """,
                (source_post_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_posts_for_model(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
            SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
            FROM posts p JOIN kols k ON k.id=p.kol_id
            JOIN classifications c ON c.post_id=p.post_id
            WHERE p.review_status='pending' AND c.is_candidate=1
                AND c.model_status='not_requested'
                AND c.ocr_status IN ('not_needed','completed','failed')
                AND (c.rule_symbols_json='[]' OR c.rule_direction='' OR c.evidence_type='ambiguous')
            ORDER BY p.posted_at DESC LIMIT ?
        """
        with self.connect() as db:
            values = [self._row(row) for row in db.execute(query, (max(1, min(limit, 500)),)).fetchall()]
        return [{**value, "is_candidate": True} for value in values if value]

    def claim_posts_for_ocr(self, limit: int = 8) -> list[dict[str, Any]]:
        timestamp = now_iso()
        stale_before = (datetime.now(SHANGHAI) - timedelta(minutes=30)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE classifications SET ocr_status='not_requested',ocr_updated_at=? "
                "WHERE ocr_status='running' AND ocr_updated_at<?",
                (timestamp, stale_before),
            )
            db.execute(
                "UPDATE classifications SET ocr_status='not_needed',ocr_updated_at=? "
                "WHERE ocr_status='not_requested' AND is_candidate=0",
                (timestamp,),
            )
            rows = db.execute(
                """
                SELECT p.post_id FROM posts p JOIN classifications c ON c.post_id=p.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                    AND p.local_media_json NOT IN ('','[]')
                    AND (
                        c.ocr_status='not_requested' OR (
                            c.ocr_status='failed' AND c.ocr_attempts<3
                            AND (c.ocr_next_retry_at='' OR c.ocr_next_retry_at<=?)
                        )
                    )
                ORDER BY p.posted_at DESC LIMIT ?
                """,
                (timestamp, max(1, min(limit, 50))),
            ).fetchall()
            ids = [str(row["post_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE classifications SET ocr_status='running',ocr_updated_at=? "
                    f"WHERE post_id IN ({placeholders})",
                    (timestamp, *ids),
                )
        return [self.get_post(post_id) for post_id in ids]

    def recover_interrupted_classification(self) -> dict[str, int]:
        timestamp = now_iso()
        with self.connect() as db:
            ocr = db.execute(
                "UPDATE classifications SET ocr_status='not_requested',ocr_updated_at=?,updated_at=? "
                "WHERE ocr_status='running'",
                (timestamp, timestamp),
            ).rowcount
            model = db.execute(
                "UPDATE classifications SET model_status='not_requested',model_error='',updated_at=? "
                "WHERE model_status='running'",
                (timestamp,),
            ).rowcount
        return {"ocr": max(0, ocr), "model": max(0, model)}

    def save_ocr_result(
        self,
        post_id: str,
        text: str = "",
        *,
        error: str = "",
        provider: str = "",
        confidence: float = 0,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        status = "failed" if error else "completed"
        retry_at = (
            (datetime.now(SHANGHAI) + timedelta(hours=1)).isoformat(timespec="seconds")
            if error
            else ""
        )
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE classifications SET ocr_status=?,ocr_attempts=ocr_attempts+1,
                    ocr_next_retry_at=?,ocr_text=?,ocr_provider=?,ocr_confidence=?,
                    ocr_details_json=?,ocr_error=?,ocr_updated_at=?,updated_at=?
                WHERE post_id=?
                """,
                (
                    status, retry_at, text[:20000], provider[:80], max(0, min(float(confidence), 1)),
                    _json(details or []), error[:2000], timestamp, timestamp, post_id,
                ),
            )

    def prepare_model_queue(self) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE classifications SET model_status='not_needed',model_name='rules+ocr',
                    model_error='',updated_at=?
                WHERE post_id IN (SELECT post_id FROM posts WHERE review_status='pending')
                    AND is_candidate=1 AND model_status='not_requested'
                    AND rule_symbols_json<>'[]' AND rule_direction<>''
                    AND ocr_status IN ('not_needed','completed','failed')
                """,
                (now_iso(),),
            )
        return int(cursor.rowcount)

    def claim_posts_for_model(
        self,
        limit: int = 1,
        *,
        post_id: str = "",
        force: bool = False,
    ) -> list[dict[str, Any]]:
        timestamp = now_iso()
        stale_before = (datetime.now(SHANGHAI) - timedelta(minutes=10)).isoformat(timespec="seconds")
        row_limit = max(1, min(limit, 100))
        select = """
            SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
            FROM posts p JOIN kols k ON k.id=p.kol_id
            JOIN classifications c ON c.post_id=p.post_id
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE classifications SET model_status='not_requested',updated_at=?
                WHERE model_status='running' AND updated_at<?
                """,
                (timestamp, stale_before),
            )
            if post_id:
                status_clause = "c.model_status<>'running'" if force else "c.model_status='not_requested'"
                rows = db.execute(
                    select
                    + f" WHERE p.post_id=? AND p.review_status='pending' AND {status_clause} LIMIT 1",
                    (post_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    select
                    + """
                    WHERE p.review_status='pending' AND c.is_candidate=1 AND (
                        c.model_status='not_requested' OR (
                            c.model_status='failed' AND c.model_attempts<3
                            AND (c.model_next_retry_at='' OR c.model_next_retry_at<=?)
                        )
                    )
                    AND c.ocr_status IN ('not_needed','completed','failed')
                    AND (c.rule_symbols_json='[]' OR c.rule_direction='' OR c.evidence_type='ambiguous')
                    ORDER BY p.posted_at DESC LIMIT ?
                    """,
                    (timestamp, row_limit),
                ).fetchall()
            ids = [str(row["post_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE classifications SET model_status='running',updated_at=? "
                    f"WHERE post_id IN ({placeholders})",
                    (timestamp, *ids),
                )
        values = [self._row(row) for row in rows]
        return [
            {**value, "is_candidate": bool(value.get("is_candidate")), "model_status": "running"}
            for value in values
            if value
        ]

    def set_review(self, post_id: str, action: str, note: str = "", detail: dict[str, Any] | None = None) -> None:
        if action not in {"approved", "excluded", "ignored", "capture_failed", "pending"}:
            raise ValueError("invalid review action")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE posts SET review_status=?,review_note=?,updated_at=? WHERE post_id=?",
                (action, note.strip(), now_iso(), post_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"post not found: {post_id}")
            db.execute(
                "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                (post_id, action, _json(detail or {"note": note}), now_iso()),
            )

    def record_review_action(self, post_id: str, action: str, detail: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            if db.execute("SELECT 1 FROM posts WHERE post_id=?", (post_id,)).fetchone() is None:
                raise KeyError(f"post not found: {post_id}")
            db.execute(
                "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                (post_id, action, _json(detail or {}), now_iso()),
            )

    def latest_review(self, post_id: str, action: str | None = None) -> dict[str, Any] | None:
        where = "post_id=?"
        params: tuple[Any, ...] = (post_id,)
        if action:
            where += " AND action=?"
            params = (post_id, action)
        with self.connect() as db:
            row = db.execute(
                f"SELECT * FROM reviews WHERE {where} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["detail"] = _loads(value.pop("detail_json"), {})
        return value

    def active_approval_attempt(self, post_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            started = db.execute(
                "SELECT * FROM reviews WHERE post_id=? AND action='approval_started' ORDER BY id DESC LIMIT 1",
                (post_id,),
            ).fetchone()
            terminal = db.execute(
                "SELECT MAX(id) FROM reviews WHERE post_id=? "
                "AND action IN ('approved','capture_failed','excluded','ignored')",
                (post_id,),
            ).fetchone()[0]
        if started is None or (terminal is not None and int(terminal) > int(started["id"])):
            return None
        value = dict(started)
        value["detail"] = _loads(value.pop("detail_json"), {})
        return value

    def set_capture_links(self, post_id: str, source_note: str, notion_url: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE posts SET source_note=?,notion_url=?,updated_at=? WHERE post_id=?",
                (source_note, notion_url, now_iso(), post_id),
            )

    def set_local_source_ref(self, post_id: str, source_ref: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE posts SET source_note=?,notion_url='',updated_at=? WHERE post_id=?",
                (source_ref, now_iso(), post_id),
            )

    def count_posts(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM posts").fetchone()[0])

    def has_post(self, post_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM posts WHERE post_id=?", (post_id,)).fetchone() is not None

    def count_pending(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM posts WHERE review_status='pending'").fetchone()[0])

    def summary(self) -> dict[str, Any]:
        with self.connect() as db:
            counts = {
                row["review_status"]: int(row["count"])
                for row in db.execute("SELECT review_status,COUNT(*) count FROM posts GROUP BY review_status")
            }
            candidates = int(
                db.execute(
                    "SELECT COUNT(*) FROM classifications c JOIN posts p ON p.post_id=c.post_id "
                    "WHERE c.is_candidate=1 AND p.review_status='pending'"
                ).fetchone()[0]
            )
            classification_pending = int(
                db.execute(
                    "SELECT COUNT(*) FROM classifications c JOIN posts p ON p.post_id=c.post_id "
                    "WHERE c.is_candidate=1 AND p.review_status='pending' "
                    "AND c.model_status IN ('not_requested','running','failed')"
                ).fetchone()[0]
            )
            last_run = self._row(db.execute("SELECT * FROM fetch_runs ORDER BY started_at DESC LIMIT 1").fetchone())
            stock_leads = int(db.execute("SELECT COUNT(*) FROM stock_leads").fetchone()[0])
            pending_stock_leads = int(
                db.execute("SELECT COUNT(*) FROM stock_leads WHERE status='pending'").fetchone()[0]
            )
            confirmed_stock_leads = int(
                db.execute("SELECT COUNT(*) FROM stock_leads WHERE status='confirmed'").fetchone()[0]
            )
        return {
            "total_posts": self.count_posts(),
            "pending_posts": counts.get("pending", 0),
            "actionable_posts": candidates,
            "screened_pending_posts": max(0, counts.get("pending", 0) - candidates),
            "classification_pending": classification_pending,
            "candidate_posts": candidates,
            "approved_posts": counts.get("approved", 0),
            "active_kols": len(self.list_kols("active")),
            "stock_leads": stock_leads,
            "pending_stock_leads": pending_stock_leads,
            "confirmed_stock_leads": confirmed_stock_leads,
            "last_fetch_run": last_run,
        }

    def prepare_fetch_queue(
        self,
        batch_key: str,
        kols: list[dict[str, Any]],
        *,
        requested_count: int,
    ) -> None:
        if not batch_key.strip() or not kols:
            return
        timestamp = now_iso()
        ordered = list(kols)
        offset = int(hashlib.sha256(batch_key.encode("utf-8")).hexdigest()[:8], 16) % len(ordered)
        ordered = ordered[offset:] + ordered[:offset]
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue SET state='queued',started_at='',updated_at=?
                WHERE batch_key=? AND state='running' AND updated_at<?
                """,
                (
                    timestamp,
                    batch_key,
                    (datetime.now(SHANGHAI) - timedelta(minutes=60)).isoformat(timespec="seconds"),
                ),
            )
            for position, kol in enumerate(ordered):
                db.execute(
                    """
                    INSERT OR IGNORE INTO fetch_queue(
                        batch_key,kol_id,platform,handle,position,requested_count,
                        state,queued_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'queued',?,?)
                    """,
                    (
                        batch_key,
                        int(kol["id"]),
                        str(kol.get("platform") or "X"),
                        str(kol["handle"]),
                        position,
                        max(1, int(requested_count)),
                        timestamp,
                        timestamp,
                    ),
                )

    def pending_fetch_queue(
        self,
        batch_key: str,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(SHANGHAI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        timestamp = current.astimezone(SHANGHAI).isoformat(timespec="seconds")
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT k.*
                FROM fetch_queue q JOIN kols k ON k.id=q.kol_id
                WHERE q.batch_key=? AND k.status='active'
                  AND (
                    q.state='queued'
                    OR (q.state='cooldown' AND (q.not_before='' OR q.not_before<=?))
                  )
                ORDER BY q.position,q.kol_id
                """,
                (batch_key, timestamp),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_fetch_queue_running(self, batch_key: str, kol_id: int) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue
                SET state='running',attempts=attempts+1,started_at=?,updated_at=?
                WHERE batch_key=? AND kol_id=? AND state IN ('queued','cooldown')
                """,
                (timestamp, timestamp, batch_key, kol_id),
            )

    def complete_fetch_queue_item(self, batch_key: str, kol_id: int) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue
                SET state='completed',not_before='',last_error_code='',last_error='',
                    completed_at=?,updated_at=?
                WHERE batch_key=? AND kol_id=?
                """,
                (timestamp, timestamp, batch_key, kol_id),
            )

    def defer_fetch_queue_item(
        self,
        batch_key: str,
        kol_id: int,
        *,
        error_code: str,
        error: str,
        cooldown_seconds: int,
    ) -> None:
        current = datetime.now(SHANGHAI)
        not_before = (
            current + timedelta(seconds=max(0, int(cooldown_seconds)))
        ).isoformat(timespec="seconds")
        state = "cooldown" if error_code in {"rate_limited", "authentication_failed"} else "queued"
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue
                SET state=?,not_before=?,last_error_code=?,last_error=?,updated_at=?
                WHERE batch_key=? AND kol_id=?
                """,
                (
                    state,
                    not_before,
                    error_code,
                    error[:1000],
                    current.isoformat(timespec="seconds"),
                    batch_key,
                    kol_id,
                ),
            )

    def fetch_queue_status(self, batch_key: str) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT state,COUNT(*) count FROM fetch_queue
                WHERE batch_key=? GROUP BY state
                """,
                (batch_key,),
            ).fetchall()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "batch_key": batch_key,
            "total": sum(counts.values()),
            "completed": counts.get("completed", 0),
            "pending": sum(
                counts.get(state, 0) for state in ("queued", "running", "cooldown")
            ),
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "cooldown": counts.get("cooldown", 0),
        }

    def latest_pending_fetch_batch(self, platform: str = "") -> str:
        query = (
            "SELECT batch_key,MAX(updated_at) latest FROM fetch_queue "
            "WHERE state<>'completed'"
        )
        params: list[Any] = []
        if platform:
            query += " AND lower(platform)=?"
            params.append(platform.casefold())
        query += " GROUP BY batch_key ORDER BY latest DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, params).fetchone()
        return str(row["batch_key"]) if row else ""

    def start_fetch_run(self, requested_count: int, *, total_kols: int = 0) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO fetch_runs(
                    run_id,started_at,status,requested_count,total_kols,stage
                ) VALUES(?,?,?, ?,?,'fetching')
                """,
                (run_id, now_iso(), "running", requested_count, max(0, total_kols)),
            )
        return run_id

    def update_fetch_progress(
        self,
        run_id: str,
        *,
        processed_kols: int,
        successful_kols: int,
        failed_kols: int,
        new_posts: int,
        candidate_posts: int,
        stage: str = "fetching",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_runs SET processed_kols=?,successful_kols=?,failed_kols=?,
                    new_posts=?,candidate_posts=?,stage=?
                WHERE run_id=? AND status='running'
                """,
                (
                    max(0, processed_kols),
                    max(0, successful_kols),
                    max(0, failed_kols),
                    max(0, new_posts),
                    max(0, candidate_posts),
                    stage,
                    run_id,
                ),
            )

    def finish_fetch_run(self, summary: FetchSummary) -> None:
        status = (
            "blocked"
            if summary.blocked_platforms and summary.successful_kols == 0
            else "failed"
            if summary.successful_kols == 0 and summary.failed_kols
            else "partial"
            if summary.failed_kols
            else "success"
        )
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_runs SET completed_at=?,status=?,stage='completed',
                    processed_kols=CASE WHEN total_kols>0 THEN total_kols ELSE ? END,
                    successful_kols=?,failed_kols=?,
                    new_posts=?,candidate_posts=?,auth_status=?,gap_kols_json=?,fallback_kols_json=?,
                    shadow_failed_kols_json=?,errors_json=? WHERE run_id=?
                """,
                (
                    now_iso(),
                    status,
                    summary.successful_kols + summary.failed_kols,
                    summary.successful_kols,
                    summary.failed_kols,
                    summary.new_posts,
                    summary.candidate_posts,
                    summary.auth_status,
                    _json(summary.gap_kols),
                    _json(summary.fallback_kols),
                    _json(summary.shadow_failed_kols),
                    _json(summary.errors),
                    summary.run_id,
                ),
            )

    def save_fetch_model_result(self, run_id: str, completed: int, failed: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE fetch_runs SET codex_completed=?,codex_failed=? WHERE run_id=?",
                (max(0, completed), max(0, failed), run_id),
            )

    def codex_failure_streak(self) -> int:
        with self.connect() as db:
            rows = db.execute(
                "SELECT codex_completed,codex_failed FROM fetch_runs "
                "WHERE codex_completed + codex_failed > 0 ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
        streak = 0
        for row in rows:
            if int(row["codex_failed"]) > 0 and int(row["codex_completed"]) == 0:
                streak += 1
            else:
                break
        return streak

    def recent_fetch_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM fetch_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def interrupt_stale_fetch_runs(
        self,
        *,
        now: datetime | None = None,
        max_age_minutes: int = 60,
    ) -> int:
        current = now or datetime.now(SHANGHAI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        cutoff = current.astimezone(SHANGHAI) - timedelta(minutes=max(1, max_age_minutes))
        recovered = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT run_id,started_at FROM fetch_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                try:
                    started = datetime.fromisoformat(str(row["started_at"]))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=SHANGHAI)
                except ValueError:
                    started = datetime.min.replace(tzinfo=SHANGHAI)
                if started.astimezone(SHANGHAI) > cutoff:
                    continue
                recovered += db.execute(
                    """
                    UPDATE fetch_runs SET status='failed',completed_at=?,errors_json=?
                    WHERE run_id=? AND status='running'
                    """,
                    (current.isoformat(timespec="seconds"), _json(["stale_run_recovered"]), row["run_id"]),
                ).rowcount
        return max(0, recovered)


def initialize_seed_kols(store: KolPostStore) -> int:
    created = 0
    for display_name, handle, domain in SEED_KOLS:
        _, was_created = store.add_kol(display_name, handle, domain)
        created += int(was_created)
    return created


def load_stock_aliases(*paths: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for path in paths:
        if not Path(path).exists():
            continue
        try:
            with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = str(row.get("symbol") or "").strip()
                    name = str(row.get("security_name") or row.get("name") or "").strip()
                    if re.fullmatch(r"\d{6}", symbol) and name:
                        aliases[symbol] = name
        except (OSError, UnicodeError, csv.Error):
            continue
    return aliases


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


# Preserve the existing import name while routing every DeepSeek task through Go.
DeepSeekCredentialStore = OpenCodeGoCredentialStore


class TwitterCliProvider:
    name = "twitter-cli"

    def __init__(
        self,
        command: str = "twitter",
        credentials: KeyringCredentialStore | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 60,
        proxy_url: str = "",
    ):
        self.command = command
        self.credentials = credentials or KeyringCredentialStore()
        self.runner = runner
        self.timeout_seconds = max(10, min(timeout_seconds, 180))
        self.proxy_url = proxy_url.strip()

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        started = time.perf_counter()
        env = os.environ.copy()
        env.update(self.credentials.load())
        if self.proxy_url:
            env["TWITTER_PROXY"] = self.proxy_url
        try:
            completed = self.runner(
                [self.command, "user-posts", handle.lstrip("@"), "-n", str(max_count), "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TwitterProviderError(
                f"twitter-cli timed out after {self.timeout_seconds} seconds"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "twitter-cli failed").strip()
            lowered = detail.lower()
            if "not_authenticated" in lowered or "no twitter cookies" in lowered or "unauthorized" in lowered:
                raise TwitterAuthenticationError("Twitter authentication failed")
            if "rate" in lowered and "limit" in lowered or "429" in lowered:
                raise TwitterRateLimitError("Twitter rate limit reached")
            raise TwitterProviderError(detail[-2000:])
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TwitterProviderError(f"twitter-cli returned invalid JSON: {exc}") from exc
        if isinstance(payload, dict):
            payload = payload.get("tweets") or payload.get("data") or []
        if not isinstance(payload, list):
            raise TwitterProviderError("twitter-cli output is not a post list")
        posts = [dict(item) for item in payload if isinstance(item, dict)]
        return ProviderFetchResult(
            provider=self.name,
            posts=posts,
            attempts=[
                ProviderAttempt(
                    provider=self.name,
                    status="success",
                    post_count=len(posts),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
            warnings=[],
        )


class ZhihuProfileProvider:
    name = "zhihu-local"

    def __init__(
        self,
        script_path: Path,
        *,
        python_command: str = "python",
        browser_path: str = "",
        profile_directory: str = "Default",
        user_data_dir: str = "",
        port: int = 9222,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 120,
    ):
        self.script_path = Path(script_path)
        self.python_command = python_command
        self.browser_path = browser_path
        self.profile_directory = profile_directory
        self.user_data_dir = user_data_dir
        self.port = int(port)
        self.runner = runner
        self.timeout_seconds = max(30, min(int(timeout_seconds), 300))
        self.session_prepared = False
        self.preflight_error = ""

    def prepare_session(self, handle: str) -> None:
        """Preflight the shared browser once before a Zhihu batch."""
        if self.session_prepared:
            return
        if not self.script_path.is_file():
            raise ZhihuProviderError(f"Zhihu capture script is missing: {self.script_path}")
        command = [
            self.python_command,
            str(self.script_path),
            "--handle",
            handle,
            "--port",
            str(self.port),
            "--profile-directory",
            self.profile_directory,
            "--prepare-only",
        ]
        if self.browser_path:
            command.extend(["--browser-path", self.browser_path])
        if self.user_data_dir:
            command.extend(["--user-data-dir", self.user_data_dir])
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.preflight_error = (
                f"Zhihu browser preflight timed out after {self.timeout_seconds} seconds"
            )
            raise ZhihuProviderError(self.preflight_error) from exc
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            self.preflight_error = f"Zhihu browser preflight returned invalid JSON: {exc}"
            raise ZhihuProviderError(self.preflight_error) from exc
        if completed.returncode != 0 or not payload.get("ok"):
            self.preflight_error = str(
                payload.get("error") or completed.stderr or "Zhihu browser preflight failed"
            )[-2000:]
            raise ZhihuProviderError(self.preflight_error)
        self.session_prepared = True

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        if not self.script_path.is_file():
            raise ZhihuProviderError(f"Zhihu capture script is missing: {self.script_path}")
        started = time.perf_counter()
        command = [
            self.python_command,
            str(self.script_path),
            "--handle",
            handle,
            "--limit",
            str(max(1, min(int(max_count), 200))),
            "--port",
            str(self.port),
            "--profile-directory",
            self.profile_directory,
            "--no-launch",
        ]
        if self.browser_path:
            command.extend(["--browser-path", self.browser_path])
        if self.user_data_dir:
            command.extend(["--user-data-dir", self.user_data_dir])
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ZhihuProviderError(
                f"Zhihu profile capture timed out after {self.timeout_seconds} seconds"
            ) from exc
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ZhihuProviderError(f"Zhihu profile capture returned invalid JSON: {exc}") from exc
        if completed.returncode != 0 or not payload.get("ok"):
            detail = str(payload.get("error") or completed.stderr or "Zhihu profile capture failed")
            raise ZhihuProviderError(detail[-2000:])
        raw_posts = payload.get("posts") or []
        if not isinstance(raw_posts, list):
            raise ZhihuProviderError("Zhihu profile capture output is not a post list")
        posts = [dict(item) for item in raw_posts if isinstance(item, dict)]
        return ProviderFetchResult(
            provider=self.name,
            posts=posts,
            attempts=[
                ProviderAttempt(
                    provider=self.name,
                    status="success",
                    post_count=len(posts),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
            warnings=[],
        )


def _snowflake_timestamp(post_id: str) -> str:
    if not post_id.isdigit() or len(post_id) < 15:
        raise TwitterProviderError("Nitter post has no usable snowflake ID")
    timestamp_ms = (int(post_id) >> 22) + 1_288_834_974_657
    parsed = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    if parsed.year < 2010 or parsed.year > 2100:
        raise TwitterProviderError("Nitter post snowflake timestamp is out of range")
    return parsed.isoformat(timespec="seconds")


def _parse_nitter_timestamp(value: str, post_id: str) -> tuple[str, str]:
    clean = value.strip()
    match = re.fullmatch(
        r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})\s+"
        r"[^A-Za-z0-9\s:]{1,3}\s+"
        r"(\d{1,2}):(\d{2})\s+(AM|PM)\s+UTC",
        clean,
    )
    if not match:
        return _snowflake_timestamp(post_id), "snowflake"
    parsed = datetime.strptime(
        " ".join(match.groups()),
        "%b %d %Y %I %M %p",
    ).replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds"), "nitter"


def _xtf_tweet_payload(item: dict[str, Any], requested_handle: str) -> dict[str, Any]:
    post_id = str(item.get("tweet_id") or "").strip()
    created_at, timestamp_source = _parse_nitter_timestamp(
        str(item.get("time_ago") or ""), post_id
    )
    author_handle = str(item.get("author") or requested_handle).strip().lstrip("@")
    media = [
        {"type": "photo", "url": str(url)}
        for url in item.get("media") or []
        if str(url).startswith(("https://", "http://"))
    ]
    quoted = item.get("quoted_tweet") if isinstance(item.get("quoted_tweet"), dict) else {}
    quoted_payload: dict[str, Any] = {}
    if quoted:
        quoted_payload = {
            "id": str(quoted.get("tweet_id") or ""),
            "text": str(quoted.get("text") or ""),
            "author": {
                "screenName": str(quoted.get("author") or "").lstrip("@"),
                "name": str(quoted.get("author_name") or ""),
            },
        }
    return {
        "id": post_id,
        "text": str(item.get("text") or ""),
        "url": f"https://x.com/{author_handle}/status/{post_id}",
        "author": {
            "screenName": author_handle,
            "name": str(item.get("author_name") or author_handle),
        },
        "metrics": {
            "likes": int(item.get("likes") or 0),
            "retweets": int(item.get("retweets") or 0),
            "replies": int(item.get("replies") or 0),
            "views": int(item.get("views") or 0),
        },
        "createdAtISO": created_at,
        "timestampSource": timestamp_source,
        "media": media,
        "isRetweet": bool(item.get("retweeted_by")),
        "retweetedBy": str(item.get("retweeted_by") or ""),
        "quotedTweet": quoted_payload or None,
        "lang": "",
    }


class XtfNitterProvider:
    name = "nitter"

    def __init__(
        self,
        command: str,
        nitter_url: str = "http://127.0.0.1:9377",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 30,
    ):
        self.command = command
        self.nitter_url = nitter_url.rstrip("/")
        self.runner = runner
        self.timeout_seconds = max(5, min(int(timeout_seconds), 180))

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        started = time.perf_counter()
        env = os.environ.copy()
        env.update(
            {
                "XTF_NITTER": self.nitter_url,
                "XTF_LANG": "zh",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        completed = self.runner(
            [
                self.command,
                "--user",
                handle.lstrip("@"),
                "--limit",
                str(max_count),
                "--backend",
                "nitter",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            env=env,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TwitterProviderError(f"xtf returned invalid JSON: {exc}") from exc
        if completed.returncode != 0 or payload.get("error"):
            code = str(payload.get("error_code") or "provider_error")
            detail = str(payload.get("error") or completed.stderr or "xtf nitter failed")[-1000:]
            if code == "rate_limited":
                raise TwitterRateLimitError(detail)
            raise TwitterProviderError(f"{code}: {detail}")
        raw_posts = payload.get("tweets") or []
        if not isinstance(raw_posts, list):
            raise TwitterProviderError("xtf nitter output is not a tweet list")
        posts = [_xtf_tweet_payload(dict(item), handle) for item in raw_posts if isinstance(item, dict)]
        warnings = [str(payload["warning"])] if payload.get("warning") else []
        if any(post.get("timestampSource") == "snowflake" for post in posts):
            warnings.append("timestamp_recovered_from_snowflake")
        return ProviderFetchResult(
            provider=self.name,
            posts=posts,
            attempts=[
                ProviderAttempt(
                    provider=self.name,
                    status="success",
                    post_count=len(posts),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
            warnings=warnings,
        )


def _provider_result(value: ProviderFetchResult | list[dict[str, Any]], provider: str) -> ProviderFetchResult:
    if isinstance(value, ProviderFetchResult):
        return value
    return ProviderFetchResult(provider, list(value), [], [])


def _post_provider_warning(payload: dict[str, Any], provider: str) -> str:
    warnings: list[str] = []
    if provider == "nitter" and str(payload.get("timestampSource") or "") == "snowflake":
        warnings.append("timestamp_recovered_from_snowflake")
    return ";".join(warnings)


class FallbackXPostProvider:
    name = "auto"

    def __init__(self, primary: XPostProvider, fallback: XPostProvider, mode: str = "enabled"):
        if mode not in {"enabled", "shadow"}:
            raise ValueError(f"unsupported fallback mode: {mode}")
        self.primary = primary
        self.fallback = fallback
        self.mode = mode
        self.primary_auth_failed = False
        self.shadow_fallback_failed = False

    @staticmethod
    def _failed_attempt(provider: str, started: float, exc: Exception) -> ProviderAttempt:
        code = (
            "authentication_failed" if isinstance(exc, TwitterAuthenticationError)
            else "rate_limited" if isinstance(exc, TwitterRateLimitError)
            else "provider_error"
        )
        return ProviderAttempt(
            provider=provider,
            status="failed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_code=code,
            error=str(exc)[:1000],
        )

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        attempts: list[ProviderAttempt] = []
        warnings: list[str] = []
        primary_error: Exception | None = None
        if not self.primary_auth_failed:
            started = time.perf_counter()
            try:
                primary = _provider_result(
                    self.primary.fetch_user_posts(handle, max_count),
                    self.primary.name,
                )
                attempts.extend(primary.attempts or [
                    ProviderAttempt(primary.provider, "success", len(primary.posts))
                ])
                if primary.posts:
                    if self.mode == "enabled":
                        return ProviderFetchResult(primary.provider, primary.posts, attempts, primary.warnings)
                    return self._shadow_compare(handle, max_count, primary, attempts)
                warnings.append("primary_suspicious_empty")
                attempts.append(ProviderAttempt(primary.provider, "suspicious_empty", 0))
                primary_error = TwitterProviderError("primary returned a suspicious empty timeline")
            except TwitterAuthenticationError as exc:
                self.primary_auth_failed = True
                warnings.append("primary_authentication_failed")
                attempts.append(self._failed_attempt(self.primary.name, started, exc))
                primary_error = exc
            except (TwitterRateLimitError, TwitterProviderError) as exc:
                warnings.append("primary_provider_failed")
                attempts.append(self._failed_attempt(self.primary.name, started, exc))
                primary_error = exc
        else:
            warnings.append("primary_authentication_failed")
            primary_error = TwitterAuthenticationError("primary authentication circuit is open")

        started = time.perf_counter()
        try:
            fallback = _provider_result(
                self.fallback.fetch_user_posts(handle, max_count),
                self.fallback.name,
            )
            attempts.extend(fallback.attempts or [
                ProviderAttempt(fallback.provider, "success", len(fallback.posts))
            ])
            if not fallback.posts:
                raise TwitterProviderError("Nitter returned a suspicious empty timeline")
        except (TwitterAuthenticationError, TwitterRateLimitError, TwitterProviderError) as exc:
            attempts.extend(getattr(exc, "attempts", []))
            if not getattr(exc, "attempts", None):
                attempts.append(self._failed_attempt(self.fallback.name, started, exc))
            exc.attempts = attempts
            raise
        if self.mode == "shadow":
            attempts.append(ProviderAttempt("shadow", "comparison_only", len(fallback.posts)))
            assert primary_error is not None
            primary_error.attempts = attempts
            raise primary_error
        return ProviderFetchResult(
            fallback.provider,
            fallback.posts,
            attempts,
            [*warnings, *fallback.warnings, "fallback_used"],
        )

    @staticmethod
    def _ids(posts: list[dict[str, Any]]) -> set[str]:
        return {
            str(item.get("id") or item.get("tweet_id") or "")
            for item in posts
            if str(item.get("id") or item.get("tweet_id") or "")
        }

    def _shadow_compare(
        self,
        handle: str,
        max_count: int,
        primary: ProviderFetchResult,
        attempts: list[ProviderAttempt],
    ) -> ProviderFetchResult:
        if self.shadow_fallback_failed:
            return ProviderFetchResult(
                primary.provider,
                primary.posts,
                attempts,
                [*primary.warnings, "shadow_fallback_circuit_open"],
            )
        started = time.perf_counter()
        warnings = [*primary.warnings]
        try:
            fallback = _provider_result(
                self.fallback.fetch_user_posts(handle, max_count),
                self.fallback.name,
            )
            attempts.extend(fallback.attempts or [
                ProviderAttempt(fallback.provider, "success", len(fallback.posts))
            ])
            if not fallback.posts:
                raise TwitterProviderError("Nitter returned a suspicious empty timeline")
            primary_ids = self._ids(primary.posts)
            fallback_ids = self._ids(fallback.posts)
            matching = len(primary_ids & fallback_ids)
            coverage = matching / len(primary_ids) if primary_ids else 0.0
            warnings.extend([*fallback.warnings, "shadow_compared"])
            if coverage < 0.95:
                warnings.append("shadow_coverage_below_threshold")
            return ProviderFetchResult(
                primary.provider,
                primary.posts,
                attempts,
                warnings,
                [{
                    "handle": handle,
                    "primary_count": len(primary_ids),
                    "fallback_count": len(fallback_ids),
                    "matching_count": matching,
                    "coverage": coverage,
                }],
            )
        except (TwitterAuthenticationError, TwitterRateLimitError, TwitterProviderError) as exc:
            self.shadow_fallback_failed = True
            attempts.extend(getattr(exc, "attempts", []))
            if not getattr(exc, "attempts", None):
                attempts.append(self._failed_attempt(self.fallback.name, started, exc))
            warnings.append("shadow_fallback_failed")
            return ProviderFetchResult(primary.provider, primary.posts, attempts, warnings)


def build_x_post_provider(
    mode: str,
    *,
    twitter_credentials: KeyringCredentialStore | None = None,
    twitter_command: str = "twitter",
    xtf_command: str,
    nitter_url: str = "http://127.0.0.1:9377",
    fallback_mode: str = "enabled",
    proxy_url: str | None = None,
) -> XPostProvider:
    if mode not in {"auto", "twitter", "nitter"}:
        raise ValueError(f"unsupported X provider: {mode}")
    primary = TwitterCliProvider(
        twitter_command,
        twitter_credentials or KeyringCredentialStore(),
        proxy_url=(proxy_url if proxy_url is not None else os.environ.get("KOL_X_PROXY", "http://127.0.0.1:7897")),
    )
    fallback = XtfNitterProvider(xtf_command, nitter_url)
    if mode == "twitter":
        return primary
    if mode == "nitter":
        return fallback
    return FallbackXPostProvider(primary, fallback, mode=fallback_mode)


def download_images(
    post: PostRecord,
    media_root: Path,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], list[str]]:
    destination = Path(media_root) / post.post_id
    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(post.media):
        if str(item.get("type") or "").lower() not in {"photo", "image"}:
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        request = urllib.request.Request(url, headers={"User-Agent": "ai-hub-kol-research/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    errors.append(f"{url}: non-image response")
                    continue
                data = response.read(20 * 1024 * 1024 + 1)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        if len(data) > 20 * 1024 * 1024:
            errors.append(f"{url}: image exceeds 20 MB")
            continue
        destination.mkdir(parents=True, exist_ok=True)
        extension = mimetypes.guess_extension(content_type) or Path(url).suffix or ".jpg"
        path = destination / f"{index + 1}{extension}"
        path.write_bytes(data)
        saved.append(
            {
                "type": "image",
                "source_url": url,
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    return saved, errors


def _run_command_with_tree_timeout(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: float,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        else:
            process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise RuntimeError(f"{operation} timed out after {timeout_seconds:g} seconds") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _resolve_codex_command(command: str) -> str:
    path = Path(command)
    if path.is_absolute() or path.suffix:
        return str(path)
    if os.name != "nt":
        return shutil.which(command) or command

    candidates = [shutil.which(f"{command}.cmd")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(str(Path(appdata) / "npm" / f"{command}.cmd"))
    candidates.append(shutil.which(f"{command}.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        if Path(candidate).suffix.lower() == ".exe" and "windowsapps" in candidate.lower():
            continue
        return str(Path(candidate))
    return command


def _codex_command_prefix(command: str) -> list[str]:
    path = Path(command)
    if os.name == "nt" and path.suffix.lower() == ".cmd":
        javascript = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        bundled_node = path.parent / "node.exe"
        node = str(bundled_node) if bundled_node.is_file() else shutil.which("node.exe") or shutil.which("node")
        if node and javascript.is_file():
            # Calling the npm .cmd shim can return before its Node child exits. Running
            # Node directly keeps the temporary JSON output alive until Codex finishes.
            return [node, str(javascript)]
    return [command]


class OcrBatchClassifier(Protocol):
    def classify(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]: ...


class RapidOcrBatchClassifier:
    provider_name = "rapidocr"

    def __init__(
        self,
        python: Path,
        runner_script: Path,
        timeout_seconds: float | None = 90,
    ):
        self.python = Path(python)
        self.runner_script = Path(runner_script)
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return self.python.is_file() and self.runner_script.is_file()

    def classify(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.available():
            raise RuntimeError(f"RapidOCR runtime is not available: {self.python}")
        manifest = [
            {
                "post_id": post["post_id"],
                "images": [
                    str(item.get("path") or "")
                    for item in post.get("local_media", [])
                    if item.get("path") and Path(str(item["path"])).is_file()
                ],
            }
            for post in posts
        ]
        with tempfile.TemporaryDirectory(prefix="kol-rapid-ocr-") as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            output_path = Path(temp_dir) / "result.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            completed = _run_command_with_tree_timeout(
                [
                    str(self.python),
                    str(self.runner_script),
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                input_text="",
                timeout_seconds=max(1, self.timeout_seconds or 90),
                operation="RapidOCR batch",
            )
            if completed.returncode != 0 or not output_path.exists():
                detail = (completed.stderr or completed.stdout or "RapidOCR failed").strip()
                raise RuntimeError(detail[-2000:])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("RapidOCR returned an invalid batch result")
        return {
            str(post_id): {
                "text": str(value.get("text") or ""),
                "error": str(value.get("error") or ""),
                "provider": self.provider_name,
                "average_confidence": float(value.get("average_confidence") or 0),
                "lines": list(value.get("lines") or []),
            }
            for post_id, value in payload.items()
            if isinstance(value, dict)
        }


class UnlimitedOcrBatchClassifier:
    def __init__(self, ocr_root: Path, runner_script: Path, timeout_seconds: float | None = None):
        self.ocr_root = Path(ocr_root)
        self.runner_script = Path(runner_script)
        self.python = self.ocr_root / ".venv" / "Scripts" / "python.exe"
        self.timeout_seconds = timeout_seconds

    def available(self) -> bool:
        return self.python.is_file() and self.runner_script.is_file()

    def classify(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.available():
            raise RuntimeError(f"Unlimited-OCR runtime is not available: {self.ocr_root}")
        manifest = [
            {
                "post_id": post["post_id"],
                "images": [
                    str(item.get("path") or "")
                    for item in post.get("local_media", [])
                    if item.get("path") and Path(str(item["path"])).is_file()
                ],
            }
            for post in posts
        ]
        with tempfile.TemporaryDirectory(prefix="kol-ocr-") as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            output_path = Path(temp_dir) / "result.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            batch_timeout = self.timeout_seconds or max(900, len(posts) * 300)
            completed = _run_command_with_tree_timeout(
                [
                    str(self.python),
                    str(self.runner_script),
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output_path),
                ],
                input_text="",
                timeout_seconds=max(1, batch_timeout),
                operation="Unlimited-OCR batch",
            )
            if completed.returncode != 0 or not output_path.exists():
                detail = (completed.stderr or completed.stdout or "Unlimited-OCR failed").strip()
                raise RuntimeError(detail[-2000:])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Unlimited-OCR returned an invalid batch result")
        return {
            str(post_id): {
                "text": str(value.get("text") or ""),
                "error": str(value.get("error") or ""),
                "provider": "unlimited-ocr",
                "average_confidence": 0,
                "lines": [],
            }
            for post_id, value in payload.items()
            if isinstance(value, dict)
        }


def process_pending_with_ocr(
    store: KolPostStore,
    classifier: OcrBatchClassifier,
    rule_classifier: RuleClassifier,
    *,
    limit: int = 8,
) -> tuple[int, int]:
    posts = store.claim_posts_for_ocr(limit)
    if not posts:
        return 0, 0
    try:
        results = classifier.classify(posts)
    except Exception as exc:
        for post in posts:
            store.save_ocr_result(
                post["post_id"],
                error=str(exc),
                provider=str(getattr(classifier, "provider_name", classifier.__class__.__name__)),
            )
        return 0, len(posts)
    completed = failed = 0
    for post in posts:
        result = results.get(post["post_id"], {})
        text = str(result.get("text") or "")
        error = str(result.get("error") or "")
        if error or not text.strip():
            store.save_ocr_result(
                post["post_id"],
                error=error or "OCR returned no text",
                provider=str(result.get("provider") or getattr(classifier, "provider_name", classifier.__class__.__name__)),
            )
            failed += 1
            continue
        store.save_ocr_result(
            post["post_id"],
            text,
            provider=str(result.get("provider") or getattr(classifier, "provider_name", classifier.__class__.__name__)),
            confidence=float(result.get("average_confidence") or 0),
            details=list(result.get("lines") or []),
        )
        store.save_rule_classification(post["post_id"], rule_classifier.classify(post, text))
        completed += 1
    return completed, failed


class CodexPostClassifier:
    prompt_version = "kol-post-v3"
    model_name = "codex"

    def __init__(self, schema_path: Path, workspace: Path, command: str = "codex", timeout_seconds: float = 240):
        self.schema_path = Path(schema_path)
        self.workspace = Path(workspace)
        self.command = _resolve_codex_command(command)
        self.command_prefix = _codex_command_prefix(self.command)
        self.timeout_seconds = max(1, timeout_seconds)

    def classify(self, post: dict[str, Any]) -> dict[str, Any]:
        content = {
            "post_id": post["post_id"],
            "platform": post.get("platform") or "X",
            "kol": post["display_name"],
            "handle": post["handle"],
            "posted_at": post["posted_at"],
            "post_type": post["post_type"],
            "text": post["text"],
            "article_title": post["article_title"],
            "article_text": post["article_text"],
            "quoted_text": post["quoted_text"],
            "ocr_text": post.get("ocr_text") or "",
            "rule_symbols": post.get("rule_symbols", []),
            "rule_direction": post.get("rule_direction", ""),
        }
        prompt = (
            "Classify this source item for an A-share KOL audit system. Do not give investment advice. "
            "The platform may be X or Zhihu. Zhihu answers can be longer essays, numbered lists, "
            "retrospectives, or market analysis; evaluate them by evidence, not by platform. "
            "Distinguish original pre-event recommendations from retrospective claims, secondhand "
            "content, methodology, and market commentary. Return only the requested schema. "
            "One draft must contain exactly one A-share symbol. Evidence spans must be exact, "
            "verbatim substrings from the declared source. Mark image/OCR dependency explicitly, "
            "preserve every entry condition, and distinguish recommendations from holdings, "
            "retrospectives, and analysis. When the source is Chinese, summary, thesis, and "
            "conditions must be concise Chinese; do not translate exact evidence_spans. A thesis "
            "must state the author's actual reason and must not invent a target price, holding "
            "period, or causal claim. Also classify action (buy/add/hold/watch/reduce/sell/avoid), "
            "horizon (intraday/short/swing/medium_long/unspecified), and strength "
            "(explicit/moderate/weak/unspecified); use unspecified when absent.\n\n"
            + _json(content)
        )
        with tempfile.TemporaryDirectory(prefix="kol-classify-") as temp_dir:
            output = Path(temp_dir) / "result.json"
            command = [
                *self.command_prefix,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                str(self.workspace),
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(output),
                "--color",
                "never",
            ]
            for item in post.get("local_media", [])[:4]:
                path = str(item.get("path") or "")
                if path and Path(path).exists():
                    command.extend(["--image", path])
            command.append("-")
            completed = _run_command_with_tree_timeout(
                command,
                input_text=prompt,
                timeout_seconds=self.timeout_seconds,
                operation="Codex classification",
            )
            if completed.returncode != 0 or not output.exists():
                detail = (completed.stderr or completed.stdout or "Codex classification failed").strip()
                _raise_codex_failure(detail, "Codex classification failed")
            value = json.loads(output.read_text(encoding="utf-8"))
        return validate_model_payload(value)


class CodexBatchPostClassifier:
    prompt_version = "kol-morning-batch-v1"
    model_name = "codex-batch"

    def __init__(
        self,
        schema_path: Path,
        workspace: Path,
        command: str = "codex",
        timeout_seconds: float = 480,
    ):
        self.schema_path = Path(schema_path)
        self.workspace = Path(workspace)
        self.command = _resolve_codex_command(command)
        self.command_prefix = _codex_command_prefix(self.command)
        self.timeout_seconds = max(1, timeout_seconds)

    def classify_many(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not 1 <= len(posts) <= 10:
            raise ValueError("Codex batches must contain between 1 and 10 posts")
        contents = [
            {
                "post_id": post["post_id"],
                "platform": post.get("platform") or "X",
                "kol": post["display_name"],
                "handle": post["handle"],
                "posted_at": post["posted_at"],
                "post_type": post["post_type"],
                "text": post["text"],
                "article_title": post["article_title"],
                "article_text": post["article_text"],
                "quoted_text": post["quoted_text"],
                "ocr_text": post.get("ocr_text") or "",
                "rule_symbols": post.get("rule_symbols", []),
                "rule_direction": post.get("rule_direction", ""),
            }
            for post in posts
        ]
        prompt = (
            "Classify each source item for an A-share KOL evidence audit. The platform may be X or Zhihu; "
            "Zhihu answers can be longer essays, numbered stock lists, retrospectives, or market analysis. "
            "Return one result for every post_id and only the requested JSON schema. Separate original pre-event recommendations "
            "from retrospectives, holdings, analysis, secondhand content and promotion. For a "
            "multi-stock recommendation, emit one draft per stock. A stock list without individual "
            "reasons must use the concise Chinese thesis '列入当日个股分享，原帖未提供具体个股理由'. "
            "Do not use promotional text as a thesis. Evidence spans must be exact source substrings. "
            "Preserve entry conditions and mark OCR dependency. Do not give investment advice or invent "
            "symbols, reasons, prices or holding periods. For every recommendation draft also classify "
            "the author's action (buy/add/hold/watch/reduce/sell/avoid), horizon "
            "(intraday/short/swing/medium_long/unspecified), and strength "
            "(explicit/moderate/weak/unspecified). Use unspecified when the source does not say.\n\n"
            + _json({"posts": contents})
        )
        with tempfile.TemporaryDirectory(prefix="kol-batch-classify-") as temp_dir:
            output = Path(temp_dir) / "result.json"
            command = [
                *self.command_prefix,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                str(self.workspace),
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(output),
                "--color",
                "never",
                "-",
            ]
            completed = _run_command_with_tree_timeout(
                command,
                input_text=prompt,
                timeout_seconds=self.timeout_seconds,
                operation="Codex batch classification",
            )
            if completed.returncode != 0 or not output.exists():
                detail = (completed.stderr or completed.stdout or "Codex batch classification failed").strip()
                _raise_codex_failure(detail, "Codex batch classification failed")
            raw = json.loads(output.read_text(encoding="utf-8"))
        requested = {str(post["post_id"]) for post in posts}
        results: dict[str, dict[str, Any]] = {}
        for value in raw.get("results", []):
            post_id = str(value.get("post_id") or "")
            if post_id not in requested or post_id in results:
                raise RuntimeError(f"unexpected or duplicate post_id in batch result: {post_id}")
            results[post_id] = validate_model_payload(
                {key: item for key, item in value.items() if key != "post_id"}
            )
        missing = requested - set(results)
        if missing:
            raise RuntimeError("batch result omitted posts: " + ", ".join(sorted(missing)))
        return results


def _deepseek_source_item(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "post_id": post["post_id"],
        "platform": post.get("platform") or "X",
        "kol": post.get("display_name") or "",
        "handle": post.get("handle") or "",
        "posted_at": post.get("posted_at") or "",
        "post_type": post.get("post_type") or "",
        "text": post.get("text") or "",
        "article_title": post.get("article_title") or "",
        "article_text": post.get("article_text") or "",
        "quoted_text": post.get("quoted_text") or "",
        "ocr_text": post.get("ocr_text") or "",
        "rule_symbols": post.get("rule_symbols", []),
        "rule_direction": post.get("rule_direction", ""),
    }


def _deepseek_review_instruction(schema: str, *, batch: bool) -> str:
    target = "one result for every post_id" if batch else "one result for the source item"
    return (
        "You classify public source items for an A-share KOL evidence audit. Do not give investment advice. "
        f"Return {target} as one valid JSON object and follow the supplied JSON Schema exactly. "
        "Separate original pre-event recommendations from retrospectives, holdings, analysis, secondhand "
        "content and promotion. For multi-stock recommendations emit one draft per stock. Evidence spans "
        "must be exact substrings from text, article_text, quoted_text or ocr_text. Never invent symbols, "
        "reasons, prices, time horizons or conditions. Preserve explicit entry conditions. If a stock list "
        "has no individual reason, state in Chinese that it was listed for the day and no stock-specific "
        "reason was provided. Do not emit recommendation drafts for themes, industries, concepts or market "
        "indexes, including provider-specific 88xxxx theme index codes; keep those as analysis only. "
        "Chinese source summaries and theses must remain concise Chinese.\n\n"
        "JSON Schema:\n" + schema
    )


def _parse_json_object_content(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("model content is not text")
    text = content.lstrip("\ufeff").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("model result is not an object")
    return value


class _DeepSeekClassifierBase:
    model_name = OPENCODE_GO_MODEL
    provider_name = "opencode-go"
    api_url = OPENCODE_GO_API_URL

    def __init__(
        self,
        schema_path: Path,
        credentials: DeepSeekCredentialStore | None = None,
        *,
        timeout_seconds: float = 180,
        client: httpx.Client | None = None,
    ):
        self.schema_path = Path(schema_path)
        self.credentials = credentials or DeepSeekCredentialStore()
        self.timeout_seconds = max(1, timeout_seconds)
        self.client = client

    def _request(self, user_payload: dict[str, Any], *, batch: bool) -> dict[str, Any]:
        schema = self.schema_path.read_text(encoding="utf-8")
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _deepseek_review_instruction(schema, batch=batch)},
                {"role": "user", "content": "Return JSON only.\n" + _json(user_payload)},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.credentials.load()}"}
        try:
            if self.client is None:
                response = httpx.post(
                    self.api_url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_seconds,
                )
            else:
                response = self.client.post(
                    self.api_url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_seconds,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelProviderUnavailableError(f"OpenCode Go connection failed: {exc.__class__.__name__}") from exc
        if response.status_code in {401, 402, 403, 408, 409, 429} or response.status_code >= 500:
            raise ModelProviderUnavailableError(f"OpenCode Go API unavailable (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise RuntimeError(f"OpenCode Go request rejected (HTTP {response.status_code})")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            value = _parse_json_object_content(content)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenCode Go returned invalid JSON") from exc
        return value


class DeepSeekPostClassifier(_DeepSeekClassifierBase):
    prompt_version = "kol-post-opencode-go-v1"

    def classify(self, post: dict[str, Any]) -> dict[str, Any]:
        return validate_model_payload(self._request(_deepseek_source_item(post), batch=False))


class DeepSeekBatchPostClassifier(_DeepSeekClassifierBase):
    prompt_version = "kol-morning-opencode-go-batch-v1"
    request_batch_size = 3

    def classify_many(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not 1 <= len(posts) <= 10:
            raise ValueError("DeepSeek batches must contain between 1 and 10 posts")
        self.last_errors: dict[str, str] = {}
        results: dict[str, dict[str, Any]] = {}
        for index in range(0, len(posts), self.request_batch_size):
            results.update(self._classify_chunk(posts[index:index + self.request_batch_size]))
        return results

    def _classify_chunk(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        requested = {str(post["post_id"]) for post in posts}
        try:
            raw = self._request({"posts": [_deepseek_source_item(post) for post in posts]}, batch=True)
        except ModelProviderUnavailableError:
            raise
        except Exception as exc:
            if len(posts) > 1:
                recovered: dict[str, dict[str, Any]] = {}
                for post in posts:
                    recovered.update(self._classify_chunk([post]))
                return recovered
            self.last_errors[str(posts[0]["post_id"])] = str(exc)
            return {}

        results: dict[str, dict[str, Any]] = {}
        retry_ids: set[str] = set()
        raw_results = raw.get("results", [])
        if not isinstance(raw_results, list):
            raw_results = []
            retry_ids.update(requested)
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            post_id = str(item.get("post_id") or "")
            if post_id not in requested or post_id in results:
                continue
            try:
                results[post_id] = validate_model_payload(
                    {key: value for key, value in item.items() if key != "post_id"}
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                self.last_errors[post_id] = str(exc)
                retry_ids.add(post_id)

        retry_ids.update(requested - set(results))
        if retry_ids and len(posts) > 1:
            posts_by_id = {str(post["post_id"]): post for post in posts}
            for post_id in sorted(retry_ids):
                results.update(self._classify_chunk([posts_by_id[post_id]]))
        elif retry_ids:
            post_id = next(iter(retry_ids))
            self.last_errors.setdefault(post_id, "DeepSeek omitted this post")
        return results


def _is_transient_model_error(exc: Exception) -> bool:
    if isinstance(exc, (ModelProviderUnavailableError, FileNotFoundError)):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    lowered = str(exc).lower()
    return any(marker in lowered for marker in (
        "timed out",
        "connection",
        "temporarily unavailable",
        "command not found",
        "not recognized",
        "cannot find the file",
    ))


class _FallbackClassifierBase:
    def __init__(self, primary: Any, backup: Any):
        self.primary = primary
        self.backup = backup
        self.last_provider = str(getattr(primary, "model_name", primary.__class__.__name__))
        self.last_fallback_reason = ""
        self.primary_available = True

    @property
    def prompt_version(self) -> str:
        selected = self.backup if self.last_provider == getattr(self.backup, "model_name", "") else self.primary
        return str(getattr(selected, "prompt_version", ""))

    @property
    def model_name(self) -> str:
        return self.last_provider

    @property
    def timeout_seconds(self) -> float:
        return float(getattr(self.primary, "timeout_seconds", 240))

    @timeout_seconds.setter
    def timeout_seconds(self, value: float) -> None:
        if hasattr(self.primary, "timeout_seconds"):
            self.primary.timeout_seconds = value
        if hasattr(self.backup, "timeout_seconds"):
            self.backup.timeout_seconds = value

    def _fallback(self, exc: Exception) -> bool:
        if not _is_transient_model_error(exc):
            return False
        self.last_fallback_reason = str(exc)[:500]
        self.last_provider = str(getattr(self.backup, "model_name", self.backup.__class__.__name__))
        self.primary_available = False
        return True


class FallbackPostClassifier(_FallbackClassifierBase):
    def classify(self, post: dict[str, Any]) -> dict[str, Any]:
        if not self.primary_available:
            self.last_provider = str(getattr(self.backup, "model_name", self.backup.__class__.__name__))
            return self.backup.classify(post)
        self.last_provider = str(getattr(self.primary, "model_name", self.primary.__class__.__name__))
        try:
            return self.primary.classify(post)
        except Exception as exc:
            if not self._fallback(exc):
                raise
        return self.backup.classify(post)


class FallbackBatchPostClassifier(_FallbackClassifierBase):
    def classify_many(self, posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.primary_available:
            self.last_provider = str(getattr(self.backup, "model_name", self.backup.__class__.__name__))
            return self.backup.classify_many(posts)
        self.last_provider = str(getattr(self.primary, "model_name", self.primary.__class__.__name__))
        try:
            return self.primary.classify_many(posts)
        except Exception as exc:
            if not self._fallback(exc):
                raise
        return self.backup.classify_many(posts)


def build_post_classifier(
    schema_path: Path,
    workspace: Path,
    *,
    deepseek_credentials: DeepSeekCredentialStore | None = None,
) -> FallbackPostClassifier:
    return FallbackPostClassifier(
        CodexPostClassifier(schema_path, workspace),
        DeepSeekPostClassifier(schema_path, deepseek_credentials),
    )


def build_batch_post_classifier(
    schema_path: Path,
    workspace: Path,
    *,
    deepseek_credentials: DeepSeekCredentialStore | None = None,
) -> FallbackBatchPostClassifier:
    return FallbackBatchPostClassifier(
        CodexBatchPostClassifier(schema_path, workspace),
        DeepSeekBatchPostClassifier(schema_path, deepseek_credentials),
    )


def classify_pending_with_codex(
    store: KolPostStore,
    classifier: CodexPostClassifier,
    limit: int = 0,
) -> tuple[int, int]:
    completed = 0
    failed = 0
    remaining = max(0, limit)
    with store.model_worker():
        store.prepare_model_queue()
        while limit == 0 or remaining > 0:
            candidates = store.claim_posts_for_model(1)
            if not candidates:
                break
            for post in candidates:
                try:
                    payload = classifier.classify(post)
                    store.save_model_classification(
                        post["post_id"],
                        payload,
                        model_name=str(getattr(classifier, "model_name", "codex")),
                        prompt_version=classifier.prompt_version,
                    )
                    completed += 1
                except Exception as exc:
                    store.save_model_classification(
                        post["post_id"],
                        None,
                        model_name=str(getattr(classifier, "model_name", "codex")),
                        prompt_version=classifier.prompt_version,
                        error=str(exc),
                    )
                    failed += 1
                if limit:
                    remaining -= 1
                    if remaining <= 0:
                        break
    return completed, failed


def _fetch_with_retry(
    provider: XPostProvider,
    handle: str,
    requested: int,
    retry_delays: tuple[float, ...],
) -> ProviderFetchResult:
    attempts: list[ProviderAttempt] = []
    for attempt in range(len(retry_delays) + 1):
        started = time.perf_counter()
        try:
            result = _provider_result(provider.fetch_user_posts(handle, requested), provider.name)
            return ProviderFetchResult(
                provider=result.provider,
                posts=result.posts,
                attempts=[*attempts, *result.attempts],
                warnings=result.warnings,
                comparisons=result.comparisons,
            )
        except TwitterAuthenticationError as exc:
            if not getattr(exc, "attempts", None):
                exc.attempts = [
                    ProviderAttempt(
                        provider.name,
                        "failed",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        error_code="authentication_failed",
                        error=str(exc)[:1000],
                    )
                ]
            raise
        except (TwitterRateLimitError, TwitterProviderError) as exc:
            captured = list(getattr(exc, "attempts", []))
            if not captured:
                captured = [
                    ProviderAttempt(
                        provider.name,
                        "failed",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        error_code=("rate_limited" if isinstance(exc, TwitterRateLimitError) else "provider_error"),
                        error=str(exc)[:1000],
                    )
                ]
            attempts.extend(captured)
            if attempt >= len(retry_delays):
                exc.attempts = attempts
                raise
            if retry_delays[attempt] > 0:
                time.sleep(retry_delays[attempt])
    raise AssertionError("unreachable retry state")


def _payload_post_ids(posts: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("id") or item.get("tweet_id") or "").strip()
        for item in posts
        if str(item.get("id") or item.get("tweet_id") or "").strip()
    }


def _fetch_with_cursor_search(
    provider: XPostProvider,
    handle: str,
    requested: int,
    retry_delays: tuple[float, ...],
    *,
    previous_last_id: str,
    max_pages: int,
) -> ProviderFetchResult:
    result = _fetch_with_retry(provider, handle, requested, retry_delays)
    if (
        not previous_last_id
        or previous_last_id in _payload_post_ids(result.posts)
        or max_pages <= 1
    ):
        return result
    attempts = list(result.attempts)
    warnings = list(result.warnings)
    comparisons = list(result.comparisons)
    latest = result
    for page in range(2, max_pages + 1):
        try:
            expanded = _fetch_with_retry(
                provider,
                handle,
                requested * page,
                retry_delays,
            )
        except (TwitterAuthenticationError, TwitterRateLimitError, TwitterProviderError) as exc:
            attempts.extend(list(getattr(exc, "attempts", [])))
            warnings.append(
                "gap_search_incomplete:"
                + (
                    "rate_limited"
                    if isinstance(exc, TwitterRateLimitError)
                    else "authentication_failed"
                    if isinstance(exc, TwitterAuthenticationError)
                    else "provider_error"
                )
            )
            return ProviderFetchResult(
                latest.provider,
                latest.posts,
                attempts,
                list(dict.fromkeys(warnings)),
                comparisons,
            )
        attempts.extend(expanded.attempts)
        warnings.extend(expanded.warnings)
        comparisons.extend(expanded.comparisons)
        latest = expanded
        if previous_last_id in _payload_post_ids(expanded.posts):
            return ProviderFetchResult(
                expanded.provider,
                expanded.posts,
                attempts,
                list(dict.fromkeys(warnings)),
                comparisons,
            )
        if len(expanded.posts) < requested * page:
            break
    warnings.append("gap_search_exhausted")
    return ProviderFetchResult(
        latest.provider,
        latest.posts,
        attempts,
        list(dict.fromkeys(warnings)),
        comparisons,
    )


def run_post_fetch(
    store: KolPostStore,
    provider: XPostProvider,
    *,
    platform_providers: dict[str, XPostProvider] | None = None,
    platforms: set[str] | None = None,
    handles: set[str] | None = None,
    max_count: int = 50,
    classifier: RuleClassifier | None = None,
    download_media: bool = True,
    sleep_seconds: float = 2.0,
    retry_delays: tuple[float, ...] = (5.0, 15.0),
    batch_key: str = "",
    rate_limit_cooldown_seconds: int = 1800,
    max_gap_pages: int = 3,
    dry_run: bool = False,
) -> FetchSummary:
    rule_classifier = classifier or RuleClassifier()
    if not dry_run:
        store.interrupt_stale_fetch_runs()
    active_kols = store.list_kols("active")
    if platforms:
        allowed_platforms = {value.casefold() for value in platforms}
        active_kols = [
            kol for kol in active_kols
            if str(kol.get("platform") or "X").casefold() in allowed_platforms
        ]
    if handles:
        allowed_handles = {
            str(value).strip().lstrip("@").casefold()
            for value in handles
            if str(value).strip()
        }
        active_kols = [
            kol for kol in active_kols
            if str(kol.get("handle") or "").strip().casefold() in allowed_handles
        ]
    initial_backfill = any(
        not str(kol.get("last_fetched_at") or "")
        and str(kol.get("platform") or "X").casefold() != "zhihu"
        for kol in active_kols
    )
    requested_depth = max(
        [max_count, 100 if initial_backfill else max_count]
        + [
            int(kol.get("backfill_requested") or 0)
            for kol in active_kols
            if str(kol.get("backfill_status") or "") == "queued"
        ]
    )
    if batch_key and not dry_run:
        store.prepare_fetch_queue(
            batch_key,
            active_kols,
            requested_count=requested_depth,
        )
        active_kols = store.pending_fetch_queue(batch_key)
    run_id = (
        f"dry-run-{uuid.uuid4().hex}"
        if dry_run
        else store.start_fetch_run(requested_depth, total_kols=len(active_kols))
    )
    successful = 0
    failed = 0
    new_posts = 0
    candidate_posts = 0
    gaps: list[str] = []
    fallback_kols: list[str] = []
    shadow_failed_kols: list[str] = []
    errors: list[str] = []
    authentication_failed = False
    rate_limit_paused = False
    platform_auth_failures: set[str] = set()
    platform_blocked_errors: dict[str, str] = {}
    blocked_reported: set[str] = set()
    providers_by_platform = {
        str(key).casefold(): value for key, value in (platform_providers or {}).items()
    }
    zhihu_kols = [
        kol for kol in active_kols
        if str(kol.get("platform") or "X").casefold() == "zhihu"
    ]
    zhihu_provider = providers_by_platform.get("zhihu", provider)
    if zhihu_kols and hasattr(zhihu_provider, "prepare_session"):
        try:
            zhihu_provider.prepare_session(str(zhihu_kols[0].get("handle") or ""))
        except Exception as exc:
            platform_blocked_errors["zhihu"] = str(exc)
    for index, kol in enumerate(active_kols):
        platform = str(kol.get("platform") or "X").casefold()
        selected_provider = providers_by_platform.get(platform, provider)
        if platform in platform_blocked_errors:
            if platform not in blocked_reported:
                errors.append(
                    f"{platform} batch blocked before account fetch: {platform_blocked_errors[platform]}"
                )
                blocked_reported.add(platform)
            if not dry_run:
                store.update_fetch_progress(
                    run_id,
                    processed_kols=index + 1,
                    successful_kols=successful,
                    failed_kols=failed,
                    new_posts=new_posts,
                    candidate_posts=candidate_posts,
                )
            continue
        if platform in platform_auth_failures:
            if batch_key:
                break
            failed += 1
            errors.append(f"@{kol['handle']}: {kol.get('platform') or 'X'} authentication circuit is open")
            if not dry_run:
                store.mark_fetch_failed(kol["id"], "authentication_failed")
                store.update_fetch_progress(
                    run_id,
                    processed_kols=index + 1,
                    successful_kols=successful,
                    failed_kols=failed,
                    new_posts=new_posts,
                    candidate_posts=candidate_posts,
                )
            continue
        if batch_key and not dry_run:
            store.mark_fetch_queue_running(batch_key, int(kol["id"]))
        previous_last_id = str(kol.get("last_post_id") or "")
        last_fetched = str(kol.get("last_fetched_at") or "")
        requested = max_count if platform == "zhihu" or last_fetched else 100
        backfill_queued = str(kol.get("backfill_status") or "") == "queued"
        archive_only_backfill = (
            platform == "zhihu"
            and str(kol.get("tracking_mode") or "") == "direct_profile"
            and backfill_queued
        )
        if backfill_queued:
            backfill_target = int(kol.get("backfill_requested") or 0)
            completed_depth = int(kol.get("backfill_completed_depth") or 0)
            requested = min(backfill_target, completed_depth + 100)
        if last_fetched and not backfill_queued:
            try:
                if datetime.fromisoformat(last_fetched) < datetime.now(SHANGHAI) - timedelta(days=3):
                    requested = max(max_count, 100)
            except ValueError:
                requested = max_count
        rate_limited_this_account = False
        try:
            fetch_result = _fetch_with_cursor_search(
                selected_provider,
                kol["handle"],
                requested,
                retry_delays,
                previous_last_id=previous_last_id,
                max_pages=max_gap_pages if platform == "x" else 1,
            )
            if not dry_run:
                store.record_fetch_attempts(run_id, kol["id"], kol["handle"], fetch_result.attempts)
                store.record_provider_comparisons(run_id, kol["id"], fetch_result.comparisons)
            if fetch_result.provider == "nitter":
                fallback_kols.append(kol["handle"])
            if "shadow_fallback_failed" in fetch_result.warnings:
                shadow_failed_kols.append(kol["handle"])
            seen_ids: list[str] = []
            for payload in fetch_result.posts:
                try:
                    if platform == "zhihu":
                        post = normalise_zhihu_answer(
                            payload,
                            kol,
                            provider=fetch_result.provider,
                        )
                    else:
                        post = normalise_twitter_post(
                            payload,
                            kol,
                            provider=fetch_result.provider,
                            provider_warning=_post_provider_warning(payload, fetch_result.provider),
                        )
                except ValueError as exc:
                    errors.append(f"@{kol['handle']} skipped unusable post: {exc}")
                    continue
                seen_ids.append(post.post_id)
                created = not store.has_post(post.post_id) if dry_run else store.upsert_post(post, run_id=run_id)
                if not dry_run and post.platform == "Zhihu" and post.post_type == "aggregation":
                    store.replace_digest_attributions(
                        post.post_id,
                        list(post.raw_payload.get("attributions") or []),
                    )
                if created:
                    new_posts += 1
                if not dry_run and download_media and post.media:
                    existing_media = store.get_post(post.post_id).get("local_media", [])
                    expected_images = sum(
                        str(item.get("type") or "").lower() in {"photo", "image"}
                        for item in post.media
                    )
                    if len(existing_media) < expected_images:
                        local_media, media_errors = download_images(post, store.media_root)
                        if local_media:
                            store.save_local_media(post.post_id, local_media)
                        if media_errors:
                            store.record_review_action(
                                post.post_id,
                                "media_download_failed",
                                {"errors": media_errors[:10]},
                            )
                rule = rule_classifier.classify(post)
                if not dry_run:
                    store.save_rule_classification(post.post_id, rule)
                    if created and archive_only_backfill:
                        store.set_review(
                            post.post_id,
                            "ignored",
                            "Zhihu historical backfill is archive-only.",
                            {
                                "policy": "zhihu_direct_profile_backfill_archive_only",
                                "tracking_mode": kol.get("tracking_mode"),
                                "backfill_requested": kol.get("backfill_requested"),
                            },
                        )
                if created and rule.is_candidate and not archive_only_backfill:
                    candidate_posts += 1
            if fetch_result.posts and not seen_ids:
                raise TwitterProviderError(
                    "provider returned posts but none had usable content",
                    attempts=[
                        ProviderAttempt(
                            fetch_result.provider,
                            "unusable_content",
                            post_count=len(fetch_result.posts),
                            error_code="unusable_content",
                            error="all returned posts failed normalization",
                        )
                    ],
                )
            gap_search_incomplete = any(
                warning.startswith("gap_search_incomplete:")
                for warning in fetch_result.warnings
            )
            gap_detected = (
                "gap_search_exhausted" in fetch_result.warnings
                or gap_search_incomplete
                or (
                fetch_result.provider == "nitter"
                and bool(previous_last_id)
                and previous_last_id not in seen_ids
                )
            )
            if gap_detected:
                gaps.append(kol["handle"])
            newest = (
                previous_last_id
                if gap_detected and previous_last_id
                else seen_ids[0]
                if seen_ids
                else previous_last_id
            )
            if not dry_run:
                store.update_fetch_state(
                    kol["id"],
                    newest,
                    "gap_detected" if gap_detected else "success",
                    fetched_count=len(seen_ids),
                    requested_count=requested,
                )
                if batch_key:
                    if gap_search_incomplete:
                        store.defer_fetch_queue_item(
                            batch_key,
                            int(kol["id"]),
                            error_code="gap_search_incomplete",
                            error="cursor search interrupted before the saved post was found",
                            cooldown_seconds=60,
                        )
                    else:
                        store.complete_fetch_queue_item(batch_key, int(kol["id"]))
            successful += 1
        except Exception as exc:
            attempts = list(getattr(exc, "attempts", []))
            rate_limited_this_account = isinstance(exc, TwitterRateLimitError) or any(
                attempt.error_code == "rate_limited" for attempt in attempts
            )
            failed += 1
            errors.append(f"@{kol['handle']}: {exc}")
            if not dry_run:
                store.mark_fetch_failed(kol["id"])
                store.record_fetch_attempts(
                    run_id,
                    kol["id"],
                    kol["handle"],
                    attempts,
                )
            if isinstance(exc, TwitterAuthenticationError):
                authentication_failed = True
                platform_auth_failures.add(platform)
            if batch_key and not dry_run:
                error_code = (
                    "rate_limited"
                    if rate_limited_this_account
                    else "authentication_failed"
                    if isinstance(exc, TwitterAuthenticationError)
                    else "provider_error"
                )
                store.defer_fetch_queue_item(
                    batch_key,
                    int(kol["id"]),
                    error_code=error_code,
                    error=str(exc),
                    cooldown_seconds=(
                        rate_limit_cooldown_seconds
                        if error_code in {"rate_limited", "authentication_failed"}
                        else 300
                    ),
                )
        if not dry_run:
            store.update_fetch_progress(
                run_id,
                processed_kols=index + 1,
                successful_kols=successful,
                failed_kols=failed,
                new_posts=new_posts,
                candidate_posts=candidate_posts,
            )
        if sleep_seconds and index < len(active_kols) - 1:
            time.sleep(sleep_seconds)
        if batch_key and rate_limited_this_account:
            rate_limit_paused = True
            break
    if not dry_run and any(
        str(kol.get("platform") or "X").casefold() == "zhihu" for kol in active_kols
    ):
        store.reconcile_zhihu_historical_backfill()
    queue_status = (
        store.fetch_queue_status(batch_key)
        if batch_key and not dry_run
        else {"total": 0, "completed": 0, "pending": 0}
    )
    summary = FetchSummary(
        run_id=run_id,
        successful_kols=successful,
        failed_kols=failed,
        new_posts=new_posts,
        candidate_posts=candidate_posts,
        pending_reviews=store.count_pending(),
        auth_status=(
            "failed"
            if authentication_failed or bool(getattr(provider, "primary_auth_failed", False))
            else "ok" if successful else "unknown"
        ),
        gap_kols=gaps,
        fallback_kols=fallback_kols,
        shadow_failed_kols=shadow_failed_kols,
        errors=errors,
        queue_total=int(queue_status["total"]),
        queue_completed=int(queue_status["completed"]),
        queue_pending=int(queue_status["pending"]),
        rate_limit_paused=rate_limit_paused,
        blocked_platforms=sorted(platform_blocked_errors),
    )
    if not dry_run:
        store.finish_fetch_run(summary)
    return summary


def media_disk_usage(path: Path) -> int:
    if not Path(path).exists():
        return 0
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())
