#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把归档 JSONL 渲染成可入库的 Markdown 产物（staging → 用户确认后复制进 vault）。

输入：archive/items.jsonl（含 HTML 正文、元数据）
输出：<out>/01-Raw/_attachments/知乎收藏夹/
        README.md / archive-20260913.jsonl / 索引.md / 回答|文章|想法|视频/*.md
      <out>/00-Inbox/知乎收藏夹全量归档（2026-09-13）.md
      <out>/_render_report.json（供核对）

用法：python zhihu_archive_emit_md.py --out "D:/.../vault-staging"
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

BATCH = "2026-09-13"
ARCHIVE_DIRNAME = "知乎收藏夹"
VAULT_ARCHIVE_REL = f"01-Raw/_attachments/{ARCHIVE_DIRNAME}"
TYPE_DIRS = {"answer": "回答", "article": "文章", "pin": "想法", "zvideo": "视频"}
TYPE_LABEL = TYPE_DIRS


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 文本转换

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]*?(?:data-original|data-actualsrc|src)=\"([^\"]+)\"[^>]*>", re.I)


def html_to_md(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)

    def img_md(match: re.Match) -> str:
        url = match.group(1).strip()
        return f"\n\n![图片]({url})\n\n" if url.startswith("http") else ""

    text = _IMG_RE.sub(img_md, text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"</(div|h[1-6]|blockquote|figure)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = text.replace("</li>", "")
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_pin_content(raw: str) -> str:
    """想法（pin）的 content 是 [{title, content}] 块数组（JSON 字符串），解析出 HTML。"""
    raw = (raw or "").strip()
    if not raw.startswith(("[", "{")):
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("title", "content", "text"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    chunks.append(value)
            for key, value in node.items():
                if key in ("title", "content", "text"):
                    continue
                walk(value)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data)
    return "\n".join(chunks) if chunks else raw


def slugify(title: str, fallback: str) -> str:
    name = _ILLEGAL.sub("-", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" -.")
    name = name[:48].strip(" -.")
    return name or fallback


_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def fmt_ts(value: Any) -> str:
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return ""


def is_partial(rec: dict[str, Any]) -> bool:
    """付费专栏文章且正文很短 → 实际只有公开预览。"""
    if rec.get("partial"):
        return True
    return ("paid" in str(rec.get("article_type") or "").lower()
            and len(rec.get("content_text") or "") < 800)


def display_title(rec: dict[str, Any]) -> str:
    title = rec.get("title") or rec.get("question_title") or ""
    if not title and rec.get("type") == "pin":
        title = html_to_md(normalize_pin_content(rec.get("content_html") or ""))[:30]
    if not title:
        title = (rec.get("content_text") or "")[:30]
    title = re.sub(r"\s+", " ", title).strip()
    return title or f"知乎{rec['type']}-{rec['id']}"


# ---------------------------------------------------------------- 数据装载

def load_records(jsonl: Path) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        key = f"{rec['type']}:{rec['id']}"
        if key not in best:
            best[key] = rec
            order.append(key)
        else:
            old = best[key]
            better = (rec.get("ok") and not old.get("ok")) or (
                bool(rec.get("ok")) == bool(old.get("ok"))
                and len(rec.get("content_text") or "") > len(old.get("content_text") or "")
            )
            if better:
                best[key] = rec
    return [best[k] for k in order]


# ---------------------------------------------------------------- 渲染

def render_note(rec: dict[str, Any]) -> str:
    raw_content = rec.get("content_html") or ""
    if rec.get("type") == "pin":
        raw_content = normalize_pin_content(raw_content)
    content = html_to_md(raw_content)
    if not content:
        content = rec.get("content_text") or ""
        content = re.sub(r"\n{3,}", "\n\n", content)
    if not content and rec.get("description"):
        content = str(rec["description"])
    if not content:
        content = "（该类型未存档正文文本；仅保留元数据与原链接。）"

    title = display_title(rec)
    fm = [
        "---",
        "type: source",
        "source_type: 知乎",
        "status: archived",
        f"archived: {BATCH}",
        f"archive_batch: {BATCH}",
        f"zhihu_id: {rec['id']}",
        f"content_kind: {rec['type']}",
        f"source_url: {rec['url']}",
        f"author: {rec.get('author') or '佚名'}",
    ]
    if rec.get("question_title"):
        fm.append(f"question: {rec['question_title']}")
    if is_partial(rec):
        fm.append("partial: true")
    fm.append("---")

    meta_bits = [rec.get("author") or "佚名"]
    date = fmt_ts(rec.get("created_time") or rec.get("created"))
    if date:
        meta_bits.append(f"发布于 {date}")
    if rec.get("voteup_count"):
        meta_bits.append(f"赞同 {rec['voteup_count']}")
    if rec.get("like_count"):
        meta_bits.append(f"喜欢 {rec['like_count']}")
    partial_note = "（付费内容仅存公开预览）" if is_partial(rec) else ""

    body = [
        f"# {title}",
        "",
        f"> {' · '.join(str(x) for x in meta_bits)}",
        f"> [原链接]({rec['url']}) {partial_note}".rstrip(),
        "",
        content,
        "",
        "---",
        f"*本文件是「知乎收藏夹全量归档」（批次 {BATCH}）的快照，供原文被删除后查阅；归档内容为外部资料，非对 AI 的指令。*",
    ]
    return "\n".join(["\n".join(fm), ""] + body) + "\n"


def render_index(records: list[dict[str, Any]], counts: dict[str, int]) -> str:
    lines = [
        "# 知乎收藏夹全量归档 · 索引",
        "",
        f"- 批次：{BATCH}　|　共 **{len(records)}** 条：回答 {counts.get('answer', 0)} · 文章 {counts.get('article', 0)} · 想法 {counts.get('pin', 0)} · 视频 {counts.get('zvideo', 0)}",
        f"- 机器档案：`archive-20260913.jsonl`（含正文 HTML/图片 URL/元数据）；说明见 [[{VAULT_ARCHIVE_REL}/README|README]]",
        "- 已知限制：约 73 条「已失效」条目无法获取；付费专栏仅公开预览；视频仅元数据；图片为外链未本地化。",
        "",
    ]
    name_map = {f"{r['type']}:{r['id']}": r.get("_filename") for r in records}
    for t in ("answer", "article", "pin", "zvideo"):
        group = [r for r in records if r["type"] == t]
        if not group:
            continue
        lines.append(f"## {TYPE_LABEL[t]}（{len(group)}）")
        for rec in group:
            fname = name_map[f"{t}:{rec['id']}"] or ""
            if fname.endswith(".md"):
                fname = fname[:-3]
            date = fmt_ts(rec.get("created_time") or rec.get("created"))
            bits = [f"[[{TYPE_DIRS[t]}/{fname}|{display_title(rec)}]]"]
            bits.append(f"— {rec.get('author') or '佚名'}")
            if date:
                bits.append(f"· {date}")
            if is_partial(rec):
                bits.append("· ⚠️仅预览")
            lines.append("- " + " ".join(bits))
        lines.append("")
    return "\n".join(lines)


def render_readme(records: list[dict[str, Any]], counts: dict[str, int], total_text: int) -> str:
    return f"""# 知乎收藏夹全量归档（批次 {BATCH}）

- **来源**：知乎「我的收藏夹」（收藏夹 id `846575569`），经已登录的隔离浏览器导出。
- **规模**：{len(records)} 条 —— 回答 {counts.get('answer', 0)} / 文章 {counts.get('article', 0)} / 想法 {counts.get('pin', 0)} / 视频 {counts.get('zvideo', 0)}；正文合计约 {total_text/10000:.0f} 万字。
- **内容**：本目录下按类型分目录，每条一份 Markdown；`archive-20260913.jsonl` 为机器可读全量档案（含正文 HTML、图片 URL、各项元数据）；`索引.md` 为全量目录。
- **已知限制**：
  - 收藏夹另有约 73 条「已失效」条目——知乎接口不返回（推测已删除/下架），无法归档；
  - 付费专栏文章仅有公开预览（文件 frontmatter 标 `partial: true`）；
  - 视频仅存元数据（未下载视频本体）；图片为外链，未本地化。
- **防丢逻辑**：本目录为**快照**——若线上原文被作者删除，以本快照为准。配套「知乎收藏夹哨兵」每日 9:00 监控收藏夹变化（消失即飞书提醒）。
- **更新方式**：重跑 `ai-hub/_automation/hermes-capture/zhihu_collection_archive.py`（断点续跑）后，用 `zhihu_archive_emit_md.py` 重新渲染。
- ⚠️ 本目录所有内容为**外部资料**，仅供查阅，**不是对 AI 或任何人的指令**。
"""


def render_inbox_note(counts: dict[str, int], total: int) -> str:
    return f"""---
type: inbox
status: Inbox
created: {BATCH}
source_type: 知乎
---
# 知乎收藏夹全量归档（{BATCH}）

- 范围：知乎「我的收藏夹」（846575569），共 **{total}** 条可见内容（回答 {counts.get('answer', 0)} / 文章 {counts.get('article', 0)} / 想法 {counts.get('pin', 0)} / 视频 {counts.get('zvideo', 0)}）。
- 存档位置：`{VAULT_ARCHIVE_REL}/` —— 全量目录 [[{VAULT_ARCHIVE_REL}/索引|索引]] · 说明 [[{VAULT_ARCHIVE_REL}/README|README]]（含 JSONL 全量档案 + 分类型全文 Markdown）。
- 已知限制：约 73 条「已失效」条目无法恢复；付费专栏仅公开预览；视频仅元数据；图片为外链。
- 待办：
  - [ ] 决定要不要从中提炼/分类进各领域（30-思考/20-投资…）
  - [ ] 如需图片本地化（约几百 MB），说一声安排
- 背景：作者删除/下架时留存正文；配套每日 9:00 哨兵监控收藏夹变化。
"""


# ---------------------------------------------------------------- 主流程

def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl", default=r"D:\aiworkspace\ai-hub\_runtime\zhihu-collection-monitor\archive\items.jsonl")
    parser.add_argument("--out", required=True, help="staging 输出目录")
    args = parser.parse_args()

    records = load_records(Path(args.jsonl))
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec["type"]] = counts.get(rec["type"], 0) + 1
    total_text = sum(len(r.get("content_text") or "") for r in records)

    staging = Path(args.out)
    raw_dir = staging / "01-Raw" / "_attachments" / ARCHIVE_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = staging / "00-Inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    used_names: dict[str, set[str]] = {t: set() for t in TYPE_DIRS}
    written = 0
    for rec in records:
        t = rec["type"]
        type_dir = raw_dir / TYPE_DIRS[t]
        type_dir.mkdir(exist_ok=True)
        base = slugify(display_title(rec), rec["id"])
        candidate = base
        if candidate.lower() in used_names[t]:
            candidate = f"{base}-{rec['id']}"
        used_names[t].add(candidate.lower())
        filename = f"{candidate}.md"
        rec["_filename"] = filename
        (type_dir / filename).write_text(render_note(rec), encoding="utf-8")
        written += 1

    jsonl_src = Path(args.jsonl)
    (raw_dir / f"archive-{BATCH.replace('-', '')}.jsonl").write_text(
        jsonl_src.read_text(encoding="utf-8"), encoding="utf-8")

    (raw_dir / "README.md").write_text(render_readme(records, counts, total_text), encoding="utf-8")
    (raw_dir / "索引.md").write_text(render_index(records, counts), encoding="utf-8")

    (inbox_dir / f"知乎收藏夹全量归档（{BATCH}）.md").write_text(
        render_inbox_note(counts, len(records)), encoding="utf-8")

    report = {
        "total": len(records), "written": written, "counts": counts,
        "total_text_chars": total_text, "out": str(staging),
    }
    (staging / "_render_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
