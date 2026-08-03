"""Merge historical candidate queues from Notion export and Twitter likes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


DEFAULT_STAGING_DIR = r"<AI_HUB_HOME>\ai-hub\_runtime\staging"
DEFAULT_REPORT = r"<OBSIDIAN_VAULT>\06_Logs\2026-07-02-history-candidates-merged.md"

X_STATUS_RE = re.compile(
    r"(?:x|twitter)\.com/(?:i/web/)?(?P<user>[^/\s]+)/status/(?P<id>\d+)",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-dir", default=DEFAULT_STAGING_DIR)
    parser.add_argument("--obsidian-report", default=DEFAULT_REPORT)
    parser.add_argument("--max-items", type=int, default=800)
    return parser.parse_args()


def latest_file(staging_dir: Path, pattern: str) -> Path:
    files = sorted(staging_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
    if not files:
        raise FileNotFoundError(pattern)
    return files[-1]


def iter_jsonl(path: Path, source: str):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            item["_source_queue"] = source
            yield item


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


def item_key(item: dict) -> str:
    url_key = normalize_url(str(item.get("url") or ""))
    if url_key:
        return url_key
    source = item.get("_source_queue", "")
    item_id = item.get("id") or item.get("screen_name") or item.get("author")
    return f"{source}:{item_id}:{item.get('date') or item.get('created_at')}"


def item_score(item: dict) -> tuple:
    return (
        int(item.get("score") or 0),
        int(item.get("views") or item.get("views_count") or 0),
        int(item.get("likes") or item.get("favorite_count") or 0),
    )


def merge_items(items: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in items:
        key = item_key(item)
        if key not in merged:
            item["_dedupe_key"] = key
            item["_source_queues"] = [item["_source_queue"]]
            merged[key] = item
            continue
        current = merged[key]
        queues = set(current.get("_source_queues", []))
        queues.add(item["_source_queue"])
        if item_score(item) > item_score(current):
            item["_dedupe_key"] = key
            item["_source_queues"] = sorted(queues)
            merged[key] = item
        else:
            current["_source_queues"] = sorted(queues)
    result = list(merged.values())
    result.sort(key=item_score, reverse=True)
    return result


def display_date(item: dict) -> str:
    return str(item.get("date") or item.get("created_at") or "")[:20]


def display_author(item: dict) -> str:
    return str(item.get("author") or item.get("screen_name") or "")


def display_text(item: dict) -> str:
    text = str(item.get("text") or item.get("title") or "")
    text = " ".join(text.split())
    if len(text) > 220:
        return text[:219] + "..."
    return text


def build_report(summary: dict, items: list[dict]) -> str:
    lines = [
        "# Merged History Candidates",
        "",
        "This is the deduplicated review queue for historical Notion and Twitter/X material.",
        "",
        "## Snapshot",
        "",
        f"- Notion queue: `{summary['notion_queue']}`",
        f"- Twitter queue: `{summary['twitter_queue']}`",
        f"- Raw candidates: {summary['raw_count']}",
        f"- Deduplicated candidates: {summary['deduped_count']}",
        f"- Output queue: `{summary['merged_queue']}`",
        "",
        "## First Review Queue",
        "",
        "| Score | Sources | Date | Author | Text | URL |",
        "|---:|---|---|---|---|---|",
    ]
    for item in items[:30]:
        queues = ", ".join(item.get("_source_queues", [item.get("_source_queue", "")]))
        text = display_text(item).replace("|", "\\|")
        author = display_author(item).replace("|", "/")
        lines.append(
            f"| {item.get('score', '')} | {queues} | {display_date(item)} | "
            f"{author} | {text} | {item.get('url', '')} |"
        )
    lines.extend(
        [
            "",
            "## Rule",
            "",
            "- Review this queue first, not the individual raw exports.",
            "- Promote only items with a clear reusable role: source note, entity, KOL event, or strategy note.",
            "- Items appearing in both queues are stronger evidence of repeated interest, not evidence of correctness.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    staging_dir = Path(args.staging_dir)
    notion_path = latest_file(staging_dir, "notion_info_candidates_*.jsonl")
    twitter_path = latest_file(staging_dir, "twitter_likes_candidates_*.jsonl")

    all_items = list(iter_jsonl(notion_path, "notion_export")) + list(
        iter_jsonl(twitter_path, "twitter_likes")
    )
    merged = merge_items(all_items)[: args.max_items]

    date_tag = dt.datetime.now().strftime("%Y%m%d")
    merged_path = staging_dir / f"history_candidates_merged_{date_tag}.jsonl"
    summary_path = staging_dir / f"history_candidates_merged_{date_tag}.json"

    with merged_path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in merged:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary = {
        "notion_queue": str(notion_path),
        "twitter_queue": str(twitter_path),
        "raw_count": len(all_items),
        "deduped_count": len(merged),
        "merged_queue": str(merged_path),
    }
    with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    report_path = Path(args.obsidian_report) if args.obsidian_report else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(build_report(summary, merged), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "summary_path": str(summary_path),
                "merged_queue": str(merged_path),
                "obsidian_report": str(report_path) if report_path else None,
                "raw_count": len(all_items),
                "deduped_count": len(merged),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
