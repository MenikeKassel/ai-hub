from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any

from capture_pipeline import build_item, load_config


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


SAMPLE_URLS = {
    "X": "https://x.com/i/status/2072254225493697014",
    "抖音": "https://v.douyin.com/tfSndl4wF7k/",
    "小红书": "http://xhslink.com/o/2ook6KCdFaC",
    "知乎": "https://www.zhihu.com/question/19581624",
    "B站": "https://www.bilibili.com/video/BV1xx411c7mD",
    "公众号": "https://mp.weixin.qq.com/s/cpWqtlxWjAslYTXE5BySzg",
}


def parse_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--url must use PLATFORM=URL, got: {value}")
        platform, url = value.split("=", 1)
        platform = platform.strip()
        url = url.strip()
        if not platform or not url:
            raise ValueError(f"--url must use PLATFORM=URL, got: {value}")
        overrides[platform] = url
    return overrides


def probe_one(platform: str, url: str, config: dict[str, Any]) -> dict[str, Any]:
    parsed = {"command": "clip", "url": url, "note": f"reader probe: {platform}"}
    try:
        item = build_item(parsed, config)
        return {
            "platform": platform,
            "url": url,
            "ok": bool(item.get("fetch_ok")),
            "method": item.get("fetch_method", ""),
            "source_type": item.get("source_type", ""),
            "project": item.get("project", ""),
            "title": item.get("title", ""),
            "content_chars": len(item.get("content") or ""),
            "summary": compact(item.get("summary") or "", 220),
            "error": item.get("fetch_error", ""),
        }
    except Exception as exc:
        return {
            "platform": platform,
            "url": url,
            "ok": False,
            "method": "",
            "source_type": "",
            "project": "",
            "title": "",
            "content_chars": 0,
            "summary": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def probe_with_timeout(platform: str, url: str, config: dict[str, Any], timeout: int) -> dict[str, Any]:
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(target=probe_worker, args=(queue, platform, url, config))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        return {
            "platform": platform,
            "url": url,
            "ok": False,
            "method": "",
            "source_type": "",
            "project": "",
            "title": "",
            "content_chars": 0,
            "summary": "",
            "error": f"reader probe timed out after {timeout}s",
        }

    if not queue.empty():
        return queue.get()
    return {
        "platform": platform,
        "url": url,
        "ok": False,
        "method": "",
        "source_type": "",
        "project": "",
        "title": "",
        "content_chars": 0,
        "summary": "",
        "error": f"reader probe exited with code {process.exitcode}",
    }


def probe_worker(queue: multiprocessing.Queue, platform: str, url: str, config: dict[str, Any]) -> None:
    queue.put(probe_one(platform, url, config))


def compact(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def render_text(results: list[dict[str, Any]]) -> str:
    lines = []
    for result in results:
        status = "OK" if result["ok"] else "FAIL"
        lines.append(
            f"[{status}] {result['platform']} | {result.get('method') or 'unknown'} | "
            f"{result.get('title') or result['url']}"
        )
        if result.get("content_chars"):
            lines.append(f"  content_chars: {result['content_chars']}")
        if result.get("error"):
            lines.append(f"  error: {result['error']}")
        if result.get("summary"):
            lines.append(f"  summary: {result['summary']}")
    return "\n".join(lines)


def main() -> int:
    configure_stdio()

    parser = argparse.ArgumentParser(
        description="Probe hermes-capture platform readers without writing to Notion or Obsidian."
    )
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument(
        "--platform",
        action="append",
        choices=list(SAMPLE_URLS.keys()),
        help="Platform to probe. Repeatable. Defaults to all sample platforms.",
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Override a sample URL, e.g. --url 小红书=https://www.xiaohongshu.com/explore/...",
    )
    parser.add_argument("--max-chars", type=int, default=3000, help="Max content chars per reader.")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout seconds per platform.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()

    try:
        overrides = parse_overrides(args.url)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    config = load_config(Path(args.config))
    config["max_content_chars"] = str(args.max_chars)

    platforms = args.platform or list(SAMPLE_URLS.keys())
    results = [
        probe_with_timeout(platform, overrides.get(platform, SAMPLE_URLS[platform]), config, args.timeout)
        for platform in platforms
    ]

    payload = {
        "ok": all(result["ok"] for result in results),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(results))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
