#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan/download X media with bounded concurrency, retries and resumable files.

Examples (planning is the default; add --download to fetch):
  python x_media_download.py --url-list media-urls.txt --out D:/temp/x-media
  python x_media_download.py --jsonl likes_full.jsonl --out D:/temp/x-media --limit 2 --download

Lists accept a URL followed by an optional indented out=filename line (aria2
style); URL out=filename on one line also works. Filenames must be flat, safe
Windows filenames. JSONL extracts every original asset from media arrays,
including nested quoted/retweeted objects; --include-thumbnails adds previews.
URLs are deduplicated. Query strings are preserved and used in stable filenames.

Output: plan.json, manifest.jsonl (append-only completion/failure journal),
failures.txt (retryable URL/out list), run_report.json, downloaded files and
.x-media-download/ (partial bytes/validators and exclusive process lock).
Completed files are skipped only after matching manifest URL, size and SHA-256.
Partial files resume via Range + If-Range when the server supplied a validator;
otherwise they restart safely. Successful files are promoted atomically.
An interrupted process can leave .x-media-download/run.lock; inspect the recorded
PID and remove that lock manually only after that process has stopped.

Standard library only. Reads sources without modification; never writes a vault.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen

CONTROL_NAMES = {"plan.json", "manifest.jsonl", "failures.txt", "run_report.json", ".x-media-download"}
WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Asset:
    url: str
    filename: str


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".x-media-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        valid = parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password
    except ValueError:
        valid = False
    if not valid or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("media URL must be an HTTP(S) URL without credentials or whitespace")


def check_name(name: str) -> None:
    if (not name or len(name) > 200 or re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            or name in (".", "..") or name != name.rstrip(" .") or name != name.lstrip()
            or WINDOWS_RESERVED.match(name) or name.casefold() in CONTROL_NAMES):
        raise ValueError("out= must be a flat, non-reserved Windows filename (max 200 characters)")


def auto_name(url: str, prefix: str = "media") -> str:
    parts = urlsplit(url)
    extension = Path(unquote(parts.path)).suffix.lower()
    fmt = parse_qs(parts.query).get("format", [""])[0].lower()
    if fmt in ("jpg", "jpeg", "png", "webp", "gif", "mp4"):
        extension = "." + fmt
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", extension):
        extension = ".bin"
    prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", prefix)[:90] or "media"
    return f"{prefix}_{hashlib.sha256(url.encode()).hexdigest()[:20]}{extension}"


def deduplicate(assets: list[Asset]) -> list[Asset]:
    seen_urls: set[str] = set()
    seen_names: dict[str, str] = {}
    result = []
    for asset in assets:
        check_url(asset.url)
        check_name(asset.filename)
        key = asset.filename.casefold()
        if key in seen_names and seen_names[key] != asset.url:
            raise ValueError(f"conflicting output filename: {asset.filename}")
        seen_names[key] = asset.url
        if asset.url not in seen_urls:
            result.append(asset)
            seen_urls.add(asset.url)
    return result


def parse_url_list(path: Path) -> list[Asset]:
    assets: list[Asset] = []
    pending_url = pending_name = None
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("out="):
            if pending_url is None or pending_name is not None:
                raise ValueError(f"URL list line {number}: orphan or repeated out=")
            pending_name = line[4:].strip()
            check_name(pending_name)
            continue
        if pending_url is not None:
            assets.append(Asset(pending_url, pending_name or auto_name(pending_url)))
        match = re.fullmatch(r"(https?://\S+?)(?:\s+out=(.+))?", line)
        if not match:
            raise ValueError(f"URL list line {number}: expected URL or out=filename")
        pending_url, pending_name = match.groups()
    if pending_url is not None:
        assets.append(Asset(pending_url, pending_name or auto_name(pending_url)))
    return deduplicate(assets)


def extract_jsonl(path: Path, include_thumbnails: bool = False) -> list[Asset]:
    assets: list[Asset] = []

    def visit(rec: dict, location: str) -> None:
        media = rec.get("media", [])
        if not isinstance(media, list):
            raise ValueError(f"{location}: media must be an array")
        for i, item in enumerate(media, 1):
            if not isinstance(item, dict):
                raise ValueError(f"{location}: media item must be an object")
            original = item.get("original") or item.get("media_url_https") or item.get("url")
            if not isinstance(original, str) or not original:
                raise ValueError(f"{location}: media item {i} lacks an asset URL")
            prefix = f"{rec.get('screen_name', 'x')}_{rec.get('id', 'unknown')}_{item.get('type', 'media')}_{i}"
            assets.append(Asset(original, auto_name(original, prefix)))
            if include_thumbnails:
                thumb = item.get("thumbnail") or item.get("thumbnailUrl")
                if thumb:
                    assets.append(Asset(thumb, auto_name(thumb, prefix + "_thumb")))
        for key in ("quoted_status", "retweeted_status"):
            nested = rec.get(key)
            if isinstance(nested, dict):
                visit(nested, location + "." + key)

    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL line {number}: invalid JSON") from exc
            if not isinstance(rec, dict):
                raise ValueError(f"JSONL line {number}: expected a record object")
            visit(rec, f"JSONL line {number}")
    return deduplicate(assets)


def checked_child(root: Path, name: str) -> Path:
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError("output path escapes download directory")
    return path


def latest_successes(path: Path) -> dict[str, dict]:
    latest = {}
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"manifest line {number}: invalid JSON") from exc
            if not isinstance(entry, dict):
                raise ValueError(f"manifest line {number}: expected an object")
            if entry.get("status") in ("downloaded", "skipped"):
                latest[entry["filename"].casefold()] = entry
    return latest


def download_one(asset: Asset, root: Path, previous: dict | None, retries: int, timeout: float,
                 retry_delay: float = 1.0) -> dict:
    target = checked_child(root, asset.filename)
    result = {**asdict(asset), "finished_at": "", "attempts": 0}
    if target.exists():
        if (previous and previous.get("url") == asset.url
                and target.stat().st_size == previous.get("bytes")
                and digest_file(target) == previous.get("sha256")):
            return {**previous, "status": "skipped", "attempts": 0,
                    "finished_at": datetime.now(timezone.utc).isoformat()}
        # Do not overwrite an unrelated or modified user file.
        return {**result, "status": "failed", "error": "existing file is not verified by manifest; move it aside before retrying",
                "finished_at": datetime.now(timezone.utc).isoformat()}
    state = root / ".x-media-download"
    key = hashlib.sha256((asset.filename + "\0" + asset.url).encode()).hexdigest()
    partial, metadata = state / (key + ".part"), state / (key + ".json")
    for attempt in range(retries + 1):
        result["attempts"] = attempt + 1
        delay = retry_delay * min(2 ** attempt, 16)
        try:
            info = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else {}
            if info.get("url") != asset.url:
                info = {}
            validator = info.get("etag")
            if not validator or validator.startswith("W/"):
                validator = info.get("last_modified")
            offset = partial.stat().st_size if partial.exists() and validator else 0
            headers = {"User-Agent": "x-local-archive/1.0", "Accept-Encoding": "identity"}
            if offset:
                headers.update({"Range": f"bytes={offset}-", "If-Range": validator})
            with urlopen(Request(asset.url, headers=headers), timeout=timeout) as response:
                status = response.status
                if status not in (200, 206):
                    raise ValueError(f"unexpected HTTP status {status}")
                if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    raise ValueError("server sent encoded content despite Accept-Encoding identity")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type in ("text/html", "application/xhtml+xml"):
                    raise ValueError("server returned an HTML page instead of media")
                length_header = response.headers.get("Content-Length")
                length = int(length_header) if length_header is not None else None
                total = length
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not offset or not match:
                        raise ValueError("unexpected or invalid partial response")
                    start, end, total = map(int, match.groups())
                    if start != offset or end < start or end >= total or (length is not None and end - start + 1 != length):
                        raise ValueError("Content-Range does not match partial file")
                else:
                    offset = 0  # Server ignored Range or If-Range no longer matched.
                write_json(metadata, {"url": asset.url, "etag": response.headers.get("ETag"),
                                      "last_modified": response.headers.get("Last-Modified")})
                received = 0
                with partial.open("ab" if offset else "wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                if length is not None and received != length:
                    raise OSError(f"truncated body: received {received} of {length} bytes")
                size = partial.stat().st_size
                if total is not None and size != total:
                    raise OSError(f"incomplete file: received {size} of {total} bytes")
                if size == 0:
                    raise ValueError("empty media response")
                digest = digest_file(partial)
                # Never clobber a file created concurrently outside this process.
                if target.exists():
                    raise ValueError("target appeared during download; refusing overwrite")
                os.replace(partial, target)
                return {**result, "status": "downloaded", "bytes": size, "sha256": digest,
                        "content_type": content_type, "resumed_from": offset,
                        "finished_at": datetime.now(timezone.utc).isoformat()}
        except HTTPError as exc:
            result["error"] = f"HTTP {exc.code}"
            retry_after = exc.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = min(float(retry_after), 60.0)
            exc.close()
            if exc.code == 416:
                # A stale/complete .part should be re-fetched, not accepted unseen.
                write_json(metadata, {})
            elif exc.code not in RETRYABLE_HTTP:
                break
        except (OSError, URLError, http.client.HTTPException, ValueError) as exc:
            # Avoid echoing response bodies or credentials from source URLs.
            result["error"] = f"{type(exc).__name__}: {exc}" if isinstance(exc, (ValueError, OSError)) and not isinstance(exc, URLError) else type(exc).__name__
            if isinstance(exc, ValueError):
                write_json(metadata, {})
        if attempt < retries:
            time.sleep(delay)
    return {**result, "status": "failed", "finished_at": datetime.now(timezone.utc).isoformat()}


def run(assets: list[Asset], out: Path, download: bool = False, workers: int = 4,
        retries: int = 3, timeout: float = 30.0, retry_delay: float = 1.0) -> dict:
    assets = deduplicate(assets)
    if workers < 1 or retries < 0 or timeout <= 0:
        raise ValueError("workers and timeout must be positive; retries must be non-negative")
    root = out.resolve()
    if any((p / ".obsidian").exists() for p in (root, *root.parents)):
        raise ValueError("refusing download into an Obsidian vault")
    root.mkdir(parents=True, exist_ok=True)
    for name in CONTROL_NAMES | {a.filename for a in assets}:
        checked_child(root, name)
    state = root / ".x-media-download"
    state.mkdir(exist_ok=True)
    lock = state / "run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("download directory is locked; inspect .x-media-download/run.lock") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}, handle)
        write_json(root / "plan.json", {"total": len(assets), "download": download, "assets": [asdict(a) for a in assets]})
        if not download:
            return {"mode": "plan", "planned": len(assets), "out": str(root), "network_requests": 0}
        manifest = root / "manifest.jsonl"
        results = []
        # Start a fresh journal line even if the previous process died mid-write.
        if manifest.exists() and manifest.stat().st_size:
            with manifest.open("rb+") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # Preserve torn data separately; keep the journal valid JSONL.
                    handle.seek(0)
                    content = handle.read()
                    boundary = content.rfind(b"\n") + 1
                    tail = content[boundary:]
                    try:
                        json.loads(tail)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        (state / f"torn-manifest-{time.time_ns()}.txt").write_bytes(tail)
                        handle.truncate(boundary)
                    else:
                        handle.seek(0, os.SEEK_END)
                        handle.write(b"\n")
        previous = latest_successes(manifest)
        with manifest.open("a", encoding="utf-8", newline="\n") as journal:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(download_one, a, root, previous.get(a.filename.casefold()), retries, timeout, retry_delay): a for a in assets}
                for future in as_completed(futures):
                    try:
                        entry = future.result()
                    except (OSError, ValueError) as exc:
                        entry = {**asdict(futures[future]), "status": "failed", "error": type(exc).__name__,
                                 "finished_at": datetime.now(timezone.utc).isoformat()}
                    results.append(entry)
                    journal.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    journal.flush()
                    print(json.dumps({"completed": len(results), "total": len(assets), "status": entry["status"], "filename": entry["filename"]}, ensure_ascii=False), flush=True)
        failed = sorted((r for r in results if r["status"] == "failed"), key=lambda r: r["filename"])
        (root / "failures.txt").write_text("".join(f"{r['url']}\n  out={r['filename']}\n" for r in failed), encoding="utf-8")
        report = {"mode": "download", "out": str(root), "planned": len(assets),
                  **{status: sum(r["status"] == status for r in results) for status in ("downloaded", "skipped", "failed")},
                  "manifest": str(manifest), "failures": str(root / "failures.txt")}
        write_json(root / "run_report.json", report)
        return report
    finally:
        lock.unlink()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url-list", type=Path)
    source.add_argument("--jsonl", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="perform downloads (default: plan only)")
    parser.add_argument("--include-thumbnails", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="select first N unique URLs; 0 selects all")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3, help="additional attempts after the first")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    try:
        if args.limit < 0:
            raise ValueError("--limit must be non-negative")
        source_path = (args.url_list or args.jsonl).resolve()
        root = args.out.resolve()
        if source_path.is_relative_to(root) or root.is_relative_to(source_path.parent):
            # An archive/media sibling is safe; the archive source directory is not.
            raise ValueError("download output must be separate from the input directory")
        assets = parse_url_list(source_path) if args.url_list else extract_jsonl(source_path, args.include_thumbnails)
        if args.limit:
            assets = assets[:args.limit]
        report = run(assets, root, args.download, args.workers, args.retries, args.timeout)
    except (OSError, ValueError, TypeError) as exc:
        print(f"media download failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
