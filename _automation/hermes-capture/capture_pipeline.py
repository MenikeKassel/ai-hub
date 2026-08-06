from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from notion_client import NotionClient, NotionError, load_notion_key
from obsidian_writer import overwrite_note, path_is_in_output_dir, write_inbox_note
from mediacrawler_runner import build_job as build_mediacrawler_job
from mediacrawler_runner import default_queue_file as default_mediacrawler_queue_file
from mediacrawler_runner import enqueue_job as enqueue_mediacrawler_job
from mediacrawler_runner import is_zhihu_question_page


URL_RE = re.compile(r"https?://[^\s<>()\"']+")
TWEET_ID_RE = re.compile(r"(?:status|statuses)/(\d+)|\b(\d{15,25})\b")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) hermes-capture/1.1"
TRAILING_URL_CHARS = ".,;:!?)]}\"'" + "\uFF0C\u3002\uFF1B\u3001\uFF01\uFF1F"
COMMAND_ALIASES = {
    "clip": "clip",
    "c": "clip",
    "readlater": "readlater",
    "rl": "readlater",
    "idea": "idea",
    "i": "idea",
    "log": "log",
    "day": "log",
    "j": "log",
    "auto": "auto",
    "a": "auto",
    "kol": "kol",
    "k": "kol",
    "event": "event",
    "e": "event",
    "concept": "concept",
    "x": "concept",
    "holding": "holding",
    "h": "holding",
}

URL_REQUIRED_COMMANDS = {"clip", "readlater", "kol", "event"}
TEXT_REQUIRED_COMMANDS = {"idea", "log", "concept", "holding"}
NOTION_ONLY_COMMANDS = {"readlater", "log"}
OBJECT_HINT_BY_COMMAND = {
    "kol": "KOL实体线索",
    "event": "KOL推荐事件候选",
    "concept": "概念线索",
    "holding": "持仓审计线索",
}
INFO_TYPE_BY_COMMAND = {
    "log": "日志",
    "readlater": "基础信息",
    "kol": "KOL实体线索",
    "event": "KOL推荐事件候选",
    "concept": "概念线索",
    "holding": "持仓审计",
}
VALUE_SCORE_BY_COMMAND = {
    "log": 1,
    "readlater": 2,
    "clip": 3,
    "idea": 3,
    "kol": 4,
    "event": 4,
    "concept": 4,
    "holding": 5,
}


def load_config(path: Path) -> dict[str, Any]:
    defaults = {
        # 真实数据库 ID 不硬编码: 通过环境变量 NOTION_DATABASE_ID 或 config.yaml 提供
        "notion_database_id": os.environ.get("NOTION_DATABASE_ID", ""),
        "notion_version": "2022-06-28",
        "vault_path": "",  # 已废弃(第二大脑项目终止,2026-08-06)
        "obsidian_inbox_dir": "00_Inbox",
        "obsidian_project_allowlist": "",
        # .env 文件列表(分号分隔)通过环境变量 NOTION_ENV_FILES 提供, 不硬编码用户路径
        "notion_env_files": os.environ.get("NOTION_ENV_FILES", ""),
        "max_content_chars": "8000",
        "enable_zhihu_local_browser": "true",
        "zhihu_local_browser": "auto",
        "zhihu_local_browser_path": "",
        "zhihu_local_browser_profile": "Profile 2",
        "zhihu_local_user_data_dir": "",
        "zhihu_local_cdp_port": "9222",
        "zhihu_local_chrome_profile": "Profile 2",
        "zhihu_local_max_answers": "12",
        "zhihu_local_max_scrolls": "10",
        "zhihu_local_timeout": "90",
        "enable_mediacrawler_queue": "true",
        "mediacrawler_queue_sources": "小红书;知乎;抖音;B站",
        "mediacrawler_max_comments": "20",
        "mediacrawler_max_notes": "5",
    }
    if not path.exists():
        return defaults

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip('"').strip("'")
        if key.strip():
            defaults[key.strip()] = value
    # 环境变量优先于配置文件, 便于不修改 config.yaml 也能覆盖
    for key, env_name in (
        ("notion_database_id", "NOTION_DATABASE_ID"),
        ("notion_env_files", "NOTION_ENV_FILES"),
    ):
        env_value = os.environ.get(env_name, "")
        if env_value:
            defaults[key] = env_value
    return defaults


def parse_message(message: str) -> dict[str, Any]:
    raw = message.strip()
    if not raw:
        raise ValueError("empty message")

    slash = re.match(r"^/([A-Za-z0-9_-]+)(?:\s+(.*))?$", raw, flags=re.S)
    if slash:
        name = slash.group(1).lower()
        command = COMMAND_ALIASES.get(name)
        if not command:
            raise ValueError(
                "unsupported command; use /clip, /readlater, /idea, /log, /auto, /kol, /event, /concept, or /holding"
            )
        rest = (slash.group(2) or "").strip()
        if command == "auto":
            return parse_auto_message(rest)
    else:
        return parse_auto_message(raw)

    return parse_command_payload(command, rest)

    url = None
    match = URL_RE.search(rest)
    if match:
        url = match.group(0).rstrip(".,锛屻€?")
        note = (rest[: match.start()] + rest[match.end() :]).strip()
    else:
        note = rest

    if command in {"clip", "readlater"} and not url:
        raise ValueError(f"/{command} requires a URL")
    if command in {"idea", "log"} and not note:
        raise ValueError(f"/{command} requires text")

    return {"command": command, "url": url, "note": note}


def parse_auto_message(text: str) -> dict[str, Any]:
    rest = text.strip()
    if not rest:
        raise ValueError("/auto requires text or URL")

    url, note = split_url_note(rest)
    if url:
        return {"command": "clip", "url": url, "note": note}
    return {"command": "log", "url": None, "note": rest}


def parse_command_payload(command: str, rest: str) -> dict[str, Any]:
    url, note = split_url_note(rest)

    if command in URL_REQUIRED_COMMANDS and not url:
        raise ValueError(f"/{command} requires a URL")
    if command in TEXT_REQUIRED_COMMANDS and not note:
        raise ValueError(f"/{command} requires text")

    return {"command": command, "url": url, "note": note}


def split_url_note(text: str) -> tuple[str | None, str]:
    match = URL_RE.search(text)
    if not match:
        return None, text.strip()

    url = match.group(0).rstrip(TRAILING_URL_CHARS)
    note = (text[: match.start()] + text[match.end() :]).strip()
    note = re.sub(r"\s+", " ", note)
    return url, note


def fetch_url_content(url: str, max_chars: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    source_type = classify_source_type(url)
    if source_type == "X":
        x_result = fetch_x_content(url, max_chars)
        if x_result["ok"]:
            return x_result

        return {
            "ok": False,
            "content": "",
            "error": x_result.get("error") or "X public readers failed",
            "method": x_result.get("method") or "x",
        }

    if source_type == "公众号":
        wechat_result = fetch_wechat_content(url, max_chars)
        if wechat_result["ok"]:
            return wechat_result

        jina_result = fetch_jina_content(url, max_chars)
        if jina_result["ok"]:
            return jina_result

        errors = [err for err in [wechat_result.get("error"), jina_result.get("error")] if err]
        return {
            "ok": False,
            "content": jina_result.get("content") or wechat_result.get("content") or "",
            "error": " | ".join(errors) or "all readers failed",
            "method": "wechat:fallback",
        }

    if source_type == "抖音":
        douyin_result = fetch_douyin_content(url, max_chars)
        if douyin_result["ok"]:
            return douyin_result
        jina_result = fetch_jina_content(url, max_chars)
        if jina_result["ok"]:
            return jina_result
        return merge_reader_errors("douyin:fallback", douyin_result, jina_result)

    if source_type == "B站":
        bilibili_result = fetch_bilibili_content(url, max_chars)
        if bilibili_result["ok"]:
            return bilibili_result
        jina_result = fetch_jina_content(url, max_chars)
        if jina_result["ok"]:
            return jina_result
        return merge_reader_errors("bilibili:fallback", bilibili_result, jina_result)

    if source_type == "小红书":
        xhs_result = fetch_xiaohongshu_content(url, max_chars)
        if xhs_result["ok"]:
            return xhs_result
        jina_result = fetch_jina_content(url, max_chars)
        if jina_result["ok"]:
            return jina_result
        return merge_reader_errors("xiaohongshu:fallback", xhs_result, jina_result)

    if source_type == "知乎":
        zhihu_result = fetch_zhihu_content(url, max_chars, config)
        if zhihu_result["ok"]:
            return zhihu_result
        jina_result = fetch_jina_content(url, max_chars)
        if jina_result["ok"]:
            return jina_result
        return merge_reader_errors("zhihu:fallback", zhihu_result, jina_result)

    return fetch_jina_content(url, max_chars)


def fetch_jina_content(url: str, max_chars: int) -> dict[str, Any]:
    reader_url = "https://r.jina.ai/" + url
    req = urllib.request.Request(reader_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "content": "", "error": str(exc), "method": "jina"}

    error = ""
    if looks_like_blocked_page(text):
        error = "reader returned an anti-bot/captcha page"
    return {
        "ok": not error,
        "content": text[:max_chars],
        "error": error,
        "method": "jina",
    }


def merge_reader_errors(method: str, primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    errors = [err for err in [primary.get("error"), fallback.get("error")] if err]
    return {
        "ok": False,
        "content": fallback.get("content") or primary.get("content") or "",
        "error": " | ".join(errors) or "all readers failed",
        "method": method,
    }


def fetch_x_content(url: str, max_chars: int) -> dict[str, Any]:
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return {"ok": False, "content": "", "error": "could not extract X status id", "method": "x"}

    errors: list[str] = []
    for api_url, method in [
        (f"https://api.vxtwitter.com/Twitter/status/{tweet_id}", "vxtwitter"),
        (f"https://api.fxtwitter.com/status/{tweet_id}", "fxtwitter"),
    ]:
        data_result = fetch_json(api_url, method)
        if not data_result["ok"]:
            errors.append(data_result.get("error") or f"{method}: request failed")
            continue

        item = data_result["data"]
        if method == "fxtwitter":
            item = item.get("tweet") or item
        if not has_usable_x_payload_content(item):
            errors.append(f"{method}: empty tweet text/media")
            continue
        content = format_x_payload(item, url)
        text = item.get("text") or item.get("tweetText") or ""
        author = item.get("user_name") or item.get("author", {}).get("name") or ""
        username = item.get("user_screen_name") or item.get("author", {}).get("screen_name") or ""
        title_author = f"{author} (@{username})" if author and username else author or username or "X"
        summary = text.strip()[:500] if text else "X content captured."
        return {
            "ok": True,
            "content": content[:max_chars],
            "error": "",
            "method": method,
            "title": f"X - {title_author}"[:100],
            "summary": summary,
            "author": author,
            "author_username": username,
        }

    oembed_result = fetch_x_oembed(url, max_chars)
    if oembed_result["ok"]:
        return oembed_result

    return {
        "ok": False,
        "content": "",
        "error": " | ".join(errors + [oembed_result.get("error") or "X public readers failed"]),
        "method": "x",
    }


def fetch_wechat_content(url: str, max_chars: int) -> dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Mobile/15E148 MicroMessenger/8.0.49"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "content": "", "error": f"wechat-http: {exc}", "method": "wechat-http"}

    if looks_like_blocked_page(body):
        return {"ok": False, "content": "", "error": "wechat-http returned an anti-bot page", "method": "wechat-http"}

    title = first_match(
        body,
        [
            r'var\s+msg_title\s*=\s*"([^"]*)"',
            r"var\s+msg_title\s*=\s*'([^']*)'",
            r'<meta\s+property="og:title"\s+content="([^"]*)"',
            r"<title>(.*?)</title>",
        ],
    )
    author = first_match(
        body,
        [
            r'<meta\s+name="author"\s+content="([^"]*)"',
            r'id="js_name"[^>]*>\s*(.*?)\s*</a>',
        ],
    )
    description = first_match(body, [r'<meta\s+name="description"\s+content="([^"]*)"'])
    content_html = extract_wechat_content_html(body)
    content_text = html_to_text(content_html) if content_html else ""

    if not content_text and not description:
        return {"ok": False, "content": "", "error": "wechat-http: empty js_content", "method": "wechat-http"}

    content = "\n".join(
        part
        for part in [
            f"Author: {author}" if author else "",
            f"URL: {url}",
            "",
            content_text or description,
        ]
        if part is not None
    ).strip()
    return {
        "ok": True,
        "content": content[:max_chars],
        "error": "",
        "method": "wechat-http",
        "title": title or "公众号文章",
        "summary": (description or content_text)[:500],
        "author": author,
        "author_username": "",
    }


def fetch_douyin_content(url: str, max_chars: int) -> dict[str, Any]:
    result = call_mcporter("douyin.parse_douyin_video_info", {"share_link": url}, timeout=45)
    if not result["ok"]:
        return {"ok": False, "content": "", "error": result["error"], "method": "douyin-mcp"}

    data = parse_nested_json(result["data"].get("result", result["data"]))
    if not isinstance(data, dict) or data.get("status") not in {None, "success"}:
        return {"ok": False, "content": "", "error": f"douyin-mcp: {data}", "method": "douyin-mcp"}

    title = str(data.get("title") or "抖音视频")
    content = "\n".join(
        part
        for part in [
            f"Title: {title}",
            f"Video ID: {data.get('video_id', '')}",
            f"URL: {url}",
            f"Download URL: {data.get('download_url', '')}" if data.get("download_url") else "",
        ]
        if part
    )
    return {
        "ok": True,
        "content": content[:max_chars],
        "error": "",
        "method": "douyin-mcp",
        "title": title[:100],
        "summary": title[:500],
        "author": "",
        "author_username": "",
    }


def fetch_bilibili_content(url: str, max_chars: int) -> dict[str, Any]:
    bvid = extract_bilibili_bvid(url)
    if not bvid:
        return {"ok": False, "content": "", "error": "could not extract Bilibili BV id", "method": "bilibili-api"}

    view = fetch_json_url(
        f"https://api.bilibili.com/x/web-interface/view?bvid={urllib.parse.quote(bvid)}",
        method="bilibili-api",
        referer="https://www.bilibili.com",
    )
    if not view["ok"]:
        return view

    payload = view["data"]
    if payload.get("code") != 0:
        return {"ok": False, "content": "", "error": f"bilibili-api: {payload}", "method": "bilibili-api"}

    data = payload.get("data") or {}
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    subtitle_text = fetch_bilibili_subtitles(data)
    content = "\n".join(
        part
        for part in [
            f"Title: {data.get('title', '')}",
            f"Author: {owner.get('name', '')} ({owner.get('mid', '')})",
            f"BVID: {data.get('bvid', bvid)}",
            f"AID/CID: {data.get('aid', '')}/{data.get('cid', '')}",
            f"URL: {url}",
            f"Stats: views: {stat.get('view', 0)}, danmaku: {stat.get('danmaku', 0)}, replies: {stat.get('reply', 0)}, likes: {stat.get('like', 0)}, favorites: {stat.get('favorite', 0)}, coins: {stat.get('coin', 0)}",
            "",
            data.get("desc", ""),
            "",
            "Subtitles:",
            subtitle_text,
        ]
        if part
    )
    return {
        "ok": True,
        "content": content[:max_chars],
        "error": "",
        "method": "bilibili-api",
        "title": str(data.get("title") or "B站视频")[:100],
        "summary": (data.get("desc") or data.get("title") or "")[:500],
        "author": owner.get("name", ""),
        "author_username": str(owner.get("mid", "")),
    }


def fetch_xiaohongshu_content(url: str, max_chars: int) -> dict[str, Any]:
    rednote = call_mcporter("rednote.get_note_content", {"url": url}, timeout=45)
    if rednote["ok"]:
        content = json.dumps(rednote["data"], ensure_ascii=False, indent=2)
        return {
            "ok": True,
            "content": content[:max_chars],
            "error": "",
            "method": "rednote-mcp",
            "title": "小红书笔记",
            "summary": content[:500],
            "author": "",
            "author_username": "",
        }

    xhs_cli = run_command(["xhs", "read", url], timeout=45)
    if xhs_cli["ok"]:
        data = parse_nested_json(xhs_cli["stdout"])
        content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
        return {
            "ok": True,
            "content": content[:max_chars],
            "error": "",
            "method": "xhs-cli",
            "title": "小红书笔记",
            "summary": content[:500],
            "author": "",
            "author_username": "",
        }

    return {
        "ok": False,
        "content": "",
        "error": f"rednote-mcp: {rednote['error']} | xhs-cli: {xhs_cli['stderr'] or xhs_cli['stdout']}",
        "method": "xiaohongshu:fallback",
    }


def fetch_zhihu_content(url: str, max_chars: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    if is_zhihu_local_browser_url(url) and config_bool(config.get("enable_zhihu_local_browser", "true")):
        local_result = fetch_zhihu_local_browser(url, max_chars, config)
        if local_result["ok"]:
            return local_result
        strict_default = config.get("zhihu_local_strict_question", "true")
        if config_bool(config.get("zhihu_local_strict", strict_default)):
            return local_result

    api_url = zhihu_api_url(url)
    if not api_url:
        return {"ok": False, "content": "", "error": "unsupported Zhihu URL pattern", "method": "zhihu-api"}

    result = fetch_json_url(api_url, method="zhihu-api", referer="https://www.zhihu.com")
    if not result["ok"]:
        return result

    data = result["data"]
    if isinstance(data, dict) and data.get("error"):
        return {"ok": False, "content": "", "error": f"zhihu-api: {data['error']}", "method": "zhihu-api"}

    title = extract_zhihu_title(data, url)
    author = ""
    if isinstance(data.get("author"), dict):
        author = data["author"].get("name", "")
    content_source = data.get("content") or data.get("excerpt") or data.get("title") or ""
    if isinstance(content_source, list):
        content_source = "\n".join(str(part) for part in content_source)
    content_text = html_to_text(str(content_source))
    content = "\n".join(
        part
        for part in [
            f"Title: {title}",
            f"Author: {author}" if author else "",
            f"URL: {url}",
            "",
            content_text,
        ]
        if part
    )
    return {
        "ok": bool(content_text),
        "content": content[:max_chars],
        "error": "" if content_text else "zhihu-api: empty content",
        "method": "zhihu-api",
        "title": title[:100],
        "summary": content_text[:500],
        "author": author,
        "author_username": "",
    }


def fetch_zhihu_local_browser(url: str, max_chars: int, config: dict[str, Any]) -> dict[str, Any]:
    script = Path(__file__).with_name("zhihu_local_capture.py")
    if not script.exists():
        return {
            "ok": False,
            "content": "",
            "error": f"zhihu local script not found: {script}",
            "method": "zhihu-local-browser",
        }

    command = [
        sys.executable,
        str(script),
        "--url",
        url,
        "--json",
        "--max-chars",
        str(max_chars),
        "--max-answers",
        str(config.get("zhihu_local_max_answers", "12")),
        "--max-scrolls",
        str(config.get("zhihu_local_max_scrolls", "10")),
        "--port",
        str(config.get("zhihu_local_cdp_port", "9222")),
        "--profile-directory",
        str(config.get("zhihu_local_browser_profile") or config.get("zhihu_local_chrome_profile", "Profile 2")),
        "--wait",
        str(config.get("zhihu_local_wait", "30")),
    ]
    browser_path = str(config.get("zhihu_local_browser_path") or config.get("zhihu_local_chrome_path") or "").strip()
    if browser_path:
        command.extend(["--browser-path", browser_path])
    else:
        command.extend(["--browser", str(config.get("zhihu_local_browser", "auto"))])
    user_data_dir = str(config.get("zhihu_local_user_data_dir") or "").strip()
    if user_data_dir:
        command.extend(["--user-data-dir", user_data_dir])
    if not config_bool(config.get("zhihu_local_launch", "true")):
        command.append("--no-launch")

    result = run_command(command, timeout=int(config.get("zhihu_local_timeout", "90")))
    if not result["ok"] and not result["stdout"]:
        return {
            "ok": False,
            "content": "",
            "error": result["stderr"] or "zhihu-local-browser failed with no output",
            "method": "zhihu-local-browser",
        }

    try:
        data = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "content": result["stdout"][:max_chars],
            "error": f"zhihu-local-browser invalid json: {exc}; stderr={result['stderr']}",
            "method": "zhihu-local-browser",
        }

    if not data.get("ok"):
        return {
            "ok": False,
            "content": str(data.get("content", ""))[:max_chars],
            "error": data.get("error", "") or result["stderr"] or "zhihu-local-browser failed",
            "method": data.get("method", "zhihu-local-browser"),
        }

    return {
        "ok": True,
        "content": str(data.get("content", ""))[:max_chars],
        "error": "",
        "method": data.get("method", "zhihu-local-browser"),
        "title": str(data.get("title", ""))[:100],
        "summary": str(data.get("summary", ""))[:500],
        "author": "",
        "author_username": "",
    }


def call_mcporter(selector: str, args: dict[str, str], timeout: int = 45) -> dict[str, Any]:
    mcporter = shutil.which("mcporter.cmd") or shutil.which("mcporter.exe") or shutil.which("mcporter")
    if not mcporter:
        return {"ok": False, "data": {}, "error": "mcporter command not found"}

    quote_values = os.name == "nt" and Path(mcporter).suffix.lower() in {".cmd", ".bat"}
    command = [mcporter, "call", selector]
    command.extend(format_mcporter_arg(key, value, quote_values) for key, value in args.items())
    result = run_command(command, timeout=timeout)
    if not result["ok"]:
        return {"ok": False, "data": {}, "error": result["stderr"] or result["stdout"]}
    data = parse_nested_json(result["stdout"])
    return {"ok": True, "data": data, "error": ""}


def format_mcporter_arg(key: str, value: str, quote_value: bool = False) -> str:
    text = str(value)
    if quote_value:
        escaped = text.replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={text}"


def run_command(command: list[str], timeout: int = 30) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def parse_nested_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    for _ in range(2):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(parsed, str):
            return parsed
        text = parsed.strip()
    return text


def fetch_json_url(url: str, method: str, referer: str = "") -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "content": "", "data": {}, "error": f"{method}: {exc}", "method": method}

    try:
        return {"ok": True, "data": json.loads(raw), "error": "", "method": method}
    except json.JSONDecodeError as exc:
        return {"ok": False, "content": raw, "data": {}, "error": f"{method}: invalid json: {exc}", "method": method}


def extract_bilibili_bvid(url: str) -> str | None:
    match = re.search(r"(BV[0-9A-Za-z]+)", url)
    return match.group(1) if match else None


def fetch_bilibili_subtitles(video: dict[str, Any]) -> str:
    aid = video.get("aid")
    cid = video.get("cid")
    bvid = video.get("bvid", "")
    if not aid or not cid:
        return ""
    result = fetch_json_url(
        f"https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}",
        method="bilibili-subtitle-api",
        referer=f"https://www.bilibili.com/video/{bvid}",
    )
    if not result["ok"]:
        return ""
    subtitles = ((result["data"].get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    texts: list[str] = []
    for subtitle in subtitles[:3]:
        url = subtitle.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            continue
        sub = fetch_json_url(url, method="bilibili-subtitle-file", referer=f"https://www.bilibili.com/video/{bvid}")
        if sub["ok"]:
            body = sub["data"].get("body") or []
            texts.extend(str(row.get("content", "")).strip() for row in body if row.get("content"))
    return "\n".join(texts)


def zhihu_api_url(url: str) -> str | None:
    answer = re.search(r"/question/(\d+)/answer/(\d+)", url)
    if answer:
        return (
            f"https://www.zhihu.com/api/v4/answers/{answer.group(2)}"
            "?include=content,excerpt,voteup_count,comment_count,created_time,updated_time,question"
        )
    question = re.search(r"/question/(\d+)", url)
    if question:
        return f"https://www.zhihu.com/api/v4/questions/{question.group(1)}"
    article = re.search(r"/p/(\d+)", url)
    if article:
        return f"https://www.zhihu.com/api/v4/articles/{article.group(1)}"
    pin = re.search(r"/pin/(\d+)", url)
    if pin:
        return f"https://www.zhihu.com/api/v4/pins/{pin.group(1)}"
    return None


def is_zhihu_answer_page(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if "zhihu.com" not in host:
        return False
    return re.search(r"^/question/\d+/answer/\d+$", path) is not None


def is_zhihu_pin_page(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if "zhihu.com" not in host:
        return False
    return re.search(r"^/pin/\d+$", path) is not None


def is_zhihu_local_browser_url(value: str) -> bool:
    return is_zhihu_question_page(value) or is_zhihu_answer_page(value) or is_zhihu_pin_page(value)


def extract_zhihu_title(data: dict[str, Any], url: str) -> str:
    if isinstance(data.get("question"), dict) and data["question"].get("title"):
        return data["question"]["title"]
    if data.get("title"):
        return str(data["title"])
    return f"鐭ヤ箮 - {urllib.parse.urlparse(url).path.strip('/')}"


def first_match(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.S | re.I)
        if match:
            value = html.unescape(match.group(1)).strip()
            value = re.sub(r"\s+", " ", value)
            if value:
                return value
    return ""


def extract_wechat_content_html(body: str) -> str:
    patterns = [
        r'<div[^>]+id="js_content"[^>]*>(.*?)</div>\s*</div>\s*<div[^>]+id="js_pc_qr_code"',
        r'<div[^>]+id="js_content"[^>]*>(.*?)</div>\s*<script',
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.S | re.I)
        if match:
            return match.group(1)
    return ""


def html_to_text(value: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", value, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>|</section>|</h[1-6]>|</li>|</blockquote>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def fetch_json(url: str, method: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "data": {}, "error": f"{method}: {exc}"}

    try:
        return {"ok": True, "data": json.loads(raw), "error": ""}
    except json.JSONDecodeError as exc:
        return {"ok": False, "data": {}, "error": f"{method}: invalid json: {exc}"}


def fetch_x_oembed(url: str, max_chars: int) -> dict[str, Any]:
    api_url = "https://publish.twitter.com/oembed?omit_script=true&dnt=true&url=" + urllib.parse.quote(url, safe="")
    data_result = fetch_json(api_url, "twitter-oembed")
    if not data_result["ok"]:
        result = {"ok": False, "content": "", "method": "twitter-oembed"}
        result.update(data_result)
        return result

    data = data_result["data"]
    body = html.unescape(re.sub(r"<[^>]+>", " ", data.get("html", "")))
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return {"ok": False, "content": "", "error": "twitter-oembed: empty body", "method": "twitter-oembed"}

    author = data.get("author_name", "")
    content = "\n".join(
        part
        for part in [
            f"Author: {author}" if author else "",
            f"URL: {data.get('url') or url}",
            "",
            body,
        ]
        if part is not None
    ).strip()
    return {
        "ok": True,
        "content": content[:max_chars],
        "error": "",
        "method": "twitter-oembed",
        "title": f"X - {author}"[:100] if author else "X - tweet",
        "summary": body[:500],
        "author": author,
        "author_username": "",
    }


def extract_tweet_id(url: str) -> str | None:
    match = TWEET_ID_RE.search(url)
    if not match:
        return None
    return match.group(1) or match.group(2)


def has_usable_x_payload_content(item: dict[str, Any]) -> bool:
    text = item.get("text") or item.get("tweetText") or item.get("description") or ""
    if str(text).strip():
        return True

    media = item.get("mediaURLs") or item.get("media_extended") or item.get("media") or []
    if isinstance(media, str):
        if media.strip():
            return True
    elif isinstance(media, list) and media:
        return True

    quoted = item.get("qrt") or item.get("quote")
    if isinstance(quoted, dict):
        quoted_text = quoted.get("text") or quoted.get("tweetText") or quoted.get("description") or ""
        quoted_media = quoted.get("mediaURLs") or quoted.get("media_extended") or quoted.get("media") or []
        if str(quoted_text).strip():
            return True
        if isinstance(quoted_media, str):
            return bool(quoted_media.strip())
        if isinstance(quoted_media, list):
            return bool(quoted_media)

    return False


def format_x_payload(item: dict[str, Any], source_url: str) -> str:
    text = item.get("text") or item.get("tweetText") or ""
    author = item.get("user_name") or item.get("author", {}).get("name") or ""
    username = item.get("user_screen_name") or item.get("author", {}).get("screen_name") or ""
    tweet_url = item.get("tweetURL") or item.get("url") or source_url
    stats = []
    for label, key in [("likes", "likes"), ("retweets", "retweets"), ("replies", "replies")]:
        value = item.get(key)
        if value is not None:
            stats.append(f"{label}: {value}")

    lines = [
        f"Author: {author}" + (f" (@{username})" if username else ""),
        f"Date: {item.get('date', '')}",
        f"URL: {tweet_url}",
        f"Stats: {', '.join(stats)}" if stats else "",
        "",
        text.strip(),
    ]

    media_urls = item.get("mediaURLs") or []
    if media_urls:
        lines.extend(["", "Media:"])
        lines.extend(f"- {media_url}" for media_url in media_urls)

    quoted = item.get("qrt") or item.get("quote")
    if isinstance(quoted, dict):
        q_author = quoted.get("user_name") or quoted.get("author", {}).get("name") or ""
        q_username = quoted.get("user_screen_name") or quoted.get("author", {}).get("screen_name") or ""
        q_text = quoted.get("text") or quoted.get("tweetText") or ""
        q_url = quoted.get("tweetURL") or quoted.get("url") or item.get("qrtURL") or ""
        lines.extend(
            [
                "",
                "Quoted Tweet:",
                f"Author: {q_author}" + (f" (@{q_username})" if q_username else ""),
                f"URL: {q_url}" if q_url else "",
                q_text.strip(),
            ]
        )

    return "\n".join(line for line in lines if line is not None).strip()


def looks_like_blocked_page(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in [
            "requiring captcha",
            "captcha",
            "verify you are human",
            "environment abnormal",
            "当前环境异常",
            "安全验证",
            "请完成验证",
            "unusual traffic",
        ]
    )


def title_from_content(content: str, url: str | None, command: str, note: str) -> str:
    if command == "log":
        return f"日志 - {datetime.now().strftime('%Y-%m-%d')} - {note[:30]}".strip()
    if command == "holding":
        return f"持仓审计 - {note[:50]}".strip()
    if command == "concept":
        return f"概念 - {note[:60]}".strip()
    if command == "idea":
        return note[:60] or "Idea"

    for line in content.splitlines():
        clean = line.strip()
        if clean.lower().startswith("title:"):
            title = clean.split(":", 1)[1].strip()
            if title:
                return title[:100]
        if clean.startswith("# "):
            return clean[2:].strip()[:100]

    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc:
            return f"{classify_source_type(url)} - {parsed.netloc}"
    return "Untitled capture"


def summarize(content: str, note: str, command: str) -> str:
    if command == "log":
        return f"日常记录：{note[:300]}"
    if command == "holding":
        return f"持仓审计线索：{note[:300]}"
    if command == "concept":
        return f"概念线索：{note[:300]}"
    if command == "kol":
        return f"KOL实体线索：{note[:300]}" if note else "KOL实体线索，待整理账号领域和历史可靠性。"
    if command == "event":
        return f"KOL推荐事件候选：{note[:300]}" if note else "KOL推荐事件候选，待核验六要素。"
    if command == "idea":
        return note[:300]
    if note:
        return note[:300]
    clean_lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not clean_lines:
        return "待读取或手动整理。"
    return " ".join(clean_lines[:3])[:500]


def truncate_text(value: Any, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def douyin_share_text_fallback(url: str, note: str, failed_fetch: dict[str, Any], max_chars: int) -> dict[str, Any]:
    clean = re.sub(r"\s+", " ", note).strip()
    author = ""
    author_match = re.search(r"看看【(.+?)的(?:图文)?作品】", clean)
    if author_match:
        author = author_match.group(1).strip()

    title_text = clean
    if "】" in title_text:
        title_text = title_text.split("】", 1)[1]
    title_text = re.split(r"复制此链接|打开抖音|https?://", title_text, maxsplit=1)[0].strip(" .。-—")
    if not title_text:
        title_text = clean
    title = f"{title_text[:80]} - 抖音" if title_text else "抖音分享"

    reader_error = truncate_text(failed_fetch.get("error", ""), 500)
    content = "\n".join(
        part
        for part in [
            f"Title: {title}",
            f"Author: {author}" if author else "",
            f"URL: {url}",
            "",
            "Share text:",
            clean,
            "",
            f"Reader fallback failed: {reader_error}" if reader_error else "",
        ]
        if part
    )
    return {
        "ok": True,
        "content": content[:max_chars],
        "error": "",
        "method": "douyin-share-text",
        "title": title[:100],
        "summary": clean[:500],
        "author": author,
        "author_username": "",
    }

def classify_source_type(url: str | None) -> str:
    if not url:
        return "自己想法"
    host = urllib.parse.urlparse(url).netloc.lower()
    if "x.com" in host or "twitter.com" in host:
        return "X"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "小红书"
    if "zhihu.com" in host:
        return "知乎"
    if "mp.weixin.qq.com" in host:
        return "公众号"
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "抖音"
    if "bilibili.com" in host or "b23.tv" in host:
        return "B站"
    if "youtube.com" in host:
        return "视频"
    return "网页"


def extract_x_profile_handle(url: str | None) -> str:
    if not url or classify_source_type(url) != "X":
        return ""
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return ""
    handle = parts[0].lstrip("@")
    if not re.match(r"^[A-Za-z0-9_]{1,15}$", handle):
        return ""
    reserved = {
        "compose",
        "explore",
        "hashtag",
        "home",
        "i",
        "intent",
        "messages",
        "notifications",
        "search",
        "settings",
        "share",
    }
    return "" if handle.lower() in reserved else handle


def x_profile_kol_fallback(url: str, handle: str, note: str, max_chars: int) -> dict[str, Any]:
    clean_note = note.strip()
    title = f"X KOL候选 - @{handle}"
    content = "\n".join(
        part
        for part in [
            f"Title: {title}",
            f"Handle: @{handle}",
            f"URL: {url}",
            "",
            "Capture note:",
            clean_note or "用户手动加入 KOL 待选。",
            "",
            "Profile content:",
            "未读取 X profile 正文。该条目只表示候选账号已进入观察池，需要后续补充代表性推文、领域、可靠性和边界。",
        ]
        if part
    )
    return {
        "ok": True,
        "content": content[:max_chars],
        "error": "",
        "method": "x-profile-kol-fallback",
        "title": title,
        "summary": f"KOL实体线索：@{handle}。{clean_note or '待补充代表性推文和领域判断。'}",
        "author": handle,
        "author_username": f"@{handle}",
    }


def classify_project(text: str, source_type: str) -> str:
    haystack = text.lower()
    if any(word in haystack for word in ["宝妈指数", "宝妈", "母婴", "小红书", "种草"]):
        return "宝妈指数"
    if any(word in haystack for word in ["kol指数", "kol index"]):
        return "KOL指数"
    if source_type == "X" and any(word in haystack for word in ["股票", "a股", "美股", "btc", "nvda", "tsla", "kol", "看多", "看空"]):
        return "KOL指数"
    if any(word in haystack for word in ["交易系统", "交易", "量化", "选股", "回测", "因子", "股票", "a股"]):
        return "交易系统"
    if source_type == "自己想法" and any(word in haystack for word in ["今天", "昨天", "明天", "完成", "做了", "干了", "日志", "复盘", "日记"]):
        return "生活记录"
    if any(word in haystack for word in ["知识库", "知识管理", "ai", "agent", "codex", "claude", "hermes", "obsidian", "notion"]):
        return "知识管理"
    return "知识管理"


def build_item(parsed: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    url = parsed.get("url")
    command = parsed["command"]
    note = parsed.get("note", "")
    max_chars = int(config.get("max_content_chars", 8000))
    fetch = {"ok": True, "content": "", "error": ""}
    source_type = classify_source_type(url)

    if url:
        x_profile_handle = extract_x_profile_handle(url) if command == "kol" else ""
        if x_profile_handle:
            fetch = x_profile_kol_fallback(url, x_profile_handle, note, max_chars)
        else:
            fetch = fetch_url_content(url, max_chars, config)
        if source_type == "抖音" and not fetch.get("ok") and note.strip():
            fetch = douyin_share_text_fallback(url, note, fetch, max_chars)

    content = fetch.get("content", "")
    combined = " ".join([url or "", note, content])
    project = classify_project(combined, source_type)
    if command in {"kol", "event"}:
        project = "KOL指数"
    elif command in {"concept", "holding"}:
        project = "交易系统"
    if command == "log":
        source_type = "自己想法"
        project = "生活记录"
        content = build_log_content(note)
        fetch = {"ok": True, "content": content, "error": "", "method": "hermes-log"}
    title = fetch.get("title") or title_from_content(content, url, command, note)

    summary = fetch.get("summary") or summarize(content, note, command)
    if fetch.get("error"):
        summary = f"{summary}\n\n读取失败：{truncate_text(fetch['error'])}".strip()

    return {
        "command": command,
        "title": title,
        "url": url,
        "note": note,
        "content": content,
        "summary": summary,
        "fetch_ok": bool(fetch.get("ok")),
        "fetch_method": fetch.get("method", ""),
        "fetch_error": truncate_text(fetch.get("error", ""), 800),
        "author": fetch.get("author", ""),
        "author_username": fetch.get("author_username", ""),
        "source_type": source_type,
        "project": project,
        "status": "Inbox",
        "value_score": VALUE_SCORE_BY_COMMAND.get(command, 3),
        "info_type": INFO_TYPE_BY_COMMAND.get(command, "知识型信息"),
        "object_hint": OBJECT_HINT_BY_COMMAND.get(command, "来源"),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_log_content(note: str) -> str:
    activities = split_log_items(note)
    possible_todos = [
        item
        for item in activities
        if any(marker in item for marker in ["要", "需要", "准备", "计划", "待", "明天", "下次", "继续"])
    ]
    lines = [
        "原文：",
        note.strip(),
        "",
        "活动拆解：",
    ]
    lines.extend(f"- {item}" for item in activities)
    lines.extend(["", "鍙兘鐨勫緟鍔烇細"])
    if possible_todos:
        lines.extend(f"- [ ] {item}" for item in possible_todos)
    else:
        lines.append("- 暂无明显待办。")
    lines.extend(
        [
            "",
            "复盘提示：",
            "- 今天最重要的一件事是什么？",
            "- 哪件事值得继续推进？",
            "- 哪件事只是消耗精力？",
        ]
    )
    return "\n".join(lines)


def split_log_items(note: str) -> list[str]:
    parts = [
        part.strip(" \t-—，。；;、")
        for part in re.split(r"[\n；;。]+|，然后|然后|还有|另外|以及", note)
        if part.strip(" \t-—，。；;、")
    ]
    if not parts:
        return [note.strip()]
    return parts[:20]


def maybe_enqueue_mediacrawler(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if item.get("command") in NOTION_ONLY_COMMANDS:
        return item
    if item.get("fetch_ok", True):
        return item
    if not item.get("url"):
        return item
    if not config_bool(config.get("enable_mediacrawler_queue", "true")):
        return item

    eligible_sources = {
        part.strip()
        for part in str(config.get("mediacrawler_queue_sources", "小红书;知乎;抖音;B站")).replace(",", ";").split(";")
        if part.strip()
    }
    if item.get("source_type") not in eligible_sources:
        return item
    if item.get("source_type") == "知乎" and is_zhihu_question_page(str(item.get("url", ""))):
        item.update(
            {
                "heavy_backend": "MediaCrawler",
                "heavy_backend_status": "needs_specific_url",
                "heavy_backend_error": (
                    "MediaCrawler 知乎 detail 不支持裸 question 页面；"
                    "请保存具体回答 /question/<qid>/answer/<aid>、专栏 /p/<id> 或 /zvideo/<id> 链接。"
                ),
            }
        )
        item["summary"] = append_heavy_backend_summary(item.get("summary", ""), item)
        return item

    try:
        args = argparse.Namespace(
            repo=str(config.get("mediacrawler_repo", "")),
            platform="auto",
            type="detail",
            url=[item["url"]],
            id=[],
            creator_id=[],
            keyword=[],
            keywords="",
            login=str(config.get("mediacrawler_login", "qrcode")),
            cookies="",
            save_option=str(config.get("mediacrawler_save_option", "jsonl")),
            save_path=str(config.get("mediacrawler_save_path", "")),
            comments=str(config.get("mediacrawler_comments", "true")).lower(),
            sub_comments=str(config.get("mediacrawler_sub_comments", "false")).lower(),
            max_comments=int(config.get("mediacrawler_max_comments", 20)),
            max_notes=int(config.get("mediacrawler_max_notes", 5)),
            concurrency=int(config.get("mediacrawler_concurrency", 1)),
            headless=str(config.get("mediacrawler_headless", "false")).lower(),
        )
        job = build_mediacrawler_job(args)
        queue_file = Path(str(config.get("mediacrawler_queue_file", ""))) if config.get("mediacrawler_queue_file") else default_mediacrawler_queue_file()
        queue_result = enqueue_mediacrawler_job(job, queue_file)
        item.update(
            {
                "heavy_backend": "MediaCrawler",
                "heavy_backend_status": "queued" if queue_result.get("queued") else "existing",
                "heavy_backend_job_id": queue_result.get("job_id", ""),
                "heavy_backend_queue_file": queue_result.get("queue_file", ""),
                "heavy_backend_dedupe_key": queue_result.get("dedupe_key", ""),
                "heavy_backend_command": job.get("command_text", ""),
            }
        )
        item["summary"] = append_heavy_backend_summary(item.get("summary", ""), item)
    except Exception as exc:
        item.update(
            {
                "heavy_backend": "MediaCrawler",
                "heavy_backend_status": "enqueue_failed",
                "heavy_backend_error": str(exc),
            }
        )
        item["summary"] = append_heavy_backend_summary(item.get("summary", ""), item)
    return item


def append_heavy_backend_summary(summary: str, item: dict[str, Any]) -> str:
    status = item.get("heavy_backend_status")
    if not status:
        return summary
    lines = [
        summary.strip(),
        "",
        f"补采任务：{item.get('heavy_backend', 'MediaCrawler')} / {status}",
    ]
    if item.get("heavy_backend_job_id"):
        lines.append(f"Job ID：{item['heavy_backend_job_id']}")
    if item.get("heavy_backend_queue_file"):
        lines.append(f"队列：{item['heavy_backend_queue_file']}")
    if item.get("heavy_backend_error"):
        lines.append(f"补采入队失败：{item['heavy_backend_error']}")
    return "\n".join(line for line in lines if line is not None).strip()


def heavy_backend_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in [
            "heavy_backend",
            "heavy_backend_status",
            "heavy_backend_job_id",
            "heavy_backend_queue_file",
            "heavy_backend_dedupe_key",
            "heavy_backend_error",
        ]
        if item.get(key)
    }


def config_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def should_write_obsidian(item: dict[str, Any], config: dict[str, Any]) -> bool:
    if item.get("command") in NOTION_ONLY_COMMANDS:
        return False

    configured = str(config.get("obsidian_project_allowlist", "")).strip()
    if not configured:
        # Obsidian 写入已停用(2026-08-06): allowlist 为空即不写
        return False

    allowed_projects = {
        project.strip()
        for project in re.split(r"[;,]", configured)
        if project.strip()
    }
    return str(item.get("project", "")).strip() in allowed_projects


def needs_manual_supplement(item: dict[str, Any]) -> bool:
    return item.get("command") not in NOTION_ONLY_COMMANDS and bool(item.get("url")) and not item.get("fetch_ok", True)


def manual_supplement_result(item: dict[str, Any]) -> dict[str, Any]:
    fetch_error = truncate_text(item.get("fetch_error") or "reader returned no usable content", 800)
    return {
        "ok": False,
        "created_or_existing": "not_captured",
        "notion_url": "",
        "obsidian_path": "",
        "source_type": item.get("source_type", ""),
        "project": item.get("project", ""),
        "fetch_method": item.get("fetch_method", ""),
        "fetch_status": "failed",
        "fetch_error": fetch_error,
        "needs_manual_supplement": True,
        "manual_supplement_prompt": (
            "没有采到可沉淀正文，已停止落库。"
            "请在飞书里补充摘要、关键内容或你的判断后，再重新发送 /clip。"
        ),
        **heavy_backend_result(item),
        "error": f"未落库：没有采到可沉淀正文。原因：{fetch_error}",
    }


def _persist_item(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    env_files = [part for part in str(config["notion_env_files"]).split(";") if part]
    database_id = str(config.get("notion_database_id") or "").strip()
    if not database_id or database_id == "REPLACE_WITH_NOTION_DATABASE_ID":
        raise ValueError(
            "Notion 数据库 ID 未配置: 请设置环境变量 NOTION_DATABASE_ID, "
            "或在 config.yaml 中填写真实的 notion_database_id"
        )
    notion_key = load_notion_key(env_files)
    field_config = {
        key.removeprefix("field_"): str(value)
        for key, value in config.items()
        if key.startswith("field_") and str(value)
    }
    notion = NotionClient(
        notion_key,
        database_id,
        str(config.get("notion_version", "2022-06-28")),
        fields=field_config,
    )
    added_schema = notion.ensure_minimum_schema()

    existing = notion.find_by_url(item.get("url"))
    if existing:
        notion_url = existing.get("url", "")
        obsidian_path = notion.existing_obsidian_path(existing)
        if item.get("fetch_ok", True):
            notion.update_capture_page(existing["id"], item)
            notion.replace_page_blocks(existing["id"], item)
            if should_write_obsidian(item, config):
                try:
                    preferred_output = str(item.get("obsidian_output_dir") or "").strip()
                    needs_preferred_page = bool(
                        preferred_output
                        and not path_is_in_output_dir(obsidian_path, preferred_output)
                    )
                    if obsidian_path and not needs_preferred_page:
                        obsidian_path = overwrite_note(obsidian_path, item, notion_url, config)
                    else:
                        obsidian_path = write_inbox_note(item, notion_url, config)
                        notion.update_obsidian_path(existing["id"], obsidian_path)
                except Exception as exc:
                    return {
                        "ok": False,
                        "created_or_existing": "updated_notion_only",
                        "notion_url": notion_url,
                        "obsidian_path": obsidian_path,
                        "source_type": item["source_type"],
                        "project": item["project"],
                        "error": f"Obsidian refresh failed: {exc}",
                        "added_schema": added_schema,
                        **heavy_backend_result(item),
                    }
        return {
            "ok": True,
            "created_or_existing": "updated_existing" if item.get("fetch_ok", True) else "existing",
            "notion_url": notion_url,
            "obsidian_path": obsidian_path,
            "source_type": item["source_type"],
            "project": item["project"],
            "added_schema": added_schema,
            **heavy_backend_result(item),
        }

    page = notion.create_capture_page(item)
    notion_url = page.get("url", "")
    obsidian_path = ""

    if should_write_obsidian(item, config):
        try:
            obsidian_path = write_inbox_note(item, notion_url, config)
            notion.update_obsidian_path(page["id"], obsidian_path)
        except Exception as exc:
            return {
                "ok": False,
                "created_or_existing": "created_notion_only",
                "notion_url": notion_url,
                "obsidian_path": "",
                "source_type": item["source_type"],
                "project": item["project"],
                "error": f"Obsidian write failed: {exc}",
                "added_schema": added_schema,
                **heavy_backend_result(item),
            }

    return {
        "ok": True,
        "created_or_existing": "created",
        "notion_url": notion_url,
        "obsidian_path": obsidian_path,
        "source_type": item["source_type"],
        "project": item["project"],
        "added_schema": added_schema,
        **heavy_backend_result(item),
    }


def capture_prefetched_item(
    item: dict[str, Any],
    source: str,
    config_path: Path,
) -> dict[str, Any]:
    config = load_config(config_path)
    prepared = dict(item)
    prepared.setdefault("command", "event")
    prepared.setdefault("source", source)
    prepared.setdefault("source_type", "X")
    prepared.setdefault("project", "KOL指数")
    prepared.setdefault("status", "Inbox")
    prepared.setdefault("value_score", 4)
    prepared.setdefault("object_hint", "KOL推荐事件候选")
    prepared.setdefault("fetch_ok", True)
    prepared.setdefault("fetch_method", "twitter-cli-prefetched")
    prepared.setdefault("fetch_error", "")
    prepared.setdefault("captured_at", datetime.now().isoformat(timespec="seconds"))
    if not str(prepared.get("url") or "").startswith(("https://", "http://")):
        raise ValueError("prefetched capture requires a valid source URL")
    if not str(prepared.get("content") or "").strip():
        raise ValueError("prefetched capture requires usable content")
    return _persist_item(prepared, config)


def run(message: str, source: str, config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    parsed = parse_message(message)
    item = build_item(parsed, config)
    item = maybe_enqueue_mediacrawler(item, config)
    if needs_manual_supplement(item):
        return manual_supplement_result(item)
    return _persist_item(item, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Feishu/Hermes messages to Notion and Obsidian.")
    parser.add_argument("--message", required=True, help="Raw message, e.g. '/clip https://example.com note'")
    parser.add_argument("--source", default="local", help="Source label, e.g. feishu")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    args = parser.parse_args()

    try:
        result = run(args.message, args.source, Path(args.config))
    except (ValueError, NotionError) as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": f"unexpected error: {exc}"}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    controlled_stop = result.get("created_or_existing") == "not_captured"
    return 0 if result.get("ok") or controlled_stop else 1


if __name__ == "__main__":
    sys.exit(main())







