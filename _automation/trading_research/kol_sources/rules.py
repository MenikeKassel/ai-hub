from __future__ import annotations
import re
from typing import Any, Callable, Protocol
from .core import FINANCE_WORDS, LONG_WORDS, NUMBERED_STOCK_LINE, PROSPECTIVE_PLAN_MARKERS, PostRecord, RECOMMENDATION_ACTIONS, RECOMMENDATION_HORIZONS, RECOMMENDATION_STRENGTHS, RETROSPECTIVE_CLAIM, RETROSPECTIVE_WORDS, RuleResult, SHORT_WORDS, STRUCTURED_RECOMMENDATION_HEADER


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
