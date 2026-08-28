from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PLATFORM_ALIASES = {
    "xhs": "xhs",
    "xiaohongshu": "xhs",
    "小红书": "xhs",
    "rednote": "xhs",
    "dy": "dy",
    "douyin": "dy",
    "抖音": "dy",
    "bili": "bili",
    "bilibili": "bili",
    "b站": "bili",
    "哔哩哔哩": "bili",
    "zhihu": "zhihu",
    "知乎": "zhihu",
}

SUPPORTED_TYPES = {"search", "detail", "creator"}
SUPPORTED_SAVE_OPTIONS = {"jsonl", "json", "csv", "sqlite", "excel"}


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Build or run MediaCrawler jobs as an external heavy crawling backend."
    )
    parser.add_argument("--repo", default="", help="Path to local MediaCrawler repo.")
    parser.add_argument("--platform", default="auto", help="xhs, dy, bili, zhihu, or auto from URL.")
    parser.add_argument("--type", default="detail", choices=sorted(SUPPORTED_TYPES), help="Crawler type.")
    parser.add_argument("--url", action="append", default=[], help="Detail URL/ID. Repeatable.")
    parser.add_argument("--id", action="append", default=[], help="Detail ID. Repeatable.")
    parser.add_argument("--creator-id", action="append", default=[], help="Creator ID/URL. Repeatable.")
    parser.add_argument("--keyword", action="append", default=[], help="Search keyword. Repeatable.")
    parser.add_argument("--keywords", default="", help="Comma-separated search keywords.")
    parser.add_argument("--login", default="qrcode", choices=["qrcode", "phone", "cookie"], help="Login type.")
    parser.add_argument("--cookies", default="", help="Cookie string for --login cookie. Avoid shell history if sensitive.")
    parser.add_argument("--save-option", default="jsonl", choices=sorted(SUPPORTED_SAVE_OPTIONS))
    parser.add_argument("--save-path", default="", help="Output root. Defaults to ai-hub/_runtime/mediacrawler.")
    parser.add_argument("--comments", default="true", choices=["true", "false"], help="Fetch first-level comments.")
    parser.add_argument("--sub-comments", default="false", choices=["true", "false"], help="Fetch second-level comments.")
    parser.add_argument("--max-comments", type=int, default=20, help="Max first-level comments per item.")
    parser.add_argument("--max-notes", type=int, default=5, help="Max notes/videos for search/creator.")
    parser.add_argument("--concurrency", type=int, default=1, help="MediaCrawler max concurrency.")
    parser.add_argument("--headless", default="false", choices=["true", "false"], help="Headless browser mode.")
    parser.add_argument("--timeout", type=int, default=1800, help="Execution timeout seconds.")
    parser.add_argument("--enqueue", action="store_true", help="Append this job to the MediaCrawler queue.")
    parser.add_argument("--queue-file", default="", help="Queue JSONL path. Defaults to _runtime/mediacrawler/queue.jsonl.")
    parser.add_argument("--execute", action="store_true", help="Actually run MediaCrawler. Default is dry run.")
    args = parser.parse_args()

    try:
        job = build_job(args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    if args.enqueue and args.execute:
        print(json.dumps({"ok": False, "error": "Use either --enqueue or --execute, not both."}, ensure_ascii=False, indent=2))
        return 2

    if args.enqueue:
        queue_result = enqueue_job(job, Path(args.queue_file) if args.queue_file else default_queue_file())
        print(json.dumps({"ok": True, "execute": False, **queue_result, **job}, ensure_ascii=False, indent=2))
        return 0

    if not args.execute:
        print(json.dumps({"ok": True, "execute": False, **job}, ensure_ascii=False, indent=2))
        return 0

    result = execute_job(job, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def build_job(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_repo(args.repo)
    if not repo.exists():
        raise ValueError(f"MediaCrawler repo not found: {repo}")
    if not (repo / "main.py").exists():
        raise ValueError(f"MediaCrawler main.py not found under: {repo}")

    values = [*args.url, *args.id, *args.creator_id]
    platform = normalize_platform(args.platform, values)
    if platform not in PLATFORM_ALIASES.values():
        raise ValueError(f"Unsupported MediaCrawler platform: {args.platform}")

    save_path = Path(args.save_path) if args.save_path else default_save_path()
    save_path = save_path.resolve()
    keywords = normalize_keywords(args.keyword, args.keywords)
    ids = unique_non_empty([*args.url, *args.id])
    creator_ids = unique_non_empty(args.creator_id)

    if args.type == "detail" and not ids:
        raise ValueError("--type detail requires --url or --id")
    if args.type == "creator" and not creator_ids:
        raise ValueError("--type creator requires --creator-id")
    if args.type == "search" and not keywords:
        raise ValueError("--type search requires --keyword or --keywords")
    validate_targets(platform, args.type, ids)

    command = build_command(
        platform=platform,
        crawler_type=args.type,
        login=args.login,
        save_option=args.save_option,
        save_path=save_path,
        keywords=keywords,
        ids=ids,
        creator_ids=creator_ids,
        comments=args.comments,
        sub_comments=args.sub_comments,
        max_comments=args.max_comments,
        max_notes=args.max_notes,
        concurrency=args.concurrency,
        headless=args.headless,
        cookies=args.cookies,
    )

    return {
        "repo": str(repo),
        "platform": platform,
        "type": args.type,
        "save_path": str(save_path),
        "output_hint": str(save_path / platform),
        "command": command,
        "command_text": command_text(command),
        "notes": [
            "MediaCrawler is an external heavy backend. It may open/login browser windows.",
            "Output files are expected under <save_path>/<platform>/<format>/.",
            "Use --execute only after confirming login state and platform terms.",
        ],
    }


def resolve_repo(value: str) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    env_value = os.environ.get("MEDIACRAWLER_REPO")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (repo_root() / "_external" / "MediaCrawler").resolve()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_save_path() -> Path:
    return repo_root() / "_runtime" / "mediacrawler"


def default_queue_file() -> Path:
    return default_save_path() / "queue.jsonl"


def enqueue_job(job: dict[str, Any], queue_file: Path) -> dict[str, Any]:
    queue_file = queue_file.resolve()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    dedupe_key = hashlib.sha1(job["command_text"].encode("utf-8")).hexdigest()
    existing = find_existing_queue_entry(queue_file, dedupe_key)
    if existing:
        return {
            "queued": False,
            "existing": True,
            "job_id": existing.get("job_id", ""),
            "queue_file": str(queue_file),
            "dedupe_key": dedupe_key,
        }

    created_at = datetime.now().isoformat(timespec="seconds")
    job_id = hashlib.sha1(f"{created_at}|{dedupe_key}".encode("utf-8")).hexdigest()[:16]
    entry = {
        "job_id": job_id,
        "dedupe_key": dedupe_key,
        "status": "pending",
        "created_at": created_at,
        "platform": job["platform"],
        "type": job["type"],
        "save_path": job["save_path"],
        "command": job["command"],
        "command_text": job["command_text"],
    }
    with queue_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"queued": True, "existing": False, "job_id": job_id, "queue_file": str(queue_file), "dedupe_key": dedupe_key}


def find_existing_queue_entry(queue_file: Path, dedupe_key: str) -> dict[str, Any] | None:
    if not queue_file.exists():
        return None
    for line in queue_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        status = entry.get("status", "pending")
        if status in {"done", "failed", "cancelled"}:
            continue
        if entry.get("dedupe_key") == dedupe_key:
            return entry
        if not entry.get("dedupe_key") and hashlib.sha1(str(entry.get("command_text", "")).encode("utf-8")).hexdigest() == dedupe_key:
            return entry
    return None


def normalize_platform(value: str, values: list[str]) -> str:
    raw = value.strip().lower()
    if raw == "auto":
        for item in values:
            detected = detect_platform(item)
            if detected:
                return detected
        raise ValueError("--platform auto could not detect platform from URL/ID")
    return PLATFORM_ALIASES.get(raw, raw)


def detect_platform(value: str) -> str | None:
    host = urlparse(value).netloc.lower()
    if "xiaohongshu.com" in host or "xhslink.com" in host or "rednote.com" in host:
        return "xhs"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "dy"
    if "bilibili.com" in host or "b23.tv" in host:
        return "bili"
    if "zhihu.com" in host:
        return "zhihu"
    return None


def validate_targets(platform: str, crawler_type: str, ids: list[str]) -> None:
    if platform == "zhihu" and crawler_type == "detail":
        unsupported = [value for value in ids if is_zhihu_question_page(value)]
        if unsupported:
            raise ValueError(
                "MediaCrawler Zhihu detail does not support bare /question/ URLs. "
                "Use a specific answer URL like https://www.zhihu.com/question/<qid>/answer/<aid>, "
                "or a Zhihu article /p/<id>, or a /zvideo/<id> URL."
            )


def is_zhihu_question_page(value: str) -> bool:
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if "zhihu.com" not in host:
        return False
    if "/answer/" in path or path.startswith("/p/") or path.startswith("/zvideo/"):
        return False
    return re.search(r"^/question/\d+$", path) is not None


def normalize_keywords(repeated: list[str], comma_separated: str) -> str:
    values = [item.strip() for item in repeated if item.strip()]
    values.extend(item.strip() for item in comma_separated.split(",") if item.strip())
    return ",".join(dict.fromkeys(values))


def unique_non_empty(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def build_command(
    *,
    platform: str,
    crawler_type: str,
    login: str,
    save_option: str,
    save_path: Path,
    keywords: str,
    ids: list[str],
    creator_ids: list[str],
    comments: str,
    sub_comments: str,
    max_comments: int,
    max_notes: int,
    concurrency: int,
    headless: str,
    cookies: str,
) -> list[str]:
    runner = ["uv", "run"] if shutil.which("uv") else [sys.executable]
    command = [
        *runner,
        "main.py",
        "--platform",
        platform,
        "--lt",
        login,
        "--type",
        crawler_type,
        "--save_data_option",
        save_option,
        "--save_data_path",
        str(save_path),
        "--get_comment",
        comments,
        "--get_sub_comment",
        sub_comments,
        "--max_comments_count_singlenotes",
        str(max_comments),
        "--crawler_max_notes_count",
        str(max_notes),
        "--max_concurrency_num",
        str(concurrency),
        "--headless",
        headless,
    ]
    if crawler_type == "search":
        command.extend(["--keywords", keywords])
    if crawler_type == "detail":
        command.extend(["--specified_id", ",".join(ids)])
    if crawler_type == "creator":
        command.extend(["--creator_id", ",".join(creator_ids)])
    if cookies:
        command.extend(["--cookies", cookies])
    return command


def execute_job(job: dict[str, Any], timeout: int) -> dict[str, Any]:
    repo = Path(job["repo"])
    save_path = Path(job["save_path"])
    save_path.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    try:
        completed = subprocess.run(
            job["command"],
            cwd=repo,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "execute": True,
            **job,
            "exit_code": None,
            "stdout": trim(exc.stdout or ""),
            "stderr": trim(exc.stderr or ""),
            "error": f"MediaCrawler timed out after {timeout}s",
            "outputs": find_outputs(save_path, job["platform"], started_at),
        }
    except Exception as exc:
        return {
            "ok": False,
            "execute": True,
            **job,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
            "outputs": find_outputs(save_path, job["platform"], started_at),
        }

    return {
        "ok": completed.returncode == 0,
        "execute": True,
        **job,
        "exit_code": completed.returncode,
        "stdout": trim(completed.stdout),
        "stderr": trim(completed.stderr),
        "outputs": find_outputs(save_path, job["platform"], started_at),
    }


def find_outputs(save_path: Path, platform: str, since: float) -> list[dict[str, Any]]:
    root = save_path / platform
    if not root.exists():
        return []
    outputs: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime + 1 < since:
            continue
        outputs.append(
            {
                "path": str(path),
                "relative_path": str(path.relative_to(save_path)),
                "bytes": stat.st_size,
                "modified_at": int(stat.st_mtime),
            }
        )
    return sorted(outputs, key=lambda item: item["path"])


def command_text(command: list[str]) -> str:
    parts = []
    skip_next = False
    for index, part in enumerate(command):
        if skip_next:
            skip_next = False
            continue
        if part == "--cookies" and index + 1 < len(command):
            parts.extend(["--cookies", "<redacted>"])
            skip_next = True
            continue
        parts.append(quote_arg(part))
    return " ".join(parts)


def quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def trim(text: str, limit: int = 8000) -> str:
    clean = text or ""
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "\n...[truncated]"


if __name__ == "__main__":
    sys.exit(main())
