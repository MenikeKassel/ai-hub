from __future__ import annotations
import hashlib
import mimetypes
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol
from .core import PostRecord, proxy_opener


def download_images(
    post: PostRecord,
    media_root: Path,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], list[str]]:
    destination = Path(media_root) / post.post_id
    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(post.media):
        if str(item.get("type") or "").lower() not in {"photo", "image"}:
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        request = urllib.request.Request(url, headers={"User-Agent": "ai-hub-kol-research/2.0"})
        try:
            with proxy_opener().open(request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    errors.append(f"{url}: non-image response")
                    continue
                data = response.read(20 * 1024 * 1024 + 1)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        if len(data) > 20 * 1024 * 1024:
            errors.append(f"{url}: image exceeds 20 MB")
            continue
        destination.mkdir(parents=True, exist_ok=True)
        extension = mimetypes.guess_extension(content_type) or Path(url).suffix or ".jpg"
        path = destination / f"{index + 1}{extension}"
        path.write_bytes(data)
        saved.append(
            {
                "type": "image",
                "source_url": url,
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    return saved, errors


def media_disk_usage(path: Path) -> int:
    if not Path(path).exists():
        return 0
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())
