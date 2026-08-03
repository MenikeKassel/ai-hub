from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PIPELINE_DIR = Path(__file__).resolve().parent
PIPELINE = PIPELINE_DIR / "capture_pipeline.py"
URL_RE = re.compile(r"https?://[^\s<>()\"']+")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def source_platform_value(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def configured_auto_platforms() -> set[str]:
    raw = os.getenv("HERMES_CAPTURE_AUTO_PLATFORMS", "feishu")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def gateway_capture_rewrite_text(
    text: str,
    platform: str,
    *,
    auto_links: bool = True,
    auto_text_logs: bool = False,
) -> str | None:
    clean = (text or "").strip()
    if not clean or clean.startswith("/"):
        return None
    if auto_links and URL_RE.search(clean):
        return f"/auto {clean}"
    if auto_text_logs:
        return f"/auto {clean}"
    return None


def pre_gateway_capture(event: Any, **_: Any) -> dict[str, str] | None:
    platform = source_platform_value(getattr(event, "source", None))
    allowed_platforms = configured_auto_platforms()
    if allowed_platforms and platform not in allowed_platforms:
        return None

    rewritten = gateway_capture_rewrite_text(
        getattr(event, "text", "") or "",
        platform,
        auto_links=env_flag("HERMES_CAPTURE_AUTO_LINKS", True),
        auto_text_logs=env_flag("HERMES_CAPTURE_AUTO_TEXT_LOGS", False),
    )
    if not rewritten:
        return None
    return {"action": "rewrite", "text": rewritten}


def build_message(command: str, raw_args: str) -> str:
    args = (raw_args or "").strip()
    return f"/{command} {args}".strip()


def run_pipeline(command: str, raw_args: str) -> dict[str, Any]:
    message = build_message(command, raw_args)
    completed = subprocess.run(
        [
            sys.executable,
            str(PIPELINE),
            "--message",
            message,
            "--source",
            "feishu",
        ],
        cwd=str(PIPELINE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    stdout = completed.stdout.strip()
    try:
        data = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        data = {}

    if data:
        data["_exit_code"] = completed.returncode
        if completed.stderr.strip():
            data["_stderr"] = completed.stderr.strip()
        return data

    return {
        "ok": False,
        "error": completed.stderr.strip() or stdout or f"pipeline exited with {completed.returncode}",
        "_exit_code": completed.returncode,
    }


def format_response(result: dict[str, Any]) -> str:
    if result.get("needs_manual_supplement"):
        reason = result.get("fetch_error") or result.get("error") or "没有采到可沉淀正文"
        return "\n".join(
            [
                "没有采到可沉淀正文，先不落库。",
                f"原因: {reason}",
                "请在飞书里补充摘要、关键内容或你的判断后，再重新发送 /clip。",
            ]
        )

    if not result.get("ok"):
        return "保存失败，未确认落库。\n原因: " + str(result.get("error") or "unknown error")

    lines = [
        "已保存",
        f"Notion: {result.get('notion_url') or '未写入'}",
        f"Obsidian: {result.get('obsidian_path') or '未写入'}",
        f"分类: {result.get('source_type') or '未分类'} / {result.get('project') or '未归属'}",
    ]
    if result.get("heavy_backend_status"):
        job = f" {result.get('heavy_backend_job_id')}" if result.get("heavy_backend_job_id") else ""
        lines.append(f"补采: {result.get('heavy_backend')}/{result.get('heavy_backend_status')}{job}")
    return "\n".join(lines)


def format_capture_response(result: dict[str, Any]) -> str:
    if result.get("needs_manual_supplement"):
        reason = result.get("fetch_error") or result.get("error") or "没有采到可沉淀正文"
        return "\n".join(
            [
                "没有采到可沉淀正文，先不落库。",
                f"原因: {reason}",
                "请在飞书里补充摘要、关键内容或你的判断后，再重新发送 /clip。",
            ]
        )

    if not result.get("ok"):
        return "保存失败，未确认落库。\n原因: " + str(result.get("error") or "unknown error")

    lines = [
        "已保存",
        f"Notion: {result.get('notion_url') or '未写入'}",
        f"Obsidian: {result.get('obsidian_path') or '未写入'}",
        f"分类: {result.get('source_type') or '未分类'} / {result.get('project') or '未归属'}",
    ]
    if result.get("heavy_backend_status"):
        job = f" {result.get('heavy_backend_job_id')}" if result.get("heavy_backend_job_id") else ""
        lines.append(f"补采: {result.get('heavy_backend')}/{result.get('heavy_backend_status')}{job}")
    return "\n".join(lines)


def handle_capture(command: str, raw_args: str) -> str:
    return format_capture_response(run_pipeline(command, raw_args))


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_capture)
    ctx.register_command(
        "auto",
        handler=lambda raw_args: handle_capture("auto", raw_args),
        description="Auto-capture raw shared text: URL becomes /clip; plain text becomes /log.",
        args_hint="<shared text or log text>",
    )
    ctx.register_command(
        "clip",
        handler=lambda raw_args: handle_capture("clip", raw_args),
        description="Capture a URL to Notion and Obsidian.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "idea",
        handler=lambda raw_args: handle_capture("idea", raw_args),
        description="Capture an idea to Notion and Obsidian.",
        args_hint="<text>",
    )
    ctx.register_command(
        "readlater",
        handler=lambda raw_args: handle_capture("readlater", raw_args),
        description="Capture a URL to Notion only.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "log",
        handler=lambda raw_args: handle_capture("log", raw_args),
        description="Capture a daily activity log to Notion only.",
        args_hint="<text>",
    )
    ctx.register_command(
        "kol",
        handler=lambda raw_args: handle_capture("kol", raw_args),
        description="Capture a KOL profile/source as a KOL entity lead.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "event",
        handler=lambda raw_args: handle_capture("event", raw_args),
        description="Capture a KOL recommendation candidate for later six-element audit.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "concept",
        handler=lambda raw_args: handle_capture("concept", raw_args),
        description="Capture a reusable trading/research concept lead.",
        args_hint="<text or url> [note]",
    )
    ctx.register_command(
        "holding",
        handler=lambda raw_args: handle_capture("holding", raw_args),
        description="Capture a real holding or position audit note.",
        args_hint="<text>",
    )
    ctx.register_command(
        "day",
        handler=lambda raw_args: handle_capture("log", raw_args),
        description="Alias for /log.",
        args_hint="<text>",
    )
    ctx.register_command(
        "c",
        handler=lambda raw_args: handle_capture("clip", raw_args),
        description="Alias for /clip.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "i",
        handler=lambda raw_args: handle_capture("idea", raw_args),
        description="Alias for /idea.",
        args_hint="<text>",
    )
    ctx.register_command(
        "rl",
        handler=lambda raw_args: handle_capture("readlater", raw_args),
        description="Alias for /readlater.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "j",
        handler=lambda raw_args: handle_capture("log", raw_args),
        description="Alias for /log.",
        args_hint="<text>",
    )
    ctx.register_command(
        "k",
        handler=lambda raw_args: handle_capture("kol", raw_args),
        description="Alias for /kol.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "e",
        handler=lambda raw_args: handle_capture("event", raw_args),
        description="Alias for /event.",
        args_hint="<url> [note]",
    )
    ctx.register_command(
        "x",
        handler=lambda raw_args: handle_capture("concept", raw_args),
        description="Alias for /concept.",
        args_hint="<text or url> [note]",
    )
    ctx.register_command(
        "h",
        handler=lambda raw_args: handle_capture("holding", raw_args),
        description="Alias for /holding.",
        args_hint="<text>",
    )
    ctx.register_command(
        "a",
        handler=lambda raw_args: handle_capture("auto", raw_args),
        description="Alias for /auto.",
        args_hint="<shared text or log text>",
    )
