from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class ModelDailyBudget:
    """Persistent cross-process daily cap for KOL candidate classification."""

    def __init__(self, store: Any, *, daily_limit: int = 250):
        self.store = store
        self.daily_limit = max(1, int(daily_limit))
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_daily_usage(
                    usage_date TEXT PRIMARY KEY,
                    daily_limit INTEGER NOT NULL,
                    attempted INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def usage_date() -> str:
        return datetime.now(SHANGHAI).date().isoformat()

    def status(self) -> dict[str, Any]:
        usage_date = self.usage_date()
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM model_daily_usage WHERE usage_date=?",
                (usage_date,),
            ).fetchone()
        value = dict(row) if row else {
            "usage_date": usage_date,
            "daily_limit": self.daily_limit,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "updated_at": "",
        }
        effective_limit = max(1, int(value.get("daily_limit") or self.daily_limit))
        value["daily_limit"] = effective_limit
        value["remaining"] = max(0, effective_limit - int(value.get("attempted") or 0))
        return value

    def reserve(self) -> bool:
        usage_date = self.usage_date()
        timestamp = _now_iso()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO model_daily_usage(
                    usage_date,daily_limit,attempted,completed,failed,updated_at
                ) VALUES(?,?,0,0,0,?)
                """,
                (usage_date, self.daily_limit, timestamp),
            )
            cursor = db.execute(
                """
                UPDATE model_daily_usage
                SET attempted=attempted+1,daily_limit=?,updated_at=?
                WHERE usage_date=? AND attempted<daily_limit
                """,
                (self.daily_limit, timestamp, usage_date),
            )
        return cursor.rowcount == 1

    def finish(self, *, success: bool) -> None:
        column = "completed" if success else "failed"
        with self.store.connect() as db:
            db.execute(
                f"UPDATE model_daily_usage SET {column}={column}+1,updated_at=? WHERE usage_date=?",
                (_now_iso(), self.usage_date()),
            )


class OcrDailyBudget:
    """Persistent OCR accounting with an explicit bounded/unlimited mode."""

    def __init__(self, store: Any, *, daily_limit: int = 150, limit_mode: str = "unlimited"):
        self.store = store
        self.daily_limit = max(1, int(daily_limit))
        if limit_mode not in {"bounded", "unlimited"}:
            raise ValueError("limit_mode must be bounded or unlimited")
        self.limit_mode = limit_mode
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ocr_daily_usage(
                    usage_date TEXT PRIMARY KEY,
                    limit_mode TEXT NOT NULL DEFAULT 'bounded'
                        CHECK(limit_mode IN ('bounded','unlimited')),
                    daily_limit INTEGER NOT NULL,
                    attempted INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            existing = {row[1] for row in db.execute("PRAGMA table_info(ocr_daily_usage)").fetchall()}
            if "limit_mode" not in existing:
                db.execute("ALTER TABLE ocr_daily_usage ADD COLUMN limit_mode TEXT NOT NULL DEFAULT 'bounded'")

    @staticmethod
    def usage_date() -> str:
        return datetime.now(SHANGHAI).date().isoformat()

    def status(self) -> dict[str, Any]:
        usage_date = self.usage_date()
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM ocr_daily_usage WHERE usage_date=?", (usage_date,)).fetchone()
        value = dict(row) if row else {
            "usage_date": usage_date,
            "limit_mode": self.limit_mode,
            "daily_limit": self.daily_limit,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "updated_at": "",
        }
        # The caller's policy is authoritative for the current run.  This
        # lets an unlimited run resume a legacy bounded row without first
        # hitting the old row's remaining counter; reserve() persists the
        # selected mode on the next attempt.
        mode = self.limit_mode
        value["limit_mode"] = mode
        effective_limit = max(1, int(value.get("daily_limit") or self.daily_limit))
        value["daily_limit"] = None if mode == "unlimited" else effective_limit
        value["remaining"] = None if mode == "unlimited" else max(0, effective_limit - int(value.get("attempted") or 0))
        return value

    def reserve(self) -> bool:
        usage_date = self.usage_date()
        timestamp = _now_iso()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO ocr_daily_usage(usage_date,limit_mode,daily_limit,updated_at) VALUES(?,?,?,?)",
                (usage_date, self.limit_mode, self.daily_limit, timestamp),
            )
            cursor = db.execute(
                "UPDATE ocr_daily_usage SET limit_mode=?,daily_limit=?,attempted=attempted+1,updated_at=? "
                "WHERE usage_date=? AND (limit_mode='unlimited' OR attempted<daily_limit)",
                (self.limit_mode, self.daily_limit, timestamp, usage_date),
            )
        return cursor.rowcount == 1

    def finish(self, *, success: bool) -> None:
        column = "completed" if success else "failed"
        with self.store.connect() as db:
            db.execute(
                f"UPDATE ocr_daily_usage SET {column}={column}+1,updated_at=? WHERE usage_date=?",
                (_now_iso(), self.usage_date()),
            )
