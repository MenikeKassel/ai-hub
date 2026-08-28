"""Build a full processing ledger for historical Notion and X favorites.

This script is the all-item layer of the Notion/X -> Obsidian workflow.
It does not bulk-promote everything into Obsidian. Instead, every raw item is:

1. normalized;
2. deduplicated;
3. classified by project, entropy, evidence level, and required action;
4. written to a durable JSONL ledger and action-specific queue files.

The Obsidian vault remains the compiled layer. The ledger is the audit trail
that proves every item has at least passed the same triage gate.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# 本地路径/ID 不再硬编码: 通过环境变量提供(缺失时在 main/find_info_csv 给出清晰错误)
DEFAULT_NOTION_ROOT = os.environ.get("NOTION_EXPORT_ROOT", "")
DEFAULT_TWITTER_INPUT = os.environ.get("TWITTER_FAVORITES_JSON", "")
DEFAULT_OUT_DIR = os.environ.get("STAGING_DIR", "")
DEFAULT_VAULT = os.environ.get("OBSIDIAN_VAULT", "")
DEFAULT_OBSIDIAN_REPORT = os.environ.get("OBSIDIAN_REPORT_PATH", "")
INFO_COLLECTION_ID = os.environ.get("NOTION_INFO_COLLECTION_ID", "")

CHINESE_KEYS = {
    "title": "日期",
    "notion_date": "Notion日期",
    "url": "URL",
    "id": "id",
    "text": "text",
    "author": "作者",
    "author_id": "作者ID",
    "author_username": "作者用户名",
    "category": "信息类别",
    "created_at": "创建日期 ",
    "likes": "喜欢数",
    "replies": "回复数",
    "quotes": "引用数",
    "bookmarks": "收藏数",
    "tag": "标签",
    "tag2": "标签 2",
    "views": "浏览数",
    "language": "语言",
    "retweets": "转推数",
}

HOST_LABELS = {
    "x.com": "X",
    "twitter.com": "X",
    "www.zhihu.com": "Zhihu",
    "zhuanlan.zhihu.com": "Zhihu",
    "mp.weixin.qq.com": "WeChat",
    "github.com": "GitHub",
    "gist.github.com": "GitHub",
    "www.xiaohongshu.com": "XHS",
    "www.douyin.com": "Douyin",
    "www.bilibili.com": "Bilibili",
    "b23.tv": "Bilibili",
}

INVESTMENT_WORDS = [
    "股票",
    "A股",
    "港股",
    "美股",
    "投资",
    "量化",
    "交易",
    "选股",
    "财报",
    "估值",
    "收益",
    "仓位",
    "回撤",
    "基金",
    "ETF",
    "SP500",
    "NASDAQ",
    "ROE",
    "ROIC",
    "PE",
    "PB",
    "做多",
    "做空",
    "买入",
    "卖出",
    "资产配置",
    "仓位配置",
]

AI_WORDS = [
    "AI",
    "LLM",
    "Agent",
    "Codex",
    "Claude",
    "OpenAI",
    "ChatGPT",
    "模型",
    "大模型",
    "工具",
    "Prompt",
    "RAG",
    "MCP",
]

KOL_SIGNAL_WORDS = [
    "推荐股票",
    "推荐A股",
    "推荐标的",
    "看多",
    "看空",
    "标的",
    "策略",
    "逻辑",
    "复盘",
    "组合",
    "喊单",
]

HIGH_ENTROPY_WORDS = [
    "方法论",
    "框架",
    "体系",
    "路线",
    "教程",
    "指南",
    "完整",
    "深度",
    "报告",
    "论文",
    "复盘",
    "Dashboard",
    "Skill",
    "Agent Skill",
    "GitHub",
    "开源",
    "脚本",
    "评分",
    "模型",
    "产业链",
    "供应链",
    "瓶颈",
    "稀缺",
    "证据",
]

LOW_VALUE_WORDS = [
    "抽奖",
    "优惠码",
    "邀请码",
    "广告",
    "转发",
    "求关注",
    "福利",
    "免费领取",
]

PUBLIC_IMPLEMENTATION_WORDS = [
    "github.com",
    "gist.github.com",
    "GitHub",
    "repo",
    "仓库",
    "开源",
    "Skill",
    "Dashboard",
    "脚本",
    "SQLite",
]

CASTAG_RE = re.compile(r"\$[A-Z][A-Z0-9]{0,5}\b")
CN_STOCK_CODE_RE = re.compile(r"(?<!\d)(?:[036]\d{5})(?!\d)")
X_STATUS_RE = re.compile(
    r"(?:x|twitter)\.com/(?:i/web/)?(?P<user>[^/\s]+)/status/(?P<id>\d+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
PUBLIC_SOURCE_URL_RE = re.compile(
    r"https?://(?:github\.com|gist\.github\.com|arxiv\.org|huggingface\.co|raw\.githubusercontent\.com)/",
    re.IGNORECASE,
)
SHORT_URL_RE = re.compile(r"https?://t\.co/\w+", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notion-root", default=DEFAULT_NOTION_ROOT)
    parser.add_argument("--twitter-input", default=DEFAULT_TWITTER_INPUT)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--obsidian-report", default=DEFAULT_OBSIDIAN_REPORT)
    parser.add_argument("--top-queue-size", type=int, default=300)
    return parser.parse_args()


def repair_mojibake(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    markers = ("æ", "ä", "å", "ç", "è", "é", "â", "Â", "ð")
    if not any(marker in value for marker in markers):
        return value
    current_cjk = sum("\u4e00" <= char <= "\u9fff" for char in value)
    for encoding in ("cp1252", "latin1"):
        try:
            fixed = value.encode(encoding, errors="strict").decode(
                "utf-8", errors="strict"
            )
        except Exception:
            continue
        if sum("\u4e00" <= char <= "\u9fff" for char in fixed) > current_cjk:
            return fixed
    return value


def get_cell(row: dict[str, str], key_name: str) -> str:
    return row.get(CHINESE_KEYS[key_name], "")


def find_info_csv(root: Path) -> Path:
    if not INFO_COLLECTION_ID:
        raise ValueError(
            "未配置 Notion 信息收集库 ID: 请设置环境变量 NOTION_INFO_COLLECTION_ID"
        )
    matches = [
        path
        for path in root.rglob(f"*{INFO_COLLECTION_ID}*_all.csv")
        if "official_export" not in str(path)
    ]
    if not matches:
        matches = list(root.rglob(f"*{INFO_COLLECTION_ID}*_all.csv"))
    if not matches:
        raise FileNotFoundError(f"*{INFO_COLLECTION_ID}*_all.csv")
    return max(matches, key=lambda path: path.stat().st_size)


def to_int(value: object) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def compact(text: str, limit: int = 500) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def has_token(text: str, word: str) -> bool:
    """Match Chinese terms by substring and ASCII tickers/abbreviations by token.

    This avoids treating words like "OpenViking" as a PE/PB investment signal.
    """
    if not word:
        return False
    if word.isascii():
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return word.lower() in text.lower()


def has_any(text: str, words: list[str]) -> bool:
    return any(has_token(text, word) for word in words)


def host_from_url(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if host.startswith("www.") and host not in HOST_LABELS:
        return host[4:]
    return host


def source_type_from_host(host: str) -> str:
    return HOST_LABELS.get(host, host or "unknown")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    match = X_STATUS_RE.search(url)
    if match:
        return f"x-status:{match.group('id')}"
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.lower()
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"s", "t"}
    ]
    cleaned = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        query=urlencode(query),
        fragment="",
    )
    return urlunparse(cleaned).rstrip("/")


def stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def x_url(screen_name: object, status_id: object, raw_url: object = "") -> str:
    if raw_url:
        return str(raw_url)
    if screen_name and status_id:
        return f"https://x.com/{screen_name}/status/{status_id}"
    return ""


def iter_notion_items(source_csv: Path):
    with source_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row_num, raw_row in enumerate(reader, start=2):
            row = {
                repair_mojibake(key): repair_mojibake(value)
                for key, value in raw_row.items()
                if key is not None
            }
            url = get_cell(row, "url")
            host = host_from_url(url)
            title = get_cell(row, "title")
            text = get_cell(row, "text") or title
            item = {
                "raw_sources": ["notion_export"],
                "raw_refs": [f"{source_csv}:{row_num}"],
                "source_type": source_type_from_host(host),
                "host": host,
                "date": get_cell(row, "notion_date") or get_cell(row, "created_at"),
                "author": get_cell(row, "author") or get_cell(row, "author_username"),
                "author_username": get_cell(row, "author_username"),
                "url": url,
                "id": get_cell(row, "id"),
                "title": compact(title, 180),
                "text": compact(text),
                "likes": to_int(get_cell(row, "likes")),
                "bookmarks": to_int(get_cell(row, "bookmarks")),
                "retweets": to_int(get_cell(row, "retweets")),
                "views": to_int(get_cell(row, "views")),
            }
            yield enrich_item(item)


def iter_twitter_items(source_path: Path):
    with source_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        data = json.load(handle)
    for index, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        screen_name = raw.get("screen_name")
        url = x_url(screen_name, raw.get("id"), raw.get("url"))
        author = f"{raw.get('name') or ''} (@{screen_name or ''})".strip()
        item = {
            "raw_sources": ["twitter_likes"],
            "raw_refs": [f"{source_path}#{index}"],
            "source_type": "X",
            "host": host_from_url(url),
            "date": str(raw.get("created_at") or ""),
            "author": author,
            "author_username": screen_name or "",
            "url": url,
            "id": raw.get("id"),
            "title": "",
            "text": compact(str(raw.get("full_text") or "")),
            "likes": to_int(raw.get("favorite_count")),
            "bookmarks": to_int(raw.get("bookmark_count")),
            "retweets": to_int(raw.get("retweet_count")),
            "views": to_int(raw.get("views_count")),
        }
        yield enrich_item(item)


def labels_for_item(item: dict) -> list[str]:
    body = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("text") or ""),
            str(item.get("url") or ""),
            str(item.get("source_type") or ""),
        ]
    )
    labels: set[str] = set()
    if has_any(body, INVESTMENT_WORDS):
        labels.add("investment")
    if has_any(body, AI_WORDS):
        labels.add("ai_tools")
    if has_any(body, KOL_SIGNAL_WORDS):
        labels.add("kol_signal")
    if has_any(body, HIGH_ENTROPY_WORDS):
        labels.add("high_entropy_signal")
    if has_any(body, PUBLIC_IMPLEMENTATION_WORDS):
        labels.add("public_implementation")
    if has_any(body, LOW_VALUE_WORDS):
        labels.add("possible_low_value")
    if item.get("source_type") == "GitHub":
        labels.add("public_implementation")
    if item.get("source_type") == "Zhihu":
        labels.add("zhihu")
    if item.get("source_type") == "X":
        labels.add("x")

    text = str(item.get("text") or "")
    if CASTAG_RE.search(text.upper()):
        labels.add("us_symbols")
    if CN_STOCK_CODE_RE.search(text):
        labels.add("cn_stock_codes")
    return sorted(labels)


def project_for_item(labels: list[str], item: dict) -> str:
    text = " ".join([str(item.get("text") or ""), str(item.get("url") or "")]).lower()
    label_set = set(labels)
    has_symbols = "cn_stock_codes" in label_set or "us_symbols" in label_set
    is_public_ai = "public_implementation" in label_set or "ai_tools" in label_set
    if "investment" in label_set or has_symbols:
        if "public_implementation" in label_set and not has_symbols:
            return "交易系统"
        if has_symbols and "kol_signal" in label_set:
            return "KOL指数"
        return "交易系统"
    if is_public_ai:
        if any(
            token in text
            for token in (
                "obsidian",
                "notion",
                "codex",
                "claude",
                "hermes",
                "rag",
                "memory",
                "agent",
                "skill",
                "prompt",
            )
        ):
            return "知识库系统"
        return "AI工具"
    if "zhihu" in label_set:
        return "知识库系统"
    return "暂不处理"


def symbols_for_item(item: dict) -> tuple[list[str], list[str]]:
    text = str(item.get("text") or "")
    us_symbols = sorted(set(CASTAG_RE.findall(text.upper())))
    cn_codes = sorted(set(CN_STOCK_CODE_RE.findall(text)))
    return us_symbols, cn_codes


def entropy_for_item(labels: list[str], item: dict) -> str:
    label_set = set(labels)
    text = str(item.get("text") or "")
    long_text = len(text) >= 360
    high = (
        "public_implementation" in label_set
        or "high_entropy_signal" in label_set
        or item.get("source_type") in {"GitHub", "WeChat", "Zhihu"}
        or long_text
    )
    actionable = (
        ("cn_stock_codes" in label_set or "us_symbols" in label_set)
        and ("kol_signal" in label_set or "investment" in label_set)
    )
    if actionable:
        return "actionable"
    if high:
        return "high"
    if "possible_low_value" in label_set or not text.strip():
        return "low"
    if "investment" in label_set or "ai_tools" in label_set:
        return "medium"
    return "low"


def evidence_for_item(item: dict) -> str:
    source_type = item.get("source_type")
    text = str(item.get("text") or "")
    if source_type == "GitHub":
        return "implementation"
    if has_any(text, ["公告", "财报", "年报", "季报", "订单", "合同", "客户认证", "专利", "监管"]):
        return "claims_strong_evidence"
    if source_type in {"WeChat", "Zhihu", "Bilibili"}:
        return "medium_secondary"
    if source_type == "X":
        return "weak_social"
    return "unknown"


def score_for_item(item: dict, labels: list[str], entropy: str) -> int:
    score = 0
    label_set = set(labels)
    if "investment" in label_set:
        score += 4
    if "kol_signal" in label_set:
        score += 2
    if "ai_tools" in label_set:
        score += 1
    if "public_implementation" in label_set:
        score += 4
    if "cn_stock_codes" in label_set:
        score += 4
    if "us_symbols" in label_set:
        score += 3
    if entropy == "high":
        score += 2
    if entropy == "actionable":
        score += 3
    if to_int(item.get("views")) >= 100_000:
        score += 1
    engagement = to_int(item.get("likes")) + to_int(item.get("bookmarks")) * 2 + to_int(item.get("retweets")) * 2
    if engagement >= 500:
        score += 1
    if "possible_low_value" in label_set:
        score -= 3
    return max(score, 0)


def workflow_action(entropy: str, labels: list[str], already_promoted: bool) -> str:
    if already_promoted:
        return "already_promoted"
    label_set = set(labels)
    if entropy == "actionable":
        if "kol_signal" in label_set:
            return "kol_event_candidate"
        return "source_note_needed"
    if entropy == "high":
        return "llm_wiki_needed"
    if entropy == "medium":
        return "source_note_needed"
    return "raw_index_only"


def needs_source_resolution(item: dict) -> bool:
    text = " ".join([str(item.get("text") or ""), str(item.get("url") or "")])
    labels = set(item.get("labels", []))
    if item.get("source_type") != "X":
        return False
    if "public_implementation" not in labels and "high_entropy_signal" not in labels:
        return False
    if PUBLIC_SOURCE_URL_RE.search(text):
        return False
    return SHORT_URL_RE.search(text) is not None


def enrich_item(item: dict) -> dict:
    labels = labels_for_item(item)
    us_symbols, cn_codes = symbols_for_item(item)
    entropy = entropy_for_item(labels, item)
    project = project_for_item(labels, item)
    evidence = evidence_for_item(item)
    score = score_for_item(item, labels, entropy)
    item.update(
        {
            "labels": labels,
            "us_symbols": us_symbols,
            "cn_stock_codes": cn_codes,
            "entropy": entropy,
            "project": project,
            "evidence_level": evidence,
            "score": score,
        }
    )
    return item


def dedupe_key(item: dict) -> str:
    normalized = normalize_url(str(item.get("url") or ""))
    if normalized:
        return normalized
    fallback = "|".join(
        [
            str(item.get("source_type") or ""),
            str(item.get("author_username") or item.get("author") or ""),
            str(item.get("date") or ""),
            str(item.get("text") or "")[:120],
        ]
    )
    return f"text:{stable_id(fallback)}"


def merge_duplicate(current: dict, new: dict) -> dict:
    merged = dict(current)
    merged["raw_sources"] = sorted(set(current.get("raw_sources", [])) | set(new.get("raw_sources", [])))
    merged["raw_refs"] = list(current.get("raw_refs", [])) + list(new.get("raw_refs", []))

    if (new.get("score") or 0) > (current.get("score") or 0):
        keep_keys = [
            "source_type",
            "host",
            "date",
            "author",
            "author_username",
            "url",
            "id",
            "title",
            "text",
            "likes",
            "bookmarks",
            "retweets",
            "views",
            "labels",
            "us_symbols",
            "cn_stock_codes",
            "entropy",
            "project",
            "evidence_level",
            "score",
        ]
        for key in keep_keys:
            merged[key] = new.get(key)
    else:
        merged["labels"] = sorted(set(current.get("labels", [])) | set(new.get("labels", [])))
        merged["us_symbols"] = sorted(set(current.get("us_symbols", [])) | set(new.get("us_symbols", [])))
        merged["cn_stock_codes"] = sorted(set(current.get("cn_stock_codes", [])) | set(new.get("cn_stock_codes", [])))
    return merged


def scan_existing_promotions(vault: Path) -> set[str]:
    promoted: set[str] = set()
    if not vault.exists():
        return promoted
    promoted_roots = {
        "01_Sources",
        "02_Concepts",
        "03_Entities",
        "04_Projects",
        "05_Strategies",
        "wiki",
    }
    for path in vault.rglob("*.md"):
        try:
            relative = path.relative_to(vault)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] not in promoted_roots:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for url in URL_RE.findall(text):
            key = normalize_url(url)
            if key:
                promoted.add(key)
        for match in re.finditer(r"x-status:(\d+)", text):
            promoted.add(f"x-status:{match.group(1)}")
    return promoted


def sort_key(item: dict) -> tuple[int, int, int, str]:
    return (
        int(item.get("score") or 0),
        int(item.get("views") or 0),
        int(item.get("likes") or 0),
        str(item.get("date") or ""),
    )


def build_report(summary: dict, top_items: list[dict]) -> str:
    lines = [
        "# Notion + X 全量处理台账",
        "",
        "这个文件是处理账本，不是知识正文。目标是让 Notion 信息收集库和 X 喜欢导出中的每一条，都至少经过同一套分流规则。",
        "",
        "## 输入",
        "",
        f"- Notion CSV: `{summary['notion_csv']}`",
        f"- X likes JSON: `{summary['twitter_input']}`",
        "",
        "## 全量结果",
        "",
        f"- Raw items: {summary['raw_items']}",
        f"- Deduped items: {summary['deduped_items']}",
        f"- Already promoted in Obsidian: {summary['already_promoted']}",
        f"- Ledger: `{summary['ledger_path']}`",
        "",
        "## 按动作分流",
        "",
    ]
    for action, count in summary["actions"]:
        queue_path = summary["queue_paths"].get(action)
        suffix = f" -> `{queue_path}`" if queue_path else ""
        lines.append(f"- {action}: {count}{suffix}")

    lines.extend(["", "## 按信息熵分流", ""])
    for entropy, count in summary["entropy"]:
        lines.append(f"- {entropy}: {count}")

    lines.extend(["", "## 按项目分流", ""])
    for project, count in summary["projects"]:
        lines.append(f"- {project}: {count}")

    lines.extend(
        [
            "",
            "## 下一批优先处理",
            "",
            "| Score | Action | Entropy | Project | Source | Date | Author | Symbols | Text | URL |",
            "|---:|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in top_items[:30]:
        symbols = ", ".join(item.get("cn_stock_codes", []) + item.get("us_symbols", []))
        text = str(item.get("text") or item.get("title") or "").replace("|", "\\|")
        if len(text) > 160:
            text = text[:159] + "..."
        lines.append(
            f"| {item.get('score', '')} | {item.get('workflow_action', '')} | "
            f"{item.get('entropy', '')} | {item.get('project', '')} | "
            f"{item.get('source_type', '')} | {str(item.get('date', ''))[:16]} | "
            f"{str(item.get('author', '')).replace('|', '/')} | {symbols} | {text} | {item.get('url', '')} |"
        )

    lines.extend(
        [
            "",
            "## 固定规则",
            "",
            "- `raw_index_only`：只保留在台账，不进入 Obsidian 正文。",
            "- `source_note_needed`：写 `01_Sources/`，必要时连到项目页。",
            "- `source_resolution_needed`：只有 X/t.co 线索，必须先解析公开源；未确认前不写 Wiki。",
            "- `llm_wiki_needed`：必须走 `01_Sources -> wiki/summaries -> wiki/concepts/entities -> wiki/synthesis -> wiki/index`。",
            "- `kol_event_candidate`：先进入来源页或事件候选，只有满足 KOL、标的、方向、理由、日期、原始链接六要素，才写入 KOL 推荐事件表。",
            "- `already_promoted`：已在 Obsidian 有链接或来源记录，不重复写。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_jsonl(path: Path, items: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()
    if not (
        args.notion_root
        and args.twitter_input
        and args.out_dir
        and args.vault
        and args.obsidian_report
    ):
        raise ValueError(
            "缺少必要路径: 请设置环境变量 NOTION_EXPORT_ROOT / TWITTER_FAVORITES_JSON / "
            "STAGING_DIR / OBSIDIAN_VAULT / OBSIDIAN_REPORT_PATH, 或对应的 --* 命令行参数"
        )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    vault = Path(args.vault)
    notion_csv = find_info_csv(Path(args.notion_root))
    twitter_input = Path(args.twitter_input)
    if not twitter_input.exists():
        raise FileNotFoundError(twitter_input)

    raw_items = list(iter_notion_items(notion_csv)) + list(iter_twitter_items(twitter_input))
    promoted_urls = scan_existing_promotions(vault)

    merged: dict[str, dict] = {}
    for item in raw_items:
        key = dedupe_key(item)
        if key in merged:
            merged[key] = merge_duplicate(merged[key], item)
        else:
            item["_dedupe_key"] = key
            merged[key] = item

    processed_at = dt.datetime.now().isoformat(timespec="seconds")
    deduped_items: list[dict] = []
    for index, item in enumerate(merged.values(), start=1):
        key = item["_dedupe_key"]
        already = key in promoted_urls or normalize_url(str(item.get("url") or "")) in promoted_urls
        action = workflow_action(item["entropy"], item["labels"], already)
        if action == "llm_wiki_needed" and needs_source_resolution(item):
            action = "source_resolution_needed"
        item["workflow_action"] = action
        item["workflow_status"] = "triaged"
        item["ledger_id"] = f"hist-{stable_id(key)}"
        item["processed_at"] = processed_at
        item["ledger_index"] = index
        deduped_items.append(item)

    deduped_items.sort(key=sort_key, reverse=True)

    date_tag = dt.datetime.now().strftime("%Y%m%d")
    ledger_path = out_dir / f"history_item_ledger_{date_tag}.jsonl"
    write_jsonl(ledger_path, deduped_items)

    queues: dict[str, list[dict]] = collections.defaultdict(list)
    for item in deduped_items:
        queues[item["workflow_action"]].append(item)

    queue_paths: dict[str, str] = {}
    for action, items in sorted(queues.items()):
        queue_path = out_dir / f"history_queue_{action}_{date_tag}.jsonl"
        write_jsonl(queue_path, items[: args.top_queue_size] if action != "raw_index_only" else items)
        queue_paths[action] = str(queue_path)

    action_counts = collections.Counter(item["workflow_action"] for item in deduped_items)
    entropy_counts = collections.Counter(item["entropy"] for item in deduped_items)
    project_counts = collections.Counter(item["project"] for item in deduped_items)
    source_counts = collections.Counter(item["source_type"] for item in deduped_items)

    top_next = [
        item
        for item in deduped_items
        if item["workflow_action"] not in {"raw_index_only", "already_promoted"}
    ]
    top_next.sort(key=sort_key, reverse=True)

    summary = {
        "notion_csv": str(notion_csv),
        "twitter_input": str(twitter_input),
        "raw_items": len(raw_items),
        "deduped_items": len(deduped_items),
        "already_promoted": action_counts.get("already_promoted", 0),
        "ledger_path": str(ledger_path),
        "queue_paths": queue_paths,
        "actions": action_counts.most_common(),
        "entropy": entropy_counts.most_common(),
        "projects": project_counts.most_common(),
        "sources": source_counts.most_common(),
        "generated_at": processed_at,
    }
    summary_path = out_dir / f"history_item_ledger_summary_{date_tag}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report_path = Path(args.obsidian_report) if args.obsidian_report else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(build_report(summary, top_next), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "raw_items": len(raw_items),
                "deduped_items": len(deduped_items),
                "ledger_path": str(ledger_path),
                "summary_path": str(summary_path),
                "obsidian_report": str(report_path) if report_path else None,
                "actions": action_counts.most_common(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
