#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the X likes snapshot locally; no network, vault writes or media fetches.

Run with no arguments for the 2026-09-13 batch. Defaults deliberately point to
absolute paths outside a maintenance worktree. --out is an archive root, with
only archive/ and vault-staging/ generated below it. Input files are read-only.
Historical records come from Birdbear's tweets array, not its relationship rows,
and never enter the main CSV/Markdown. Dates in readable output use UTC+08:00.

The same inputs produce byte-identical outputs. Invalid JSON/IDs, conflicting
duplicates and count mismatches fail before writing. Missing optional metadata
is preserved and audited. Custom fixtures can use --expected-main 0.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

BATCH = "2026-09-13"
DEFAULT_MERGED = Path(r"D:\aiworkspace\x-collection-archive\work\likes_merged_20260913.jsonl")
DEFAULT_BIRDBEAR = Path(r"D:\aiworkspace\x-collection-archive\source\birdbear-export-2025-10-07.json")
DEFAULT_OUT = Path(r"D:\aiworkspace\ai-hub\_runtime\x-collection-archive")
LOCAL_TZ = timezone(timedelta(hours=8))
PROV_KEYS = ("in_20260610", "in_20260913")
MAIN_FIELDS = (
    "id", "created_at", "full_text", "media", "screen_name", "name",
    "profile_image_url", "user_id", "in_reply_to", "retweeted_status",
    "quoted_status", "media_tags", "favorite_count", "retweet_count",
    "bookmark_count", "quote_count", "reply_count", "views_count",
    "favorited", "retweeted", "bookmarked", "url", "metadata",
)
HISTORY_FIELDS = ("id", "created_at", "full_text", "media", "screen_name", "name", "url")
CSV_FIELDS = ("日期", "年", "作者名", "@用户名", "URL", "正文摘要", "媒体数", "视频数", "点赞数", "转推数", "浏览数", "来源标记")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, chunks: Any) -> None:
    """Replace one completed file, never expose partially written JSONL/Markdown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".x-archive-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            if isinstance(chunks, str):
                handle.write(chunks)
            else:
                handle.writelines(chunks)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def valid_url(value: Any) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username
    except ValueError:
        return False


def parsed_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            result = parse(value)
            if result.tzinfo is None:
                result = result.replace(tzinfo=LOCAL_TZ)
            return result.astimezone(LOCAL_TZ)
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def normalized_id(value: Any, location: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(f"{location}: missing or invalid tweet id")
    return str(value)


def load_main(path: Path) -> tuple[list[dict], dict]:
    records: dict[str, dict] = {}
    total = duplicates = 0
    duplicate_ids = []
    with path.open(encoding="utf-8-sig") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"merged line {lineno}: blank JSONL record")
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"merged line {lineno}: invalid JSON") from exc
            if not isinstance(row, dict) or not isinstance(row.get("item"), dict):
                raise ValueError(f"merged line {lineno}: item must be an object")
            item = row["item"]
            key = normalized_id(row.get("id"), f"merged line {lineno}")
            if normalized_id(item.get("id"), f"merged item {key}") != key:
                raise ValueError(f"merged line {lineno}: wrapper/item id mismatch")
            prov = row.get("prov")
            if (not isinstance(prov, dict) or any(type(prov.get(k)) is not bool for k in PROV_KEYS)
                    or not any(prov[k] for k in PROV_KEYS)):
                raise ValueError(f"merged id {key}: invalid provenance flags")
            if "_prov" in item:
                raise ValueError(f"merged id {key}: reserved _prov field already present")
            rec = dict(item, _prov=prov.copy())
            if key in records:
                previous = records[key]
                if {k: v for k, v in previous.items() if k != "_prov"} != item:
                    raise ValueError(f"merged id {key}: conflicting duplicate; refusing data loss")
                previous["_prov"] = {**previous["_prov"], **prov,
                                     **{k: previous["_prov"][k] or prov[k] for k in PROV_KEYS}}
                duplicates += 1
                duplicate_ids.append(key)
            else:
                records[key] = rec
    return list(records.values()), {"input_rows": total, "unique_ids": len(records),
                                   "duplicates_removed": duplicates, "duplicate_ids": duplicate_ids}


def load_history(path: Path, main_ids: set[str]) -> tuple[list[dict], dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("tweets"), list):
        raise ValueError("Birdbear: tweets must be an array")
    seen: dict[str, dict] = {}
    extras, anomalies = [], []
    duplicates = overlap = 0
    for number, item in enumerate(data["tweets"], 1):
        if not isinstance(item, dict):
            raise ValueError(f"Birdbear tweets row {number}: must be an object")
        key = normalized_id(item.get("id"), f"Birdbear tweets row {number}")
        if key in seen:
            if item != seen[key]:
                raise ValueError(f"Birdbear id {key}: conflicting duplicate")
            duplicates += 1
            continue
        seen[key] = item
        if key in main_ids:
            overlap += 1
            continue
        media = item.get("media")
        if isinstance(media, str):
            try:
                media = json.loads(media)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Birdbear id {key}: invalid media JSON") from exc
        if media is None:
            media = []
        if not isinstance(media, list) or any(not isinstance(m, dict) for m in media):
            raise ValueError(f"Birdbear id {key}: media must contain objects")
        mapped_media = []
        for m in media:
            mapped = dict(m)
            mapped["original"] = m.get("original") or m.get("url") or ""
            mapped["thumbnail"] = m.get("thumbnail") or m.get("thumbnailUrl") or (
                mapped["original"] if m.get("type") == "photo" else "")
            mapped_media.append(mapped)
        dt = parsed_date(item.get("created_at"))
        username = item.get("tweet_by_username") or ""
        rec = {
            "id": key,
            "created_at": dt.strftime("%Y-%m-%d %H:%M:%S +08:00") if dt else item.get("created_at"),
            "full_text": item.get("full_text"), "media": mapped_media,
            "screen_name": username, "name": item.get("tweet_by_full_name") or "",
            "url": f"https://x.com/{quote(str(username), safe='')}/status/{key}" if username else f"https://x.com/i/status/{key}",
            "_source": "birdbear-2025-10",
        }
        extras.append(rec)
    relationship_counts = {k: len(data.get(k, [])) for k in ("likes", "bookmarks")}
    likes = {str(r.get("tweet_id")) for r in data.get("likes", []) if isinstance(r, dict)}
    bookmarks = {str(r.get("tweet_id")) for r in data.get("bookmarks", []) if isinstance(r, dict)}
    return extras, {
        "selection": "unique tweets absent from main; likes/bookmarks are relationship arrays",
        "input_tweets": len(data["tweets"]), "unique_tweet_ids": len(seen),
        "duplicates_removed": duplicates, "overlap_with_main": overlap,
        "extras": len(extras), "relationship_rows": relationship_counts,
        "extras_in_likes": sum(r["id"] in likes for r in extras),
        "extras_in_bookmarks": sum(r["id"] in bookmarks for r in extras),
    }, anomalies


def field_quality(records: list[dict], fields: tuple[str, ...]) -> dict:
    return {key: {
        "absent": sum(key not in r for r in records),
        "null": sum(key in r and r[key] is None for r in records),
        "empty_string": sum(r.get(key) == "" for r in records),
        "empty_array": sum(r.get(key) == [] for r in records),
    } for key in fields}


def record_anomalies(records: list[dict], layer: str) -> list[dict]:
    findings = []
    for rec in records:
        fields = []
        for field in ("full_text", "screen_name", "name"):
            if not isinstance(rec.get(field), str) or not rec[field].strip():
                fields.append(field)
        if parsed_date(rec.get("created_at")) is None:
            fields.append("created_at")
        if not valid_url(rec.get("url")):
            fields.append("url")
        media = rec.get("media")
        if not isinstance(media, list):
            fields.append("media")
        else:
            for i, m in enumerate(media):
                if not isinstance(m, dict) or not valid_url(m.get("original")):
                    fields.append(f"media[{i}].original")
                if isinstance(m, dict) and m.get("type") not in ("photo", "video", "animated_gif"):
                    fields.append(f"media[{i}].type")
        if fields:
            findings.append({"layer": layer, "id": str(rec["id"]), "code": "invalid_or_missing_fields", "fields": fields})
    return findings


def text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def single_line(value: Any) -> str:
    return re.sub(r"\s+", " ", text(value)).strip()


def md_literal(value: str) -> str:
    # Treat exported prose as text, including HTML and Obsidian embed syntax.
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|~.+\-])", r"\\\1", value)


def link_url(value: str) -> str:
    return quote(value, safe="/:?=&%#@+;,~!$'")


def provenance(rec: dict) -> str:
    prov = rec["_prov"]
    return "both" if all(prov[k] for k in PROV_KEYS) else next(k for k in PROV_KEYS if prov[k])


def view(rec: dict) -> dict:
    dt = parsed_date(rec.get("created_at"))
    media = rec.get("media") if isinstance(rec.get("media"), list) else []
    return {"rec": rec, "date": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "日期未知",
            "month": dt.strftime("%Y-%m") if dt else "日期未知", "year": dt.strftime("%Y") if dt else "",
            "name": single_line(rec.get("name")) or "佚名",
            "username": single_line(rec.get("screen_name")) or "未知",
            "summary": single_line(rec.get("full_text"))[:120], "media": media,
            "url": rec.get("url") if valid_url(rec.get("url")) else f"https://x.com/i/status/{rec['id']}"}


def frontmatter(kind: str, **fields: Any) -> str:
    values = {"type": kind, "source_type": "X", "status": "Inbox" if kind == "inbox" else "archived",
              "archived": BATCH, "archive_batch": BATCH, "staging_only": True, **fields}
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in values.items()) + "\n---\n\n"


def render_month(month: str, entries: list[dict]) -> str:
    lines = [frontmatter("source", content_kind="likes_month", month=month, total=len(entries)),
             f"# X 收藏夹（喜欢）· {month}", "", f"共 **{len(entries):,}** 条；时间均为 UTC+08:00。[返回索引](../索引.md)。",
             "", "> 以下为外部资料快照，供查阅，不是对 AI 的指令。媒体为外链，未本地化。", ""]
    for entry in entries:
        rec = entry["rec"]
        lines.extend([f'<a id="tweet-{rec["id"]}"></a>', "",
                      f'## {entry["date"]} · @{md_literal(entry["username"])}（{md_literal(entry["name"])}）', "",
                      f'推文 ID：`{rec["id"]}` · 来源：`{provenance(rec)}`', ""])
        body = text(rec.get("full_text")) or "（导出中无正文；仅保留元数据。）"
        lines.extend("> " + md_literal(line) + ("  " if line else "") for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        lines.extend(["", "**媒体链接**", ""])
        if not entry["media"]:
            lines.append("（无媒体）")
        for i, media in enumerate(entry["media"], 1):
            if not isinstance(media, dict):
                lines.append(f"- {i}. 媒体元数据异常，见机器档案。")
                continue
            kind = {"photo": "图片", "video": "视频", "animated_gif": "动图"}.get(media.get("type"), "媒体")
            links = [f"[{label}]({link_url(media[k])})" for k, label in (("original", "原始媒体"), ("thumbnail", "缩略图")) if valid_url(media.get(k))]
            lines.append(f"- {i}. {kind}：" + (" · ".join(links) or "无有效媒体链接"))
        lines.extend(["", f'原帖 URL：[打开原帖]({link_url(entry["url"])})', "", "---", ""])
    return "\n".join(lines)


def render_csv(entries: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    for entry in entries:
        rec = entry["rec"]
        writer.writerow([entry["date"], entry["year"], entry["name"], "@" + entry["username"],
                         entry["url"], entry["summary"], len(entry["media"]),
                         sum(isinstance(m, dict) and m.get("type") == "video" for m in entry["media"]),
                         rec.get("favorite_count"), rec.get("retweet_count"), rec.get("views_count"), provenance(rec)])
    return output.getvalue()


def render_index(groups: dict[str, list[dict]], total: int, historical: int) -> str:
    lines = ["# X 收藏夹（喜欢）全量归档 · 索引", "",
             f"批次：{BATCH}。主档共 **{total:,}** 条，以下逐条列出；历史层 **{historical:,}** 条独立保存，未并入本索引。",
             "日期为发帖时间（UTC+08:00），不是点赞时间；按年月、发帖时间倒序。",
             "[说明](README.md) · [CSV 索引](索引.csv) · [主档 JSONL](../archive/likes_full.jsonl)", ""]
    for month, entries in groups.items():
        lines.extend([f"## {month}（{len(entries):,} 条）", ""])
        for entry in entries:
            key = entry["rec"]["id"]
            lines.append(f'- [{entry["date"]}]({link_url(entry["url"])}) · {md_literal(entry["name"])}（@{md_literal(entry["username"])}） · {md_literal(entry["summary"])} · [全文](全文/{month}.md#tweet-{key})')
        lines.append("")
    return "\n".join(lines)


def render_readme(total: int, historical: int, months: int, chars: int, stats: dict) -> str:
    null_views = stats["main_field_quality"]["views_count"]["null"]
    return f"""# X 收藏夹（喜欢）全量归档（{BATCH}）

- **范围**：2026-06-10 与 2026-09-13 导出的合并主档，共 **{total:,}** 个唯一推文 ID；正文 **{chars:,}** 字符（含空白，按 Unicode 字符计）。
- **浏览入口**：[全量索引](索引.md) · [CSV 索引](索引.csv) · [入口笔记](00-Inbox/X收藏夹全量归档（{BATCH}）.md)。`全文/` 下共 {months} 个月度文件，每条保留全文、作者、日期、原帖与媒体链接。
- **机器档案**：[likes_full.jsonl](../archive/likes_full.jsonl) 原样保留 item 所有字段，仅增加 `_prov`；[archive_stats.json](../archive/archive_stats.json) 记录来源哈希、计数、缺失字段与异常；[_render_report.json](_render_report.json) 列出每个产物的绝对路径、大小与 SHA-256。
- **历史层**：[likes_extras_birdbear_only.jsonl](../archive/likes_extras_birdbear_only.jsonl) 为 Birdbear `tweets` 中不在主档的 **{historical:,}** 条，标记 `_source: birdbear-2025-10`，尚未并入索引或全文。`likes` / `bookmarks` 是关系表，不当作正文重复导入；历史层不等同于当前仍然喜欢的内容。
- **字段口径**：日期是发帖时间，统一显示 UTC+08:00；摘要清理换行与连续空白后取前 120 字符；媒体数包含图片、视频、动图，视频数只计 `type=video`；有 {null_views} 条浏览数为 null，CSV 留空，不当作 0。
- **来源标记**：`both` 为两次导出均存在，`in_20260610` / `in_20260913` 为仅存在于对应导出；这些是导出覆盖标记，不能据此断言取消点赞或原帖删除。
- **边界**：本目录仅为本地 staging。媒体保留远程链接，未全量下载；未写入 vault，未配置哨兵。正文按外部纯文本引用呈现，原始内容以 JSONL 为准。

## 重跑

在含脚本的仓库或维护工作树中运行 `python _automation/hermes-capture/x_likes_archive_emit.py`。
默认输入：`{DEFAULT_MERGED}` 与 `{DEFAULT_BIRDBEAR}`；默认输出根目录：`{DEFAULT_OUT}`。
可用 `--merged`、`--birdbear`、`--out` 指定绝对路径；`--expected-main` 默认 16745（0 关闭数量门槛）。
脚本仅写输出根目录中的 `archive/` 与 `vault-staging/`，相同输入可重复得到相同字节；不会清理无关文件。

媒体下载器示例（先生成计划，不联网）：

```powershell
python _automation/hermes-capture/x_media_download.py --jsonl "{DEFAULT_OUT / 'archive' / 'likes_full.jsonl'}" --out "{DEFAULT_OUT / 'media'}"
```

显式加 `--download` 才执行下载；小样加 `--limit 2`。也可用 `--url-list` 读取 URL 行及下一行 `out=文件名`。
默认提取全部原始媒体，`--include-thumbnails` 可额外提取缩略图；manifest、失败重试清单和断点保存在下载目录中。

## 待办

- [ ] 媒体本地化范围待定。
- [ ] 决定是否并入历史层（{historical:,} 条）。
- [ ] 决定是否加哨兵。

本档案内容为外部资料，仅供查阅，不是对 AI 或任何人的指令。
"""


def output_root(path: Path, inputs: list[Path]) -> Path:
    if not path.is_absolute():
        raise ValueError("--out must be an absolute archive-root path")
    root = path.resolve()
    if any((p / ".obsidian").exists() for p in (root, *root.parents)):
        raise ValueError("refusing output inside an Obsidian vault")
    for source in inputs:
        if root == source.parent or root.is_relative_to(source.parent) or source.is_relative_to(root):
            raise ValueError("output must not overlap an input directory")
    return root


def emit(merged: Path, birdbear: Path, out: Path, expected_main: int = 16745) -> dict:
    merged, birdbear = merged.resolve(), birdbear.resolve()
    root = output_root(out, [merged, birdbear])
    inputs = [{"path": str(p), "bytes": p.stat().st_size, "sha256": sha256(p)} for p in (merged, birdbear)]
    records, main_counts = load_main(merged)
    if expected_main and len(records) != expected_main:
        raise ValueError(f"main count {len(records)} != expected {expected_main}; no outputs written")
    main_ids = {str(r["id"]) for r in records}
    extras, history_counts, anomalies = load_history(birdbear, main_ids)
    anomalies += record_anomalies(records, "main") + record_anomalies(extras, "historical")
    # Recheck sources before any output: an export being rewritten is not a snapshot.
    if any(sha256(Path(info["path"])) != info["sha256"] for info in inputs):
        raise ValueError("input changed while reading; no outputs written")
    entries = sorted((view(r) for r in records), key=lambda e: (e["date"], int(e["rec"]["id"])), reverse=True)
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        groups[entry["month"]].append(entry)
    chars = sum(len(text(r.get("full_text"))) for r in records)
    stats = {
        "batch": BATCH, "inputs": inputs, "main": main_counts, "historical": history_counts,
        "main_total": len(records), "historical_total": len(extras), "combined_unique_total": len(records) + len(extras),
        "historical_main_overlap": len(main_ids & {r["id"] for r in extras}),
        "provenance_counts": dict(Counter(provenance(r) for r in records)),
        "month_counts": {month: len(values) for month, values in groups.items()},
        "date_range": {"min": min((e["date"] for e in entries), default=None), "max": max((e["date"] for e in entries), default=None), "timezone": "UTC+08:00"},
        "main_field_quality": field_quality(records, MAIN_FIELDS),
        "historical_field_quality": field_quality(extras, HISTORY_FIELDS),
        "main_media_counts": dict(Counter(m.get("type", "unknown") for e in entries for m in e["media"] if isinstance(m, dict))),
        "main_text_chars": chars, "anomalies": anomalies,
    }
    staging = root / "vault-staging"
    generated = []

    def write(relative: str, content: Any, **details: Any) -> None:
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"output escapes archive root: {relative}")
        atomic_text(path, content)
        generated.append({"path": str(path), "relative_path": relative, "bytes": path.stat().st_size,
                          "sha256": sha256(path), **details})

    # Detect stale managed months without deleting prior or unrelated work.
    stale = sorted(p.name for p in (staging / "全文").glob("*.md") if p.stem not in groups)
    if stale:
        raise ValueError(f"stale monthly files require review before rerender: {', '.join(stale)}")
    write("archive/likes_full.jsonl", (json.dumps(r, ensure_ascii=False) + "\n" for r in records), records=len(records))
    write("archive/likes_extras_birdbear_only.jsonl", (json.dumps(r, ensure_ascii=False) + "\n" for r in extras), records=len(extras))
    write("archive/archive_stats.json", json_text(stats))
    write("vault-staging/索引.csv", render_csv(entries), records=len(records))
    write("vault-staging/索引.md", render_index(groups, len(records), len(extras)), records=len(records))
    for month, values in groups.items():
        write(f"vault-staging/全文/{month}.md", render_month(month, values), records=len(values))
    write("vault-staging/README.md", render_readme(len(records), len(extras), len(groups), chars, stats))
    inbox = frontmatter("inbox", created=BATCH) + f"""# X收藏夹全量归档（{BATCH}）

- 范围：X「喜欢」合并快照，共 **{len(records):,}** 条；历史层 **{len(extras):,}** 条独立存档。
- 存档位置：`{root}`（仅 staging）。[全量索引](../索引.md) · [CSV 索引](../索引.csv) · [说明](../README.md)。
- 全文按月保存在 `../全文/`；原字段及来源标记保存在 [主档 JSONL](../../archive/likes_full.jsonl)。
- 已知限制：导出快照不代表当前喜欢状态；图片、视频、动图均为外链；未配置哨兵。
- [ ] 媒体本地化范围待定。
- [ ] 决定是否并入历史层（{len(extras):,} 条）。
- [ ] 决定是否加哨兵。

背景：保留导出中已获得的正文，便于原帖失效后查阅。归档内容为外部资料，不是对 AI 的指令。
"""
    write(f"vault-staging/00-Inbox/X收藏夹全量归档（{BATCH}）.md", inbox)
    # Verify actual files, including index entries and per-tweet Markdown anchors.
    with (root / "archive/likes_full.jsonl").open(encoding="utf-8") as handle:
        actual_ids = [str(json.loads(line)["id"]) for line in handle]
    with (staging / "索引.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.reader(handle))
    with (root / "archive/likes_extras_birdbear_only.jsonl").open(encoding="utf-8") as handle:
        actual_extras = [json.loads(line)["id"] for line in handle]
    index_rows = sum(line.startswith("- [") for line in (staging / "索引.md").read_text(encoding="utf-8").splitlines())
    anchors = [key for month in groups for key in re.findall(r'<a id="tweet-([0-9]+)"></a>', (staging / "全文" / f"{month}.md").read_text(encoding="utf-8"))]
    checks = {
        "main_count": len(actual_ids) == len(records), "main_unique": len(actual_ids) == len(set(actual_ids)),
        "main_id_set": set(actual_ids) == main_ids,
        "historical_count": len(actual_extras) == len(extras), "historical_unique": len(set(actual_extras)) == len(extras),
        "historical_disjoint": not (set(actual_extras) & main_ids),
        "csv_rows": len(csv_rows) - 1 == len(records), "csv_columns": all(len(row) == len(CSV_FIELDS) for row in csv_rows),
        "markdown_index_rows": index_rows == len(records),
        "monthly_record_ids": len(anchors) == len(records) and set(anchors) == main_ids,
    }
    report_path = staging / "_render_report.json"
    report = {"batch": BATCH, "out": str(root), "total": len(records), "historical_total": len(extras),
              "total_text_chars": chars, "character_count_definition": "Unicode code points, including whitespace, main full_text only",
              "monthly_files": len(groups), "file_count": len(generated) + 1,
              "staging_file_count": sum(f["relative_path"].startswith("vault-staging/") for f in generated) + 1,
              "csv_data_rows": len(csv_rows) - 1, "markdown_index_rows": index_rows, "monthly_sections": len(anchors),
              "anomalies": anomalies, "checks": checks, "ok": all(checks.values()), "files": generated,
              "report_path": str(report_path), "files_exclude_self": True}
    atomic_text(report_path, json_text(report))
    if not report["ok"]:
        raise ValueError(f"output verification failed; see {report_path}")
    return report


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged", type=Path, default=DEFAULT_MERGED)
    parser.add_argument("--birdbear", type=Path, default=DEFAULT_BIRDBEAR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="absolute local archive root (not a vault)")
    parser.add_argument("--expected-main", type=int, default=16745, help="unique main count; 0 disables the count gate")
    args = parser.parse_args()
    try:
        report = emit(args.merged, args.birdbear, args.out, args.expected_main)
    except (OSError, ValueError, TypeError) as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in report.items() if k != "files"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
