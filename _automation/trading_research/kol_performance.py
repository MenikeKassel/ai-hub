"""Deterministic, batch-weighted KOL performance analysis.

This module deliberately sits above the existing return tracker.  It reads the
frozen checkpoint CSV and never rewrites marks, baselines, or event status.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import fmean, median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from filelock import FileLock
import httpx

from kol_posts import DeepSeekCredentialStore, ModelProviderUnavailableError
from opencode_go import OPENCODE_GO_API_URL, OPENCODE_GO_MODEL
from kol_tracker import (
    EventRecord,
    KolStore,
    PRIMARY_WARNINGS,
    is_executable_event,
    is_long_event,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
PERFORMANCE_VERSION = "kol-performance-v4"
HORIZONS = ("1W", "1M", "3M", "6M")
RECENT_WINDOWS = (7, 30, 90)


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _warning_tokens(event: EventRecord) -> set[str]:
    return {item.strip() for item in event.execution_warning.split(";") if item.strip()}


def _is_executable_long(event: EventRecord) -> bool:
    """Long events that are executable and free of primary execution warnings.

    Mirrors the primary-universe rule (PRIMARY_WARNINGS + is_executable_event)
    without the post-store evidence checks, so it can count the executable long
    universe from the event table alone.
    """
    return (
        is_long_event(event)
        and is_executable_event(event)
        and not (_warning_tokens(event) & PRIMARY_WARNINGS)
    )


def _parse_post_id(event: EventRecord) -> str:
    if event.source_post_id:
        return event.source_post_id
    match = re.search(r"(?:^|:)post:([^\s]+)", event.source_note)
    if match:
        return match.group(1)
    match = re.search(r"(?:status|answer)/(\d+)", event.source_url)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class KolIdentity:
    key: str
    kol_id: str
    handle: str
    display_name: str
    platform: str


@dataclass(frozen=True)
class BatchOutcome:
    key: str
    identity: KolIdentity
    source_url: str
    source_post_id: str
    posted_at: str
    event_ids: tuple[str, ...]
    symbols: tuple[str, ...]
    horizon: str
    trade_date: str
    directional_return: float
    directional_excess: float
    max_adverse: float
    max_favorable: float


@dataclass(frozen=True)
class AuditCounts:
    """Per-identity event counts that are independent of the return sample.

    These are audit-caliber counters over the active/completed event table and
    do not follow the window/horizon filters that shape the return metrics.
    """

    short_by_identity: dict[str, int]
    executable_long_by_identity: dict[str, int]


def bootstrap_ci(values: list[float], *, seed: str, iterations: int = 2000) -> tuple[float, float] | None:
    """Return a deterministic percentile CI for the median, if sample size permits."""
    if len(values) < 5:
        return None
    state = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16)
    samples: list[float] = []
    for _ in range(iterations):
        draw: list[float] = []
        for _ in values:
            state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
            draw.append(values[state % len(values)])
        samples.append(float(median(draw)))
    return (_percentile(samples, 0.025), _percentile(samples, 0.975))  # type: ignore[return-value]


def _metrics(outcomes: list[BatchOutcome], *, input_key: str) -> dict[str, Any]:
    """Return-sample metrics plus directional event counts.

    Field caliber (documented, stable since kol-performance-v3):
    - long_event_count: long events inside this metrics view's return sample
      (identical to event_count; follows the window/horizon filters).
    - short_event_count: short events excluded from returns (audit caliber,
      does not follow window/horizon filters; the service overrides the 0
      default with the per-identity audit count).
    - executable_long_event_count: long events that are executable and free of
      primary execution warnings (audit caliber; the service overrides the
      long_event_count fallback with the per-identity audit count).
    - audit_event_count: audit-retained event total = long_event_count +
      short_event_count.
    Returns are computed on long events only; short events never enter the
    return sample, win rate, or ranking.
    """
    if not outcomes:
        return {
            "batch_count": 0,
            "samples": 0,
            "event_count": 0,
            "long_event_count": 0,
            "short_event_count": 0,
            "executable_long_event_count": 0,
            "audit_event_count": 0,
            "recommendation_days": 0,
            "unique_symbols": 0,
            "unmatured_batch_count": 0,
            "median_return": None,
            "mean_return": None,
            "median_excess": None,
            "mean_excess": None,
            "win_rate": None,
            "median_mae": None,
            "median_adverse": None,
            "median_mfe": None,
            "worst_batch_excess": None,
            "p10_excess": None,
            "confidence_interval": None,
            "sample_status": "no_mature_samples",
        }
    returns = [item.directional_return for item in outcomes]
    excess = [item.directional_excess for item in outcomes]
    mae = [item.max_adverse for item in outcomes]
    mfe = [item.max_favorable for item in outcomes]
    ci = bootstrap_ci(excess, seed=input_key)
    long_event_count = sum(len(item.event_ids) for item in outcomes)
    return {
        "batch_count": len(outcomes),
        "samples": len(outcomes),
        "event_count": long_event_count,
        "long_event_count": long_event_count,
        "short_event_count": 0,
        "executable_long_event_count": long_event_count,
        "audit_event_count": long_event_count,
        "recommendation_days": len({item.posted_at[:10] for item in outcomes}),
        "unique_symbols": len({symbol for item in outcomes for symbol in item.symbols}),
        "unmatured_batch_count": 0,
        "median_return": median(returns),
        "mean_return": fmean(returns),
        "median_excess": median(excess),
        "mean_excess": fmean(excess),
        "win_rate": sum(value > 0 for value in excess) / len(excess),
        "median_mae": median(mae),
        "median_adverse": median(mae),
        "median_mfe": median(mfe),
        "worst_batch_excess": min(excess),
        "p10_excess": _percentile(excess, 0.10),
        "confidence_interval": {"lower": ci[0], "upper": ci[1]} if ci else None,
        "sample_status": "sufficient_for_ci" if ci else "sample_insufficient",
    }


def _tier(horizons: dict[str, dict[str, Any]]) -> tuple[str, str]:
    one_week = horizons["1W"]
    if one_week["batch_count"] < 5 or one_week["recommendation_days"] < 3:
        return "collecting", "1W"
    if horizons["6M"]["batch_count"] >= 20 and horizons["6M"]["recommendation_days"] >= 10:
        return "long_term", "6M"
    if horizons["3M"]["batch_count"] >= 20 and horizons["3M"]["recommendation_days"] >= 10:
        return "reliable", "3M"
    if horizons["1M"]["batch_count"] >= 10 and horizons["1M"]["recommendation_days"] >= 5:
        return "provisional", "1M"
    return "watch", "1W"


def _tier_label(tier: str) -> str:
    return {
        "collecting": "样本中",
        "watch": "观察中",
        "provisional": "初步排名",
        "reliable": "较可信",
        "long_term": "长期验证",
    }.get(tier, tier)


def _rule_narrative(row_name: str, tier: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """A citation-free fallback narrative; a model may replace only this layer."""
    strengths: list[str] = []
    risks: list[str] = []
    changes: list[str] = []
    if metrics.get("win_rate") is not None and metrics["win_rate"] >= 0.5:
        strengths.append("已成熟批次的超额胜率不低于50%")
    if metrics.get("median_excess") is not None and metrics["median_excess"] > 0:
        strengths.append("批次中位超额为正")
    if metrics.get("median_mae") is not None and metrics["median_mae"] < -0.05:
        risks.append("中位最大不利波动超过5%")
    if metrics.get("unmatured_batch_count", 0):
        risks.append(f"仍有{metrics['unmatured_batch_count']}个批次未到检查节点")
    if metrics.get("batch_count", 0) < 5:
        risks.append("成熟批次少于5个，不能形成稳定统计结论")
    summary = f"{row_name}当前处于{_tier_label(tier)}；本视图只描述已落地批次，不代表未来收益。"
    return {
        "summary": summary,
        "strengths": strengths,
        "risks": risks,
        "changes": changes,
        "limitations": ["只统计看多事件；看空事件保留审计记录但不计入A股收益", "同帖多股等权聚合", "不模拟手续费、滑点、仓位和资金占用", "AI解读不参与排名"],
        "evidence_refs": [],
        "provider": "rules",
        "model": "none",
        "status": "fallback",
    }


class PerformanceStore:
    """Append-only SQLite snapshots separate from the frozen return CSVs."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "performance.db"
        self.lock_path = self.root / "performance.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init(self) -> None:
        db = self.connect()
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS performance_runs(
                    run_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    foundation_release_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS performance_snapshots(
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    kol_key TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    window_name TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    rank INTEGER,
                    input_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    foundation_release_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(input_hash, platform, kol_key, horizon, window_name)
                );
                CREATE TABLE IF NOT EXISTS performance_narratives(
                    narrative_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    kol_key TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    window_name TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    foundation_release_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(input_hash, platform, kol_key, horizon, window_name)
                );
                CREATE INDEX IF NOT EXISTS idx_performance_series
                    ON performance_snapshots(platform,kol_key,horizon,window_name,as_of);
                """
            )
            for table, column in (
                ("performance_runs", "foundation_release_id"),
                ("performance_snapshots", "foundation_release_id"),
                ("performance_narratives", "foundation_release_id"),
            ):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            db.commit()
        finally:
            db.close()

    def save_run(
        self,
        run_id: str,
        as_of: str,
        input_hash: str,
        status: str = "completed",
        error: str = "",
        foundation_release_id: str = "",
    ) -> None:
        now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        db = self.connect()
        try:
            with FileLock(str(self.lock_path), timeout=60):
                db.execute(
                    """
                    INSERT OR REPLACE INTO performance_runs(
                        run_id,as_of,mode,algorithm_version,input_hash,status,started_at,completed_at,error,foundation_release_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (run_id, as_of, "deterministic", PERFORMANCE_VERSION, input_hash, status, now, now, error, foundation_release_id),
                )
                db.commit()
        finally:
            db.close()

    def save_snapshots(self, rows: Iterable[dict[str, Any]]) -> int:
        inserted = 0
        now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        db = self.connect()
        try:
            with FileLock(str(self.lock_path), timeout=60):
                for row in rows:
                    cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO performance_snapshots(
                        run_id,as_of,platform,kol_key,horizon,window_name,tier,rank,
                        input_hash,payload_json,created_at,foundation_release_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["run_id"], row["as_of"], row["platform"], row["kol_key"],
                        row["horizon"], row["window_name"], row["tier"], row.get("rank"),
                        row["input_hash"], json.dumps(row["payload"], ensure_ascii=False, sort_keys=True), now,
                        row.get("foundation_release_id", ""),
                    ),
                    )
                    inserted += int(cursor.rowcount > 0)
                db.commit()
        finally:
            db.close()
        return inserted

    def save_narrative(self, *, as_of: str, platform: str, kol_key: str, horizon: str, window_name: str, input_hash: str, payload: dict[str, Any], provider: str = "rules", model: str = "none", status: str = "fallback", foundation_release_id: str = "") -> bool:
        now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        db = self.connect()
        try:
            with FileLock(str(self.lock_path), timeout=60):
                cursor = db.execute(
                    """
                    INSERT OR IGNORE INTO performance_narratives(
                        as_of,platform,kol_key,horizon,window_name,input_hash,provider,model,status,payload_json,created_at,foundation_release_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (as_of, platform, kol_key, horizon, window_name, input_hash, provider, model, status, json.dumps(payload, ensure_ascii=False, sort_keys=True), now, foundation_release_id),
                )
                db.commit()
                return bool(cursor.rowcount > 0)
        finally:
            db.close()

    def latest(self, *, platform: str | None = None, kol_key: str | None = None) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if platform and platform != "all":
            clauses.append("platform=?")
            params.append(platform)
        if kol_key:
            clauses.append("kol_key=?")
            params.append(kol_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        db = self.connect()
        try:
            rows = db.execute(
                f"""
                SELECT * FROM performance_snapshots s
                {where} AND NOT EXISTS(
                    SELECT 1 FROM performance_snapshots newer
                    WHERE newer.platform=s.platform AND newer.kol_key=s.kol_key
                      AND newer.horizon=s.horizon AND newer.window_name=s.window_name
                      AND (newer.as_of>s.as_of OR (newer.as_of=s.as_of AND newer.snapshot_id>s.snapshot_id))
                )
                ORDER BY s.platform,s.kol_key,s.horizon,s.window_name
                """ if where else """
                SELECT * FROM performance_snapshots s
                WHERE NOT EXISTS(
                    SELECT 1 FROM performance_snapshots newer
                    WHERE newer.platform=s.platform AND newer.kol_key=s.kol_key
                      AND newer.horizon=s.horizon AND newer.window_name=s.window_name
                      AND (newer.as_of>s.as_of OR (newer.as_of=s.as_of AND newer.snapshot_id>s.snapshot_id))
                )
                ORDER BY s.platform,s.kol_key,s.horizon,s.window_name
                """,
                params,
            ).fetchall()
        finally:
            db.close()
        return [self._decode(row) for row in rows]

    def series(self, kol_key: str, *, horizon: str, window_name: str) -> list[dict[str, Any]]:
        db = self.connect()
        try:
            rows = db.execute(
                """
                SELECT * FROM performance_snapshots
                WHERE kol_key=? AND horizon=? AND window_name=?
                  AND NOT EXISTS(
                    SELECT 1 FROM performance_snapshots newer
                    WHERE newer.platform=performance_snapshots.platform
                      AND newer.kol_key=performance_snapshots.kol_key
                      AND newer.horizon=performance_snapshots.horizon
                      AND newer.window_name=performance_snapshots.window_name
                      AND newer.as_of=performance_snapshots.as_of
                      AND newer.snapshot_id>performance_snapshots.snapshot_id
                  )
                ORDER BY as_of
                """,
                (kol_key, horizon, window_name),
            ).fetchall()
        finally:
            db.close()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value


class KolPerformanceService:
    def __init__(self, event_store: KolStore, *, post_store: Any | None = None, store: PerformanceStore | None = None):
        self.event_store = event_store
        self.post_store = post_store
        self.store = store or PerformanceStore(event_store.root)
        self._post_cache: dict[str, dict[str, Any] | None] = {}

    def resolve_identity(self, event: EventRecord) -> KolIdentity:
        post_id = _parse_post_id(event)
        post = None
        if post_id and self.post_store is not None:
            post = self._post_cache.get(post_id)
            if post is None and post_id not in self._post_cache:
                try:
                    post = self.post_store.get_post(post_id)
                except (KeyError, OSError, sqlite3.Error):
                    post = None
                self._post_cache[post_id] = post
        platform = str(event.platform or (post or {}).get("platform") or "X")
        kol_id = str(event.kol_id or (post or {}).get("kol_id") or "")
        handle = str(event.kol_handle or (post or {}).get("handle") or "")
        display_name = str((post or {}).get("display_name") or event.kol_name)
        if kol_id:
            key = f"{platform}:{kol_id}"
        else:
            # Legacy data is intentionally isolated from future stable identities.
            key = f"legacy:{platform}:{event.kol_name.strip().lower()}"
            handle = handle or "legacy"
            kol_id = "legacy"
        return KolIdentity(key, kol_id, handle, display_name, platform)

    def _is_primary(self, event: EventRecord) -> bool:
        if event.status not in {"active", "completed"} or not is_executable_event(event):
            return False
        if _warning_tokens(event) & PRIMARY_WARNINGS:
            return False
        post_id = _parse_post_id(event)
        if self.post_store is None or not post_id:
            return True
        post = self._post_cache.get(post_id)
        if post is None and post_id not in self._post_cache:
            try:
                post = self.post_store.get_post(post_id)
            except (KeyError, OSError, sqlite3.Error):
                post = None
            self._post_cache[post_id] = post
        if not post:
            return True
        if post.get("post_type") in {"retweet", "aggregation"}:
            return False
        if post.get("evidence_type") in {"retrospective", "secondhand", "ambiguous"}:
            return False
        return True

    def _foundation_release_id(self) -> str:
        for row in reversed(self.event_store.load_marks()):
            value = str(row.get("foundation_release_id") or "")
            if value:
                return value
        return ""

    def _events_and_identities(self) -> tuple[list[EventRecord], dict[str, KolIdentity], dict[str, bool]]:
        events = self.event_store.load_events()
        identities: dict[str, KolIdentity] = {}
        primary: dict[str, bool] = {}
        for event in events:
            identity = self.resolve_identity(event)
            identities[identity.key] = identity
            primary[event.event_id] = self._is_primary(event)
        if self.post_store is not None:
            try:
                for kol in self.post_store.list_kols():
                    if str(kol.get("tracking_mode") or "") == "aggregation":
                        continue
                    platform = str(kol.get("platform") or "X")
                    key = f"{platform}:{kol['id']}"
                    identities.setdefault(
                        key,
                        KolIdentity(key, str(kol["id"]), str(kol.get("handle") or ""), str(kol.get("display_name") or ""), platform),
                    )
            except (OSError, sqlite3.Error):
                pass
        return events, identities, primary

    def _outcomes(
        self, *, as_of: date, horizon: str, primary_only: bool | None
    ) -> tuple[list[BatchOutcome], set[str], set[str], AuditCounts]:
        events, identities, primary = self._events_and_identities()
        event_by_id = {event.event_id: event for event in events}
        all_batch_keys: set[str] = set()
        short_by_identity: dict[str, int] = {}
        executable_long_by_identity: dict[str, int] = {}
        for event in events:
            identity = identities[self.resolve_identity(event).key]
            if event.status not in {"active", "completed"}:
                continue
            if not is_long_event(event):
                # Short events never enter the return pipeline; they are
                # counted here so the audit totals keep them visible.
                short_by_identity[identity.key] = short_by_identity.get(identity.key, 0) + 1
                continue
            if _is_executable_long(event):
                executable_long_by_identity[identity.key] = executable_long_by_identity.get(identity.key, 0) + 1
            if primary_only is True and not primary.get(event.event_id, False):
                continue
            if primary_only is False and primary.get(event.event_id, False):
                continue
            batch_id = _parse_post_id(event)
            all_batch_keys.add(f"{identity.key}|{batch_id or event.source_url}")
        grouped: dict[str, list[tuple[EventRecord, dict[str, str], KolIdentity]]] = {}
        for row in self.event_store.load_checkpoints():
            if str(row.get("horizon") or "") != horizon or row.get("verification_status") != "verified":
                continue
            event = event_by_id.get(str(row.get("event_id") or ""))
            if event is None or event.status not in {"active", "completed"}:
                continue
            if not is_long_event(event):
                continue
            if primary_only is True and not primary.get(event.event_id, False):
                continue
            if primary_only is False and primary.get(event.event_id, False):
                continue
            identity = self.resolve_identity(event)
            batch_id = _parse_post_id(event)
            batch_key = f"{identity.key}|{batch_id or event.source_url}"
            grouped.setdefault(batch_key, []).append((event, row, identity))

        outcomes: list[BatchOutcome] = []
        mature_keys: set[str] = set()
        for batch_key, items in grouped.items():
            identity = items[0][2]
            trade_dates = [str(item[1].get("trade_date") or "") for item in items]
            effective_date = max((item for item in trade_dates if item), default="")
            if effective_date and effective_date > as_of.isoformat():
                continue
            returns = [_number(item[1].get("directional_return")) for item in items]
            excess = [_number(item[1].get("directional_excess_return")) for item in items]
            adverse = [_number(item[1].get("max_adverse_return")) for item in items]
            favorable = [_number(item[1].get("max_favorable_return")) for item in items]
            if any(value is None for value in (*returns, *excess, *adverse, *favorable)):
                continue
            event_ids = tuple(item[0].event_id for item in items)
            outcomes.append(
                BatchOutcome(
                    key=batch_key,
                    identity=identity,
                    source_url=items[0][0].source_url,
                    source_post_id=_parse_post_id(items[0][0]),
                    posted_at=min(item[0].posted_at for item in items),
                    event_ids=event_ids,
                    symbols=tuple(sorted({item[0].symbol for item in items})),
                    horizon=horizon,
                    trade_date=effective_date,
                    directional_return=fmean(value for value in returns if value is not None),
                    directional_excess=fmean(value for value in excess if value is not None),
                    max_adverse=fmean(value for value in adverse if value is not None),
                    max_favorable=fmean(value for value in favorable if value is not None),
                )
            )
            mature_keys.add(batch_key)
        return (
            outcomes,
            all_batch_keys - mature_keys,
            all_batch_keys,
            AuditCounts(
                short_by_identity=short_by_identity,
                executable_long_by_identity=executable_long_by_identity,
            ),
        )

    def _row_for_identity(
        self,
        identity: KolIdentity,
        *,
        as_of: date,
        horizon: str,
        window_name: str,
        primary_only: bool | None,
    ) -> dict[str, Any]:
        outcomes, unmatured_keys, _, audit_counts = self._outcomes(as_of=as_of, horizon=horizon, primary_only=primary_only)
        matching = [item for item in outcomes if item.identity.key == identity.key]
        if window_name != "all":
            days = int(window_name)
            start = as_of - timedelta(days=days)
            matching = [item for item in matching if start.isoformat() <= item.trade_date <= as_of.isoformat()]
        metrics = _metrics(matching, input_key=f"{as_of}|{identity.key}|{horizon}|{window_name}|{primary_only}")
        self._apply_audit_counts(metrics, identity, audit_counts)
        # Count the maturity backlog for the same KOL even when the recent window is empty.
        metrics["unmatured_batch_count"] = sum(
            1 for key in unmatured_keys if key.startswith(identity.key + "|")
        )
        tier_source = {}
        for item_horizon in HORIZONS:
            values, _, _, horizon_audit = self._outcomes(as_of=as_of, horizon=item_horizon, primary_only=primary_only)
            values = [item for item in values if item.identity.key == identity.key]
            horizon_metrics = _metrics(values, input_key=f"{as_of}|{identity.key}|{item_horizon}|all|{primary_only}")
            self._apply_audit_counts(horizon_metrics, identity, horizon_audit)
            tier_source[item_horizon] = horizon_metrics
        tier, rank_horizon = _tier(tier_source)
        row = {
            "kol_key": identity.key,
            "kol_id": identity.kol_id,
            "kol_handle": identity.handle,
            "kol_name": identity.display_name,
            "platform": identity.platform,
            "tier": tier,
            "tier_label": _tier_label(tier),
            "rank": None,
            "rank_horizon": rank_horizon,
            "horizon": horizon,
            "window": window_name,
            "as_of": as_of.isoformat(),
            "metrics": metrics,
            "horizons": tier_source,
            "primary_only": primary_only,
        }
        row["narrative"] = _rule_narrative(identity.display_name, tier, metrics)
        return row

    @staticmethod
    def _apply_audit_counts(metrics: dict[str, Any], identity: KolIdentity, audit_counts: AuditCounts) -> None:
        """Overlay audit-caliber directional counts onto a metrics dict.

        long_event_count stays the return-sample count; short_event_count and
        executable_long_event_count are audit-caliber (full active/completed
        universe for the identity, independent of window/horizon/primary_only).
        audit_event_count is the audit-retained total = long + short.
        """
        metrics["short_event_count"] = audit_counts.short_by_identity.get(identity.key, 0)
        metrics["executable_long_event_count"] = audit_counts.executable_long_by_identity.get(identity.key, 0)
        metrics["audit_event_count"] = metrics["long_event_count"] + metrics["short_event_count"]

    @staticmethod
    def _rank(rows: list[dict[str, Any]], horizon: str) -> None:
        qualified = [
            row for row in rows
            if row["tier"] in {"provisional", "reliable", "long_term"}
            and row["horizons"][horizon]["batch_count"] > 0
        ]
        qualified.sort(
            key=lambda row: (
                row["horizons"][horizon]["median_excess"] if row["horizons"][horizon]["median_excess"] is not None else float("-inf"),
                row["horizons"][horizon]["win_rate"] if row["horizons"][horizon]["win_rate"] is not None else float("-inf"),
                -(row["horizons"][horizon]["median_mae"] or 0),
                row["horizons"][horizon]["batch_count"],
            ),
            reverse=True,
        )
        for index, row in enumerate(qualified, 1):
            row["rank"] = index

    def compute(self, *, as_of: date, platform: str | None = None, window: int | str = "all", horizon: str = "1W", primary_only: bool = True) -> dict[str, Any]:
        if horizon not in HORIZONS:
            raise ValueError(f"unsupported horizon: {horizon}")
        window_name = str(window)
        if window_name not in {"all", *(str(value) for value in RECENT_WINDOWS)}:
            raise ValueError("window must be 7, 30, 90, or all")
        _, identities, _ = self._events_and_identities()
        selected = [item for item in identities.values() if not platform or platform == "all" or item.platform == platform]
        rows = [self._row_for_identity(item, as_of=as_of, horizon=horizon, window_name=window_name, primary_only=primary_only) for item in selected]
        # Rank within platform.  A combined view never compares X and Zhihu.
        for platform_name in sorted({row["platform"] for row in rows}):
            self._rank([row for row in rows if row["platform"] == platform_name], horizon)
        rows.sort(key=lambda row: (row["platform"], row["rank"] is None, row["rank"] or 9999, row["kol_name"]))
        events = self.event_store.load_events()
        primary_events = [event for event in events if self._is_primary(event)]
        return {
            "version": PERFORMANCE_VERSION,
            "as_of": as_of.isoformat(),
            "platform": platform or "all",
            "window": window_name,
            "horizon": horizon,
            "primary_only": primary_only,
            "coverage": {
                "total_events": sum(event.status in {"active", "completed"} for event in events),
                "primary_events": len(primary_events),
                "kol_count": len(rows),
                "ranked_count": sum(row["rank"] is not None for row in rows),
                "mature_batch_count": sum(row["metrics"]["batch_count"] for row in rows),
                "unmatured_batch_count": sum(row["metrics"]["unmatured_batch_count"] for row in rows),
                "sample_note": "主分析仅含已验证且可执行的事前看多推荐；看空事件保留审计记录但不计入A股收益，小样本只展示、不排名。",
            },
            "rows": rows,
            "secondary": self.compute_secondary(as_of=as_of, platform=platform, window=window_name, horizon=horizon),
        }

    def compute_secondary(self, *, as_of: date, platform: str | None, window: str, horizon: str) -> dict[str, Any]:
        _, identities, _ = self._events_and_identities()
        selected = [item for item in identities.values() if not platform or platform == "all" or item.platform == platform]
        rows = [self._row_for_identity(item, as_of=as_of, horizon=horizon, window_name=window, primary_only=False) for item in selected]
        return {
            "description": "不可执行、条件、来源受限或二手看多事件的观点表现，仅供审计，不参与能力判断；看空事件不纳入A股收益口径。",
            "rows": rows,
        }

    def refresh(self, *, as_of: date) -> dict[str, Any]:
        result = self.compute(as_of=as_of, window="all", horizon="1W", primary_only=True)
        run_id = f"perf-{uuid.uuid4().hex[:12]}"
        foundation_release_id = self._foundation_release_id()
        input_hash = _hash(result)
        snapshots: list[dict[str, Any]] = []
        for row in result["rows"]:
            for horizon in HORIZONS:
                metrics = row["horizons"][horizon]
                narrative = _rule_narrative(row["kol_name"], row["tier"], metrics)
                payload = {
                    **row,
                    "horizon": horizon,
                    "metrics": metrics,
                    "narrative": narrative,
                    "foundation_release_id": foundation_release_id,
                }
                snapshots.append({
                    "run_id": run_id,
                    "as_of": as_of.isoformat(),
                    "platform": row["platform"],
                    "kol_key": row["kol_key"],
                    "horizon": horizon,
                    "window_name": "all",
                    "tier": row["tier"],
                    "rank": row["rank"],
                    "input_hash": _hash(payload),
                    "payload": payload,
                    "foundation_release_id": foundation_release_id,
                })
                self.store.save_narrative(
                    as_of=as_of.isoformat(),
                    platform=row["platform"],
                    kol_key=row["kol_key"],
                    horizon=horizon,
                    window_name="all",
                    input_hash=_hash(payload),
                    payload=narrative,
                    foundation_release_id=foundation_release_id,
                )
        inserted = self.store.save_snapshots(snapshots)
        self.store.save_run(run_id, as_of.isoformat(), input_hash, foundation_release_id=foundation_release_id)
        result["run_id"] = run_id
        result["snapshots_inserted"] = inserted
        return result

    def explain(self, result: dict[str, Any], provider: "DeepSeekPerformanceInterpreter") -> dict[str, Any]:
        for row in result.get("rows", []):
            narrative = provider.interpret(row)
            row["narrative"] = narrative
            payload = {key: value for key, value in row.items() if key != "narrative"}
            self.store.save_narrative(
                as_of=str(result["as_of"]),
                platform=str(row["platform"]),
                kol_key=str(row["kol_key"]),
                horizon=str(row["horizon"]),
                window_name="all",
                input_hash=_hash(payload),
                payload=narrative,
                provider=str(narrative.get("provider") or "rules"),
                model=str(narrative.get("model") or "none"),
                status=str(narrative.get("status") or "fallback"),
                foundation_release_id=self._foundation_release_id(),
            )
        return result

    def migrate_event_identities(self) -> dict[str, Any]:
        """Fill stable fields when a saved post can prove the identity.

        The migration is intentionally conservative: a legacy event without a
        resolvable post keeps its display name and is isolated under a legacy
        key instead of being guessed into a real account.
        """
        events = self.event_store.load_events()
        changed: list[str] = []
        migrated: list[EventRecord] = []
        for event in events:
            post_id = _parse_post_id(event)
            identity = self.resolve_identity(event)
            values: dict[str, str] = {}
            if event.source_post_id != post_id and post_id:
                values["source_post_id"] = post_id
            if identity.kol_id != "legacy" and event.kol_id != identity.kol_id:
                values["kol_id"] = identity.kol_id
            if identity.handle != "legacy" and event.kol_handle != identity.handle:
                values["kol_handle"] = identity.handle
            updated = replace(event, **values) if values else event
            migrated.append(updated)
            if values:
                changed.append(event.event_id)
        if changed:
            self.event_store.save_events(migrated)
        return {"changed": changed, "unchanged": len(events) - len(changed), "total": len(events)}

    def doctor(self) -> dict[str, Any]:
        events = self.event_store.load_events()
        missing = [event.event_id for event in events if event.status in {"active", "completed"} and not self.resolve_identity(event).key]
        identity_counts: dict[str, int] = {}
        for event in events:
            identity = self.resolve_identity(event)
            identity_counts[identity.platform] = identity_counts.get(identity.platform, 0) + 1
        return {
            "ok": not missing,
            "version": PERFORMANCE_VERSION,
            "events": len(events),
            "active_or_completed": sum(event.status in {"active", "completed"} for event in events),
            "missing_identity": missing,
            "platform_event_counts": identity_counts,
            "performance_db": str(self.store.path),
        }


def build_weekly_message(result: dict[str, Any]) -> str:
    rows = [row for row in result.get("rows", []) if row.get("rank") is not None]
    if not rows:
        return f"[KOL表现周报] 截至 {result.get('as_of', '')} 无新增达到正式排名门槛的成熟样本；小样本和未成熟批次仍在观察。"
    lines = [f"[KOL表现周报] 截至 {result.get('as_of', '')}，正式排名仅按平台内看多事件中位超额排序。"]
    for row in rows[:10]:
        metrics = row["horizons"][row["rank_horizon"]]
        value = metrics.get("median_excess")
        lines.append(f"{row['platform']} {row['kol_name']}：{row['tier_label']}，{metrics['batch_count']}批，中位超额 {value:.2%}，胜率 {metrics['win_rate']:.1%}。" if value is not None else f"{row['platform']} {row['kol_name']}：{row['tier_label']}，样本不足。")
    lines.append("限制：只统计看多事件，看空事件保留审计记录但不计入A股收益；同帖多股按等权批次统计；X与知乎分开；AI解读不参与排名。")
    return "\n".join(lines)


class DeepSeekPerformanceInterpreter:
    """Optional narrative provider; it receives aggregate facts only."""

    model_name = OPENCODE_GO_MODEL
    provider_name = "opencode-go"
    api_url = OPENCODE_GO_API_URL

    def __init__(self, credentials: DeepSeekCredentialStore | None = None, *, timeout_seconds: float = 90):
        self.credentials = credentials or DeepSeekCredentialStore()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _fallback(row: dict[str, Any]) -> dict[str, Any]:
        return row.get("narrative") or _rule_narrative(str(row.get("kol_name") or "KOL"), str(row.get("tier") or "collecting"), row.get("metrics") or {})

    def interpret(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            api_key = self.credentials.load()
            facts = {
                "as_of": row.get("as_of"),
                "platform": row.get("platform"),
                "kol_name": row.get("kol_name"),
                "tier": row.get("tier"),
                "horizon": row.get("horizon"),
                "metrics": row.get("metrics"),
            }
            body = {
                "model": self.model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是KOL表现审计解释器。只解释输入的聚合事实，不提供投资建议，不推断未给出的原因。"
                            "只返回JSON对象，键必须为summary、strengths、risks、changes、limitations、evidence_refs；"
                            "所有值分别为字符串或字符串数组，证据引用只能填写输入字段路径。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            response = httpx.post(
                self.api_url,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=self.timeout_seconds,
            )
            if response.status_code >= 400:
                raise ModelProviderUnavailableError(f"OpenCode Go performance explanation HTTP {response.status_code}")
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
            payload = json.loads(content)
            required = ("summary", "strengths", "risks", "changes", "limitations", "evidence_refs")
            if not isinstance(payload, dict) or any(key not in payload for key in required):
                raise ValueError("OpenCode Go performance explanation schema mismatch")
            if not isinstance(payload["summary"], str) or any(not isinstance(payload[key], list) for key in required[1:]):
                raise ValueError("OpenCode Go performance explanation types mismatch")
            payload["provider"] = self.provider_name
            payload["model"] = self.model_name
            payload["status"] = "ready"
            return payload
        except Exception:
            return self._fallback(row)
