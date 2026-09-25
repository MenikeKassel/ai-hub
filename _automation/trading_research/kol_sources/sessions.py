from __future__ import annotations
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from filelock import FileLock, Timeout as FileLockTimeout
from kol_tracker import SHANGHAI, now_iso
from .core import DEFAULT_OUTBOUND_PROXY, CredentialStorageError, TwitterProviderError, TwitterRateLimitError, XBudgetDeferredError, XSessionUnavailableError, validate_twitter_credentials


class XSessionManager:
    """Guard a small, auditable pool of X reader sessions.

    Credentials stay in Windows Credential Manager.  This class only persists
    slot identity, request reservations, cooldowns, leases and pagination
    checkpoints in the local KOL database.  A lease fixes one session to one
    batch, so callers cannot switch sessions after a rate-limit response.
    """

    SLOT_IDS = (1, 2, 3)
    SERVICE_PREFIX = "ai-hub/x-session/slot-"
    DEFAULT_POLICY = {
        "global_limit_24h": 180,
        "session_limit_24h": 90,
        "min_interval_seconds": 60,
    }

    def __init__(self, store: Any, *, gate_path: Path | None = None):
        self.store = store
        self.gate_path = Path(gate_path or Path(store.path).with_name("x-session-gate.lock"))
        self._ensure_slots()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(SHANGHAI)

    @classmethod
    def _timestamp(cls) -> str:
        return cls._now().isoformat(timespec="seconds")

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)
        except ValueError:
            return None

    @classmethod
    def _service(cls, slot_id: int) -> str:
        if int(slot_id) not in cls.SLOT_IDS:
            raise ValueError("X session slot must be 1, 2 or 3")
        return f"{cls.SERVICE_PREFIX}{int(slot_id)}"

    @staticmethod
    def _keyring() -> Any:
        import keyring  # type: ignore

        return keyring

    @classmethod
    def _read_payload(cls, slot_id: int) -> dict[str, str] | None:
        try:
            raw = cls._keyring().get_password(cls._service(slot_id), "session") or ""
            if not raw:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None
            auth = str(value.get("auth_token") or "")
            ct0 = str(value.get("ct0") or "")
            if not auth or not ct0:
                return None
            return {"auth_token": auth, "ct0": ct0}
        except Exception:
            return None

    @classmethod
    def _write_payload(cls, slot_id: int, auth_token: str, ct0: str) -> None:
        # Store one encrypted pair so an interrupted update cannot leave a
        # slot with a new auth_token and an old ct0.
        payload = json.dumps(
            {"auth_token": auth_token, "ct0": ct0},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cls._keyring().set_password(cls._service(slot_id), "session", payload)

    def _ensure_slots(self) -> None:
        timestamp = self._timestamp()
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO x_collection_policy(policy_id,enabled,global_limit_24h,session_limit_24h,min_interval_seconds,public_enabled,public_limit_24h,next_slot_id,updated_at) VALUES(1,1,?,?,?,?,?,?,?)",
                    (
                        self.DEFAULT_POLICY["global_limit_24h"],
                        self.DEFAULT_POLICY["session_limit_24h"],
                        self.DEFAULT_POLICY["min_interval_seconds"],
                        1,
                        30,
                        1,
                        timestamp,
                    ),
                )
                for slot_id in self.SLOT_IDS:
                    db.execute(
                        "INSERT OR IGNORE INTO x_session_slots(slot_id,label,credential_service,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (slot_id, f"X session {slot_id}", self._service(slot_id), timestamp, timestamp),
                    )
        # Only the existing primary slot is eligible for one-time migration.
        # Reader/Nitter are deliberately not copied because they may be the
        # same account and must never silently create a second identity.
        if self._read_payload(1) is None:
            try:
                keyring = self._keyring()
                auth = keyring.get_password("ai-hub/twitter-cli", "auth_token") or ""
                ct0 = keyring.get_password("ai-hub/twitter-cli", "ct0") or ""
                if auth and ct0:
                    self._write_payload(1, auth, ct0)
            except Exception:
                pass

    def _row(self, slot_id: int) -> dict[str, Any] | None:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM x_session_slots WHERE slot_id=?", (int(slot_id),)).fetchone()
        return dict(row) if row else None

    def slots(self) -> list[dict[str, Any]]:
        self._ensure_slots()
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM x_session_slots ORDER BY slot_id")]
        now = self._now()
        identities: dict[str, int] = {}
        for row in rows:
            identity = str(row.get("user_id") or "")
            if identity:
                identities[identity] = identities.get(identity, 0) + 1
        for row in rows:
            row["credential_configured"] = self._read_payload(int(row["slot_id"])) is not None
            cooldown = self._parse_time(str(row.get("cooldown_until") or ""))
            if row["status"] == "cooldown" and cooldown is not None and cooldown <= now:
                eligible = bool(row["user_id"] and row["credential_configured"])
                row["status"] = "ready" if eligible else "pending_verification"
                row["enabled"] = int(eligible)
            row["cooldown_active"] = bool(cooldown and cooldown > now)
            row["duplicate_identity"] = bool(row.get("user_id") and identities.get(str(row["user_id"]), 0) > 1)
        return rows

    def credentials_for(self, slot_id: int) -> dict[str, str]:
        payload = self._read_payload(int(slot_id))
        if payload is None:
            raise XSessionUnavailableError(f"X session slot {int(slot_id)} has no complete credentials")
        return {"TWITTER_AUTH_TOKEN": payload["auth_token"], "TWITTER_CT0": payload["ct0"]}

    def cli_config_dir(self) -> Path:
        """Return a private config directory with retries disabled.

        twitter-cli discovers config.yaml from its working directory.  Keeping
        this file under the local runtime avoids modifying the user-level tool
        installation and prevents a 429 from being retried by the CLI itself.
        """
        directory = Path(self.store.path).parent / "twitter-cli-config"
        directory.mkdir(parents=True, exist_ok=True)
        config = directory / "config.yaml"
        if not config.exists():
            config.write_text(
                "fetch:\n  count: 20\nrateLimit:\n  requestDelay: 0\n  maxRetries: 0\n  retryBaseDelay: 60\n  maxCount: 20\n",
                encoding="utf-8",
                newline="\n",
            )
        return directory

    def save_credentials(self, slot_id: int, auth_token: str, ct0: str, *, label: str = "") -> dict[str, Any]:
        auth_token, ct0 = validate_twitter_credentials(auth_token, ct0)
        slot_id = int(slot_id)
        previous = self._read_payload(slot_id)
        try:
            self._write_payload(slot_id, auth_token, ct0)
        except Exception as exc:
            if previous:
                try:
                    self._write_payload(slot_id, previous["auth_token"], previous["ct0"])
                except Exception:
                    pass
            raise CredentialStorageError("Windows Credential Manager 保存失败，本次输入未生效") from exc
        timestamp = self._timestamp()
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute(
                    """UPDATE x_session_slots SET label=COALESCE(NULLIF(?,''),label),user_id='',screen_name='',
                       status='pending_verification',enabled=0,last_verified_at='',cooldown_until='',
                       last_error_code='',last_error='',consecutive_rate_limits=0,updated_at=? WHERE slot_id=?""",
                    (label.strip(), timestamp, slot_id),
                )
        return next(item for item in self.slots() if int(item["slot_id"]) == slot_id)

    def set_status(self, slot_id: int, status: str, *, reason: str = "") -> dict[str, Any]:
        allowed = {"pending_verification", "ready", "cooldown", "auth_required", "disabled"}
        if status not in allowed:
            raise ValueError(f"unsupported X session status: {status}")
        timestamp = self._timestamp()
        enabled = 1 if status == "ready" else 0
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                row = db.execute("SELECT user_id FROM x_session_slots WHERE slot_id=?", (int(slot_id),)).fetchone()
                if not row:
                    raise KeyError(f"X session slot {slot_id} not found")
                if status == "ready" and (not row["user_id"] or self._read_payload(int(slot_id)) is None):
                    raise ValueError("X session must be verified and have complete credentials before enabling")
                db.execute(
                    "UPDATE x_session_slots SET status=?,enabled=?,last_error=?,updated_at=? WHERE slot_id=?",
                    (status, enabled, reason[:2000], timestamp, int(slot_id)),
                )
        return next(item for item in self.slots() if int(item["slot_id"]) == int(slot_id))

    @staticmethod
    def _identity_from_payload(value: Any) -> tuple[str, str]:
        found: list[tuple[str, str]] = []
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                identity = str(node.get("id") or node.get("user_id") or node.get("userId") or "").strip()
                name = str(node.get("screenName") or node.get("username") or node.get("screen_name") or node.get("name") or "").strip()
                if identity and identity.isdigit():
                    found.append((identity, name))
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(value)
        return found[0] if found else ("", "")

    def verify_slot(self, slot_id: int, command: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
        slot_id = int(slot_id)
        batch_key = f"verify:slot-{slot_id}:{uuid.uuid4().hex}"
        credential_env = self.credentials_for(slot_id)
        request_id = self.reserve_request(slot_id, batch_key=batch_key, operation="verify", cost=3, allow_unverified=True)
        env = os.environ.copy()
        env.update(credential_env)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        # twitter-cli resolves its outbound proxy solely from TWITTER_PROXY;
        # without it every verification stalls on a direct connection because
        # x.com is unreachable from this network.  Mirror proxy_opener():
        # KOL_X_PROXY overrides, an explicit empty value forces direct.
        proxy = (
            os.environ["KOL_X_PROXY"]
            if "KOL_X_PROXY" in os.environ
            else os.environ.get("TWITTER_PROXY") or DEFAULT_OUTBOUND_PROXY
        ).strip()
        if proxy:
            env["TWITTER_PROXY"] = proxy
        else:
            env.pop("TWITTER_PROXY", None)
        try:
            completed = subprocess.run(
                [command, "whoami", "--json"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=max(10, min(timeout_seconds, 60)),
                env=env, cwd=str(self.cli_config_dir()), check=False,
            )
            detail = (completed.stderr or completed.stdout or "").strip()
            if completed.returncode != 0:
                lowered = detail.casefold()
                code = "rate_limited" if "429" in lowered or "rate" in lowered and "limit" in lowered else "auth_required" if any(token in lowered for token in ("auth", "unauthorized", "login", "cookie")) else "provider_error"
                self.record_failure(slot_id, code, "X session verification failed")
                self.finish_request(request_id, status="failed", error_code=code, error="verification failed")
                return next(item for item in self.slots() if int(item["slot_id"]) == slot_id)
            try:
                payload = json.loads(completed.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            user_id, screen_name = self._identity_from_payload(payload)
            if not user_id:
                self.record_failure(slot_id, "provider_error", "X session identity was not returned")
                self.finish_request(request_id, status="failed", error_code="provider_error", error="identity missing")
                return next(item for item in self.slots() if int(item["slot_id"]) == slot_id)
            self.mark_verified(slot_id, user_id, screen_name)
            self.finish_request(request_id, status="completed")
            return next(item for item in self.slots() if int(item["slot_id"]) == slot_id)
        except subprocess.TimeoutExpired:
            self.record_failure(slot_id, "provider_error", "X session verification timed out")
            self.finish_request(request_id, status="failed", error_code="provider_error", error="verification timed out")
            return next(item for item in self.slots() if int(item["slot_id"]) == slot_id)

    def mark_verified(self, slot_id: int, user_id: str, screen_name: str = "") -> None:
        timestamp = self._timestamp()
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE x_session_slots SET label=CASE WHEN label='' OR label LIKE 'X session %' THEN ? ELSE label END,user_id=?,screen_name=?,status='ready',enabled=1,last_verified_at=?,last_error_code='',last_error='',cooldown_until='',updated_at=? WHERE slot_id=?",
                    (str(screen_name)[:100] or f"X session {slot_id}", str(user_id), str(screen_name)[:100], timestamp, timestamp, int(slot_id)),
                )

    def _policy(self) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM x_collection_policy WHERE policy_id=1").fetchone()
        if not row:
            self._ensure_slots()
            return self._policy()
        return dict(row)

    def _recent_usage(self, *, slot_id: int | None = None) -> int:
        cutoff = (self._now() - timedelta(hours=24)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            if slot_id is None:
                row = db.execute("SELECT COALESCE(SUM(estimated_requests),0) AS total FROM x_request_budget WHERE reserved_at>=? AND status<>'cancelled'", (cutoff,)).fetchone()
            else:
                slot = db.execute("SELECT user_id FROM x_session_slots WHERE slot_id=?", (int(slot_id),)).fetchone()
                user_id = str(slot["user_id"] or "") if slot else ""
                if user_id:
                    row = db.execute("SELECT COALESCE(SUM(b.estimated_requests),0) AS total FROM x_request_budget b JOIN x_session_slots s ON s.slot_id=b.slot_id WHERE b.reserved_at>=? AND b.source='primary' AND s.user_id=? AND b.status<>'cancelled'", (cutoff, user_id)).fetchone()
                else:
                    row = db.execute("SELECT COALESCE(SUM(estimated_requests),0) AS total FROM x_request_budget WHERE reserved_at>=? AND source='primary' AND slot_id=? AND status<>'cancelled'", (cutoff, int(slot_id))).fetchone()
        return int(row["total"] if row else 0)

    def policy_status(self) -> dict[str, Any]:
        policy = self._policy()
        now = self._now()
        paused = self._parse_time(str(policy.get("paused_until") or ""))
        global_used = self._recent_usage()
        cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            public_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND source='public' AND status<>'cancelled'", (cutoff,)).fetchone()[0])
        public_paused = self._parse_time(str(policy.get("public_paused_until") or ""))
        slots = self.slots()
        for row in slots:
            row["used_24h"] = self._recent_usage(slot_id=int(row["slot_id"]))
            row["remaining_24h"] = max(0, int(policy["session_limit_24h"]) - row["used_24h"])
        return {
            "enabled": bool(policy["enabled"]),
            "global_limit_24h": int(policy["global_limit_24h"]),
            "session_limit_24h": int(policy["session_limit_24h"]),
            "min_interval_seconds": int(policy["min_interval_seconds"]),
            "global_used_24h": global_used,
            "global_remaining_24h": max(0, int(policy["global_limit_24h"]) - global_used),
            "paused_until": str(policy.get("paused_until") or "") if paused and paused > now else "",
            "pause_reason": str(policy.get("pause_reason") or ""),
            "public_backup": {
                "enabled": bool(policy.get("public_enabled", 1)) and bool(policy.get("enabled", 1)),
                "provider": "fxtwitter",
                "adapter_version": "x-tweet-fetcher-3.0.0+f057d6b",
                "limit_24h": int(policy.get("public_limit_24h", 30)),
                "used_24h": public_used,
                "remaining_24h": max(0, int(policy.get("public_limit_24h", 30)) - public_used),
                "paused_until": str(policy.get("public_paused_until") or "") if public_paused and public_paused > now else "",
                "pause_reason": str(policy.get("public_pause_reason") or ""),
            },
            "next_slot_id": int(policy["next_slot_id"]),
            "slots": slots,
        }

    def set_policy(self, *, enabled: bool | None = None, paused: bool | None = None, reason: str = "") -> dict[str, Any]:
        timestamp = self._timestamp()
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                current = db.execute("SELECT * FROM x_collection_policy WHERE policy_id=1").fetchone()
                if not current:
                    self._ensure_slots()
                    current = db.execute("SELECT * FROM x_collection_policy WHERE policy_id=1").fetchone()
                new_enabled = int(current["enabled"] if enabled is None else bool(enabled))
                pause_until = ""
                if paused:
                    pause_until = (self._now() + timedelta(hours=24)).isoformat(timespec="seconds")
                db.execute("UPDATE x_collection_policy SET enabled=?,paused_until=?,pause_reason=?,updated_at=? WHERE policy_id=1", (new_enabled, pause_until, reason[:2000], timestamp))
        return self.policy_status()

    def pause_global(self, reason: str, *, seconds: int = 7200) -> None:
        timestamp = self._timestamp()
        until = (self._now() + timedelta(seconds=max(60, int(seconds)))).isoformat(timespec="seconds")
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute("UPDATE x_collection_policy SET paused_until=?,pause_reason=?,updated_at=? WHERE policy_id=1", (until, reason[:2000], timestamp))

    def _lease_slot(self, batch_key: str, operation: str, *, allow_unverified: bool = False) -> int:
        policy = self._policy()
        now = self._now()
        paused = self._parse_time(str(policy.get("paused_until") or ""))
        if not bool(policy["enabled"]):
            raise XSessionUnavailableError("X collection is manually paused")
        if paused and paused > now:
            raise XBudgetDeferredError(f"X collection paused until {paused.isoformat(timespec='seconds')}")
        with self.store.connect() as db:
            existing = db.execute("SELECT slot_id FROM x_batch_leases WHERE batch_key=? AND status IN ('active','paused')", (batch_key,)).fetchone()
            if existing:
                return int(existing["slot_id"])
            rows = [dict(row) for row in db.execute("SELECT * FROM x_session_slots ORDER BY slot_id")]
        start = int(policy["next_slot_id"])
        ordered = sorted(rows, key=lambda row: ((int(row["slot_id"]) - start) % 3))
        global_used = self._recent_usage()
        if global_used >= int(policy["global_limit_24h"]):
            self.pause_global("global request budget exhausted", seconds=3600)
            raise XBudgetDeferredError("X global request budget exhausted")
        for row in ordered:
            slot_id = int(row["slot_id"])
            if self._read_payload(slot_id) is None:
                continue
            cooldown = self._parse_time(str(row.get("cooldown_until") or ""))
            if cooldown and cooldown > now:
                continue
            ready_after_cooldown = row["status"] == "ready" or (
                row["status"] == "cooldown" and cooldown is not None and cooldown <= now
            )
            enabled_after_cooldown = bool(row["enabled"]) or (row["status"] == "cooldown" and cooldown is not None and cooldown <= now)
            if not allow_unverified and (not ready_after_cooldown or not enabled_after_cooldown or not row["user_id"]):
                continue
            slot_used = self._recent_usage(slot_id=slot_id)
            minimum_cost = 2 if row.get("user_id") else 4
            if int(policy["session_limit_24h"]) - slot_used < minimum_cost:
                continue
            timestamp = self._timestamp()
            with FileLock(str(self.gate_path), timeout=10):
                with self.store.connect() as db:
                    check = db.execute("SELECT slot_id FROM x_batch_leases WHERE batch_key=? AND status IN ('active','paused')", (batch_key,)).fetchone()
                    if check:
                        return int(check["slot_id"])
                    db.execute("INSERT INTO x_batch_leases(batch_key,slot_id,operation,status,started_at,updated_at) VALUES(?,?,?,'active',?,?)", (batch_key, slot_id, operation, timestamp, timestamp))
                    db.execute("UPDATE x_collection_policy SET next_slot_id=?,updated_at=? WHERE policy_id=1", (1 if slot_id == 3 else slot_id + 1, timestamp))
            return slot_id
        raise XSessionUnavailableError("no verified X session is currently available")

    def batch_slot(self, batch_key: str, operation: str = "collection") -> int:
        return self._lease_slot(batch_key, operation)

    def reserve_request(
        self,
        slot_id: int,
        *,
        batch_key: str,
        operation: str,
        cost: int = 1,
        allow_unverified: bool = False,
    ) -> int:
        slot_id = int(slot_id)
        cost = max(1, int(cost))
        policy = self._policy()
        if not bool(policy["enabled"]):
            raise XSessionUnavailableError("X collection is manually paused")
        if not allow_unverified:
            row = self._row(slot_id)
            cooldown = self._parse_time(str(row.get("cooldown_until") or "")) if row else None
            ready_after_cooldown = bool(row and (row["status"] == "ready" or (row["status"] == "cooldown" and cooldown and cooldown <= self._now())))
            enabled_after_cooldown = bool(row and (row["enabled"] or (row["status"] == "cooldown" and cooldown and cooldown <= self._now())))
            if not row or not ready_after_cooldown or not enabled_after_cooldown:
                raise XSessionUnavailableError(f"X session slot {slot_id} is not ready")
        cutoff = (self._now() - timedelta(hours=24)).isoformat(timespec="seconds")
        for _ in range(3):
            wait_seconds = 0.0
            with FileLock(str(self.gate_path), timeout=30):
                with self.store.connect() as db:
                    paused = self._parse_time(str(policy.get("paused_until") or ""))
                    if paused and paused > self._now():
                        raise XBudgetDeferredError(f"X collection paused until {paused.isoformat(timespec='seconds')}")
                    global_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND status<>'cancelled'", (cutoff,)).fetchone()[0])
                    slot_identity = db.execute("SELECT user_id FROM x_session_slots WHERE slot_id=?", (slot_id,)).fetchone()
                    user_id = str(slot_identity["user_id"] or "") if slot_identity else ""
                    if user_id:
                        session_used = int(db.execute("SELECT COALESCE(SUM(b.estimated_requests),0) FROM x_request_budget b JOIN x_session_slots s ON s.slot_id=b.slot_id WHERE b.reserved_at>=? AND b.source='primary' AND s.user_id=? AND b.status<>'cancelled'", (cutoff, user_id)).fetchone()[0])
                    else:
                        session_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND source='primary' AND slot_id=? AND status<>'cancelled'", (cutoff, slot_id)).fetchone()[0])
                    if global_used + cost > int(policy["global_limit_24h"]) or session_used + cost > int(policy["session_limit_24h"]):
                        until = (self._now() + timedelta(hours=1)).isoformat(timespec="seconds")
                        db.execute("UPDATE x_collection_policy SET paused_until=?,pause_reason=?,updated_at=? WHERE policy_id=1", (until, "X request budget exhausted", self._timestamp()))
                        raise XBudgetDeferredError("X request budget exhausted")
                    latest = db.execute("SELECT MAX(reserved_at) FROM x_request_budget WHERE reserved_at>=?", (cutoff,)).fetchone()[0]
                    if user_id:
                        latest_slot = db.execute("SELECT MAX(b.reserved_at) FROM x_request_budget b JOIN x_session_slots s ON s.slot_id=b.slot_id WHERE b.reserved_at>=? AND b.source='primary' AND s.user_id=?", (cutoff, user_id)).fetchone()[0]
                    else:
                        latest_slot = db.execute("SELECT MAX(reserved_at) FROM x_request_budget WHERE reserved_at>=? AND source='primary' AND slot_id=?", (cutoff, slot_id)).fetchone()[0]
                    for value in (latest, latest_slot):
                        parsed = self._parse_time(str(value or ""))
                        if parsed:
                            wait_seconds = max(wait_seconds, int(policy["min_interval_seconds"]) - (self._now() - parsed).total_seconds())
                    if wait_seconds <= 0:
                        cursor = db.execute("INSERT INTO x_request_budget(slot_id,source,batch_key,operation,estimated_requests,reserved_at) VALUES(?, 'primary', ?,?,?,?)", (slot_id, batch_key, operation, cost, self._timestamp()))
                        return int(cursor.lastrowid)
            if wait_seconds > 0:
                if wait_seconds > int(policy["min_interval_seconds"]) + 5:
                    raise XBudgetDeferredError(f"X request interval opens in {int(wait_seconds)} seconds")
                time.sleep(wait_seconds)
        raise XBudgetDeferredError("X request gate could not reserve a permit")

    def finish_request(self, request_id: int, *, status: str = "completed", error_code: str = "", error: str = "") -> None:
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute("UPDATE x_request_budget SET status=?,completed_at=?,error_code=?,error=? WHERE request_id=?", (status, self._timestamp(), error_code[:80], error[:2000], int(request_id)))

    def record_success(self, slot_id: int) -> None:
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute("UPDATE x_session_slots SET last_success_at=?,cooldown_until='',last_error_code='',last_error='',consecutive_rate_limits=0,status='ready',enabled=1,updated_at=? WHERE slot_id=? AND status<>'disabled'", (self._timestamp(), self._timestamp(), int(slot_id)))

    def record_failure(self, slot_id: int, error_code: str, error: str) -> None:
        now = self._now()
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                row = db.execute("SELECT consecutive_rate_limits FROM x_session_slots WHERE slot_id=?", (int(slot_id),)).fetchone()
                count = int(row["consecutive_rate_limits"] if row else 0)
                if error_code == "rate_limited":
                    count += 1
                cooldown = now + timedelta(hours=24 if count >= 2 else 2)
                status = "cooldown" if error_code == "rate_limited" else "auth_required" if error_code == "auth_required" else "pending_verification"
                db.execute("UPDATE x_session_slots SET status=?,enabled=0,cooldown_until=?,last_error_code=?,last_error=?,consecutive_rate_limits=?,updated_at=? WHERE slot_id=?", (status, cooldown.isoformat(timespec="seconds") if error_code == "rate_limited" else "", error_code[:80], error[:2000], count, self._timestamp(), int(slot_id)))

    def kol_id_for_handle(self, handle: str) -> int:
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM kols WHERE platform='X' AND handle=? COLLATE NOCASE", (str(handle).lstrip("@"),)).fetchone()
        if not row:
            raise KeyError(f"X KOL handle not found: {handle}")
        return int(row["id"])

    def checkpoint(self, kol_id: int) -> dict[str, Any] | None:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM x_page_checkpoints WHERE kol_id=?", (int(kol_id),)).fetchone()
        return dict(row) if row else None

    def save_checkpoint(self, kol_id: int, slot_id: int, *, phase: str, user_id: str = "", latest_seen_post_id: str = "", contiguous_post_id: str = "", cursor: str = "", pages_completed: int = 0, last_page_new_ids: int = 0, stop_reason: str = "") -> None:
        timestamp = self._timestamp()
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute("""INSERT INTO x_page_checkpoints(kol_id,slot_id,user_id,phase,latest_seen_post_id,contiguous_post_id,cursor,pages_completed,last_page_new_ids,stop_reason,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kol_id) DO UPDATE SET slot_id=excluded.slot_id,user_id=excluded.user_id,phase=excluded.phase,latest_seen_post_id=excluded.latest_seen_post_id,contiguous_post_id=excluded.contiguous_post_id,cursor=excluded.cursor,pages_completed=excluded.pages_completed,last_page_new_ids=excluded.last_page_new_ids,stop_reason=excluded.stop_reason,updated_at=excluded.updated_at""", (int(kol_id), int(slot_id), str(user_id), phase, latest_seen_post_id, contiguous_post_id, cursor, int(pages_completed), int(last_page_new_ids), stop_reason[:2000], timestamp))


class PublicBackupDeferredError(TwitterProviderError):
    """The cookie-free public backup is paused or over its shared budget."""


class PublicBackupRateLimitError(TwitterRateLimitError):
    """FxTwitter returned an upstream rate-limit response."""


class PublicBackupNotFoundError(TwitterProviderError):
    """The requested public URL is not available from FxTwitter."""


class PublicBackupGate:
    """Cookie-free request gate sharing the X global budget ledger."""

    def __init__(self, store: Any, *, gate_path: Path | None = None):
        self.store = store
        self.gate_path = Path(gate_path or Path(store.path).with_name("x-session-gate.lock"))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(SHANGHAI)

    def _policy(self) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM x_collection_policy WHERE policy_id=1").fetchone()
        return dict(row) if row else {
            "enabled": 1, "global_limit_24h": 180, "public_enabled": 1,
            "public_limit_24h": 30, "min_interval_seconds": 60,
            "paused_until": "", "public_paused_until": "",
        }

    def status(self) -> dict[str, Any]:
        policy = self._policy()
        cutoff = (self._now() - timedelta(hours=24)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            global_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND status<>'cancelled'", (cutoff,)).fetchone()[0])
            public_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND source='public' AND status<>'cancelled'", (cutoff,)).fetchone()[0])
        now = self._now()
        paused = XSessionManager._parse_time(str(policy.get("public_paused_until") or ""))
        return {
            "enabled": bool(policy.get("public_enabled", 1)) and bool(policy.get("enabled", 1)),
            "provider": "fxtwitter",
            "adapter_version": "x-tweet-fetcher-3.0.0+f057d6b",
            "global_limit_24h": int(policy.get("global_limit_24h", 180)),
            "public_limit_24h": int(policy.get("public_limit_24h", 30)),
            "global_used_24h": global_used,
            "public_used_24h": public_used,
            "global_remaining_24h": max(0, int(policy.get("global_limit_24h", 180)) - global_used),
            "public_remaining_24h": max(0, int(policy.get("public_limit_24h", 30)) - public_used),
            "paused_until": str(policy.get("public_paused_until") or "") if paused and paused > now else "",
            "pause_reason": str(policy.get("public_pause_reason") or ""),
        }

    def reserve(self, *, batch_key: str, operation: str, cost: int = 1) -> int:
        cost = max(1, int(cost))
        for _ in range(3):
            policy = self._policy()
            if not bool(policy.get("enabled", 1)) or not bool(policy.get("public_enabled", 1)):
                raise PublicBackupDeferredError("public X backup is disabled")
            now = self._now()
            public_paused = XSessionManager._parse_time(str(policy.get("public_paused_until") or ""))
            global_paused = XSessionManager._parse_time(str(policy.get("paused_until") or ""))
            if global_paused and global_paused > now:
                raise PublicBackupDeferredError("X collection is globally paused")
            if public_paused and public_paused > now:
                raise PublicBackupDeferredError("public X backup is cooling down")
            cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")
            wait_seconds = 0.0
            with FileLock(str(self.gate_path), timeout=30):
                with self.store.connect() as db:
                    global_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND status<>'cancelled'", (cutoff,)).fetchone()[0])
                    public_used = int(db.execute("SELECT COALESCE(SUM(estimated_requests),0) FROM x_request_budget WHERE reserved_at>=? AND source='public' AND status<>'cancelled'", (cutoff,)).fetchone()[0])
                    if global_used + cost > int(policy.get("global_limit_24h", 180)):
                        raise PublicBackupDeferredError("X global request budget exhausted")
                    if public_used + cost > int(policy.get("public_limit_24h", 30)):
                        oldest = db.execute("SELECT MIN(reserved_at) FROM x_request_budget WHERE reserved_at>=? AND source='public' AND status<>'cancelled'", (cutoff,)).fetchone()[0]
                        reset = XSessionManager._parse_time(str(oldest or "")) or now
                        until = max(now + timedelta(hours=2), reset + timedelta(hours=24))
                        db.execute("UPDATE x_collection_policy SET public_paused_until=?,public_pause_reason=?,updated_at=? WHERE policy_id=1", (until.isoformat(timespec="seconds"), "FxTwitter public backup budget exhausted", now.isoformat(timespec="seconds")))
                        raise PublicBackupDeferredError("public X backup budget exhausted")
                    latest = db.execute("SELECT MAX(reserved_at) FROM x_request_budget WHERE reserved_at>=? AND status<>'cancelled'", (cutoff,)).fetchone()[0]
                    parsed = XSessionManager._parse_time(str(latest or ""))
                    if parsed:
                        wait_seconds = max(0.0, int(policy.get("min_interval_seconds", 60)) - (now - parsed).total_seconds())
                    if wait_seconds <= 0:
                        cursor = db.execute("INSERT INTO x_request_budget(slot_id,source,batch_key,operation,estimated_requests,reserved_at) VALUES(NULL,'public',?,?,?,?)", (batch_key, operation, cost, now.isoformat(timespec="seconds")))
                        return int(cursor.lastrowid)
            if wait_seconds > int(policy.get("min_interval_seconds", 60)) + 5:
                raise PublicBackupDeferredError("public X request interval is not open")
            time.sleep(wait_seconds)
        raise PublicBackupDeferredError("public X request gate could not reserve a permit")

    def finish(self, request_id: int, *, status: str = "completed", error_code: str = "", error: str = "") -> None:
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute("UPDATE x_request_budget SET status=?,completed_at=?,error_code=?,error=? WHERE request_id=?", (status, self._now().isoformat(timespec="seconds"), error_code[:80], error[:2000], int(request_id)))

    def pause(self, reason: str, *, seconds: int = 7200) -> None:
        until = (self._now() + timedelta(seconds=max(60, int(seconds)))).isoformat(timespec="seconds")
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                db.execute("UPDATE x_collection_policy SET public_paused_until=?,public_pause_reason=?,updated_at=? WHERE policy_id=1", (until, reason[:2000], self._now().isoformat(timespec="seconds")))

    def set_policy(self, *, enabled: bool | None = None, paused: bool | None = None, reason: str = "") -> dict[str, Any]:
        with FileLock(str(self.gate_path), timeout=10):
            with self.store.connect() as db:
                current = db.execute("SELECT * FROM x_collection_policy WHERE policy_id=1").fetchone()
                if not current:
                    return self.status()
                public_enabled = int(current["public_enabled"] if enabled is None else bool(enabled))
                pause_until = str(current["public_paused_until"] or "")
                pause_reason = str(current["public_pause_reason"] or "")
                if paused is True:
                    pause_until = (self._now() + timedelta(hours=24)).isoformat(timespec="seconds")
                    pause_reason = reason or "public X backup manually paused"
                elif paused is False:
                    pause_until = ""
                    pause_reason = ""
                db.execute("UPDATE x_collection_policy SET public_enabled=?,public_paused_until=?,public_pause_reason=?,updated_at=? WHERE policy_id=1", (public_enabled, pause_until, pause_reason[:2000], self._now().isoformat(timespec="seconds")))
        return self.status()
