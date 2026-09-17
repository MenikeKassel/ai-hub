from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from .core import ProviderAttempt, ProviderFetchResult, TwitterAuthenticationError, TwitterProviderError, TwitterRateLimitError, XPostProvider, ZhihuProviderError
from .credentials import KeyringCredentialStore
from .sessions import PublicBackupGate, PublicBackupNotFoundError, PublicBackupRateLimitError, XSessionManager


class FxTwitterPublicPostProvider:
    """Strict, no-cookie single-post adapter backed by the pinned XTF parser."""

    name = "fxtwitter"
    adapter_version = "x-tweet-fetcher-3.0.0+f057d6b"
    _url_pattern = re.compile(r"^https://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{5,25})/?$")

    def __init__(self, gate: PublicBackupGate, *, timeout_seconds: int = 15):
        self.gate = gate
        self.timeout_seconds = max(5, min(int(timeout_seconds), 30))

    @staticmethod
    def _normalise(tweet: dict[str, Any], response: dict[str, Any], username: str, post_id: str, url: str) -> dict[str, Any]:
        site_packages = Path(os.environ.get("XTF_SITE_PACKAGES", Path(__file__).resolve().parents[3] / "_runtime" / "venv-x-fetcher" / "Lib" / "site-packages"))
        import sys
        if site_packages.is_dir() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
        try:
            from xtf.backends.fxtwitter import normalize_tweet_json  # type: ignore
            normalized = normalize_tweet_json(tweet)
        except Exception as exc:
            raise TwitterProviderError(f"pinned XTF parser unavailable: {type(exc).__name__}") from exc
        tweet_id = str(tweet.get("id") or response.get("tweet_id") or "")
        if tweet_id != post_id:
            raise TwitterProviderError("FxTwitter returned a different post ID")
        created = str(tweet.get("created_at") or "")
        created_at = parsedate_to_datetime(created).astimezone(timezone.utc).isoformat(timespec="seconds") if created else ""
        if not created_at:
            raise TwitterProviderError("FxTwitter returned no exact timestamp")
        media: list[dict[str, Any]] = []
        for item in (tweet.get("media") or {}).get("all", []) if isinstance(tweet.get("media"), dict) else []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            media.append({"type": str(item.get("type") or "image"), "url": str(item.get("url")), "width": item.get("width", 0), "height": item.get("height", 0)})
        article = normalized.get("article") if isinstance(normalized.get("article"), dict) else {}
        quote = tweet.get("quote") if isinstance(tweet.get("quote"), dict) else {}
        quote_author = quote.get("author") if isinstance(quote.get("author"), dict) else {}
        return {
            "id": post_id,
            "text": str(normalized.get("text") or ""),
            "articleTitle": str(article.get("title") or ""),
            "articleText": str(article.get("full_text") or ""),
            "author": {"name": str(normalized.get("author") or username), "screenName": str(normalized.get("screen_name") or username)},
            "createdAtISO": created_at,
            "url": url,
            "media": media,
            "metrics": {"likes": normalized.get("likes", 0), "retweets": normalized.get("retweets", 0), "replies": normalized.get("replies_count", 0), "views": normalized.get("views", 0), "bookmarks": normalized.get("bookmarks", 0)},
            "quotedTweet": {"id": str(quote.get("id") or ""), "text": str(quote.get("text") or ""), "author": {"name": str(quote_author.get("name") or ""), "screenName": str(quote_author.get("screen_name") or "")}} if quote else {},
            "lang": str(normalized.get("lang") or ""),
            "isRetweet": bool(tweet.get("retweeted_tweet") or tweet.get("retweet")),
            "rawResponse": response,
        }

    def fetch_post(self, url: str, *, batch_key: str) -> dict[str, Any]:
        match = self._url_pattern.fullmatch(str(url).strip())
        if not match:
            raise ValueError("public X backup accepts only a standard HTTPS post URL")
        username, post_id = match.groups()
        request_id = self.gate.reserve(batch_key=batch_key, operation="public_single_post", cost=1)
        api_url = f"https://api.fxtwitter.com/{username}/status/{post_id}"
        try:
            request = urllib.request.Request(api_url, headers={"User-Agent": "ai-hub-x-public-backup/1"})
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(10 * 1024 * 1024 + 1)
                if len(raw) > 10 * 1024 * 1024:
                    raise TwitterProviderError("FxTwitter response exceeded 10 MB")
                payload = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict):
                raise TwitterProviderError("FxTwitter returned a non-object response")
            code = int(payload.get("code") or 0)
            if code == 404:
                raise PublicBackupNotFoundError("FxTwitter public post was not found")
            if code in {403, 429} or "rate" in str(payload.get("message") or "").casefold():
                self.gate.pause("FxTwitter public backup rate limited", seconds=7200)
                raise PublicBackupRateLimitError("FxTwitter public backup rate limited")
            if code != 200 or not isinstance(payload.get("tweet"), dict):
                raise TwitterProviderError("FxTwitter public response was unavailable")
            result = self._normalise(dict(payload["tweet"]), payload, username, post_id, str(url).strip())
            self.gate.finish(request_id, status="completed")
            return result
        except urllib.error.HTTPError as exc:
            code = "rate_limited" if exc.code in {403, 429} else "upstream_error"
            self.gate.finish(request_id, status="failed", error_code=code, error="FxTwitter HTTP response")
            if exc.code in {403, 429}:
                self.gate.pause("FxTwitter public backup rate limited", seconds=7200)
                raise PublicBackupRateLimitError("FxTwitter public backup rate limited") from exc
            if exc.code == 404:
                raise PublicBackupNotFoundError("FxTwitter public post was not found") from exc
            raise TwitterProviderError("FxTwitter public upstream HTTP error") from exc
        except PublicBackupRateLimitError:
            self.gate.finish(request_id, status="failed", error_code="rate_limited", error="FxTwitter public response")
            raise
        except PublicBackupNotFoundError:
            self.gate.finish(request_id, status="failed", error_code="not_found", error="FxTwitter public response")
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.gate.finish(request_id, status="failed", error_code="upstream_error", error="FxTwitter public request failed")
            raise TwitterProviderError("FxTwitter public request failed") from exc
        except Exception:
            self.gate.finish(request_id, status="failed", error_code="provider_error", error="FxTwitter public parser failed")
            raise


def _resolve_twitter_command(command: str) -> str:
    """Resolve the managed twitter-cli executable for scheduled tasks."""
    configured = str(command or os.environ.get("KOL_TWITTER_COMMAND", "twitter")).strip()
    candidates = [configured]
    if configured.casefold() in {"twitter", "twitter.exe"}:
        candidates.extend(
            [
                str(Path.home() / ".local" / "bin" / "twitter.exe"),
                str(Path.home() / ".local" / "bin" / "twitter"),
            ]
        )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve())
    return configured


class TwitterCliProvider:
    name = "twitter-cli"

    def __init__(
        self,
        command: str = "twitter",
        credentials: KeyringCredentialStore | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 60,
        proxy_url: str = "",
        session_manager: XSessionManager | None = None,
        batch_key: str = "",
        history_mode: bool = False,
    ):
        self.command = _resolve_twitter_command(command)
        self.credentials = credentials or KeyringCredentialStore()
        self.runner = runner
        self.timeout_seconds = max(10, min(timeout_seconds, 180))
        self.proxy_url = proxy_url.strip()
        self.session_manager = session_manager
        self.batch_key = batch_key or f"x-run:{uuid.uuid4().hex}"
        self.history_mode = bool(history_mode)
        self._slot_id: int | None = None
        self._direct_client: Any | None = None
        self._direct_client_slot: int | None = None
        self._direct_user_ids: dict[str, str] = {}

    def _direct_client_for_slot(self, slot_id: int) -> Any:
        if self._direct_client is not None and self._direct_client_slot == slot_id:
            return self._direct_client
        site_packages = Path(
            os.environ.get(
                "TWITTER_CLI_SITE_PACKAGES",
                Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "twitter-cli" / "Lib" / "site-packages",
            )
        )
        if not site_packages.is_dir():
            raise TwitterProviderError(f"twitter-cli 0.8.5 runtime missing: {site_packages}")
        import sys
        if str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
        try:
            from twitter_cli.client import TwitterClient  # type: ignore
        except Exception as exc:
            raise TwitterProviderError(f"twitter-cli 0.8.5 import failed: {type(exc).__name__}") from exc
        credentials = self.session_manager.credentials_for(slot_id) if self.session_manager else {
            "TWITTER_AUTH_TOKEN": self.credentials.load_values()[0],
            "TWITTER_CT0": self.credentials.load_values()[1],
        }
        self._direct_client = TwitterClient(
            credentials["TWITTER_AUTH_TOKEN"],
            credentials["TWITTER_CT0"],
            {"requestDelay": 0, "maxRetries": 0, "retryBaseDelay": 60, "maxCount": 20},
            cookie_string=f"auth_token={credentials['TWITTER_AUTH_TOKEN']}; ct0={credentials['TWITTER_CT0']}",
        )
        self._direct_client_slot = slot_id
        return self._direct_client

    def _fetch_guarded_page(self, handle: str, max_count: int) -> ProviderFetchResult:
        assert self.session_manager is not None
        if self._slot_id is None:
            self._slot_id = self.session_manager.batch_slot(
                self.batch_key,
                "history" if self.history_mode else "freshness",
            )
        slot_id = self._slot_id
        kol_id = self.session_manager.kol_id_for_handle(handle)
        checkpoint = self.session_manager.checkpoint(kol_id) if kol_id else None
        cursor = str(checkpoint.get("cursor") or "") if checkpoint else ""
        user_id = str(checkpoint.get("user_id") or "") if checkpoint else ""
        operation = "history_page" if self.history_mode else "freshness_page"
        # Each page includes one authenticated profile lookup and one
        # timeline request; the first client construction also performs the
        # library's homepage/transaction bootstrap.  Charging conservatively
        # keeps the project budget below the platform-facing request count.
        cost = 2 if self._direct_client is not None or user_id else 4
        request_id = self.session_manager.reserve_request(
            slot_id,
            batch_key=self.batch_key,
            operation=operation,
            cost=cost,
        )
        started = time.perf_counter()
        try:
            client = self._direct_client_for_slot(slot_id)
            from twitter_cli.client import _deep_get, parse_timeline_response  # type: ignore
            from twitter_cli.graphql import FEATURES  # type: ignore
            from twitter_cli.serialization import tweet_to_dict  # type: ignore
            if not user_id:
                profile = client.fetch_user(handle.lstrip("@"))
                user_id = str(profile.id)
            variables = {
                "userId": user_id,
                "count": min(max(1, int(max_count)), 20),
                "includePromotedContent": False,
                "latestControlAvailable": True,
                "requestContext": "launch",
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            }
            if cursor:
                variables["cursor"] = cursor
            data = client._graphql_get("UserTweets", variables, FEATURES)
            tweets, next_cursor = parse_timeline_response(
                data,
                lambda value: _deep_get(value, "data", "user", "result", "timeline_v2", "timeline", "instructions"),
            )
            posts = [tweet_to_dict(tweet) for tweet in tweets if getattr(tweet, "id", "")]
            self.session_manager.finish_request(request_id, status="completed")
            self.session_manager.record_success(slot_id)
            return ProviderFetchResult(
                provider=self.name,
                posts=posts,
                attempts=[ProviderAttempt(self.name, "success", len(posts), int((time.perf_counter() - started) * 1000))],
                warnings=[],
                next_cursor=str(next_cursor or ""),
                user_id=user_id,
                exhausted=not bool(next_cursor),
            )
        except Exception as exc:
            detail = str(exc)
            lowered = detail.casefold()
            error_code = "rate_limited" if getattr(exc, "status_code", 0) == 429 or "429" in lowered or "rate" in lowered and "limit" in lowered else "auth_required" if any(token in lowered for token in ("unauthorized", "not_authenticated", "login", "cookie")) else "provider_error"
            self.session_manager.finish_request(request_id, status="failed", error_code=error_code, error="X reader request failed")
            if error_code == "rate_limited":
                self.session_manager.pause_global("X reader rate limit", seconds=7200)
                self.session_manager.record_failure(slot_id, error_code, "X reader rate limit")
                raise TwitterRateLimitError("X reader rate limit; collection deferred") from exc
            if error_code == "auth_required":
                self.session_manager.record_failure(slot_id, error_code, "X reader authentication required")
                raise TwitterAuthenticationError("X reader authentication required") from exc
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            api_code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
            detail = f"X reader request failed: {type(exc).__name__}"
            if status is not None and str(status).isdigit():
                detail += f" status={int(status)}"
            if api_code is not None and str(api_code)[:80].replace("_", "").isalnum():
                detail += f" code={str(api_code)[:80]}"
            raise TwitterProviderError(detail) from exc

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        if self.session_manager is not None:
            return self._fetch_guarded_page(handle, max_count)
        started = time.perf_counter()
        env = os.environ.copy()
        env.update(self.credentials.load())
        if self.proxy_url:
            env["TWITTER_PROXY"] = self.proxy_url
        try:
            completed = self.runner(
                [self.command, "user-posts", handle.lstrip("@"), "-n", str(max_count), "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TwitterProviderError(
                f"twitter-cli executable not found: {self.command}; "
                "install twitter-cli 0.8.5 or set KOL_TWITTER_COMMAND"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TwitterProviderError(
                f"twitter-cli timed out after {self.timeout_seconds} seconds"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "twitter-cli failed").strip()
            lowered = detail.lower()
            if "not_authenticated" in lowered or "no twitter cookies" in lowered or "unauthorized" in lowered:
                raise TwitterAuthenticationError("Twitter authentication failed")
            if "rate" in lowered and "limit" in lowered or "429" in lowered:
                raise TwitterRateLimitError("Twitter rate limit reached")
            raise TwitterProviderError(detail[-2000:])
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TwitterProviderError(f"twitter-cli returned invalid JSON: {exc}") from exc
        if isinstance(payload, dict):
            payload = payload.get("tweets") or payload.get("data") or []
        if not isinstance(payload, list):
            raise TwitterProviderError("twitter-cli output is not a post list")
        posts = [dict(item) for item in payload if isinstance(item, dict)]
        return ProviderFetchResult(
            provider=self.name,
            posts=posts,
            attempts=[
                ProviderAttempt(
                    provider=self.name,
                    status="success",
                    post_count=len(posts),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
            warnings=[],
        )


class ZhihuProfileProvider:
    name = "zhihu-local"

    def __init__(
        self,
        script_path: Path,
        *,
        python_command: str = "python",
        browser_path: str = "",
        profile_directory: str = "Default",
        user_data_dir: str = "",
        port: int = 9223,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 120,
    ):
        self.script_path = Path(script_path)
        self.python_command = python_command
        self.browser_path = browser_path
        self.profile_directory = profile_directory
        self.user_data_dir = user_data_dir
        self.port = int(port)
        self.runner = runner
        self.timeout_seconds = max(30, min(int(timeout_seconds), 300))
        self.session_prepared = False
        self.preflight_error = ""

    def prepare_session(self, handle: str) -> None:
        """Preflight the shared browser once before a Zhihu batch."""
        if self.session_prepared:
            return
        if not self.script_path.is_file():
            raise ZhihuProviderError(f"Zhihu capture script is missing: {self.script_path}")
        command = [
            self.python_command,
            str(self.script_path),
            "--handle",
            handle,
            "--port",
            str(self.port),
            "--profile-directory",
            self.profile_directory,
            "--prepare-only",
        ]
        if self.browser_path:
            command.extend(["--browser-path", self.browser_path])
        if self.user_data_dir:
            command.extend(["--user-data-dir", self.user_data_dir])
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.preflight_error = (
                f"Zhihu browser preflight timed out after {self.timeout_seconds} seconds"
            )
            raise ZhihuProviderError(self.preflight_error) from exc
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            self.preflight_error = f"Zhihu browser preflight returned invalid JSON: {exc}"
            raise ZhihuProviderError(self.preflight_error) from exc
        if completed.returncode != 0 or not payload.get("ok"):
            self.preflight_error = str(
                payload.get("error") or completed.stderr or "Zhihu browser preflight failed"
            )[-2000:]
            raise ZhihuProviderError(self.preflight_error)
        self.session_prepared = True

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        if not self.script_path.is_file():
            raise ZhihuProviderError(f"Zhihu capture script is missing: {self.script_path}")
        started = time.perf_counter()
        command = [
            self.python_command,
            str(self.script_path),
            "--handle",
            handle,
            "--limit",
            # Historical recovery is paged and resumable; allow a larger
            # bounded batch than the ordinary freshness sweep.
            str(max(1, min(int(max_count), 1000))),
            "--port",
            str(self.port),
            "--profile-directory",
            self.profile_directory,
            "--no-launch",
        ]
        if self.browser_path:
            command.extend(["--browser-path", self.browser_path])
        if self.user_data_dir:
            command.extend(["--user-data-dir", self.user_data_dir])
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ZhihuProviderError(
                f"Zhihu profile capture timed out after {self.timeout_seconds} seconds"
            ) from exc
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ZhihuProviderError(f"Zhihu profile capture returned invalid JSON: {exc}") from exc
        if completed.returncode != 0 or not payload.get("ok"):
            detail = str(payload.get("error") or completed.stderr or "Zhihu profile capture failed")
            raise ZhihuProviderError(detail[-2000:])
        raw_posts = payload.get("posts") or []
        if not isinstance(raw_posts, list):
            raise ZhihuProviderError("Zhihu profile capture output is not a post list")
        posts = [dict(item) for item in raw_posts if isinstance(item, dict)]
        return ProviderFetchResult(
            provider=self.name,
            posts=posts,
            attempts=[
                ProviderAttempt(
                    provider=self.name,
                    status="success",
                    post_count=len(posts),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
            warnings=[],
        )


def _snowflake_timestamp(post_id: str) -> str:
    if not post_id.isdigit() or len(post_id) < 15:
        raise TwitterProviderError("Nitter post has no usable snowflake ID")
    timestamp_ms = (int(post_id) >> 22) + 1_288_834_974_657
    parsed = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    if parsed.year < 2010 or parsed.year > 2100:
        raise TwitterProviderError("Nitter post snowflake timestamp is out of range")
    return parsed.isoformat(timespec="seconds")


def _parse_nitter_timestamp(value: str, post_id: str) -> tuple[str, str]:
    clean = value.strip()
    match = re.fullmatch(
        r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})\s+"
        r"[^A-Za-z0-9\s:]{1,3}\s+"
        r"(\d{1,2}):(\d{2})\s+(AM|PM)\s+UTC",
        clean,
    )
    if not match:
        return _snowflake_timestamp(post_id), "snowflake"
    parsed = datetime.strptime(
        " ".join(match.groups()),
        "%b %d %Y %I %M %p",
    ).replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds"), "nitter"


def _xtf_tweet_payload(item: dict[str, Any], requested_handle: str) -> dict[str, Any]:
    post_id = str(item.get("tweet_id") or "").strip()
    created_at, timestamp_source = _parse_nitter_timestamp(
        str(item.get("time_ago") or ""), post_id
    )
    author_handle = str(item.get("author") or requested_handle).strip().lstrip("@")
    media = [
        {"type": "photo", "url": str(url)}
        for url in item.get("media") or []
        if str(url).startswith(("https://", "http://"))
    ]
    quoted = item.get("quoted_tweet") if isinstance(item.get("quoted_tweet"), dict) else {}
    quoted_payload: dict[str, Any] = {}
    if quoted:
        quoted_payload = {
            "id": str(quoted.get("tweet_id") or ""),
            "text": str(quoted.get("text") or ""),
            "author": {
                "screenName": str(quoted.get("author") or "").lstrip("@"),
                "name": str(quoted.get("author_name") or ""),
            },
        }
    return {
        "id": post_id,
        "text": str(item.get("text") or ""),
        "url": f"https://x.com/{author_handle}/status/{post_id}",
        "author": {
            "screenName": author_handle,
            "name": str(item.get("author_name") or author_handle),
        },
        "metrics": {
            "likes": int(item.get("likes") or 0),
            "retweets": int(item.get("retweets") or 0),
            "replies": int(item.get("replies") or 0),
            "views": int(item.get("views") or 0),
        },
        "createdAtISO": created_at,
        "timestampSource": timestamp_source,
        "media": media,
        "isRetweet": bool(item.get("retweeted_by")),
        "retweetedBy": str(item.get("retweeted_by") or ""),
        "quotedTweet": quoted_payload or None,
        "lang": "",
    }


class XtfNitterProvider:
    name = "nitter"

    def __init__(
        self,
        command: str,
        nitter_url: str = "http://127.0.0.1:9377",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 30,
    ):
        self.command = command
        self.nitter_url = nitter_url.rstrip("/")
        self.runner = runner
        self.timeout_seconds = max(5, min(int(timeout_seconds), 180))

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        started = time.perf_counter()
        env = os.environ.copy()
        env.update(
            {
                "XTF_NITTER": self.nitter_url,
                "XTF_LANG": "zh",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        completed = self.runner(
            [
                self.command,
                "--user",
                handle.lstrip("@"),
                "--limit",
                str(max_count),
                "--backend",
                "nitter",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            env=env,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TwitterProviderError(f"xtf returned invalid JSON: {exc}") from exc
        if completed.returncode != 0 or payload.get("error"):
            code = str(payload.get("error_code") or "provider_error")
            detail = str(payload.get("error") or completed.stderr or "xtf nitter failed")[-1000:]
            if code == "rate_limited":
                raise TwitterRateLimitError(detail)
            raise TwitterProviderError(f"{code}: {detail}")
        raw_posts = payload.get("tweets") or []
        if not isinstance(raw_posts, list):
            raise TwitterProviderError("xtf nitter output is not a tweet list")
        posts = [_xtf_tweet_payload(dict(item), handle) for item in raw_posts if isinstance(item, dict)]
        warnings = [str(payload["warning"])] if payload.get("warning") else []
        if any(post.get("timestampSource") == "snowflake" for post in posts):
            warnings.append("timestamp_recovered_from_snowflake")
        return ProviderFetchResult(
            provider=self.name,
            posts=posts,
            attempts=[
                ProviderAttempt(
                    provider=self.name,
                    status="success",
                    post_count=len(posts),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            ],
            warnings=warnings,
        )


def _provider_result(value: ProviderFetchResult | list[dict[str, Any]], provider: str) -> ProviderFetchResult:
    if isinstance(value, ProviderFetchResult):
        return value
    return ProviderFetchResult(provider, list(value), [], [])


def _post_provider_warning(payload: dict[str, Any], provider: str) -> str:
    warnings: list[str] = []
    if provider == "nitter" and str(payload.get("timestampSource") or "") == "snowflake":
        warnings.append("timestamp_recovered_from_snowflake")
    return ";".join(warnings)


class FallbackXPostProvider:
    name = "auto"

    def __init__(self, primary: XPostProvider, fallback: XPostProvider, mode: str = "enabled"):
        if mode not in {"enabled", "shadow", "disabled"}:
            raise ValueError(f"unsupported fallback mode: {mode}")
        self.primary = primary
        self.fallback = fallback
        self.mode = mode
        self.primary_auth_failed = False
        self.shadow_fallback_failed = False

    @property
    def credentials(self) -> KeyringCredentialStore | None:
        """Expose the primary reader store for discovery health checks only."""
        return getattr(self.primary, "credentials", None)

    @staticmethod
    def _failed_attempt(provider: str, started: float, exc: Exception) -> ProviderAttempt:
        code = (
            "authentication_failed" if isinstance(exc, TwitterAuthenticationError)
            else "rate_limited" if isinstance(exc, TwitterRateLimitError)
            else "provider_error"
        )
        return ProviderAttempt(
            provider=provider,
            status="failed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_code=code,
            error=str(exc)[:1000],
        )

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        if self.mode == "disabled":
            return _provider_result(
                self.primary.fetch_user_posts(handle, max_count),
                self.primary.name,
            )
        attempts: list[ProviderAttempt] = []
        warnings: list[str] = []
        primary_error: Exception | None = None
        if not self.primary_auth_failed:
            started = time.perf_counter()
            try:
                primary = _provider_result(
                    self.primary.fetch_user_posts(handle, max_count),
                    self.primary.name,
                )
                attempts.extend(primary.attempts or [
                    ProviderAttempt(primary.provider, "success", len(primary.posts))
                ])
                if primary.posts:
                    if self.mode == "enabled":
                        return ProviderFetchResult(primary.provider, primary.posts, attempts, primary.warnings)
                    return self._shadow_compare(handle, max_count, primary, attempts)
                warnings.append("primary_suspicious_empty")
                attempts.append(ProviderAttempt(primary.provider, "suspicious_empty", 0))
                primary_error = TwitterProviderError("primary returned a suspicious empty timeline")
            except TwitterAuthenticationError as exc:
                self.primary_auth_failed = True
                warnings.append("primary_authentication_failed")
                attempts.append(self._failed_attempt(self.primary.name, started, exc))
                primary_error = exc
            except (TwitterRateLimitError, TwitterProviderError) as exc:
                warnings.append("primary_provider_failed")
                attempts.append(self._failed_attempt(self.primary.name, started, exc))
                primary_error = exc
        else:
            warnings.append("primary_authentication_failed")
            primary_error = TwitterAuthenticationError("primary authentication circuit is open")

        started = time.perf_counter()
        try:
            fallback = _provider_result(
                self.fallback.fetch_user_posts(handle, max_count),
                self.fallback.name,
            )
            attempts.extend(fallback.attempts or [
                ProviderAttempt(fallback.provider, "success", len(fallback.posts))
            ])
            if not fallback.posts:
                raise TwitterProviderError("Nitter returned a suspicious empty timeline")
        except (TwitterAuthenticationError, TwitterRateLimitError, TwitterProviderError) as exc:
            attempts.extend(getattr(exc, "attempts", []))
            if not getattr(exc, "attempts", None):
                attempts.append(self._failed_attempt(self.fallback.name, started, exc))
            exc.attempts = attempts
            raise
        if self.mode == "shadow":
            attempts.append(ProviderAttempt("shadow", "comparison_only", len(fallback.posts)))
            assert primary_error is not None
            primary_error.attempts = attempts
            raise primary_error
        return ProviderFetchResult(
            fallback.provider,
            fallback.posts,
            attempts,
            [*warnings, *fallback.warnings, "fallback_used"],
        )

    @staticmethod
    def _ids(posts: list[dict[str, Any]]) -> set[str]:
        return {
            str(item.get("id") or item.get("tweet_id") or "")
            for item in posts
            if str(item.get("id") or item.get("tweet_id") or "")
        }

    def _shadow_compare(
        self,
        handle: str,
        max_count: int,
        primary: ProviderFetchResult,
        attempts: list[ProviderAttempt],
    ) -> ProviderFetchResult:
        if self.shadow_fallback_failed:
            return ProviderFetchResult(
                primary.provider,
                primary.posts,
                attempts,
                [*primary.warnings, "shadow_fallback_circuit_open"],
            )
        started = time.perf_counter()
        warnings = [*primary.warnings]
        try:
            fallback = _provider_result(
                self.fallback.fetch_user_posts(handle, max_count),
                self.fallback.name,
            )
            attempts.extend(fallback.attempts or [
                ProviderAttempt(fallback.provider, "success", len(fallback.posts))
            ])
            if not fallback.posts:
                raise TwitterProviderError("Nitter returned a suspicious empty timeline")
            primary_ids = self._ids(primary.posts)
            fallback_ids = self._ids(fallback.posts)
            matching = len(primary_ids & fallback_ids)
            coverage = matching / len(primary_ids) if primary_ids else 0.0
            warnings.extend([*fallback.warnings, "shadow_compared"])
            if coverage < 0.95:
                warnings.append("shadow_coverage_below_threshold")
            return ProviderFetchResult(
                primary.provider,
                primary.posts,
                attempts,
                warnings,
                [{
                    "handle": handle,
                    "primary_count": len(primary_ids),
                    "fallback_count": len(fallback_ids),
                    "matching_count": matching,
                    "coverage": coverage,
                }],
            )
        except (TwitterAuthenticationError, TwitterRateLimitError, TwitterProviderError) as exc:
            self.shadow_fallback_failed = True
            attempts.extend(getattr(exc, "attempts", []))
            if not getattr(exc, "attempts", None):
                attempts.append(self._failed_attempt(self.fallback.name, started, exc))
            warnings.append("shadow_fallback_failed")
            return ProviderFetchResult(primary.provider, primary.posts, attempts, warnings)


def build_x_post_provider(
    mode: str,
    *,
    twitter_credentials: KeyringCredentialStore | None = None,
    twitter_command: str = "twitter",
    xtf_command: str,
    nitter_url: str = "http://127.0.0.1:9377",
    fallback_mode: str = "enabled",
    proxy_url: str | None = None,
    twitter_timeout_seconds: int = 60,
    session_manager: XSessionManager | None = None,
    batch_key: str = "",
    history_mode: bool = False,
) -> XPostProvider:
    if mode not in {"auto", "twitter", "nitter"}:
        raise ValueError(f"unsupported X provider: {mode}")
    primary = TwitterCliProvider(
        twitter_command,
        twitter_credentials or KeyringCredentialStore(),
        proxy_url=(proxy_url if proxy_url is not None else os.environ.get("KOL_X_PROXY", "http://127.0.0.1:7897")),
        timeout_seconds=twitter_timeout_seconds,
        session_manager=session_manager,
        batch_key=batch_key,
        history_mode=history_mode,
    )
    fallback = XtfNitterProvider(xtf_command, nitter_url)
    if mode == "twitter":
        return primary
    if mode == "nitter":
        return fallback
    return FallbackXPostProvider(primary, fallback, mode=fallback_mode if fallback_mode in {"enabled", "shadow", "disabled"} else "shadow")


class CaptureAccountPostProvider:
    """Present a local JSON-capture account as a post-fetch provider.

    Platforms without a live adapter (Douyin, ...) are ingested from capture
    files written by ``kol-douyin-capture-sync``; this adapter exposes the
    discovery provider's ``resolve``/``fetch`` pair through the
    ``fetch_user_posts`` interface the post-fetch loop calls.
    """

    def __init__(self, discovery_provider: Any):
        self.discovery = discovery_provider
        self.name = str(getattr(discovery_provider, "name", "local-capture"))

    def fetch_user_posts(self, handle: str, max_count: int) -> ProviderFetchResult:
        account = self.discovery.resolve(handle)
        content = self.discovery.fetch(account, None, max(1, int(max_count)))
        posts = [dict(item) for item in getattr(content, "items", [])]
        warnings = [str(value) for value in (getattr(content, "warnings", []) or [])]
        return ProviderFetchResult(
            self.name,
            posts,
            [ProviderAttempt(self.name, "success", len(posts))],
            warnings,
        )
