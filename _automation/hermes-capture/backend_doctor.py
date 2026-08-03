from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


COMMANDS = {
    "mcporter": ["mcporter", "mcporter.cmd", "mcporter.ps1"],
    "xhs": ["xhs", "xhs.exe"],
    "yt-dlp": ["yt-dlp", "yt-dlp.exe"],
    "twitter": ["twitter", "twitter.exe"],
    "bili": ["bili", "bili.exe"],
    "uv": ["uv", "uv.exe"],
    "git": ["git", "git.exe"],
    "node": ["node", "node.exe"],
    "npm": ["npm", "npm.cmd"],
}

PYTHON_PACKAGES = [
    "scrapling",
    "xiaohongshu-cli",
    "yt-dlp",
    "playwright",
    "camoufox",
]

ENV_VARS = [
    "DASHSCOPE_API_KEY",
    "TWITTER_AUTH_TOKEN",
    "TWITTER_CT0",
]

OPEN_SOURCE_BACKENDS = {
    "MediaCrawler": {
        "repo": "https://github.com/NanmiCoder/MediaCrawler",
        "role": "Unified heavy crawler for Xiaohongshu, Douyin, Bilibili, Zhihu, Weibo, Tieba, Kuaishou.",
        "needed_for": ["小红书评论", "知乎问答/评论", "抖音评论", "B站评论", "批量搜索/补采"],
    },
    "XHS-Downloader": {
        "repo": "https://github.com/JoeanAmier/XHS-Downloader",
        "role": "Xiaohongshu note parsing and media download backend candidate.",
        "needed_for": ["小红书图文/视频补采", "小红书搜索结果"],
    },
    "yt-dlp": {
        "repo": "https://github.com/yt-dlp/yt-dlp",
        "role": "Video metadata, subtitles, and media backend.",
        "needed_for": ["B站字幕/视频", "跨站视频补采"],
    },
    "bilibili-API-collect": {
        "repo": "https://github.com/SocialSisterYi/bilibili-API-collect",
        "role": "Bilibili public/private API reference.",
        "needed_for": ["B站元数据", "B站字幕", "B站评论 API 研究"],
    },
    "twitter-cli": {
        "repo": "https://github.com/public-clis/twitter-cli",
        "role": "Authenticated X/Twitter timeline, tweet, article, and profile reader.",
        "needed_for": ["X 长文", "X 用户时间线", "X 搜索"],
    },
    "Scrapling": {
        "repo": "https://github.com/D4Vinci/Scrapling",
        "role": "Anti-detection web scraping fallback library.",
        "needed_for": ["小红书/知乎登录态网页 fallback"],
    },
    "Douyin_TikTok_Download_API": {
        "repo": "https://github.com/Evil0ctal/Douyin_TikTok_Download_API",
        "role": "Douyin/TikTok parsing and download API backend candidate.",
        "needed_for": ["抖音替代解析后端"],
    },
}


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Diagnose optional heavy crawler backends without writing captures."
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument(
        "--scan-root",
        action="append",
        default=[r"<AI_HUB_HOME>", r"F:\ai-workspace"],
        help="Root to scan for local backend repos. Repeatable.",
    )
    args = parser.parse_args()

    report = build_report([Path(root) for root in args.scan_root])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0 if report["overall_status"] in {"ok", "partial"} else 1


def configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def build_report(scan_roots: list[Path]) -> dict[str, Any]:
    commands = {name: locate_command(candidates) for name, candidates in COMMANDS.items()}
    packages = {name: inspect_python_package(name) for name in PYTHON_PACKAGES}
    env = {name: {"set": bool(os.environ.get(name))} for name in ENV_VARS}
    local_repos = {
        "MediaCrawler": find_named_dirs(scan_roots, "MediaCrawler"),
        "XHS-Downloader": find_named_dirs(scan_roots, "XHS-Downloader"),
    }
    mcporter = inspect_mcporter(commands["mcporter"]["path"])
    rednote_probe = inspect_rednote_probe(mcporter)
    xhs_status = inspect_xhs_status(commands["xhs"]["path"])
    twitter_status = inspect_cli_status("twitter", commands["twitter"]["path"])
    bili_status = inspect_cli_status("bili", commands["bili"]["path"])

    platforms = platform_verdicts(
        commands,
        packages,
        env,
        local_repos,
        mcporter,
        rednote_probe,
        xhs_status,
        twitter_status,
        bili_status,
    )
    missing_required = [
        platform
        for platform, verdict in platforms.items()
        if verdict["status"] == "blocked"
    ]
    overall_status = "ok" if not missing_required else "partial"

    return {
        "overall_status": overall_status,
        "commands": commands,
        "python_packages": packages,
        "env": env,
        "mcporter": mcporter,
        "rednote_probe": rednote_probe,
        "xhs_status": xhs_status,
        "twitter_status": twitter_status,
        "bili_status": bili_status,
        "local_repos": local_repos,
        "open_source_backends": OPEN_SOURCE_BACKENDS,
        "platforms": platforms,
    }


def locate_command(candidates: list[str]) -> dict[str, Any]:
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return {"available": True, "path": path}
    return {"available": False, "path": ""}


def inspect_python_package(name: str) -> dict[str, Any]:
    result = run([sys.executable, "-m", "pip", "show", name], timeout=20)
    if result["exit_code"] != 0:
        return {"installed": False, "version": "", "location": ""}

    version = ""
    location = ""
    for line in result["stdout"].splitlines():
        if line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        if line.startswith("Location:"):
            location = line.split(":", 1)[1].strip()
    return {"installed": True, "version": version, "location": location}


def inspect_mcporter(path: str) -> dict[str, Any]:
    if not path:
        return {"available": False, "servers": [], "raw": "mcporter not found"}

    result = run_shell("mcporter list", timeout=45)
    servers = []
    for line in result["stdout"].splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            name = stripped[2:].split(" ", 1)[0]
            servers.append(
                {
                    "name": name,
                    "healthy": "offline" not in stripped.lower(),
                    "line": stripped,
                }
            )
    return {
        "available": result["exit_code"] == 0,
        "servers": servers,
        "raw": compact(result["stdout"] or result["stderr"], 1200),
    }


def inspect_rednote_probe(mcporter: dict[str, Any]) -> dict[str, Any]:
    servers = {server["name"]: server for server in mcporter.get("servers", [])}
    if not servers.get("rednote", {}).get("healthy"):
        return {
            "available": False,
            "ok": False,
            "sample_url": "",
            "error": "rednote MCP server is not healthy",
            "raw": "",
        }

    result = run_shell('mcporter call rednote.search_notes keywords="登录测试" limit=1', timeout=75)
    raw = (result["stdout"] or "") + ("\n" + result["stderr"] if result["stderr"] else "")
    lower = raw.lower()
    ok = result["exit_code"] == 0 and bool(result["stdout"].strip()) and "not logged in" not in lower
    sample_url = first_line_containing(raw, "https://www.xiaohongshu.com") or ""
    return {
        "available": True,
        "ok": ok,
        "sample_url": sample_url.removeprefix("链接: ").strip(),
        "error": "" if ok else compact(result["stderr"] or result["stdout"], 800),
        "raw": compact(raw, 1200),
    }


def inspect_xhs_status(path: str) -> dict[str, Any]:
    if not path:
        return {"available": False, "authenticated": False, "guest": None, "cookie_warning": "", "raw": "xhs not found"}

    result = run_powershell("$env:PYTHONIOENCODING='utf-8'; xhs status", timeout=45)
    raw = result["stdout"] + ("\n" + result["stderr"] if result["stderr"] else "")
    lower = raw.lower()
    return {
        "available": result["exit_code"] == 0,
        "authenticated": "authenticated: true" in lower,
        "guest": "guest: true" in lower if raw else None,
        "cookie_warning": first_line_containing(raw, "cookie") or "",
        "raw": redact_xhs_user(compact(raw, 1200)),
    }


def inspect_cli_status(name: str, path: str) -> dict[str, Any]:
    if not path:
        return {"available": False, "authenticated": False, "error": f"{name} not found", "raw": ""}

    result = run_powershell(f"$env:PYTHONIOENCODING='utf-8'; {name} status --json", timeout=60)
    raw = (result["stdout"] or "") + ("\n" + result["stderr"] if result["stderr"] else "")
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError:
        payload = {}

    error = ""
    if payload.get("error"):
        err = payload["error"]
        if isinstance(err, dict):
            error = err.get("message") or err.get("code") or str(err)
        else:
            error = str(err)

    return {
        "available": True,
        "authenticated": bool(payload.get("ok")) and result["exit_code"] == 0,
        "error": compact(error or result["stderr"], 800),
        "raw": compact(raw, 1200),
    }


def platform_verdicts(
    commands: dict[str, dict[str, Any]],
    packages: dict[str, dict[str, Any]],
    env: dict[str, dict[str, bool]],
    local_repos: dict[str, list[str]],
    mcporter: dict[str, Any],
    rednote_probe: dict[str, Any],
    xhs_status: dict[str, Any],
    twitter_status: dict[str, Any],
    bili_status: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    mcporter_servers = {server["name"]: server for server in mcporter.get("servers", [])}
    rednote_healthy = "rednote" in mcporter_servers and mcporter_servers["rednote"]["healthy"]
    rednote_ok = bool(rednote_probe.get("ok"))
    xhs_cli_good = bool(xhs_status.get("authenticated")) and not bool(xhs_status.get("guest"))
    xhs_can_read = rednote_ok or xhs_cli_good

    return {
        "X": {
            "status": "partial",
            "ready": ready(
                [
                    (True, "vxtwitter/fxtwitter light reader in capture_pipeline"),
                    (commands["twitter"]["available"], "twitter-cli installed"),
                    (twitter_status.get("authenticated"), "twitter-cli authenticated session"),
                ]
            ),
            "missing": missing(
                [
                    (commands["twitter"]["available"], "twitter-cli for authenticated timeline/search/article reads"),
                    (
                        twitter_status.get("authenticated")
                        or (env["TWITTER_AUTH_TOKEN"]["set"] and env["TWITTER_CT0"]["set"]),
                        "X authenticated session (browser cookies or TWITTER_AUTH_TOKEN + TWITTER_CT0)",
                    ),
                ]
            ),
            "next_action": "Log in to x.com in a supported browser or set TWITTER_AUTH_TOKEN + TWITTER_CT0 before timeline/search/article reads.",
        },
        "抖音": {
            "status": "partial" if not env["DASHSCOPE_API_KEY"]["set"] else "ok",
            "ready": ready(
                [
                    ("douyin" in mcporter_servers and mcporter_servers["douyin"]["healthy"], "douyin MCP metadata/download tools"),
                    (env["DASHSCOPE_API_KEY"]["set"], "DASHSCOPE_API_KEY for ASR"),
                ]
            ),
            "missing": missing(
                [
                    ("douyin" in mcporter_servers and mcporter_servers["douyin"]["healthy"], "healthy douyin MCP server"),
                    (env["DASHSCOPE_API_KEY"]["set"], "DASHSCOPE_API_KEY for extract_douyin_text"),
                    (bool(local_repos["MediaCrawler"]), "MediaCrawler for comments/search/batch crawling"),
                ]
            ),
            "next_action": "Add DASHSCOPE_API_KEY to Hermes env, then validate extract_douyin_text.",
        },
        "B站": {
            "status": "partial",
            "ready": ready(
                [
                    (commands["yt-dlp"]["available"], "yt-dlp installed"),
                    (True, "public Bilibili API light reader in capture_pipeline"),
                    (commands["bili"]["available"], "bili-cli installed"),
                    (bili_status.get("authenticated"), "bili-cli authenticated session"),
                ]
            ),
            "missing": missing(
                [
                    (commands["bili"]["available"], "bili-cli for search/hot/rank/login workflows"),
                    (bili_status.get("authenticated"), "bili login for authenticated workflows"),
                    (bool(local_repos["MediaCrawler"]), "MediaCrawler for comments/batch crawling"),
                ]
            ),
            "next_action": "Keep public API for /clip; run bili login when authenticated Bilibili workflows are needed.",
        },
        "小红书": {
            "status": "partial" if xhs_can_read else "blocked",
            "ready": ready(
                [
                    (commands["xhs"]["available"], "xhs CLI installed"),
                    (rednote_healthy, "rednote MCP server healthy"),
                    (rednote_ok, "rednote search/content probe passed"),
                    (packages["scrapling"]["installed"], "Scrapling installed"),
                ]
            ),
            "missing": missing(
                [
                    (xhs_can_read, "rednote login or fresh non-guest Xiaohongshu login/cookies"),
                    (not xhs_status.get("cookie_warning") or rednote_ok, "xhs cookies without refresh warning, or rednote fallback working"),
                    (bool(local_repos["MediaCrawler"]), "MediaCrawler for comments/search/batch crawling"),
                    (bool(local_repos["XHS-Downloader"]), "XHS-Downloader optional media backend"),
                ]
            ),
            "next_action": "rednote is usable for the main capture path; refresh xhs-cli cookies only for xhs-cli-specific workflows.",
        },
        "知乎": {
            "status": "blocked" if not local_repos["MediaCrawler"] else "partial",
            "ready": ["direct API/Jina fallback exists but currently hits 403/captcha"],
            "missing": missing(
                [
                    (bool(local_repos["MediaCrawler"]), "MediaCrawler with browser login/CDP for Zhihu"),
                    (packages["playwright"]["installed"], "Playwright installed"),
                ]
            ),
            "next_action": "Add MediaCrawler as external backend and run it with local browser login state.",
        },
    }


def ready(items: list[tuple[bool, str]]) -> list[str]:
    return [label for ok, label in items if ok]


def missing(items: list[tuple[bool, str]]) -> list[str]:
    return [label for ok, label in items if not ok]


def find_named_dirs(roots: list[Path], name: str, max_depth: int = 5) -> list[str]:
    matches: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        root = root.resolve()
        for current, dirs, _files in os.walk(root):
            current_path = Path(current)
            rel_parts = current_path.relative_to(root).parts
            if len(rel_parts) > max_depth:
                dirs[:] = []
                continue
            dirs[:] = [item for item in dirs if item not in {".git", "node_modules", "__pycache__", ".venv"}]
            for item in list(dirs):
                if item.lower() == name.lower():
                    matches.append(str(current_path / item))
    return matches


def run(command: list[str], timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def run_shell(command: str, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def run_powershell(command: str, timeout: int) -> dict[str, Any]:
    return run(["powershell", "-NoProfile", "-Command", command], timeout=timeout)


def compact(text: str, limit: int) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "\n...[truncated]"


def first_line_containing(text: str, needle: str) -> str | None:
    needle = needle.lower()
    for line in text.splitlines():
        if needle in line.lower():
            return line.strip()
    return None


def redact_xhs_user(text: str) -> str:
    redacted = []
    for line in text.splitlines():
        if line.strip().startswith("id:"):
            redacted.append("    id: <redacted>")
        else:
            redacted.append(line)
    return "\n".join(redacted)


def render_text(report: dict[str, Any]) -> str:
    lines = [f"overall: {report['overall_status']}", ""]

    lines.append("commands:")
    for name, data in report["commands"].items():
        status = "OK" if data["available"] else "MISS"
        lines.append(f"  [{status}] {name}: {data.get('path') or '-'}")

    lines.extend(["", "environment:"])
    for name, data in report["env"].items():
        lines.append(f"  [{'OK' if data['set'] else 'MISS'}] {name}")

    lines.extend(["", "mcporter:"])
    for server in report["mcporter"].get("servers", []):
        lines.append(f"  [{'OK' if server['healthy'] else 'FAIL'}] {server['line']}")

    lines.extend(["", "xhs:"])
    xhs = report["xhs_status"]
    lines.append(f"  available: {xhs.get('available')}")
    lines.append(f"  authenticated: {xhs.get('authenticated')}")
    lines.append(f"  guest: {xhs.get('guest')}")
    if xhs.get("cookie_warning"):
        lines.append(f"  warning: {xhs['cookie_warning']}")

    lines.extend(["", "rednote probe:"])
    rednote = report.get("rednote_probe", {})
    lines.append(f"  available: {rednote.get('available')}")
    lines.append(f"  ok: {rednote.get('ok')}")
    if rednote.get("sample_url"):
        lines.append(f"  sample_url: {rednote['sample_url']}")
    if rednote.get("error"):
        lines.append(f"  error: {rednote['error'].splitlines()[0]}")

    lines.extend(["", "twitter:"])
    twitter = report["twitter_status"]
    lines.append(f"  available: {twitter.get('available')}")
    lines.append(f"  authenticated: {twitter.get('authenticated')}")
    if twitter.get("error"):
        lines.append(f"  error: {twitter['error'].splitlines()[0]}")

    lines.extend(["", "bili:"])
    bili = report["bili_status"]
    lines.append(f"  available: {bili.get('available')}")
    lines.append(f"  authenticated: {bili.get('authenticated')}")
    if bili.get("error"):
        lines.append(f"  error: {bili['error'].splitlines()[0]}")

    lines.extend(["", "platform verdicts:"])
    for platform, verdict in report["platforms"].items():
        lines.append(f"  {platform}: {verdict['status']}")
        if verdict["ready"]:
            lines.append(f"    ready: {'; '.join(verdict['ready'])}")
        if verdict["missing"]:
            lines.append(f"    missing: {'; '.join(verdict['missing'])}")
        lines.append(f"    next: {verdict['next_action']}")

    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
