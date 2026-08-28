from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from kol_posts import KolPostStore


@dataclass(frozen=True)
class LeadExtractionSummary:
    processed_posts: int = 0
    created: int = 0
    updated: int = 0
    confirmed_symbols: list[str] = field(default_factory=list)
    failed: int = 0


def reconcile_exact_stock_leads(
    store: KolPostStore,
    instruments: Mapping[str, Mapping[str, Any]],
    *,
    batch_size: int = 500,
    post_id: str | None = None,
) -> list[str]:
    confirmed: set[str] = set()
    pending: list[dict[str, Any]] = []
    offset = 0
    while True:
        leads = store.list_stock_leads(
            status="pending",
            post_id=post_id,
            limit=batch_size,
            offset=offset,
        )
        if not leads:
            break
        pending.extend(leads)
        offset += len(leads)
    for lead in pending:
        if lead["extraction_method"] == "exact_code" and lead["symbol"] in instruments:
            store.auto_confirm_stock_lead(
                lead["id"],
                "Exact six-digit code matched the refreshed instrument master.",
            )
            confirmed.add(lead["symbol"])
    return sorted(confirmed)


def _signature(post: Mapping[str, Any]) -> str:
    value = "|".join(
        [
            str(post.get("content_hash") or ""),
            str(post.get("classification_updated_at") or ""),
            str(post.get("model_status") or ""),
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence(content: str, needle: str, width: int = 90) -> str:
    position = content.find(needle)
    if position < 0:
        return content.strip()[: width * 2]
    start = max(0, position - width)
    end = min(len(content), position + len(needle) + width)
    return content[start:end].strip()


def _mention_kind(post: Mapping[str, Any]) -> str:
    evidence_type = str(post.get("evidence_type") or "")
    content_type = str(post.get("content_type") or "")
    text = "\n".join(
        str(post.get(key) or "") for key in ("text", "article_title", "article_text", "quoted_text")
    )
    if evidence_type == "retrospective":
        return "retrospective"
    if evidence_type == "secondhand" or post.get("post_type") == "retweet":
        return "secondhand"
    prospective = any(
        marker in text
        for marker in (
            "建仓计划", "计划建仓", "准备买入", "准备建仓", "明日建仓", "周一建仓",
            "下周建仓", "买入计划", "低吸计划", "可建仓", "明日关注", "明天关注",
        )
    )
    realized = any(word in text for word in ("已建仓", "已经建仓", "刚建仓", "当前持仓", "持仓", "买入了", "我的仓位"))
    if realized and not prospective:
        return "holding"
    if prospective:
        return "recommendation"
    if content_type == "recommendation":
        return "recommendation"
    return "analysis"


def extract_stock_leads(
    store: KolPostStore,
    *,
    instruments: Mapping[str, Mapping[str, Any]],
    aliases: Mapping[str, str],
    batch_size: int = 500,
    post_ids: list[str] | None = None,
) -> LeadExtractionSummary:
    processed = created = updated = failed = 0
    confirmed_symbols: set[str] = set()
    if post_ids is not None:
        posts = [store.get_post(post_id) for post_id in post_ids]
        batches = [posts]
    else:
        offset = 0
        batches = None
    while True:
        if batches is not None:
            if not batches:
                break
            posts = batches.pop(0)
        else:
            posts = store.list_posts(limit=batch_size, offset=offset)
            if not posts:
                break
        for post in posts:
            signature = _signature(post)
            if store.lead_extraction_signature(post["post_id"]) == signature:
                continue
            processed += 1
            try:
                content = "\n".join(
                    str(post.get(key) or "")
                    for key in ("text", "article_title", "article_text", "quoted_text")
                )
                drafts = [value for value in post.get("drafts") or [] if isinstance(value, dict)]
                structured_symbols = (
                    {str(value.get("symbol") or "") for value in drafts}
                    if post.get("model_name") == "structured-rules"
                    else set()
                )
                explicit = set(re.findall(r"(?<!\d)(\d{6})(?!\d)", content))
                candidates: dict[str, dict[str, Any]] = {}
                for symbol in explicit:
                    if structured_symbols and symbol not in structured_symbols:
                        continue
                    instrument = instruments.get(symbol, {})
                    matched = bool(instrument)
                    candidates[symbol] = {
                        "security_name": str(instrument.get("name") or ""),
                        "instrument_type": str(instrument.get("instrument_type") or "stock"),
                        "extraction_method": "exact_code",
                        "confidence": 0.99 if matched else 0.85,
                        "status": "confirmed" if matched else "pending",
                        "auto_confirmed": matched,
                        "evidence_text": _evidence(content, symbol),
                    }
                for symbol, name in aliases.items():
                    if (
                        not name
                        or name not in content
                        or symbol in candidates
                        or (structured_symbols and symbol not in structured_symbols)
                    ):
                        continue
                    instrument = instruments.get(symbol, {})
                    candidates[symbol] = {
                        "security_name": str(instrument.get("name") or name),
                        "instrument_type": str(instrument.get("instrument_type") or "stock"),
                        "extraction_method": "name_match",
                        "confidence": 0.7,
                        "status": "pending",
                        "auto_confirmed": False,
                        "evidence_text": _evidence(content, name),
                    }
                for symbol in post.get("rule_symbols") or []:
                    symbol = str(symbol)
                    if (
                        not re.fullmatch(r"\d{6}", symbol)
                        or symbol in candidates
                        or (structured_symbols and symbol not in structured_symbols)
                    ):
                        continue
                    instrument = instruments.get(symbol, {})
                    name = str(instrument.get("name") or aliases.get(symbol) or "")
                    candidates[symbol] = {
                        "security_name": name,
                        "instrument_type": str(instrument.get("instrument_type") or "stock"),
                        "extraction_method": "name_match",
                        "confidence": 0.7,
                        "status": "pending",
                        "auto_confirmed": False,
                        "evidence_text": _evidence(content, name) if name else content[:180],
                    }
                for draft in drafts:
                    symbol = str(draft.get("symbol") or "")
                    if not re.fullmatch(r"\d{6}", symbol):
                        continue
                    instrument = instruments.get(symbol, {})
                    draft_candidate = {
                        "security_name": str(draft.get("security_name") or instrument.get("name") or ""),
                        "instrument_type": str(instrument.get("instrument_type") or "stock"),
                        "extraction_method": (
                            "structured_rules"
                            if post.get("model_name") == "structured-rules"
                            else "codex"
                        ),
                        "confidence": float(draft.get("confidence") or post.get("confidence") or 0),
                        "status": "pending",
                        "auto_confirmed": False,
                        "evidence_text": str(draft.get("thesis") or post.get("model_summary") or "")[:2000],
                        "direction": str(draft.get("direction") or ""),
                        "mention_kind": str(draft.get("mention_kind") or "analysis"),
                    }
                    if symbol in candidates:
                        candidates[symbol].update(draft_candidate)
                    else:
                        candidates[symbol] = draft_candidate
                for symbol, candidate in candidates.items():
                    was_created = store.upsert_stock_lead(
                        {
                            "post_id": post["post_id"],
                            "kol_id": post["kol_id"],
                            "symbol": symbol,
                            "direction": candidate.get("direction") or post.get("rule_direction") or "",
                            "mention_kind": candidate.get("mention_kind") or _mention_kind(post),
                            **candidate,
                        }
                    )
                    created += int(was_created)
                    updated += int(not was_created)
                    if candidate["status"] == "confirmed":
                        confirmed_symbols.add(symbol)
                store.mark_lead_extraction(post["post_id"], signature)
            except Exception as exc:
                failed += 1
                store.mark_lead_extraction(post["post_id"], signature, error=str(exc))
        if batches is None:
            offset += len(posts)
    return LeadExtractionSummary(
        processed_posts=processed,
        created=created,
        updated=updated,
        confirmed_symbols=sorted(confirmed_symbols),
        failed=failed,
    )
