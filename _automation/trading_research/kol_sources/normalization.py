from __future__ import annotations
import hashlib
import re
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
