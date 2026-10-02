from __future__ import annotations
import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from kol_tracker import SHANGHAI, now_iso
from .core import PostRecord, _utc_and_local


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
        if name == "龙开无水又三人禾":
            names = ["龙开", "水又三人禾"]
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
                    "section_text": "无" if attributed_name == "龙开" else section,
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


ZHIHU_CONTENT_TYPES = {"answer", "article", "idea"}


def _zhihu_content_type(payload: dict[str, Any], url: str) -> str:
    value = str(
        payload.get("type")
        or payload.get("contentType")
        or payload.get("surface")
        or ""
    ).strip().casefold()
    aliases = {
        "answers": "answer",
        "articles": "article",
        "pins": "idea",
        "pin": "idea",
        "ideas": "idea",
        "thought": "idea",
        "thoughts": "idea",
    }
    value = aliases.get(value, value)
    if value in ZHIHU_CONTENT_TYPES:
        return value
    if re.fullmatch(r"https://www\.zhihu\.com/question/\d+/answer/\d+", url):
        return "answer"
    if re.fullmatch(r"https://zhuanlan\.zhihu\.com/p/\d+", url):
        return "article"
    if re.fullmatch(r"https://www\.zhihu\.com/pin/\d+", url):
        return "idea"
    # Old answer payloads predate the explicit type field.  Preserve their
    # compatibility while rejecting unknown new content types below.
    if not value:
        return "answer"
    raise ValueError(f"unsupported Zhihu content type: {value}")


def _zhihu_numeric_id(value: Any, content_type: str) -> str:
    raw = str(value or "").strip()
    prefix = f"zhihu:{content_type}:"
    if raw.startswith(prefix):
        raw = raw[len(prefix) :]
    legacy_prefix = f"{content_type}:"
    if raw.startswith(legacy_prefix):
        raw = raw[len(legacy_prefix) :]
    id_pattern = r"\d{5,25}" if content_type == "answer" else r"\d{1,25}"
    if not re.fullmatch(id_pattern, raw):
        raise ValueError(f"Zhihu {content_type} has an invalid id")
    return raw


def zhihu_post_id(content_type: str, raw_id: str) -> str:
    """Use legacy numeric IDs for answers and namespaced IDs for new feeds."""
    if content_type == "answer":
        return raw_id
    return f"zhihu:{content_type}:{raw_id}"


def _zhihu_created_value(payload: dict[str, Any]) -> str:
    for key in ("createdAtISO", "created_at", "created_time", "created"):
        source = payload.get(key)
        if source in (None, "", 0):
            continue
        if isinstance(source, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", str(source)):
            try:
                number = float(source)
                if number > 100_000_000_000:
                    number /= 1000
                return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError, OverflowError, OSError):
                continue
        return str(source)
    return ""


def _zhihu_text(value: Any) -> str:
    if isinstance(value, list):
        pieces = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("content") or item.get("text") or item.get("value") or ""
            pieces.append(_zhihu_text(item))
        return "\n".join(piece for piece in pieces if piece).strip()
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"[ \t]+\n", "\n", value).strip()


def normalise_zhihu_post(
    payload: dict[str, Any],
    kol: dict[str, Any],
    *,
    provider: str = "zhihu-local",
) -> PostRecord:
    if not isinstance(payload, dict):
        raise ValueError("Zhihu payload must be an object")
    url = str(payload.get("url") or "").strip()
    content_type = _zhihu_content_type(payload, url)
    supplied_id = payload.get("id")
    if not supplied_id:
        id_match = re.search(r"/(?:answer|p|pin)/(\d+)$", url)
        supplied_id = id_match.group(1) if id_match else ""
    raw_id = _zhihu_numeric_id(supplied_id, content_type)
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    author_name = str(author.get("name") or author.get("fullname") or "").strip()
    author_handle = str(
        author.get("screenName") or author.get("url_token") or author.get("screen_name") or ""
    ).strip()
    if not author_name and not author_handle:
        raise ValueError(f"Zhihu {content_type} has no verified author")
    requested_handle = str(payload.get("requestedHandle") or kol.get("handle") or "").strip()
    mismatch = bool(payload.get("authorMismatch")) or payload.get("authorMatchesHandle") is False
    if (
        author_handle
        and requested_handle
        and author_handle.casefold() != requested_handle.casefold()
    ):
        mismatch = True
    tracking_mode = str(kol.get("tracking_mode") or "").strip()
    # A direct profile feed must never silently graft a favourite/foreign row
    # onto the requested KOL.  Aggregation feeds retain the API author because
    # their purpose is explicitly to preserve second-hand attribution.
    if mismatch and tracking_mode != "aggregation":
        raise ValueError(
            f"Zhihu {content_type} author {author_handle or author_name!r} "
            f"does not match requested profile {requested_handle!r}"
        )
    text = _zhihu_text(payload.get("text"))
    if not text:
        raise ValueError(f"Zhihu {content_type} has no usable text")
    canonical_patterns = {
        "answer": r"https://www\.zhihu\.com/question/\d+/answer/\d+",
        "article": r"https://zhuanlan\.zhihu\.com/p/\d+",
        "idea": r"https://www\.zhihu\.com/pin/\d+",
    }
    if not re.fullmatch(canonical_patterns[content_type], url):
        raise ValueError(f"Zhihu {content_type} has no canonical URL")
    url_id_match = re.search(r"/(?:answer|p|pin)/(\d+)$", url)
    if not url_id_match or url_id_match.group(1) != raw_id:
        raise ValueError(f"Zhihu {content_type} id does not match its canonical URL")
    created_value = _zhihu_created_value(payload)
    if not created_value:
        raise ValueError(f"Zhihu {content_type} is missing an exact timestamp")
    try:
        utc_value, local_value = _utc_and_local(created_value)
    except ValueError as exc:
        raise ValueError(f"Zhihu {content_type} has an invalid exact timestamp") from exc
    is_aggregation = tracking_mode == "aggregation"
    raw_payload = dict(payload)
    raw_payload["type"] = content_type
    raw_payload["surface"] = {
        "answer": "answers",
        "article": "articles",
        "idea": "ideas",
    }[content_type]
    raw_payload["source_kind"] = "aggregation" if is_aggregation else "direct_profile"
    raw_payload["attributions"] = extract_zhihu_digest_attributions(text) if is_aggregation else []
    post_id = zhihu_post_id(content_type, raw_id)
    hash_input = "\n".join(
        [post_id, content_type, url, text, str(payload.get("articleTitle") or "")]
    ).encode("utf-8")
    provider_warning = "secondhand_aggregation" if is_aggregation else ""
    profile_warnings = [
        str(item).strip()
        for item in payload.get("profile_warnings") or []
        if str(item).strip()
    ]
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    coverage_status = str(coverage.get("status") or "").casefold()
    if profile_warnings:
        provider_warning = ";".join(
            filter(None, [provider_warning, *dict.fromkeys(profile_warnings)])
        )
    elif "coverage" in payload and coverage_status == "complete":
        # Keep an explicit current-run state for the UI.  A bounded run is
        # represented by raw coverage metadata and remains warning-free.
        provider_warning = ";".join(filter(None, [provider_warning, "coverage:complete"]))
    if mismatch and is_aggregation:
        provider_warning = ";".join(filter(None, [provider_warning, "author_mismatch_retained"]))
    post_type = "aggregation" if is_aggregation and content_type == "answer" else content_type
    return PostRecord(
        post_id=post_id,
        kol_id=int(kol["id"]),
        platform="Zhihu",
        handle=str(kol["handle"]),
        author_name=author_name or author_handle,
        url=url,
        text=text,
        article_title=str(payload.get("articleTitle") or payload.get("title") or ""),
        article_text=text if content_type == "article" else str(payload.get("articleText") or ""),
        quoted_id="",
        quoted_text="",
        quoted_author="",
        reply_to_id="",
        reply_to_author="",
        posted_at=local_value,
        posted_at_utc=utc_value,
        post_type=post_type,
        language=str(payload.get("lang") or "zh-CN"),
        media=list(payload.get("media") or []) if isinstance(payload.get("media"), list) else [],
        metrics=dict(payload.get("metrics") or {}),
        raw_payload=raw_payload,
        content_hash=hashlib.sha256(hash_input).hexdigest(),
        fetched_at=now_iso(),
        canonical_provider=provider,
        metrics_provider=provider,
        provider_warning=provider_warning,
    )


def normalise_zhihu_answer(
    payload: dict[str, Any],
    kol: dict[str, Any],
    *,
    provider: str = "zhihu-local",
) -> PostRecord:
    """Backward-compatible answer entry point; explicit new types dispatch too."""
    return normalise_zhihu_post(payload, kol, provider=provider)


def normalise_douyin_post(
    payload: dict[str, Any],
    kol: dict[str, Any],
    *,
    provider: str = "douyin-capture",
) -> PostRecord:
    """Normalise a Douyin capture item (archive/transcript export) into a post.

    Captures are produced by ``kol-douyin-capture-sync`` from the local Douyin
    archive (favourites manifest + whisper transcripts); text is the transcript
    when available and the post description otherwise.
    """
    item_id = str(
        payload.get("external_item_id")
        or payload.get("item_id")
        or payload.get("aweme_id")
        or ""
    ).strip()
    if not re.fullmatch(r"\d{5,25}", item_id):
        raise ValueError("Douyin item has an invalid id")
    text = str(payload.get("text") or payload.get("desc") or "").strip()
    if not text:
        raise ValueError("Douyin item has no usable text")
    posted_at = str(payload.get("posted_at") or payload.get("created_at") or "").strip()
    if not posted_at:
        raise ValueError("Douyin item is missing an exact timestamp")
    utc_value, local_value = _utc_and_local(posted_at)
    media_kind = str(
        payload.get("media_type") or payload.get("content_type") or "video"
    ).strip().casefold()
    post_type = "gallery" if media_kind in {"gallery", "images", "note"} else "video"
    url = str(payload.get("url") or "").strip()
    if not url:
        segment = "note" if post_type == "gallery" else "video"
        url = f"https://www.douyin.com/{segment}/{item_id}"
    # Same id scheme as kol_audit.discovery.models.content_id: "<platform>:<id>".
    post_id = f"douyin:{item_id}"
    hash_input = "\n".join([post_id, text]).encode("utf-8")
    return PostRecord(
        post_id=post_id,
        kol_id=int(kol["id"]),
        platform="douyin",
        handle=str(kol["handle"]),
        author_name=str(kol.get("display_name") or kol["handle"]),
        url=url,
        text=text,
        article_title="",
        article_text="",
        quoted_id="",
        quoted_text="",
        quoted_author="",
        reply_to_id="",
        reply_to_author="",
        posted_at=local_value,
        posted_at_utc=utc_value,
        post_type=post_type,
        language="zh",
        media=[],
        metrics={},
        raw_payload=dict(payload),
        content_hash=hashlib.sha256(hash_input).hexdigest(),
        fetched_at=now_iso(),
        canonical_provider=provider,
        metrics_provider=provider,
    )
