"""Inventory and score an exported Twitter/X likes JSON file.

This script is intentionally conservative:
- it never writes to Notion;
- it never writes source notes into Obsidian;
- it only creates a staging JSONL plus a compact Markdown inventory report.

The goal is to turn a large raw export into a small review queue.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_INPUT_GLOB = r"<AI_HUB_HOME>\twitter-*-1781096618442.json"
DEFAULT_OUT_DIR = r"<AI_HUB_HOME>\ai-hub\_runtime\staging"
DEFAULT_OBSIDIAN_REPORT = (
    r"<OBSIDIAN_VAULT>\06_Logs\2026-07-02-twitter-likes-inventory.md"
)

STOCK_KEYWORDS = [
    "\u80a1\u7968",
    "A\u80a1",
    "\u6e2f\u80a1",
    "\u7f8e\u80a1",
    "\u9009\u80a1",
    "\u8d22\u62a5",
    "\u4f30\u503c",
    "\u57fa\u672c\u9762",
    "\u91cf\u5316",
    "\u4ea4\u6613",
    "\u6536\u76ca",
    "\u6da8\u505c",
    "\u725b\u5e02",
    "\u718a\u5e02",
    "\u505a\u591a",
    "\u505a\u7a7a",
    "\u4e70\u5165",
    "\u5356\u51fa",
    "\u4ed3\u4f4d",
    "\u56de\u64a4",
    "\u56e0\u5b50",
    "ROE",
    "ROIC",
    "PE",
    "PB",
    "\u5229\u6da6",
    "\u73b0\u91d1\u6d41",
    "\u62a4\u57ce\u6cb3",
    "\u6307\u6570",
    "\u677f\u5757",
]

AI_KEYWORDS = [
    "AI",
    "LLM",
    "Agent",
    "Codex",
    "Claude",
    "OpenAI",
    "\u6a21\u578b",
    "\u7b97\u529b",
    "\u82af\u7247",
]

KOL_SIGNAL_KEYWORDS = [
    "\u63a8\u8350",
    "\u89c2\u70b9",
    "\u770b\u591a",
    "\u770b\u7a7a",
    "\u903b\u8f91",
    "\u6807\u7684",
    "\u7ec4\u5408",
    "\u914d\u7f6e",
    "\u590d\u76d8",
    "\u7b56\u7565",
]

CASTAG_RE = re.compile(r"\$[A-Z][A-Z0-9]{0,5}\b")
CN_STOCK_CODE_RE = re.compile(r"(?<!\d)(?:[036]\d{5})(?!\d)")
URL_RE = re.compile(r"https?://[^\s]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None, help="Path to Twitter likes JSON.")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--obsidian-report", default=DEFAULT_OBSIDIAN_REPORT)
    parser.add_argument("--max-candidates", type=int, default=500)
    return parser.parse_args()


def resolve_input(path_arg: str | None) -> Path:
    if path_arg:
        path = Path(path_arg)
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    matches = sorted(Path(r"<AI_HUB_HOME>").glob("twitter-*-1781096618442.json"))
    if not matches:
        raise FileNotFoundError(DEFAULT_INPUT_GLOB)
    return matches[0]


def has_any(text: str, words: list[str]) -> bool:
    upper = text.upper()
    for word in words:
        if word.isascii():
            if word.upper() in upper:
                return True
        elif word in text:
            return True
    return False


def to_int(value: object) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def extract_host_counts(text: str) -> collections.Counter[str]:
    counts: collections.Counter[str] = collections.Counter()
    for raw_url in URL_RE.findall(text):
        host = urlparse(raw_url).netloc.lower()
        if host:
            counts[host] += 1
    return counts


def score_item(item: dict) -> tuple[int, list[str], list[str], list[str]]:
    text = str(item.get("full_text") or "")
    labels: list[str] = []
    score = 0

    if has_any(text, STOCK_KEYWORDS):
        labels.append("investment")
        score += 3
    if has_any(text, KOL_SIGNAL_KEYWORDS):
        labels.append("opinion_strategy")
        score += 2
    if has_any(text, AI_KEYWORDS):
        labels.append("ai_tools")
        score += 1

    cashtags = sorted(set(CASTAG_RE.findall(text.upper())))
    cn_codes = sorted(set(CN_STOCK_CODE_RE.findall(text)))
    if cashtags:
        labels.append("us_symbols")
        score += 3
    if cn_codes:
        labels.append("cn_stock_codes")
        score += 4

    views = to_int(item.get("views_count"))
    likes = to_int(item.get("favorite_count"))
    bookmarks = to_int(item.get("bookmark_count"))
    retweets = to_int(item.get("retweet_count"))
    engagement = likes + bookmarks * 2 + retweets * 2

    if views >= 100_000:
        score += 1
    if engagement >= 500:
        score += 1

    return score, sorted(set(labels)), cashtags, cn_codes


def compact_text(text: str, limit: int = 260) -> str:
    one_line = " ".join(str(text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "..."


def make_x_url(item: dict) -> str:
    if item.get("url"):
        return str(item["url"])
    screen = item.get("screen_name")
    tweet_id = item.get("id")
    if screen and tweet_id:
        return f"https://x.com/{screen}/status/{tweet_id}"
    return ""


def build_report(summary: dict, candidates: list[dict]) -> str:
    top_candidates = candidates[:20]
    lines = [
        "# Twitter Likes Inventory",
        "",
        "This report is a staging inventory. It does not mean these tweets have been promoted into the knowledge base.",
        "",
        "## Snapshot",
        "",
        f"- Source file: `{summary['source_file']}`",
        f"- Total tweets: {summary['total']}",
        f"- Date range: {summary['date_min']} to {summary['date_max']}",
        f"- Investment/trading related: {summary['term_counts'].get('investment', 0)}",
        f"- AI/tools related: {summary['term_counts'].get('ai_tools', 0)}",
        f"- Opinion/strategy language: {summary['term_counts'].get('opinion_strategy', 0)}",
        f"- Candidate queue written: `{summary['candidate_path']}`",
        "",
        "## Top Authors",
        "",
    ]
    for author, count in summary["top_authors"][:15]:
        lines.append(f"- {count}: {author}")

    lines.extend(["", "## Top Cashtags", ""])
    if summary["top_cashtags"]:
        for symbol, count in summary["top_cashtags"][:20]:
            lines.append(f"- {symbol}: {count}")
    else:
        lines.append("- None found.")

    lines.extend(["", "## Top A-share Codes", ""])
    if summary["top_cn_codes"]:
        for code, count in summary["top_cn_codes"][:20]:
            lines.append(f"- {code}: {count}")
    else:
        lines.append("- None found.")

    lines.extend(["", "## First Review Queue", ""])
    lines.append("| Score | Date | Author | Symbols | Text | URL |")
    lines.append("|---:|---|---|---|---|---|")
    for item in top_candidates:
        symbols = ", ".join(item["cashtags"] + item["cn_stock_codes"])
        text = item["text"].replace("|", "\\|")
        lines.append(
            f"| {item['score']} | {item['created_at'][:10]} | "
            f"{item['author'].replace('|', '/')} | {symbols} | {text} | {item['url']} |"
        )

    lines.extend(
        [
            "",
            "## Promotion Rule",
            "",
            "- Keep the raw JSON as the historical archive.",
            "- Promote only a reviewed item into `source-note-placeholder.md",
            "- Add a row to `04_Projects/KOL推荐事件表.md` only when the item has a clear KOL, asset, date, direction, reason, and original URL.",
            "- Create or update `03_Entities/` pages only for KOLs and companies that appear repeatedly or matter to a current decision.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    source_path = resolve_input(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with source_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        data = json.load(handle)

    authors: collections.Counter[str] = collections.Counter()
    hosts: collections.Counter[str] = collections.Counter()
    cashtags_counter: collections.Counter[str] = collections.Counter()
    cn_codes_counter: collections.Counter[str] = collections.Counter()
    term_counts: collections.Counter[str] = collections.Counter()
    dates: list[str] = []
    candidates: list[dict] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("full_text") or "")
        created_at = str(item.get("created_at") or "")
        if created_at:
            dates.append(created_at[:10])

        author = f"{item.get('name') or ''} (@{item.get('screen_name') or ''})".strip()
        if item.get("screen_name"):
            authors[author] += 1

        hosts.update(extract_host_counts(text))

        if has_any(text, STOCK_KEYWORDS):
            term_counts["investment"] += 1
        if has_any(text, AI_KEYWORDS):
            term_counts["ai_tools"] += 1
        if has_any(text, KOL_SIGNAL_KEYWORDS):
            term_counts["opinion_strategy"] += 1

        score, labels, cashtags, cn_codes = score_item(item)
        for symbol in cashtags:
            cashtags_counter[symbol] += 1
        for code in cn_codes:
            cn_codes_counter[code] += 1

        if score >= 5:
            candidates.append(
                {
                    "score": score,
                    "labels": labels,
                    "id": item.get("id"),
                    "created_at": created_at,
                    "author": author,
                    "screen_name": item.get("screen_name"),
                    "user_id": item.get("user_id"),
                    "url": make_x_url(item),
                    "cashtags": cashtags,
                    "cn_stock_codes": cn_codes,
                    "favorite_count": to_int(item.get("favorite_count")),
                    "retweet_count": to_int(item.get("retweet_count")),
                    "bookmark_count": to_int(item.get("bookmark_count")),
                    "views_count": to_int(item.get("views_count")),
                    "text": compact_text(text),
                }
            )

    candidates.sort(
        key=lambda item: (
            item["score"],
            item["views_count"],
            item["favorite_count"],
            item["created_at"],
        ),
        reverse=True,
    )
    candidates = candidates[: args.max_candidates]

    date_tag = dt.datetime.now().strftime("%Y%m%d")
    candidate_path = out_dir / f"twitter_likes_candidates_{date_tag}.jsonl"
    summary_path = out_dir / f"twitter_likes_summary_{date_tag}.json"

    with candidate_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in candidates:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary = {
        "source_file": str(source_path),
        "total": len(data),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "term_counts": dict(term_counts),
        "top_authors": authors.most_common(30),
        "top_hosts": hosts.most_common(30),
        "top_cashtags": cashtags_counter.most_common(30),
        "top_cn_codes": cn_codes_counter.most_common(30),
        "candidate_count": len(candidates),
        "candidate_path": str(candidate_path),
    }
    with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    report_path = Path(args.obsidian_report) if args.obsidian_report else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(build_report(summary, candidates), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "summary_path": str(summary_path),
                "candidate_path": str(candidate_path),
                "obsidian_report": str(report_path) if report_path else None,
                "candidate_count": len(candidates),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
