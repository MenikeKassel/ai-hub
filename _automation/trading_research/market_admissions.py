from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ADMISSION_STATUSES = {"pending", "staging", "published", "failed"}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def published_manifest_path(market_root: Path) -> Path:
    return Path(market_root) / "manifests" / "published-symbols.json"


def read_published_manifest(market_root: Path) -> dict[str, Any]:
    path = published_manifest_path(market_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_published_manifest(
    market_root: Path,
    *,
    as_of: str,
    baseline_symbols: Iterable[str],
    extension_symbols: Iterable[str],
    source: str,
) -> dict[str, Any]:
    baseline = sorted({str(value) for value in baseline_symbols if re.fullmatch(r"\d{6}", str(value))})
    extensions = sorted({str(value) for value in extension_symbols if re.fullmatch(r"\d{6}", str(value))})
    published = sorted(set(baseline) | set(extensions))
    digest = hashlib.sha256("\n".join(published).encode("ascii")).hexdigest()
    value = {
        "version": 1,
        "as_of": as_of,
        "baseline_count": len(baseline),
        "extension_count": len(extensions),
        "published_count": len(published),
        "baseline_symbols": baseline,
        "extension_symbols": extensions,
        "published_symbols": published,
        "symbols_sha256": digest,
        "source": source,
        "updated_at": _now_iso(),
    }
    path = published_manifest_path(market_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return value


class MarketAdmissionRepository:
    """Durable SQLite queue between confirmed post leads and market publication."""

    def __init__(self, post_store: Any, *, ensure_schema: bool = True):
        self.post_store = post_store
        if ensure_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.post_store.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_symbol_admissions(
                    symbol TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','staging','published','failed')),
                    as_of TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    confirmed_lead_count INTEGER NOT NULL DEFAULT 0,
                    first_confirmed_at TEXT NOT NULL DEFAULT '',
                    last_confirmed_at TEXT NOT NULL DEFAULT '',
                    raw_end_date TEXT NOT NULL DEFAULT '',
                    qfq_end_date TEXT NOT NULL DEFAULT '',
                    manifest_sha256 TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_symbol_admission_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL REFERENCES market_symbol_admissions(symbol) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_symbol_admissions_status
                    ON market_symbol_admissions(status,updated_at DESC);
                """
            )

    def reconcile_confirmed(
        self,
        *,
        as_of: str,
        baseline_symbols: set[str],
        complete_symbols: set[str],
        raw_end_dates: dict[str, str],
        qfq_end_dates: dict[str, str],
        apply: bool,
    ) -> dict[str, Any]:
        with self.post_store.connect() as db:
            rows = db.execute(
                """
                SELECT symbol,COUNT(*) AS lead_count,
                       MIN(CASE WHEN reviewed_at<>'' THEN reviewed_at ELSE first_seen_at END),
                       MAX(CASE WHEN reviewed_at<>'' THEN reviewed_at ELSE updated_at END)
                FROM stock_leads
                WHERE status='confirmed'
                GROUP BY symbol
                ORDER BY symbol
                """
            ).fetchall()
        confirmed = {
            str(row[0]): {
                "lead_count": int(row[1]),
                "first_confirmed_at": str(row[2] or ""),
                "last_confirmed_at": str(row[3] or ""),
            }
            for row in rows
        }
        target_status = {
            symbol: ("published" if symbol in baseline_symbols or symbol in complete_symbols else "pending")
            for symbol in confirmed
        }
        if apply:
            timestamp = _now_iso()
            with self.post_store.connect() as db:
                for symbol, lead in confirmed.items():
                    status = target_status[symbol]
                    source = "baseline" if symbol in baseline_symbols else "confirmed_lead"
                    published_at = timestamp if status == "published" else ""
                    db.execute(
                        """
                        INSERT INTO market_symbol_admissions(
                            symbol,status,as_of,source,confirmed_lead_count,
                            first_confirmed_at,last_confirmed_at,raw_end_date,qfq_end_date,
                            manifest_sha256,error,published_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(symbol) DO UPDATE SET
                            status=excluded.status,
                            as_of=excluded.as_of,
                            source=excluded.source,
                            confirmed_lead_count=excluded.confirmed_lead_count,
                            first_confirmed_at=excluded.first_confirmed_at,
                            last_confirmed_at=excluded.last_confirmed_at,
                            raw_end_date=excluded.raw_end_date,
                            qfq_end_date=excluded.qfq_end_date,
                            error='',
                            published_at=CASE
                                WHEN excluded.status='published' AND market_symbol_admissions.published_at=''
                                THEN excluded.published_at
                                WHEN excluded.status='published' THEN market_symbol_admissions.published_at
                                ELSE '' END,
                            updated_at=excluded.updated_at
                        """,
                        (
                            symbol,
                            status,
                            as_of,
                            source,
                            lead["lead_count"],
                            lead["first_confirmed_at"],
                            lead["last_confirmed_at"],
                            raw_end_dates.get(symbol, ""),
                            qfq_end_dates.get(symbol, ""),
                            "",
                            "",
                            published_at,
                            timestamp,
                        ),
                    )
        counts = Counter(target_status.values())
        return {
            "confirmed_symbols": len(confirmed),
            "published": counts.get("published", 0),
            "pending": counts.get("pending", 0),
            "published_symbols": sorted(symbol for symbol, status in target_status.items() if status == "published"),
            "pending_symbols": sorted(symbol for symbol, status in target_status.items() if status == "pending"),
        }

    def queue_confirmed_symbols(self, symbols: Iterable[str], *, as_of: str) -> list[str]:
        requested = sorted({str(value) for value in symbols if re.fullmatch(r"\d{6}", str(value))})
        if not requested:
            return []
        timestamp = _now_iso()
        queued: list[str] = []
        with self.post_store.connect() as db:
            for symbol in requested:
                row = db.execute(
                    """
                    SELECT COUNT(*),
                           MIN(CASE WHEN reviewed_at<>'' THEN reviewed_at ELSE first_seen_at END),
                           MAX(CASE WHEN reviewed_at<>'' THEN reviewed_at ELSE updated_at END)
                    FROM stock_leads WHERE status='confirmed' AND symbol=?
                    """,
                    (symbol,),
                ).fetchone()
                if not row or not int(row[0]):
                    continue
                db.execute(
                    """
                    INSERT INTO market_symbol_admissions(
                        symbol,status,as_of,source,confirmed_lead_count,
                        first_confirmed_at,last_confirmed_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        confirmed_lead_count=excluded.confirmed_lead_count,
                        first_confirmed_at=excluded.first_confirmed_at,
                        last_confirmed_at=excluded.last_confirmed_at,
                        as_of=excluded.as_of,
                        status=CASE WHEN market_symbol_admissions.status='published'
                            THEN 'published' ELSE 'pending' END,
                        error='',updated_at=excluded.updated_at
                    """,
                    (symbol, "pending", as_of, "confirmed_lead", int(row[0]), str(row[1] or ""), str(row[2] or ""), timestamp),
                )
                queued.append(symbol)
        return queued

    def list(self, *, status: str | None = None, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        if status and status not in ADMISSION_STATUSES:
            raise ValueError("invalid market admission status")
        where = " WHERE status=?" if status else ""
        params: list[Any] = [status] if status else []
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        with self.post_store.connect() as db:
            rows = db.execute(
                f"SELECT * FROM market_symbol_admissions{where} ORDER BY updated_at DESC,symbol LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self.post_store.connect() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) FROM market_symbol_admissions GROUP BY status"
            ).fetchall()
            latest = db.execute("SELECT MAX(updated_at) FROM market_symbol_admissions").fetchone()
        counts = {str(row[0]): int(row[1]) for row in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "staging": counts.get("staging", 0),
            "published": counts.get("published", 0),
            "failed": counts.get("failed", 0),
            "updated_at": str(latest[0] or "") if latest else "",
        }
