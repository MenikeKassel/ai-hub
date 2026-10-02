"""Local topic evidence, independent of stock approval and market subscriptions."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from kol_posts import KolPostStore


CATALOG_PATH = Path(__file__).with_name("theme_catalog.json")
EXTRACTION_VERSION = "theme-evidence-v5"
THEME_KINDS = {"prospective", "recommendation", "analysis", "retrospective", "product", "secondhand"}
SOURCE_FIELDS = ("text", "article_title", "article_text", "quoted_text", "ocr_text")
_RETROSPECTIVE = re.compile(r"回顾|复盘|早在|此前|之前|当时|上次|曾经|早就|最早|已经提|上周|上个月|前几天|提示过|提过|说过|谈过|推荐过")
_CLAIMED_TIMING = re.compile(
    r"(?:早在|此前|之前|当时|从|在)?\s*(?:\d{1,2}月(?:\d{1,2}[日号]?|初|中旬|下旬|底)?|"
    r"[一二三四五六七八九十]{1,3}月(?:[一二三四五六七八九十]{1,3}[日号]|初|中旬|下旬|底)?|"
    r"\d{1,2}[/]\d{1,2}|上周|上个月|前几天|之前|当时|此前)"
)
_PROSPECTIVE = re.compile(r"下周|明天|明日|未来|后续|接下来|准备|计划|将会|有望|机会|值得关注|重点关注")
_EXPLICIT_FORWARD = re.compile(r"下周|明天|明日|未来|接下来")
_RECOMMENDATION = re.compile(r"推荐|看好|看多|看空|低吸|买入|建仓|卖出|减仓|增持|加仓")
_STOCK_ACTION = re.compile(r"买入|建仓|加仓|减仓|卖出|低吸|布局")
_PRODUCT = re.compile(r"一瓶|几瓶|瓶装|口感|喝|品尝|尝了|试饮|酒体|酒液|到货|下单|买了|买过|购买|兰亭|兰庭")
_FINANCE = re.compile(r"股票|股价|个股|板块|建仓|持仓|估值|业绩|市值|涨停|看多|看空|标的|仓位|买入")
_SECONDHAND = re.compile(r"转述|转发|转载|摘自|据.{1,20}(?:说|介绍)|有媒体报道|媒体报道|新闻报道|行业专家.{0,10}梳理|纪要摘要|公司公告|公告称|他说|她说|群友(?:说|观点)")
_RESEARCH_SUMMARY = re.compile(r"^\s*【[^】\n]{2,30}】[^\n]{0,100}(?:点评|研报|纪要)")
_WITHDRAWAL = re.compile(r"不再推荐|不推荐|不看好|没有机会|别[^。\n，,；;]{0,12}(?:买入|建仓)")


@dataclass(frozen=True)
class ThemeCatalog:
    version: str
    themes: tuple[dict[str, Any], ...]

    def search_theme_ids(self, query: str) -> list[str]:
        needle = query.strip().casefold()
        if not needle:
            return []
        return [
            theme["id"] for theme in self.themes
            if any(needle in value.casefold() for value in (
                theme["id"], theme["name"],
                *(rule["term"] for rule in theme["terms"]),
                *(item["name"] for item in theme["mapped_symbols"]),
                *(item["symbol"] for item in theme["mapped_symbols"]),
            ))
        ]


def load_theme_catalog(path: Path = CATALOG_PATH, *, store: KolPostStore | None = None) -> ThemeCatalog:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if store is not None:
        with store.connect() as db:
            custom = db.execute('SELECT * FROM research_themes ORDER BY theme_id').fetchall()
        data['themes'] = [*data.get('themes', []), *[{'id': r['theme_id'], 'name': r['name'],
            'terms': json.loads(r['terms_json']), 'mapped_symbols': []} for r in custom]]
    themes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in data.get("themes", []):
        theme_id = str(raw.get("id") or "")
        name = str(raw.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", theme_id) or theme_id in ids or not name:
            raise ValueError("invalid or duplicate theme catalog id/name")
        ids.add(theme_id)
        terms = []
        for value in raw.get("terms", []):
            rule = {"term": value} if isinstance(value, str) else dict(value)
            term = str(rule.get("term") or "").strip()
            if not term:
                raise ValueError(f"empty matching term for {theme_id}")
            terms.append({
                "term": term,
                "require_any": [str(item) for item in rule.get("require_any", []) if item],
                "context_chars": max(0, min(int(rule.get("context_chars", 60)), 200)),
            })
        if not terms:
            raise ValueError(f"theme {theme_id} has no matching terms")
        mapped = []
        for value in raw.get("mapped_symbols", []):
            symbol = str(value.get("symbol") or "")
            if not re.fullmatch(r"\d{6}", symbol) or not str(value.get("name") or "").strip():
                raise ValueError(f"invalid research mapping for {theme_id}")
            mapped.append({
                "symbol": symbol, "name": str(value["name"]),
                "instrument_type": str(value.get("instrument_type") or "stock"),
                "association": "research_only",
            })
        themes.append({"id": theme_id, "name": name, "terms": terms, "mapped_symbols": mapped})
    if not themes:
        raise ValueError("theme catalog is empty")
    fingerprint = hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()[:16]
    return ThemeCatalog(f"{data.get('version', 'custom')}:{EXTRACTION_VERSION}:{fingerprint}", tuple(themes))


def _local_span(text: str, start: int, end: int, *, width: int = 100) -> tuple[int, int]:
    # Commas separate mixed-topic clauses as well as full stops. Preserve the
    # raw field and exact offsets so every excerpt can be independently checked.
    boundaries = "\n。！？!?；;，,"
    left = max((text.rfind(character, 0, start) + 1 for character in boundaries), default=0)
    stops = [text.find(character, end) for character in boundaries]
    right = min((value for value in stops if value >= 0), default=len(text))
    return max(left, start - width), min(right, end + width)


def _role(post: Mapping[str, Any], field: str, context: str) -> str:
    if post.get("is_aggregation") or post.get("post_type") == "aggregation":
        return "aggregation"
    if post.get("post_type") == "retweet":
        return "secondhand"
    if field == "article_title" and post.get("post_type") == "answer":
        return "quoted"
    if field == "quoted_text":
        return "quoted"
    if _SECONDHAND.search(context):
        return "secondhand"
    return "original"


def _has_past_date(context: str, post: Mapping[str, Any]) -> bool:
    """Use explicit earlier month/day mentions only to label a recap.

    No reconstructed dates are stored or used as a post/detection timestamp.
    """
    try:
        posted = datetime.fromisoformat(str(post.get("posted_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    for match in re.finditer(r"(?<!\d)(\d{1,2})(月|/)(\d{1,2})?(日|号|初|中旬|下旬|底)?", context):
        month = int(match.group(1))
        if match.group(2) == "/" and not re.search(
            r"日|号|在|开始|推荐|提示|提过|看好|布局|计划|发过|回顾|当时|早在|说",
            context[max(0, match.start() - 15):match.end() + 15],
        ):
            continue
        day = int(match.group(3)) if match.group(3) else {"初": 10, "中旬": 20}.get(match.group(4))
        if 1 <= month <= 12 and (
            month < posted.month or (month == posted.month and day is not None and 1 <= day < posted.day)
        ):
            return True
    return False


def _kind(context: str, source_role: str, post: Mapping[str, Any]) -> str:
    if source_role != "original":
        return "secondhand"
    if _WITHDRAWAL.search(context):
        return "retrospective" if _RETROSPECTIVE.search(context) or _has_past_date(context, post) else "analysis"
    if _PRODUCT.search(context) and not _FINANCE.search(context):
        return "product"
    # A forward-looking clause can coexist with another theme's recap.
    if _EXPLICIT_FORWARD.search(context):
        return "prospective"
    if _RETROSPECTIVE.search(context) or _has_past_date(context, post):
        return "retrospective"
    if _PROSPECTIVE.search(context):
        return "prospective"
    if _RECOMMENDATION.search(context):
        return "recommendation"
    return "analysis"


def theme_evidence(post: Mapping[str, Any], catalog: ThemeCatalog) -> list[dict[str, Any]]:
    raw = post.get("raw_payload")
    if not isinstance(raw, dict):
        try:
            raw_text = str(post.get("raw_json") or "{}")
            raw = json.loads(raw_text) if '"source_kind"' in raw_text else {}
        except (TypeError, ValueError):
            raw = {}
    if isinstance(raw, dict) and raw.get("source_kind") == "aggregation":
        post = {**post, "is_aggregation": True}
    body = str(post.get('article_text') or post.get('text') or '')
    header = body[:500]
    external_frame = bool(_RESEARCH_SUMMARY.search(header) or (
        re.match(r'\s*Q[：:]',header) and re.search(r'(?:^|\n)A[：:]',header)
        and re.search(r'行业专家.{0,10}梳理|纪要摘要',header)))
    news_recap = bool(re.search(r'A股收盘',header[:100]) and len(re.findall(r'(?:^|\n)[•●🔹]',header)) >= 3)
    fields = {field: (str(post.get(field) or ""), str(post.get(field) or "").casefold()) for field in SOURCE_FIELDS}
    leads: list[dict[str, Any]] = []
    for theme in catalog.themes:
        evidence: list[dict[str, Any]] = []
        claimed: list[str] = []
        for field in SOURCE_FIELDS:
            text, folded = fields[field]
            for rule in theme["terms"]:
                if rule["term"].casefold() not in folded:
                    continue
                pattern = re.escape(rule["term"])
                if rule["term"].isdigit():
                    pattern = rf"(?<!\d){pattern}(?!\d)"
                elif re.fullmatch(r'[A-Za-z][A-Za-z0-9+/-]*',rule['term']):
                    pattern = rf'(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])'
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    start, end = _local_span(text, match.start(), match.end())
                    if rule["require_any"]:
                        width = rule["context_chars"]
                        context = text[max(start, match.start() - width):min(end, match.end() + width)]
                        if not any(value.casefold() in context.casefold() for value in rule["require_any"]):
                            continue
                    tail = re.match(r"[^。\n！？!?；;]{0,200}", text[match.end():])
                    withdrawal = _WITHDRAWAL.search(tail[0]) if tail else None
                    if withdrawal:
                        position = match.end() + withdrawal.start()
                        left, right = _local_span(text, position, position + len(withdrawal[0]))
                        clause = text[left:right]
                        named = {item["id"] for item in catalog.themes if any(term["term"] in clause for term in item["terms"])}
                        if not named or theme["id"] in named:
                            end = max(end, right)
                    context = text[start:end]
                    # Attribution can precede a comma; keep the surrounding
                    # sentence without borrowing a different paragraph's author.
                    sentence_start = max((text.rfind(c,0,match.start())+1 for c in '\n。！？!?；;'),default=0)
                    attribution = text[max(sentence_start,match.start()-200):end]
                    role = _role(post, field, attribution)
                    if role == 'original' and external_frame:
                        role = 'secondhand'
                    if role == 'secondhand' and _SECONDHAND.search(attribution):
                        start = max(sentence_start,match.start()-200)
                        context = text[start:end]
                    kind = _kind(context, role, post)
                    evidence.append({
                        "field": field, "start": start, "end": end, "text": context,
                        "matched_term": text[match.start():match.end()],
                        "source_role": role, "kind": kind,
                        **({"context_role": "withdrawal"} if role == "original" and _WITHDRAWAL.search(context) else {}),
                        **({"context_role": "question"} if field == "article_title" and post.get("post_type") == "answer" else {}),
                    })
                    if _RETROSPECTIVE.search(context) or _has_past_date(context, post):
                        claimed.extend(value.group(0).strip() for value in _CLAIMED_TIMING.finditer(context))
        if not evidence:
            continue
        field_order = {name: index for index, name in enumerate(("text", "article_text", "ocr_text", "article_title", "quoted_text"))}
        evidence.sort(key=lambda value: (field_order[value["field"]], value["start"], value["end"], value["matched_term"]))
        # A quote is not the author's own evidence. Both remain visible in the
        # per-span roles when a post contains original and quoted discussion.
        own = [value for value in evidence if value["source_role"] == "original"]
        selected = own or evidence
        # The first substantive body statement provides the narrative frame.
        # A recap's later macroeconomic wishes cannot turn it into a new call.
        # An explicit future stock action remains visible as current discussion.
        framed = next((span for span in selected if span["kind"] != "analysis"), selected[0])
        if selected[0].get("context_role") == "withdrawal":
            framed = selected[0]
        kind = framed["kind"]
        explicit_future = any(_EXPLICIT_FORWARD.search(span["text"]) for span in own)
        recap_frame = any(re.search(r"直播(?:复盘|回顾|总结)|复盘总结", str(post.get(field) or "")[:100]) for field in ("text", "article_text"))
        if own and not explicit_future and (
            recap_frame or (post.get("evidence_type") == "retrospective" and kind in {"analysis", "prospective", "recommendation"})
        ):
            kind = "retrospective"
        if framed.get("context_role") != "withdrawal" and own and any(
            _EXPLICIT_FORWARD.search(span["text"]) and _STOCK_ACTION.search(span["text"])
            and _FINANCE.search(span["text"]) and span.get("context_role") != "withdrawal"
            for span in own
        ):
            kind = "prospective"
        if news_recap and kind in {'analysis','prospective','recommendation'}:
            kind = 'retrospective'
        source_role = "original" if own else selected[0]["source_role"]
        leads.append({
            "theme_id": theme["id"], "theme_name": theme["name"],
            "kind": kind, "source_role": source_role,
            "matched_terms": list(dict.fromkeys(value["matched_term"] for value in evidence)),
            "evidence": evidence,
            "evidence_text": "\n".join(dict.fromkeys(value["text"] for value in evidence)),
            "claimed_timing": list(dict.fromkeys(claimed)),
            "mapped_symbols": theme["mapped_symbols"],
        })
    return leads


@dataclass(frozen=True)
class ThemeExtractionSummary:
    processed_posts: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    failed: int = 0
    catalog_version: str = ""


def extract_theme_leads(
    store: KolPostStore, *, catalog: ThemeCatalog | None = None,
    post_ids: list[str] | None = None, batch_size: int = 500,
) -> ThemeExtractionSummary:
    catalog = catalog or load_theme_catalog(store=store)
    processed = created = updated = removed = failed = 0
    after = ""
    targets = set(post_ids) if post_ids is not None else None
    while True:
        posts = store.list_posts_for_theme_extraction(
            catalog.version, after_post_id=after, post_ids=targets, limit=batch_size,
        )
        if not posts:
            break
        values = []
        for post in posts:
            processed += 1
            try:
                values.append((post, theme_evidence(post, catalog)))
            except Exception as exc:
                failed += 1
                store.mark_theme_extraction_failed(post, catalog.version, str(exc))
        if values:
            result = store.replace_theme_leads_batch(values, catalog.version)
            created += result["created"]
            updated += result["updated"]
            removed += result["removed"]
            failed += result["failed"]
        after = str(posts[-1]["post_id"])
    return ThemeExtractionSummary(processed, created, updated, removed, failed, catalog.version)


def query_theme_leads(store: KolPostStore, **filters: Any) -> dict[str, Any]:
    catalog = load_theme_catalog(store=store)
    result = store.query_theme_leads(
        query_theme_ids=catalog.search_theme_ids(str(filters.get("query") or "")), **filters,
    )
    result["catalog_version"] = catalog.version
    result["coverage"] = {
        "scope": "local_inventory",
        "note": "最早时间指本地库存原帖；库内检出时间指真实提取时钟。记录的源采集时间可能被后续采集覆盖，不能证明当时已提醒。正文可能后编辑，需结合原文与修改时间核验。",
        "zhihu": "已接入回答、文章、想法采集；历史回答记录不能代表文章、想法覆盖。当前覆盖请看逐帖采集状态，缺少状态的历史完整性未验证。未命中仅表示本地库存未检出。",
    }
    return result
