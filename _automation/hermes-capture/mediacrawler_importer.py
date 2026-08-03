from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from capture_pipeline import classify_project, load_config
from mediacrawler_runner import default_queue_file, default_save_path
from notion_client import NotionClient, NotionError, load_notion_key
from obsidian_writer import ensure_vault_dirs, slugify, yaml_value


PLATFORM_SOURCE_TYPES = {
    "xhs": "小红书",
    "douyin": "抖音",
    "dy": "抖音",
    "bili": "B站",
    "zhihu": "知乎",
}

CONTENT_KINDS = {"contents", "videos", "dynamics"}
COMMENT_KINDS = {"comments"}
SUPPORTED_SUFFIXES = {".jsonl", ".json", ".csv"}


@dataclass
class MediaCrawlerRecord:
    path: Path
    platform: str
    kind: str
    row: dict[str, Any]


@dataclass
class MediaCrawlerBundle:
    platform: str
    content_id: str
    source_url: str = ""
    title: str = ""
    author: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    files: set[Path] = field(default_factory=set)


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Import MediaCrawler output files into Obsidian and optionally Notion.")
    parser.add_argument("--output-root", default="", help="MediaCrawler output root. Defaults to _runtime/mediacrawler.")
    parser.add_argument("--queue-file", default="", help="Queue JSONL path. Defaults to _runtime/mediacrawler/queue.jsonl.")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument("--json", action="store_true", help="Print JSON output.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan importable MediaCrawler bundles.")
    scan_parser.add_argument("--platform", default="", choices=["", "xhs", "douyin", "bili", "zhihu"])
    scan_parser.add_argument("--job-id", default="", help="Use queue job platform and requested URLs as hints.")

    import_parser = subparsers.add_parser("import", help="Write Obsidian notes, optionally updating Notion.")
    import_parser.add_argument("--platform", default="", choices=["", "xhs", "douyin", "bili", "zhihu"])
    import_parser.add_argument("--job-id", default="", help="Use queue job platform and requested URLs as hints.")
    import_parser.add_argument("--notion", action="store_true", help="Create/update Notion pages too.")
    import_parser.add_argument("--limit", type=int, default=0, help="Maximum bundles to import. 0 means all.")

    args = parser.parse_args()
    output_root = Path(args.output_root) if args.output_root else default_save_path()
    output_root = output_root.resolve()
    queue_file = Path(args.queue_file) if args.queue_file else default_queue_file()
    queue_file = queue_file.resolve()
    config_path = Path(args.config)

    try:
        if args.command == "scan":
            result = scan_command(output_root, queue_file, args.platform, args.job_id)
        elif args.command == "import":
            result = import_command(
                output_root=output_root,
                queue_file=queue_file,
                config_path=config_path,
                platform=args.platform,
                job_id=args.job_id,
                write_notion=args.notion,
                limit=args.limit,
            )
        else:
            result = {"ok": False, "error": f"unsupported command: {args.command}"}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0 if result.get("ok") else 1


def configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def scan_command(output_root: Path, queue_file: Path, platform: str, job_id: str) -> dict[str, Any]:
    job = read_queue_job(queue_file, job_id) if job_id else {}
    platform_hint = platform or normalize_platform(job.get("platform", ""))
    requested_urls = requested_urls_from_job(job)
    bundles = collect_bundles(output_root, platform_hint=platform_hint, requested_urls=requested_urls)
    return {
        "ok": True,
        "output_root": str(output_root),
        "queue_file": str(queue_file),
        "job_id": job_id,
        "count": len(bundles),
        "bundles": [bundle_summary(bundle) for bundle in bundles],
    }


def import_command(
    *,
    output_root: Path,
    queue_file: Path,
    config_path: Path,
    platform: str,
    job_id: str,
    write_notion: bool,
    limit: int,
) -> dict[str, Any]:
    job = read_queue_job(queue_file, job_id) if job_id else {}
    platform_hint = platform or normalize_platform(job.get("platform", ""))
    requested_urls = requested_urls_from_job(job)
    bundles = collect_bundles(output_root, platform_hint=platform_hint, requested_urls=requested_urls)
    if limit > 0:
        bundles = bundles[:limit]

    config = load_config(config_path)
    notion = build_notion_client(config) if write_notion else None
    imported: list[dict[str, Any]] = []
    for bundle in bundles:
        obsidian_path = write_bundle_markdown(bundle, config, job_id=job_id)
        notion_url = ""
        if notion:
            item = bundle_to_capture_item(bundle, job_id=job_id)
            page = notion.find_by_url(item.get("url"))
            if page:
                notion_url = page.get("url", "")
                notion.update_capture_page(page["id"], item)
                notion.replace_page_blocks(page["id"], item)
                notion.update_obsidian_path(page["id"], obsidian_path)
            else:
                created = notion.create_capture_page(item)
                notion_url = created.get("url", "")
                notion.update_obsidian_path(created["id"], obsidian_path)
        imported.append(
            {
                **bundle_summary(bundle),
                "obsidian_path": obsidian_path,
                "notion_url": notion_url,
            }
        )

    return {
        "ok": True,
        "output_root": str(output_root),
        "queue_file": str(queue_file),
        "job_id": job_id,
        "write_notion": write_notion,
        "count": len(imported),
        "imported": imported,
    }


def build_notion_client(config: dict[str, Any]) -> NotionClient:
    env_files = [part for part in str(config["notion_env_files"]).split(";") if part]
    notion_key = load_notion_key(env_files)
    field_config = {
        key.removeprefix("field_"): str(value)
        for key, value in config.items()
        if key.startswith("field_") and str(value)
    }
    notion = NotionClient(
        notion_key,
        str(config["notion_database_id"]),
        str(config.get("notion_version", "2022-06-28")),
        fields=field_config,
    )
    notion.ensure_minimum_schema()
    return notion


def collect_bundles(output_root: Path, *, platform_hint: str = "", requested_urls: list[str] | None = None) -> list[MediaCrawlerBundle]:
    records = list(iter_records(output_root, platform_hint=platform_hint))
    requested_urls = requested_urls or []
    bundles: dict[tuple[str, str], MediaCrawlerBundle] = {}
    orphan_comments: list[MediaCrawlerRecord] = []

    for record in records:
        if record.kind in CONTENT_KINDS:
            content_id = content_id_for(record.platform, record.row)
            if not content_id:
                continue
            key = (record.platform, content_id)
            bundle = bundles.setdefault(key, MediaCrawlerBundle(platform=record.platform, content_id=content_id))
            bundle.content = record.row
            bundle.source_url = source_url_for(record.platform, record.row) or url_hint_for(record.platform, requested_urls)
            bundle.title = title_for(record.platform, record.row, content_id)
            bundle.author = author_for(record.platform, record.row)
            bundle.files.add(record.path)
        elif record.kind in COMMENT_KINDS:
            parent_id = parent_content_id_for(record.platform, record.row)
            if not parent_id:
                orphan_comments.append(record)
                continue
            key = (record.platform, parent_id)
            bundle = bundles.setdefault(key, MediaCrawlerBundle(platform=record.platform, content_id=parent_id))
            bundle.comments.append(record.row)
            bundle.files.add(record.path)

    for record in orphan_comments:
        parent_id = parent_content_id_for(record.platform, record.row) or record.row.get("comment_id") or "orphan"
        key = (record.platform, str(parent_id))
        bundle = bundles.setdefault(key, MediaCrawlerBundle(platform=record.platform, content_id=str(parent_id)))
        bundle.comments.append(record.row)
        bundle.files.add(record.path)

    for bundle in bundles.values():
        if not bundle.source_url:
            bundle.source_url = url_hint_for(bundle.platform, requested_urls)
        if not bundle.title:
            bundle.title = title_for(bundle.platform, bundle.content, bundle.content_id)
        if not bundle.author:
            bundle.author = author_for(bundle.platform, bundle.content)

    return sorted(
        bundles.values(),
        key=lambda bundle: (bundle.platform, bundle.title.lower(), bundle.content_id),
    )


def iter_records(output_root: Path, *, platform_hint: str = ""):
    if not output_root.exists():
        return
    for path in sorted(output_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        platform = platform_hint or platform_from_path(output_root, path)
        platform = normalize_platform(platform)
        if not platform:
            continue
        kind = kind_from_path(path)
        if not kind:
            continue
        for row in read_rows(path):
            if isinstance(row, dict):
                yield MediaCrawlerRecord(path=path, platform=platform, kind=kind, row=row)


def read_rows(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row
        return
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    yield row
        elif isinstance(data, dict):
            yield data
        return
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)


def platform_from_path(output_root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(output_root)
    except ValueError:
        return ""
    for part in rel.parts:
        platform = normalize_platform(part)
        if platform:
            return platform
    return ""


def normalize_platform(value: str) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "xhs": "xhs",
        "xiaohongshu": "xhs",
        "rednote": "xhs",
        "douyin": "douyin",
        "dy": "douyin",
        "bili": "bili",
        "bilibili": "bili",
        "zhihu": "zhihu",
    }
    return aliases.get(raw, "")


def kind_from_path(path: Path) -> str:
    name = path.stem.lower()
    for kind in sorted(CONTENT_KINDS | COMMENT_KINDS | {"creators", "contacts"}, key=len, reverse=True):
        if f"_{kind}_" in name or name.endswith(f"_{kind}"):
            return kind
    parent = path.parent.name.lower()
    return parent if parent in CONTENT_KINDS | COMMENT_KINDS else ""


def content_id_for(platform: str, row: dict[str, Any]) -> str:
    keys = {
        "xhs": ["note_id"],
        "douyin": ["aweme_id"],
        "bili": ["video_id", "dynamic_id"],
        "zhihu": ["content_id", "answer_id", "article_id", "question_id", "note_id"],
    }.get(platform, [])
    return first_value(row, keys + ["id"])


def parent_content_id_for(platform: str, row: dict[str, Any]) -> str:
    keys = {
        "xhs": ["note_id"],
        "douyin": ["aweme_id"],
        "bili": ["video_id", "dynamic_id"],
        "zhihu": ["content_id", "answer_id", "article_id", "question_id", "note_id"],
    }.get(platform, [])
    return first_value(row, keys)


def source_url_for(platform: str, row: dict[str, Any]) -> str:
    return first_value(
        row,
        [
            "note_url",
            "aweme_url",
            "video_url",
            "content_url",
            "url",
            "source_url",
        ],
    )


def title_for(platform: str, row: dict[str, Any], content_id: str) -> str:
    title = first_value(row, ["title", "desc", "text", "content"])
    if title:
        return one_line(title)[:120]
    source_type = PLATFORM_SOURCE_TYPES.get(platform, platform)
    return f"{source_type} - {content_id}"


def author_for(platform: str, row: dict[str, Any]) -> str:
    return first_value(row, ["nickname", "user_nickname", "user_name", "author", "creator_hash"])


def first_value(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def url_hint_for(platform: str, requested_urls: list[str]) -> str:
    source_type = PLATFORM_SOURCE_TYPES.get(platform, "")
    for url in requested_urls:
        lower = url.lower()
        if platform == "xhs" and ("xiaohongshu.com" in lower or "xhslink.com" in lower):
            return url
        if platform == "douyin" and "douyin.com" in lower:
            return url
        if platform == "bili" and ("bilibili.com" in lower or "b23.tv" in lower):
            return url
        if platform == "zhihu" and "zhihu.com" in lower:
            return url
    return requested_urls[0] if requested_urls and source_type else ""


def read_queue_job(queue_file: Path, job_id: str) -> dict[str, Any]:
    if not job_id:
        return {}
    if not queue_file.exists():
        raise RuntimeError(f"Queue file not found: {queue_file}")
    for line in queue_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job.get("job_id") == job_id:
            return job
    raise RuntimeError(f"MediaCrawler job not found: {job_id}")


def requested_urls_from_job(job: dict[str, Any]) -> list[str]:
    command = job.get("command") or []
    urls: list[str] = []
    for index, part in enumerate(command):
        if part == "--specified_id" and index + 1 < len(command):
            urls.extend(item.strip() for item in str(command[index + 1]).split(",") if item.strip())
    return urls


def bundle_to_capture_item(bundle: MediaCrawlerBundle, *, job_id: str = "") -> dict[str, Any]:
    source_type = PLATFORM_SOURCE_TYPES.get(bundle.platform, bundle.platform)
    content = bundle_content_text(bundle)
    summary = one_line(content)[:500]
    text_for_project = " ".join([bundle.source_url, bundle.title, bundle.author, content])
    return {
        "command": "clip",
        "title": bundle.title,
        "url": bundle.source_url,
        "note": f"Imported from MediaCrawler job {job_id}".strip(),
        "content": content,
        "summary": summary or f"MediaCrawler imported {source_type} item.",
        "fetch_ok": True,
        "fetch_method": "mediacrawler",
        "fetch_error": "",
        "author": bundle.author,
        "author_username": "",
        "source_type": source_type,
        "project": classify_project(text_for_project, source_type),
        "status": "Inbox",
        "value_score": 3,
        "info_type": "知识型信息",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "heavy_backend": "MediaCrawler",
        "heavy_backend_status": "imported",
        "heavy_backend_job_id": job_id,
    }


def write_bundle_markdown(bundle: MediaCrawlerBundle, config: dict[str, Any], *, job_id: str = "") -> str:
    vault = Path(config["vault_path"]).resolve()
    ensure_vault_dirs(str(vault))
    target_dir = vault / "01_Sources" / "MediaCrawler" / bundle.platform
    target_dir.mkdir(parents=True, exist_ok=True)
    date_prefix = datetime.now().strftime("%Y-%m-%d")
    slug = slugify(bundle.title or bundle.content_id)
    path = target_dir / f"{date_prefix}__{bundle.platform}__{bundle.content_id}__{slug}.md"
    path.write_text(bundle_markdown(bundle, job_id=job_id), encoding="utf-8")
    return path.relative_to(vault).as_posix()


def bundle_markdown(bundle: MediaCrawlerBundle, *, job_id: str = "") -> str:
    imported_at = datetime.now().isoformat(timespec="seconds")
    source_type = PLATFORM_SOURCE_TYPES.get(bundle.platform, bundle.platform)
    frontmatter = {
        "type": "media-crawler-source",
        "status": "imported",
        "source_url": bundle.source_url,
        "source_type": source_type,
        "platform": bundle.platform,
        "content_id": bundle.content_id,
        "title": bundle.title,
        "author": bundle.author,
        "imported_at": imported_at,
        "heavy_backend": "MediaCrawler",
        "heavy_backend_status": "imported",
        "heavy_backend_job_id": job_id,
        "comment_count": len(bundle.comments),
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {yaml_value(value)}")
    lines.extend(
        [
            "---",
            "",
            f"# {bundle.title}",
            "",
            "## Source",
            f"- Platform: {source_type}",
            f"- Content ID: {bundle.content_id}",
            f"- URL: {bundle.source_url or 'unknown'}",
            f"- Author: {bundle.author or 'unknown'}",
            f"- MediaCrawler job: {job_id or 'unknown'}",
            "",
            "## Content",
            bundle_content_text(bundle) or "No content text was found in the MediaCrawler output.",
            "",
            "## Comments",
            comments_markdown(bundle.comments),
            "",
            "## Raw Files",
        ]
    )
    lines.extend(f"- {path}" for path in sorted(str(path) for path in bundle.files))
    lines.extend(["", "## Raw Content JSON", "```json", json.dumps(bundle.content, ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def bundle_content_text(bundle: MediaCrawlerBundle) -> str:
    row = bundle.content
    parts = [
        first_value(row, ["title"]),
        first_value(row, ["desc", "content_text", "text", "content"]),
    ]
    stats = stat_lines(row)
    media = media_lines(row)
    text_parts = [part for part in parts if part]
    if stats:
        text_parts.extend(["", "Stats:", *stats])
    if media:
        text_parts.extend(["", "Media:", *media])
    return "\n".join(text_parts).strip()


def stat_lines(row: dict[str, Any]) -> list[str]:
    keys = [
        "liked_count",
        "collected_count",
        "comment_count",
        "share_count",
        "voteup_count",
        "video_play_count",
        "video_favorite_count",
        "video_share_count",
        "video_coin_count",
        "video_danmaku",
        "video_comment",
        "total_comments",
        "total_forwards",
        "total_liked",
    ]
    lines = []
    for key in keys:
        value = row.get(key)
        if value not in {None, ""}:
            lines.append(f"- {key}: {value}")
    return lines


def media_lines(row: dict[str, Any]) -> list[str]:
    keys = [
        "image_list",
        "video_url",
        "cover_url",
        "video_download_url",
        "music_download_url",
        "note_download_url",
        "video_cover_url",
    ]
    lines = []
    for key in keys:
        value = first_value(row, [key])
        if value:
            lines.append(f"- {key}: {value}")
    return lines


def comments_markdown(comments: list[dict[str, Any]], limit: int = 200) -> str:
    if not comments:
        return "No comments were found in the MediaCrawler output."
    lines = []
    for index, comment in enumerate(comments[:limit], 1):
        author = first_value(comment, ["nickname", "user_nickname", "user_name", "creator_hash"])
        text = first_value(comment, ["content", "text", "message"])
        like_count = first_value(comment, ["like_count", "liked_count"])
        meta = []
        if author:
            meta.append(author)
        if like_count:
            meta.append(f"likes={like_count}")
        lines.append(f"{index}. {' / '.join(meta) if meta else 'comment'}")
        lines.append("")
        lines.append(f"   {text or json.dumps(comment, ensure_ascii=False)}")
        lines.append("")
    if len(comments) > limit:
        lines.append(f"... {len(comments) - limit} more comments omitted.")
    return "\n".join(lines).strip()


def bundle_summary(bundle: MediaCrawlerBundle) -> dict[str, Any]:
    return {
        "platform": bundle.platform,
        "source_type": PLATFORM_SOURCE_TYPES.get(bundle.platform, bundle.platform),
        "content_id": bundle.content_id,
        "title": bundle.title,
        "source_url": bundle.source_url,
        "author": bundle.author,
        "comment_count": len(bundle.comments),
        "files": [str(path) for path in sorted(bundle.files)],
    }


def render_text(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"ERROR: {result.get('error', 'unknown error')}"
    lines = [f"count: {result.get('count', 0)}"]
    for item in result.get("imported") or result.get("bundles") or []:
        suffix = f" -> {item.get('obsidian_path')}" if item.get("obsidian_path") else ""
        lines.append(f"- {item.get('source_type')} {item.get('content_id')}: {item.get('title')}{suffix}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
