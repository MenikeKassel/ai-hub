"""Inventory a local Notion export and build a small review queue.

This script treats the Notion export as raw evidence. It does not write to
Notion and does not create source notes. Its only outputs are staging files
and a compact Obsidian report.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse


# 本地路径/ID 不再硬编码: 通过环境变量提供(缺失时在 main/find_info_csv 给出清晰错误)
DEFAULT_NOTION_ROOT = os.environ.get("NOTION_EXPORT_ROOT", "")
DEFAULT_OUT_DIR = os.environ.get("STAGING_DIR", "")
DEFAULT_REPORT = os.environ.get("OBSIDIAN_REPORT_PATH", "")
INFO_COLLECTION_ID = os.environ.get("NOTION_INFO_COLLECTION_ID", "")

CHINESE_KEYS = {
    "title": "\u65e5\u671f",
    "notion_date": "Notion\u65e5\u671f",
    "url": "URL",
    "id": "id",
    "text": "text",
    "author": "\u4f5c\u8005",
    "author_id": "\u4f5c\u8005ID",
    "author_username": "\u4f5c\u8005\u7528\u6237\u540d",
    "category": "\u4fe1\u606f\u7c7b\u522b",
    "created_at": "\u521b\u5efa\u65e5\u671f ",
    "likes": "\u559c\u6b22\u6570",
    "replies": "\u56de\u590d\u6570",
    "quotes": "\u5f15\u7528\u6570",
    "bookmarks": "\u6536\u85cf\u6570",
    "tag": "\u6807\u7b7e",
    "tag2": "\u6807\u7b7e 2",
    "views": "\u6d4f\u89c8\u6570",
    "language": "\u8bed\u8a00",
    "retweets": "\u8f6c\u63a8\u6570",
}

STOCK_KEYWORDS = [
    "\u80a1\u7968",
    "A\u80a1",
    "\u6e2f\u80a1",
    "\u7f8e\u80a1",
    "\u6295\u8d44",
    "\u91cf\u5316",
    "\u4ea4\u6613",
    "\u9009\u80a1",
    "\u8d22\u62a5",
    "\u4f30\u503c",
    "\u6536\u76ca",
    "\u4ed3\u4f4d",
    "\u56de\u64a4",
    "\u57fa\u91d1",
    "SP500",
    "NASDAQ",
    "ROE",
    "ROIC",
    "PE",
    "PB",
]

AI_KEYWORDS = [
    "AI",
    "LLM",
    "Agent",
    "Codex",
    "Claude",
    "OpenAI",
    "\u6a21\u578b",
    "\u5927\u6a21\u578b",
    "\u5de5\u5177",
    "Prompt",
]

KOL_KEYWORDS = [
    "\u63a8\u8350",
    "\u770b\u591a",
    "\u770b\u7a7a",
    "\u6807\u7684",
    "\u7b56\u7565",
    "\u903b\u8f91",
    "\u590d\u76d8",
    "\u7ec4\u5408",
]

HOST_LABELS = {
    "x.com": "X",
    "twitter.com": "X",
    "www.zhihu.com": "Zhihu",
    "zhuanlan.zhihu.com": "Zhihu",
    "mp.weixin.qq.com": "WeChat",
    "github.com": "GitHub",
    "www.xiaohongshu.com": "XHS",
    "www.douyin.com": "Douyin",
    "www.bilibili.com": "Bilibili",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notion-root", default=DEFAULT_NOTION_ROOT)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--obsidian-report", default=DEFAULT_REPORT)
    parser.add_argument("--max-candidates", type=int, default=500)
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


def get_cell(row: dict[str, str], key_name: str) -> str:
    return row.get(CHINESE_KEYS[key_name], "")


def has_any(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    for word in words:
        if word.lower() in lowered:
            return True
    return False


def to_int(value: str) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or "0"))
    except ValueError:
        return 0


def host_from_url(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if host.startswith("www.") and host not in HOST_LABELS:
        return host[4:]
    return host


def source_type_from_host(host: str) -> str:
    return HOST_LABELS.get(host, host or "unknown")


def compact(text: str, limit: int = 260) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def score_row(row: dict[str, str], host: str) -> tuple[int, list[str]]:
    title = get_cell(row, "title")
    text = get_cell(row, "text")
    tags = " ".join(
        [
            row.get("Tags", ""),
            get_cell(row, "tag"),
            get_cell(row, "tag2"),
            get_cell(row, "category"),
        ]
    )
    body = " ".join([title, text, get_cell(row, "url"), tags])
    labels: list[str] = []
    score = 0

    if has_any(body, STOCK_KEYWORDS):
        labels.append("investment")
        score += 4
    if has_any(body, KOL_KEYWORDS):
        labels.append("opinion_strategy")
        score += 2
    if has_any(body, AI_KEYWORDS):
        labels.append("ai_tools")
        score += 1
    if host in ("x.com", "twitter.com"):
        labels.append("x")
        score += 1
    if host in ("www.zhihu.com", "zhuanlan.zhihu.com"):
        labels.append("zhihu")
        score += 1
    if host == "mp.weixin.qq.com":
        labels.append("wechat")
        score += 1

    views = to_int(get_cell(row, "views"))
    likes = to_int(get_cell(row, "likes"))
    bookmarks = to_int(get_cell(row, "bookmarks"))
    retweets = to_int(get_cell(row, "retweets"))
    engagement = likes + bookmarks * 2 + retweets * 2
    if views >= 100_000:
        score += 1
    if engagement >= 500:
        score += 1

    return score, sorted(set(labels))


def build_report(summary: dict, candidates: list[dict]) -> str:
    lines = [
        "# Notion Export Inventory",
        "",
        "This is a staging report for the local Notion export. It is not a promoted knowledge page.",
        "",
        "## Snapshot",
        "",
        f"- Source CSV: `{summary['source_csv']}`",
        f"- Rows: {summary['rows']}",
        f"- Rows with URL: {summary['rows_with_url']}",
        f"- Candidate queue: `{summary['candidate_path']}`",
        "",
        "## Top Hosts",
        "",
    ]
    for host, count in summary["top_hosts"][:15]:
        lines.append(f"- {count}: {host}")

    lines.extend(["", "## Keyword Buckets", ""])
    for label, count in summary["keyword_counts"][:20]:
        lines.append(f"- {label}: {count}")

    lines.extend(["", "## Top Authors", ""])
    if summary["top_authors"]:
        for author, count in summary["top_authors"][:15]:
            lines.append(f"- {count}: {author}")
    else:
        lines.append("- No author data found.")

    lines.extend(["", "## First Review Queue", ""])
    lines.append("| Score | Source | Date | Author | Title/Text | URL |")
    lines.append("|---:|---|---|---|---|---|")
    for item in candidates[:20]:
        text = item["text"].replace("|", "\\|")
        author = item["author"].replace("|", "/")
        lines.append(
            f"| {item['score']} | {item['source_type']} | {item['date']} | "
            f"{author} | {text} | {item['url']} |"
        )

    lines.extend(
        [
            "",
            "## Use",
            "",
            "- Treat this export as a historical link library.",
            "- Use it to recover older Zhihu, WeChat, GitHub, X, XHS, Douyin, and Bilibili links.",
            "- Do not bulk import it into Obsidian.",
            "- Promote only reviewed items into source notes, entities, KOL events, or strategy notes.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    if not (args.notion_root and args.out_dir and args.obsidian_report):
        raise ValueError(
            "缺少必要路径: 请设置环境变量 NOTION_EXPORT_ROOT / STAGING_DIR / "
            "OBSIDIAN_REPORT_PATH, 或对应的 --* 命令行参数"
        )
    root = Path(args.notion_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source_csv = find_info_csv(root)

    rows = 0
    rows_with_url = 0
    hosts: collections.Counter[str] = collections.Counter()
    authors: collections.Counter[str] = collections.Counter()
    keyword_counts: collections.Counter[str] = collections.Counter()
    candidates: list[dict] = []

    with source_csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            row = {
                repair_mojibake(key): repair_mojibake(value)
                for key, value in raw_row.items()
                if key is not None
            }
            rows += 1
            url = get_cell(row, "url")
            host = host_from_url(url)
            if url:
                rows_with_url += 1
            if host:
                hosts[host] += 1
                keyword_counts[source_type_from_host(host)] += 1

            author = get_cell(row, "author") or get_cell(row, "author_username")
            if author:
                authors[author] += 1

            score, labels = score_row(row, host)
            for label in labels:
                keyword_counts[label] += 1

            if score >= 5:
                title = get_cell(row, "title")
                text = get_cell(row, "text") or title
                candidates.append(
                    {
                        "score": score,
                        "labels": labels,
                        "source_type": source_type_from_host(host),
                        "host": host,
                        "date": get_cell(row, "notion_date")
                        or get_cell(row, "created_at"),
                        "author": author,
                        "author_username": get_cell(row, "author_username"),
                        "url": url,
                        "id": get_cell(row, "id"),
                        "title": compact(title, 160),
                        "text": compact(text, 260),
                        "likes": to_int(get_cell(row, "likes")),
                        "bookmarks": to_int(get_cell(row, "bookmarks")),
                        "retweets": to_int(get_cell(row, "retweets")),
                        "views": to_int(get_cell(row, "views")),
                    }
                )

    candidates.sort(
        key=lambda item: (item["score"], item["views"], item["likes"], item["date"]),
        reverse=True,
    )
    candidates = candidates[: args.max_candidates]

    date_tag = dt.datetime.now().strftime("%Y%m%d")
    candidate_path = out_dir / f"notion_info_candidates_{date_tag}.jsonl"
    summary_path = out_dir / f"notion_info_summary_{date_tag}.json"

    with candidate_path.open("w", encoding="utf-8", newline="\n") as f:
        for item in candidates:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary = {
        "source_csv": str(source_csv),
        "rows": rows,
        "rows_with_url": rows_with_url,
        "candidate_count": len(candidates),
        "candidate_path": str(candidate_path),
        "top_hosts": hosts.most_common(30),
        "top_authors": authors.most_common(30),
        "keyword_counts": keyword_counts.most_common(30),
    }

    with summary_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

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
