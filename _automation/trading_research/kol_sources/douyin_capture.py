"""Build KOL-console capture files for Douyin KOLs from the local Douyin archive.

The console ingests platforms without a live adapter (Douyin, Xiaohongshu, ...)
through JSON captures at
``_runtime/trading/kol-discovery/captures/<platform>/{accounts,content}.json``.
This module turns the local Douyin favourites archive
(``douyin-collection-archive``: download manifest + whisper transcripts) into
that format for every active Douyin KOL in the console.

Text resolution per archived item:

1. whisper transcript (``staging/transcripts/<date>_<title>_<aweme_id>/*.txt``)
2. archived description / title (skipped when it is the ``no_title`` placeholder)

Captures are regenerated from scratch on every sync, so the archive stays the
source of truth.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEFAULT_MANIFEST = Path(
    r"D:/aiworkspace/tool/douyin-collection-archive/douyin-downloader/Downloaded/download_manifest.jsonl"
)
DEFAULT_TRANSCRIPTS = Path(
    r"D:/aiworkspace/tool/douyin-collection-archive/staging/transcripts"
)
PLACEHOLDER_TEXT = {"", "no_title", "none", "null"}
ITEM_ID_IN_NAME = re.compile(r"(\d{10,25})")
DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
GALLERY_TYPES = {"gallery", "images", "note"}


def default_manifest() -> Path:
    return Path(os.environ.get("DOUYIN_ARCHIVE_MANIFEST", str(DEFAULT_MANIFEST)))


def default_transcripts() -> Path:
    return Path(os.environ.get("DOUYIN_ARCHIVE_TRANSCRIPTS", str(DEFAULT_TRANSCRIPTS)))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def read_manifest_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Group archive manifest rows by ``author_name``."""
    by_author: dict[str, list[dict[str, Any]]] = {}
    if not path.is_file():
        return by_author
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        author = str(row.get("author_name") or "").strip()
        if author:
            by_author.setdefault(author, []).append(row)
    return by_author


def index_transcripts(root: Path) -> dict[str, Path]:
    """Map ``aweme_id`` to the transcript directory that contains it."""
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = ITEM_ID_IN_NAME.search(child.name)
        if match:
            index[match.group(1)] = child
    return index


def transcript_text(directory: Path) -> str:
    """Return the whisper transcript text stored in ``directory``."""
    candidates = sorted(
        (path for path in directory.glob("*.txt") if path.is_file()),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    for path in candidates:
        try:
            text = _clean_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if text:
            return text
    return ""


def item_text(item_id: str, row: dict[str, Any], transcripts: dict[str, Path]) -> tuple[str, str]:
    """Resolve the usable text for one archived item."""
    directory = transcripts.get(item_id)
    if directory is not None:
        text = transcript_text(directory)
        if text:
            return text, "transcript"
    for key in ("desc", "title", "text"):
        value = str(row.get(key) or "").strip()
        if value and value.casefold() not in PLACEHOLDER_TEXT:
            return _clean_text(value), "description"
    return "", ""


def _posted_at(row: dict[str, Any], item_id: str, transcripts: dict[str, Path]) -> str:
    """Derive an ISO timestamp; the archive keeps dates only, so use midday."""
    date_text = str(row.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        directory = transcripts.get(item_id)
        match = DATE_IN_NAME.search(directory.name) if directory is not None else None
        date_text = match.group(1) if match else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        return ""
    try:
        datetime.fromisoformat(date_text)
    except ValueError:
        return ""
    # Midday keeps date-only captures clear of review-window boundaries.
    return f"{date_text}T12:00:00+08:00"


def build_captures(
    kols: Iterable[dict[str, Any]],
    *,
    manifest: Path,
    transcripts: Path,
) -> dict[str, Any]:
    """Build ``accounts.json`` / ``content.json`` payloads for Douyin KOLs."""
    rows_by_author = read_manifest_rows(manifest)
    transcript_index = index_transcripts(transcripts)
    accounts: list[dict[str, Any]] = []
    content: list[dict[str, Any]] = []
    stats = {
        "kols": 0,
        "items": 0,
        "with_transcript": 0,
        "with_description": 0,
        "skipped_no_text": 0,
        "skipped_no_timestamp": 0,
    }
    for kol in kols:
        if str(kol.get("platform") or "").casefold() != "douyin":
            continue
        stats["kols"] += 1
        handle = str(kol.get("handle") or "").strip()
        display_name = str(kol.get("display_name") or handle).strip()
        profile_url = str(kol.get("profile_url") or "").strip()
        rows = rows_by_author.get(display_name, [])
        accounts.append(
            {
                "external_account_id": handle,
                "handle": handle,
                "display_name": display_name,
                "profile_url": profile_url or f"https://www.douyin.com/user/{handle}",
                "evidence": [
                    {"kind": "douyin_archive_manifest", "path": str(manifest)},
                    {"kind": "archive_item_count", "count": len(rows)},
                ],
            }
        )
        for row in rows:
            item_id = str(row.get("aweme_id") or "").strip()
            if not re.fullmatch(r"\d{5,25}", item_id):
                continue
            text, source = item_text(item_id, row, transcript_index)
            if not text:
                stats["skipped_no_text"] += 1
                continue
            posted_at = _posted_at(row, item_id, transcript_index)
            if not posted_at:
                stats["skipped_no_timestamp"] += 1
                continue
            media_type = str(row.get("media_type") or "video").strip().casefold()
            segment = "note" if media_type in GALLERY_TYPES else "video"
            content.append(
                {
                    "external_item_id": item_id,
                    "external_account_id": handle,
                    "url": f"https://www.douyin.com/{segment}/{item_id}",
                    "text": text,
                    "text_source": source,
                    "posted_at": posted_at,
                    "media_type": media_type,
                    "tags": [str(tag) for tag in (row.get("tags") or []) if str(tag).strip()],
                    "file_count": len(row.get("file_paths") or []),
                }
            )
            stats["items"] += 1
            stats["with_transcript" if source == "transcript" else "with_description"] += 1
    content.sort(key=lambda item: str(item.get("posted_at") or ""), reverse=True)
    return {"accounts": accounts, "content": content, "stats": stats}


def write_captures(capture_root: Path, payload: dict[str, Any]) -> dict[str, str]:
    """Write the capture payloads atomically; returns the written paths."""
    directory = Path(capture_root) / "douyin"
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, value in (("accounts.json", payload["accounts"]), ("content.json", payload["content"])):
        target = directory / name
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        written[name] = str(target)
    return written


def sync_douyin_captures(
    store: Any,
    *,
    manifest: Path | None = None,
    transcripts: Path | None = None,
    capture_root: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Regenerate Douyin captures from the archive for the registered KOLs."""
    manifest_path = Path(manifest) if manifest else default_manifest()
    transcripts_path = Path(transcripts) if transcripts else default_transcripts()
    kols = [
        kol
        for kol in store.list_kols("active")
        if str(kol.get("platform") or "").casefold() == "douyin"
    ]
    payload = build_captures(kols, manifest=manifest_path, transcripts=transcripts_path)
    result: dict[str, Any] = {
        "ok": True,
        "manifest": str(manifest_path),
        "manifest_found": manifest_path.is_file(),
        "transcripts": str(transcripts_path),
        "transcripts_found": transcripts_path.is_dir(),
        "capture_root": str(capture_root),
        "dry_run": bool(dry_run),
        **payload["stats"],
    }
    if dry_run:
        result["accounts"] = payload["accounts"]
        result["content"] = payload["content"]
        return result
    result["written"] = write_captures(Path(capture_root), payload)
    return result
