from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock
from market_cross_section import normalise_cross_section


SHANGHAI_INDEXES = {"000001", "000016", "000300", "000688", "000905", "000852"}
MAJOR_INDEXES = SHANGHAI_INDEXES | {"399001", "399006"}
ADJUST_FLAGS = {"raw": "3", "qfq": "2", "hfq": "1"}
MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
MARKET_OPEN_TIME = time(9, 30)
MARKET_CLOSE_TIME = time(15, 0)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _classify_baostock_instrument(
    exchange: str,
    symbol: str,
    name: str,
    type_code: str,
) -> str:
    del exchange
    if type_code == "2":
        return "index"
    if "ETF" in name.upper() or (symbol.startswith(("1", "5")) and "基金" in name):
        return "etf"
    if symbol.startswith(("0", "3", "4", "6", "8")) or symbol.startswith("92"):
        return "stock"
    return ""


def _duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("duckdb is required; install trading_research/requirements.txt") from exc
    return duckdb


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    instrument_type: str
    exchange: str
    status: str = "active"
    list_date: str = ""
    lifecycle: str = "tracking"
    source: str = "manual"
    first_seen_at: str = ""
    last_mentioned_at: str = ""


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str
    rows: int = 0


@dataclass(frozen=True)
class DataAudit:
    status: str
    issues: list[QualityIssue] = field(default_factory=list)


@dataclass(frozen=True)
class SyncResult:
    run_id: str
    symbol: str
    provider: str
    adjustment: str
    rows: int
    quality_status: str
    raw_path: str = ""
    normalized_paths: list[str] = field(default_factory=list)
    error: str = ""


class DailyBarProvider(Protocol):
    name: str

    def fetch_daily(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        adjustment: str,
    ) -> pd.DataFrame: ...


class MinuteBarProvider(Protocol):
    name: str

    def fetch_minute(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        frequency: str,
        adjustment: str,
    ) -> pd.DataFrame: ...


class MarketStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.raw_root = self.root / "raw"
        self.warehouse_root = self.root / "warehouse"
        self.audit_root = self.root / "audits"
        self.manifest_root = self.root / "manifests"
        self.db_path = self.root / "market.duckdb"
        self.manifest_path = self.manifest_root / "runs.jsonl"
        self.lock_path = self.root / ".market.lock"
        for path in (self.raw_root, self.warehouse_root, self.audit_root, self.manifest_root):
            path.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def lock(self, *, timeout: float = 30):
        with FileLock(str(self.lock_path), timeout=timeout):
            yield

    @contextmanager
    def connect(self, *, timeout: float = 30, lock: bool = True):
        """Open DuckDB only while holding the shared cross-process lock.

        DuckDB's file locking is stricter on Windows than the advisory lock
        used by the application.  Acquiring the same lock for reads as well
        as writes prevents the UI health endpoint from racing a sync task.
        """
        lock_context = self.lock(timeout=timeout) if lock else nullcontext()
        with lock_context:
            connection = _duckdb().connect(str(self.db_path))
            try:
                yield connection
            finally:
                connection.close()

    def _migrate(self) -> None:
        with self.lock(timeout=30):
            existing_version = 0
            if self.db_path.exists() and self.db_path.stat().st_size:
                try:
                    with self.connect(lock=False) as existing:
                        table = existing.execute(
                            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='schema_meta'"
                        ).fetchone()[0]
                        if table:
                            existing_version = int(existing.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)
                except Exception:
                    existing_version = 0
                if existing_version < 8:
                    backup_dir = self.root / "backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    shutil.copy2(self.db_path, backup_dir / f"{stamp}_market.duckdb")

            with self.connect(lock=False) as db:
                db.execute("BEGIN TRANSACTION")
                try:
                    db.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS instruments(
                    symbol VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    instrument_type VARCHAR NOT NULL,
                    exchange VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    list_date VARCHAR NOT NULL,
                    lifecycle VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    first_seen_at VARCHAR NOT NULL,
                    last_mentioned_at VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instrument_catalog(
                    instrument_key VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    instrument_type VARCHAR NOT NULL,
                    exchange VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    list_date VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    snapshot_date VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trading_calendar(
                    trade_date DATE PRIMARY KEY,
                    is_open BOOLEAN NOT NULL,
                    provider VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_runs(
                    run_id VARCHAR PRIMARY KEY,
                    dataset VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    adjustment VARCHAR NOT NULL,
                    started_at VARCHAR NOT NULL,
                    completed_at VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    start_date VARCHAR NOT NULL,
                    end_date VARCHAR NOT NULL,
                    raw_path VARCHAR NOT NULL,
                    normalized_paths_json VARCHAR NOT NULL,
                    content_hash VARCHAR NOT NULL,
                    quality_status VARCHAR NOT NULL,
                    parameters_json VARCHAR NOT NULL,
                    error VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quality_issues(
                    run_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    severity VARCHAR NOT NULL,
                    message VARCHAR NOT NULL,
                    affected_rows BIGINT NOT NULL,
                    created_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_coverage(
                    symbol VARCHAR NOT NULL,
                    dataset VARCHAR NOT NULL,
                    adjustment VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    start_date VARCHAR NOT NULL,
                    end_date VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    quality_status VARCHAR NOT NULL,
                    paths_json VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL,
                    PRIMARY KEY(symbol,dataset,adjustment)
                );
                CREATE TABLE IF NOT EXISTS sync_queue(
                    queue_key VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    dataset VARCHAR NOT NULL,
                    priority INTEGER NOT NULL,
                    requested_start VARCHAR NOT NULL,
                    requested_end VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_catalog(
                    board_key VARCHAR PRIMARY KEY,
                    board_code VARCHAR NOT NULL,
                    board_name VARCHAR NOT NULL,
                    board_type VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    first_seen_at VARCHAR NOT NULL,
                    last_seen_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_daily(
                    board_key VARCHAR NOT NULL,
                    trade_date DATE NOT NULL,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE NOT NULL,
                    volume DOUBLE,
                    amount DOUBLE,
                    turnover DOUBLE,
                    up_count BIGINT,
                    down_count BIGINT,
                    leader_name VARCHAR NOT NULL,
                    leader_change DOUBLE,
                    provider VARCHAR NOT NULL,
                    source_kind VARCHAR NOT NULL,
                    historical_backfill BOOLEAN NOT NULL,
                    fetched_at VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    PRIMARY KEY(board_key,trade_date)
                );
                CREATE TABLE IF NOT EXISTS board_rps(
                    board_key VARCHAR NOT NULL,
                    trade_date DATE NOT NULL,
                    return_50 DOUBLE,
                    return_120 DOUBLE,
                    return_250 DOUBLE,
                    rps_50 DOUBLE,
                    rps_120 DOUBLE,
                    rps_250 DOUBLE,
                    breadth DOUBLE,
                    turnover_ratio_20 DOUBLE,
                    status VARCHAR NOT NULL,
                    formula_version VARCHAR NOT NULL,
                    universe_size_50 BIGINT NOT NULL,
                    universe_size_120 BIGINT NOT NULL,
                    universe_size_250 BIGINT NOT NULL,
                    coverage_ratio DOUBLE NOT NULL,
                    warnings_json VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    computed_at VARCHAR NOT NULL,
                    PRIMARY KEY(board_key,trade_date,formula_version)
                );
                CREATE TABLE IF NOT EXISTS board_rank(
                    board_key VARCHAR NOT NULL,
                    trade_date DATE NOT NULL,
                    rank_50 BIGINT,
                    rank_120 BIGINT,
                    rank_250 BIGINT,
                    universe_size_50 BIGINT NOT NULL,
                    universe_size_120 BIGINT NOT NULL,
                    universe_size_250 BIGINT NOT NULL,
                    formula_version VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    computed_at VARCHAR NOT NULL,
                    PRIMARY KEY(board_key,trade_date,formula_version)
                );
                CREATE TABLE IF NOT EXISTS board_memberships(
                    board_key VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    security_name VARCHAR NOT NULL,
                    snapshot_date DATE NOT NULL,
                    provider VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL,
                    PRIMARY KEY(board_key,symbol,snapshot_date)
                );
                CREATE TABLE IF NOT EXISTS board_fetch_queue(
                    board_key VARCHAR PRIMARY KEY,
                    priority INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_runs(
                    run_id VARCHAR PRIMARY KEY,
                    operation VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    board_type VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    processed BIGINT NOT NULL,
                    succeeded BIGINT NOT NULL,
                    failed BIGINT NOT NULL,
                    started_at VARCHAR NOT NULL,
                    completed_at VARCHAR NOT NULL,
                    error VARCHAR NOT NULL
                );
                """
                    )
                    context_table_exists = db.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='event_technical_context'"
                    ).fetchone()[0]
                    context_has_snapshot_id = False
                    if context_table_exists:
                        context_has_snapshot_id = bool(db.execute(
                            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='event_technical_context' AND column_name='snapshot_id'"
                        ).fetchone()[0])
                    if context_table_exists and not context_has_snapshot_id:
                        db.execute("ALTER TABLE event_technical_context RENAME TO event_technical_context_v3")
                    db.execute(
                        """
                CREATE TABLE IF NOT EXISTS event_technical_context(
                    snapshot_id VARCHAR PRIMARY KEY,
                    event_id VARCHAR NOT NULL,
                    feature_version VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    posted_at VARCHAR NOT NULL,
                    expected_trade_date VARCHAR NOT NULL,
                    as_of_trade_date VARCHAR NOT NULL,
                    adjustment VARCHAR NOT NULL,
                    rsi14 DOUBLE,
                    macd_dif DOUBLE,
                    macd_dea DOUBLE,
                    macd_hist DOUBLE,
                    macd_hist_pct DOUBLE,
                    atr14 DOUBLE,
                    atr14_pct DOUBLE,
                    volume_ratio_5 DOUBLE,
                    return_20d DOUBLE,
                    distance_60d_high DOUBLE,
                    history_bars BIGINT NOT NULL,
                    status VARCHAR NOT NULL,
                    warnings_json VARCHAR NOT NULL,
                    source_hash VARCHAR NOT NULL,
                    error VARCHAR NOT NULL,
                    computed_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_intraday_context(
                    snapshot_id VARCHAR PRIMARY KEY,
                    event_id VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    direction VARCHAR NOT NULL DEFAULT 'long',
                    posted_at VARCHAR NOT NULL,
                    effective_start VARCHAR NOT NULL,
                    window_end VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    frequency VARCHAR NOT NULL,
                    adjustment VARCHAR NOT NULL,
                    first_bar_at VARCHAR NOT NULL,
                    first_price DOUBLE,
                    close_5m DOUBLE,
                    close_15m DOUBLE,
                    close_30m DOUBLE,
                    close_60m DOUBLE,
                    close_window DOUBLE,
                    mfe DOUBLE,
                    mae DOUBLE,
                    bars BIGINT NOT NULL,
                    status VARCHAR NOT NULL,
                    warnings_json VARCHAR NOT NULL,
                    source_hash VARCHAR NOT NULL,
                    computed_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_dossier_snapshots(
                    snapshot_id VARCHAR PRIMARY KEY,
                    event_id VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    section_status_json VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    warnings_json VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_cross_section_snapshots(
                    snapshot_id VARCHAR PRIMARY KEY,
                    trade_date DATE NOT NULL,
                    provider VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    source_hash VARCHAR NOT NULL,
                    raw_path VARCHAR NOT NULL,
                    parquet_path VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_method_research(
                    snapshot_id VARCHAR PRIMARY KEY,
                    event_id VARCHAR NOT NULL,
                    method_version VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    posted_at VARCHAR NOT NULL,
                    as_of_trade_date VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    warnings_json VARCHAR NOT NULL,
                    computed_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_method_interpretations(
                    interpretation_id VARCHAR PRIMARY KEY,
                    event_id VARCHAR NOT NULL,
                    research_snapshot_id VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    model VARCHAR NOT NULL,
                    prompt_version VARCHAR NOT NULL,
                    input_hash VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    validation_json VARCHAR NOT NULL,
                    error VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                );
                """
                    )
                    intraday_columns = {
                        str(row[0])
                        for row in db.execute(
                            "SELECT column_name FROM information_schema.columns WHERE table_name='event_intraday_context'"
                        ).fetchall()
                    }
                    if "direction" not in intraday_columns:
                        db.execute("ALTER TABLE event_intraday_context ADD COLUMN direction VARCHAR DEFAULT 'long'")
                    if context_table_exists and not context_has_snapshot_id:
                        db.execute(
                            """
                            INSERT INTO event_technical_context(
                                snapshot_id,event_id,feature_version,input_hash,symbol,posted_at,
                                expected_trade_date,as_of_trade_date,adjustment,rsi14,macd_dif,macd_dea,
                                macd_hist,macd_hist_pct,atr14,atr14_pct,volume_ratio_5,return_20d,
                                distance_60d_high,history_bars,status,warnings_json,source_hash,error,computed_at
                            )
                            SELECT
                                event_id || '|' || feature_version || '|' || input_hash || '|' || source_hash || '|' || status,
                                event_id,feature_version,input_hash,symbol,posted_at,'',as_of_trade_date,
                                adjustment,rsi14,macd_dif,macd_dea,macd_hist,macd_hist_pct,atr14,
                                atr14_pct,volume_ratio_5,return_20d,distance_60d_high,history_bars,
                                status,warnings_json,source_hash,'',computed_at
                            FROM event_technical_context_v3
                            """
                        )
                        db.execute("DROP TABLE event_technical_context_v3")
                    count = db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0]
                    if count:
                        db.execute("UPDATE schema_meta SET version=8")
                    else:
                        db.execute("INSERT INTO schema_meta VALUES (8)")
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise

    @staticmethod
    def _decode_event_context(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["warnings"] = json.loads(value.pop("warnings_json"))
        except (TypeError, json.JSONDecodeError):
            value["warnings"] = []
            value.pop("warnings_json", None)
        return value

    def save_event_technical_context(self, record: dict[str, Any], *, force: bool = False) -> bool:
        columns = [
            "snapshot_id", "event_id", "feature_version", "input_hash", "symbol", "posted_at",
            "expected_trade_date", "as_of_trade_date", "adjustment", "rsi14", "macd_dif", "macd_dea",
            "macd_hist", "macd_hist_pct", "atr14", "atr14_pct", "volume_ratio_5",
            "return_20d", "distance_60d_high", "history_bars", "status",
            "warnings_json", "source_hash", "error", "computed_at",
        ]
        missing = [column for column in columns if column not in record]
        if missing:
            raise ValueError("technical context missing fields: " + ", ".join(missing))
        with self.lock(timeout=30), self.connect(lock=False) as db:
            exists = db.execute(
                "SELECT 1 FROM event_technical_context WHERE snapshot_id=?",
                [record["snapshot_id"]],
            ).fetchone()
            if exists:
                return False
            db.execute("BEGIN TRANSACTION")
            try:
                placeholders = ",".join("?" for _ in columns)
                db.execute(
                    f"INSERT INTO event_technical_context ({','.join(columns)}) VALUES ({placeholders})",
                    [record[column] for column in columns],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return True

    def get_event_technical_context(
        self,
        event_id: str,
        *,
        input_hash: str | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM event_technical_context WHERE event_id=?"
        params: list[Any] = [event_id]
        if input_hash:
            query += " AND input_hash=?"
            params.append(input_hash)
        query += " ORDER BY computed_at DESC,snapshot_id DESC LIMIT 1"
        with self.connect() as db:
            cursor = db.execute(query, params)
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [item[0] for item in cursor.description]
        return self._decode_event_context(dict(zip(columns, row)))

    def list_event_technical_contexts(self, event_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM event_technical_context"
        params: list[Any] = []
        if event_id:
            query += " WHERE event_id=?"
            params.append(event_id)
        query += " ORDER BY computed_at DESC,snapshot_id DESC,event_id"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        return [self._decode_event_context(dict(zip(columns, row))) for row in rows]

    @staticmethod
    def _decode_event_intraday_context(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["warnings"] = json.loads(value.pop("warnings_json"))
        except (TypeError, json.JSONDecodeError):
            value["warnings"] = []
            value.pop("warnings_json", None)
        return value

    def save_event_intraday_context(self, record: dict[str, Any], *, force: bool = False) -> bool:
        columns = [
            "snapshot_id", "event_id", "symbol", "direction", "posted_at", "effective_start", "window_end",
            "provider", "frequency", "adjustment", "first_bar_at", "first_price", "close_5m",
            "close_15m", "close_30m", "close_60m", "close_window", "mfe", "mae", "bars",
            "status", "warnings_json", "source_hash", "computed_at",
        ]
        missing = [column for column in columns if column not in record]
        if missing:
            raise ValueError("intraday context missing fields: " + ", ".join(missing))
        with self.lock(timeout=30), self.connect(lock=False) as db:
            exists = db.execute(
                "SELECT 1 FROM event_intraday_context WHERE snapshot_id=?", [record["snapshot_id"]]
            ).fetchone()
            if exists and not force:
                return False
            db.execute("BEGIN TRANSACTION")
            try:
                placeholders = ",".join("?" for _ in columns)
                if force and exists:
                    db.execute("DELETE FROM event_intraday_context WHERE snapshot_id=?", [record["snapshot_id"]])
                db.execute(
                    f"INSERT INTO event_intraday_context ({','.join(columns)}) VALUES ({placeholders})",
                    [record[column] for column in columns],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return True

    def get_event_intraday_context(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            cursor = db.execute(
                "SELECT * FROM event_intraday_context WHERE event_id=? ORDER BY computed_at DESC,snapshot_id DESC LIMIT 1",
                [event_id],
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [item[0] for item in cursor.description]
        return self._decode_event_intraday_context(dict(zip(columns, row)))

    def list_event_intraday_contexts(self, event_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM event_intraday_context"
        params: list[Any] = []
        if event_id:
            query += " WHERE event_id=?"
            params.append(event_id)
        query += " ORDER BY computed_at DESC,snapshot_id DESC,event_id"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        return [self._decode_event_intraday_context(dict(zip(columns, row))) for row in rows]

    def save_event_dossier_snapshot(self, record: dict[str, Any]) -> str:
        required = {
            "snapshot_id", "event_id", "input_hash", "status",
            "section_status", "payload", "warnings", "created_at",
        }
        missing = sorted(required - set(record))
        if missing:
            raise ValueError("event dossier snapshot missing fields: " + ", ".join(missing))
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                """
                INSERT INTO event_dossier_snapshots(
                    snapshot_id,event_id,input_hash,status,section_status_json,
                    payload_json,warnings_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                [
                    record["snapshot_id"], record["event_id"], record["input_hash"],
                    record["status"], json.dumps(record["section_status"], ensure_ascii=False),
                    json.dumps(record["payload"], ensure_ascii=False),
                    json.dumps(record["warnings"], ensure_ascii=False), record["created_at"],
                ],
            )
        return str(record["snapshot_id"])

    def list_event_dossier_snapshots(self, event_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM event_dossier_snapshots"
        params: list[Any] = []
        if event_id:
            query += " WHERE event_id=?"
            params.append(event_id)
        query += " ORDER BY created_at DESC,snapshot_id DESC"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        result: list[dict[str, Any]] = []
        for row in rows:
            for source, target, default in (
                ("section_status_json", "section_status", {}),
                ("payload_json", "payload", {}),
                ("warnings_json", "warnings", []),
            ):
                try:
                    row[target] = json.loads(row.pop(source))
                except (TypeError, json.JSONDecodeError):
                    row[target] = default
                    row.pop(source, None)
            result.append(row)
        return result

    @staticmethod
    def _json_hash(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save_cross_section_snapshot(
        self,
        *,
        provider: str,
        as_of: date,
        frame: pd.DataFrame,
    ) -> dict[str, Any]:
        vendor_frame = frame.copy()
        vendor_payload = vendor_frame.to_json(
            orient="records",
            force_ascii=False,
            date_format="iso",
        )
        source_hash = hashlib.sha256(vendor_payload.encode("utf-8")).hexdigest()
        value = normalise_cross_section(frame)
        value = value[value["trade_date"] == as_of].reset_index(drop=True)
        if value.empty:
            raise ValueError(f"cross-section has no rows for {as_of.isoformat()}")
        snapshot_id = f"{provider}:{as_of.isoformat()}:{source_hash}"
        with self.connect() as db:
            previous = db.execute(
                """
                SELECT MAX(row_count)
                FROM market_cross_section_snapshots
                WHERE trade_date=? AND status='ready'
                """,
                [as_of],
            ).fetchone()
        previous_rows = int(previous[0] or 0) if previous else 0
        status = (
            "quarantined"
            if previous_rows and len(value) < previous_rows * 0.9
            else "ready"
        )
        raw_dir = self.raw_root / provider / "cross_section" / as_of.isoformat()
        raw_path = raw_dir / f"{source_hash}.json.gz"
        parquet_dir = (
            self.warehouse_root
            / "cross_section"
            / f"{as_of.year:04d}"
            / as_of.isoformat()
        )
        parquet_path = parquet_dir / f"{source_hash}.parquet"
        raw_dir.mkdir(parents=True, exist_ok=True)
        parquet_dir.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            temporary_raw = raw_path.with_suffix(".tmp")
            with gzip.open(temporary_raw, "wt", encoding="utf-8") as stream:
                stream.write(vendor_payload)
            os.replace(temporary_raw, raw_path)
        temporary_parquet = parquet_path.with_suffix(".tmp.parquet")
        value.to_parquet(temporary_parquet, index=False)
        os.replace(temporary_parquet, parquet_path)
        created_at = datetime.now().astimezone().isoformat(
            timespec="microseconds"
        )
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                """
                INSERT INTO market_cross_section_snapshots(
                    snapshot_id,trade_date,provider,row_count,source_hash,
                    raw_path,parquet_path,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                [
                    snapshot_id,
                    as_of,
                    provider,
                    len(value),
                    source_hash,
                    str(raw_path),
                    str(parquet_path),
                    status,
                    created_at,
                ],
            )
        return {
            "snapshot_id": snapshot_id,
            "trade_date": as_of.isoformat(),
            "provider": provider,
            "row_count": len(value),
            "raw_row_count": len(vendor_frame),
            "source_hash": source_hash,
            "raw_path": str(raw_path),
            "parquet_path": str(parquet_path),
            "status": status,
            "created_at": created_at,
        }

    def read_cross_sections(self, dates: Iterable[date]) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        with self.connect() as db:
            selected_paths = []
            for current in sorted(set(dates)):
                row = db.execute(
                    """
                    SELECT parquet_path
                    FROM market_cross_section_snapshots
                    WHERE trade_date=? AND status='ready'
                    ORDER BY created_at DESC,snapshot_id DESC
                    LIMIT 1
                    """,
                    [current],
                ).fetchone()
                selected_paths.append(
                    (
                        current,
                        Path(str(row[0])) if row else None,
                    )
                )
        for current, selected in selected_paths:
            path = selected or (
                self.warehouse_root
                / "cross_section"
                / f"{current.year:04d}"
                / f"{current.isoformat()}.parquet"
            )
            if path.exists():
                frames.append(pd.read_parquet(path))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def list_cross_section_snapshots(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            cursor = db.execute(
                """
                SELECT * FROM market_cross_section_snapshots
                ORDER BY trade_date DESC,created_at DESC
                """
            )
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def save_event_method_research(self, record: dict[str, Any]) -> bool:
        required = {
            "snapshot_id",
            "event_id",
            "method_version",
            "input_hash",
            "symbol",
            "posted_at",
            "as_of_trade_date",
            "status",
            "payload",
            "warnings",
            "computed_at",
        }
        missing = sorted(required - set(record))
        if missing:
            raise ValueError("event method research missing fields: " + ", ".join(missing))
        with self.lock(timeout=30), self.connect(lock=False) as db:
            exists = db.execute(
                "SELECT 1 FROM event_method_research WHERE snapshot_id=?",
                [record["snapshot_id"]],
            ).fetchone()
            if exists:
                return False
            db.execute(
                """
                INSERT INTO event_method_research(
                    snapshot_id,event_id,method_version,input_hash,symbol,posted_at,
                    as_of_trade_date,status,payload_json,warnings_json,computed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    record["snapshot_id"],
                    record["event_id"],
                    record["method_version"],
                    record["input_hash"],
                    record["symbol"],
                    record["posted_at"],
                    record["as_of_trade_date"],
                    record["status"],
                    json.dumps(record["payload"], ensure_ascii=False),
                    json.dumps(record["warnings"], ensure_ascii=False),
                    record["computed_at"],
                ],
            )
        return True

    @staticmethod
    def _decode_method_research(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for source, target, default in (
            ("payload_json", "payload", {}),
            ("warnings_json", "warnings", []),
        ):
            try:
                value[target] = json.loads(value.pop(source))
            except (TypeError, json.JSONDecodeError):
                value[target] = default
                value.pop(source, None)
        return value

    def list_event_method_research(
        self,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM event_method_research"
        params: list[Any] = []
        if event_id:
            query += " WHERE event_id=?"
            params.append(event_id)
        query += " ORDER BY computed_at DESC,snapshot_id DESC"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return [self._decode_method_research(row) for row in rows]

    def get_event_method_research(
        self,
        event_id: str,
    ) -> dict[str, Any] | None:
        values = self.list_event_method_research(event_id)
        return values[0] if values else None

    def save_event_method_interpretation(self, record: dict[str, Any]) -> bool:
        required = {
            "interpretation_id",
            "event_id",
            "research_snapshot_id",
            "provider",
            "model",
            "prompt_version",
            "input_hash",
            "status",
            "payload",
            "validation",
            "error",
            "created_at",
        }
        missing = sorted(required - set(record))
        if missing:
            raise ValueError(
                "event method interpretation missing fields: " + ", ".join(missing)
            )
        with self.lock(timeout=30), self.connect(lock=False) as db:
            exists = db.execute(
                "SELECT 1 FROM event_method_interpretations WHERE interpretation_id=?",
                [record["interpretation_id"]],
            ).fetchone()
            if exists:
                return False
            db.execute(
                """
                INSERT INTO event_method_interpretations(
                    interpretation_id,event_id,research_snapshot_id,provider,model,
                    prompt_version,input_hash,status,payload_json,validation_json,
                    error,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    record["interpretation_id"],
                    record["event_id"],
                    record["research_snapshot_id"],
                    record["provider"],
                    record["model"],
                    record["prompt_version"],
                    record["input_hash"],
                    record["status"],
                    json.dumps(record["payload"], ensure_ascii=False),
                    json.dumps(record["validation"], ensure_ascii=False),
                    record["error"],
                    record["created_at"],
                ],
            )
        return True

    def list_event_method_interpretations(
        self,
        event_id: str | None = None,
        *,
        research_snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_id:
            clauses.append("event_id=?")
            params.append(event_id)
        if research_snapshot_id:
            clauses.append("research_snapshot_id=?")
            params.append(research_snapshot_id)
        query = "SELECT * FROM event_method_interpretations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC,interpretation_id DESC"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        for row in rows:
            for source, target, default in (
                ("payload_json", "payload", {}),
                ("validation_json", "validation", {}),
            ):
                try:
                    row[target] = json.loads(row.pop(source))
                except (TypeError, json.JSONDecodeError):
                    row[target] = default
                    row.pop(source, None)
        return rows

    def upsert_instrument(self, instrument: Instrument) -> dict[str, Any]:
        if not instrument.symbol.isdigit() or len(instrument.symbol) != 6:
            raise ValueError(f"invalid instrument symbol: {instrument.symbol}")
        if instrument.instrument_type not in {"stock", "etf", "index"}:
            raise ValueError(f"invalid instrument type: {instrument.instrument_type}")
        if instrument.lifecycle not in {"pinned", "tracking", "archived"}:
            raise ValueError(f"invalid lifecycle: {instrument.lifecycle}")
        timestamp = now_iso()
        first_seen = instrument.first_seen_at or timestamp
        with self.lock(timeout=30), self.connect(lock=False) as db:
            existing = db.execute(
                "SELECT lifecycle,first_seen_at,source FROM instruments WHERE symbol=?", [instrument.symbol]
            ).fetchone()
            lifecycle = instrument.lifecycle
            source = instrument.source
            if existing and instrument.lifecycle == "archived" and existing[0] in {"pinned", "tracking"}:
                lifecycle = existing[0]
                source = existing[2]
            db.execute(
                """
                INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name,instrument_type=excluded.instrument_type,exchange=excluded.exchange,
                    status=excluded.status,list_date=excluded.list_date,lifecycle=excluded.lifecycle,
                    source=excluded.source,last_mentioned_at=CASE WHEN excluded.last_mentioned_at<>''
                        THEN excluded.last_mentioned_at ELSE instruments.last_mentioned_at END,
                    updated_at=excluded.updated_at
                """,
                [
                    instrument.symbol,
                    instrument.name,
                    instrument.instrument_type,
                    instrument.exchange.upper(),
                    instrument.status,
                    instrument.list_date,
                    lifecycle,
                    source,
                    existing[1] if existing else first_seen,
                    instrument.last_mentioned_at,
                    timestamp,
                    timestamp,
                ],
            )
        value = self.get_instrument(instrument.symbol)
        assert value is not None
        return value

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> int:
        supplied = list(instruments)
        grouped: dict[str, list[Instrument]] = {}
        for instrument in supplied:
            grouped.setdefault(instrument.symbol, []).append(instrument)
        values = [
            sorted(
                choices,
                key=lambda item: ({"stock": 0, "etf": 1, "index": 2}.get(item.instrument_type, 9), item.exchange),
            )[0]
            for choices in grouped.values()
        ]
        timestamp = now_iso()
        with self.lock(timeout=30), self.connect(lock=False) as db:
            for instrument in values:
                if not instrument.symbol.isdigit() or len(instrument.symbol) != 6:
                    continue
                if instrument.instrument_type not in {"stock", "etf", "index"}:
                    continue
                existing = db.execute(
                    """
                    SELECT lifecycle,first_seen_at,source,instrument_type,exchange,name,status,list_date
                    FROM instruments WHERE symbol=?
                    """,
                    [instrument.symbol],
                ).fetchone()
                preserve = bool(
                    existing
                    and instrument.lifecycle == "archived"
                    and existing[0] in {"pinned", "tracking"}
                )
                lifecycle = existing[0] if preserve else instrument.lifecycle
                source = existing[2] if preserve else instrument.source
                instrument_type = existing[3] if preserve else instrument.instrument_type
                exchange = existing[4] if preserve else instrument.exchange.upper()
                name = existing[5] if preserve else instrument.name
                status = existing[6] if preserve else instrument.status
                list_date = existing[7] if preserve else instrument.list_date
                db.execute(
                    """
                    INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name,instrument_type=excluded.instrument_type,exchange=excluded.exchange,
                        status=excluded.status,list_date=excluded.list_date,lifecycle=excluded.lifecycle,
                        source=excluded.source,last_mentioned_at=CASE WHEN excluded.last_mentioned_at<>''
                            THEN excluded.last_mentioned_at ELSE instruments.last_mentioned_at END,
                        updated_at=excluded.updated_at
                    """,
                    [
                        instrument.symbol, name, instrument_type,
                        exchange, status, list_date,
                        lifecycle, source, existing[1] if existing else instrument.first_seen_at or timestamp,
                        instrument.last_mentioned_at, timestamp, timestamp,
                    ],
                )
        return len(supplied)

    def upsert_instrument_catalog(
        self,
        instruments: Iterable[Instrument],
        *,
        provider: str,
        snapshot_date: date,
    ) -> int:
        values = list(instruments)
        timestamp = now_iso()
        with self.lock(timeout=30), self.connect(lock=False) as db:
            for instrument in values:
                key = f"{instrument.exchange.upper()}:{instrument.symbol}:{instrument.instrument_type}"
                db.execute(
                    """
                    INSERT INTO instrument_catalog VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(instrument_key) DO UPDATE SET
                        name=excluded.name,status=excluded.status,list_date=excluded.list_date,
                        provider=excluded.provider,snapshot_date=excluded.snapshot_date,
                        updated_at=excluded.updated_at
                    """,
                    [
                        key,
                        instrument.symbol,
                        instrument.name,
                        instrument.instrument_type,
                        instrument.exchange.upper(),
                        instrument.status,
                        instrument.list_date,
                        provider,
                        snapshot_date.isoformat(),
                        timestamp,
                    ],
                )
        return len(values)

    def instrument_catalog_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM instrument_catalog").fetchone()[0])

    def get_instrument(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as db:
            cursor = db.execute("SELECT * FROM instruments WHERE symbol=?", [symbol])
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))

    def list_instruments(self, lifecycle: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM instruments"
        params: list[Any] = []
        if lifecycle:
            query += " WHERE lifecycle=?"
            params.append(lifecycle)
        query += " ORDER BY CASE lifecycle WHEN 'pinned' THEN 0 WHEN 'tracking' THEN 1 ELSE 2 END,symbol"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def instrument_map(self) -> dict[str, dict[str, Any]]:
        return {item["symbol"]: item for item in self.list_instruments()}

    def touch_mention(self, symbol: str, mentioned_at: date) -> None:
        with self.lock(timeout=30), self.connect(lock=False) as db:
            if db.execute("SELECT 1 FROM instruments WHERE symbol=?", [symbol]).fetchone() is None:
                raise KeyError(f"instrument not found: {symbol}")
            db.execute(
                "UPDATE instruments SET lifecycle=CASE WHEN lifecycle='pinned' THEN lifecycle ELSE 'tracking' END,"
                "last_mentioned_at=?,updated_at=? WHERE symbol=?",
                [mentioned_at.isoformat(), now_iso(), symbol],
            )

    def restore_research_state(self, symbol: str, *, lifecycle: str, last_mentioned_at: str) -> None:
        if lifecycle not in {"pinned", "tracking", "archived"}:
            raise ValueError(f"invalid lifecycle: {lifecycle}")
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                "UPDATE instruments SET lifecycle=?,last_mentioned_at=?,updated_at=? WHERE symbol=?",
                [lifecycle, last_mentioned_at, now_iso(), symbol],
            )

    def replace_calendar(self, open_dates: Iterable[date], *, provider: str) -> None:
        timestamp = now_iso()
        rows = [(value.isoformat(), True, provider, timestamp) for value in sorted(set(open_dates))]
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute("DELETE FROM trading_calendar WHERE provider=?", [provider])
            if rows:
                db.executemany("INSERT OR REPLACE INTO trading_calendar VALUES (?,?,?,?)", rows)

    def latest_open_date(self, as_of: date) -> date | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(trade_date) FROM trading_calendar WHERE is_open=true AND trade_date<=?",
                [as_of.isoformat()],
            ).fetchone()
        return row[0] if row and row[0] else None

    def open_dates_between(self, start: date, end: date) -> list[date]:
        if end < start:
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT trade_date FROM trading_calendar WHERE is_open=true AND trade_date>=? AND trade_date<=? ORDER BY trade_date",
                [start.isoformat(), end.isoformat()],
            ).fetchall()
        return [row[0] for row in rows if row and row[0]]

    def next_open_date(self, value: date, *, include_value: bool = False) -> date | None:
        operator = ">=" if include_value else ">"
        with self.connect() as db:
            row = db.execute(
                f"SELECT MIN(trade_date) FROM trading_calendar WHERE is_open=true AND trade_date{operator}?",
                [value.isoformat()],
            ).fetchone()
        return row[0] if row and row[0] else None

    def refresh_lifecycles(self, *, as_of: date, tracking_sessions: int = 120) -> list[str]:
        archived: list[str] = []
        with self.lock(timeout=30), self.connect(lock=False) as db:
            rows = db.execute(
                "SELECT symbol,last_mentioned_at FROM instruments WHERE lifecycle='tracking' AND last_mentioned_at<>''"
            ).fetchall()
            for symbol, mentioned in rows:
                count = db.execute(
                    "SELECT COUNT(*) FROM trading_calendar WHERE is_open=true AND trade_date>? AND trade_date<=?",
                    [mentioned, as_of.isoformat()],
                ).fetchone()[0]
                if int(count) >= tracking_sessions:
                    db.execute(
                        "UPDATE instruments SET lifecycle='archived',updated_at=? WHERE symbol=?",
                        [now_iso(), symbol],
                    )
                    archived.append(str(symbol))
        return archived

    def enqueue_sync(
        self,
        symbol: str,
        *,
        dataset: str = "daily",
        priority: int = 50,
        start: date | None = None,
        end: date | None = None,
        reason: str = "manual",
    ) -> None:
        if self.get_instrument(symbol) is None:
            raise KeyError(f"instrument not found: {symbol}")
        timestamp = now_iso()
        key = f"{symbol}:{dataset}"
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                """
                INSERT INTO sync_queue VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(queue_key) DO UPDATE SET
                    priority=LEAST(sync_queue.priority,excluded.priority),
                    requested_start=CASE WHEN sync_queue.requested_start='' THEN excluded.requested_start
                        WHEN excluded.requested_start='' THEN sync_queue.requested_start
                        ELSE LEAST(sync_queue.requested_start,excluded.requested_start) END,
                    requested_end=GREATEST(sync_queue.requested_end,excluded.requested_end),
                    reason=excluded.reason,status='pending',last_error='',updated_at=excluded.updated_at
                """,
                [
                    key,
                    symbol,
                    dataset,
                    priority,
                    start.isoformat() if start else "",
                    end.isoformat() if end else "",
                    reason,
                    "pending",
                    0,
                    "",
                    timestamp,
                    timestamp,
                ],
            )

    def pending_sync(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            cursor = db.execute("SELECT * FROM sync_queue WHERE status IN ('pending','failed') ORDER BY priority,created_at")
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def mark_sync(self, queue_key: str, status: str, error: str = "") -> None:
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                "UPDATE sync_queue SET status=?,attempts=attempts+1,last_error=?,updated_at=? WHERE queue_key=?",
                [status, error[:2000], now_iso(), queue_key],
            )

    def _record_result(
        self,
        result: SyncResult,
        *,
        started_at: str,
        start: date,
        end: date,
        content_hash: str,
        issues: list[QualityIssue],
    ) -> None:
        completed = now_iso()
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                """
                INSERT INTO data_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    result.run_id,
                    "daily",
                    result.provider,
                    result.symbol,
                    result.adjustment,
                    started_at,
                    completed,
                    "failed" if result.error else "completed",
                    result.rows,
                    start.isoformat(),
                    end.isoformat(),
                    result.raw_path,
                    json.dumps(result.normalized_paths, ensure_ascii=False),
                    content_hash,
                    result.quality_status,
                    json.dumps({"start": start.isoformat(), "end": end.isoformat()}, ensure_ascii=False),
                    result.error,
                ],
            )
            for issue in issues:
                db.execute(
                    "INSERT INTO quality_issues VALUES (?,?,?,?,?,?,?)",
                    [result.run_id, result.symbol, issue.code, issue.severity, issue.message, issue.rows, completed],
                )
            if result.normalized_paths:
                frame = self.read_daily(result.symbol, adjustment=result.adjustment)
                db.execute(
                    """
                    INSERT INTO data_coverage VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,dataset,adjustment) DO UPDATE SET
                        provider=excluded.provider,start_date=excluded.start_date,end_date=excluded.end_date,
                        row_count=excluded.row_count,quality_status=excluded.quality_status,
                        paths_json=excluded.paths_json,updated_at=excluded.updated_at
                    """,
                    [
                        result.symbol,
                        "daily",
                        result.adjustment,
                        result.provider,
                        frame["trade_date"].min().isoformat(),
                        frame["trade_date"].max().isoformat(),
                        len(frame),
                        result.quality_status,
                        json.dumps(result.normalized_paths, ensure_ascii=False),
                        completed,
                    ],
                )
        manifest = {
            **asdict(result),
            "dataset": "daily",
            "started_at": started_at,
            "completed_at": completed,
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "content_hash": content_hash,
            "issues": [asdict(issue) for issue in issues],
        }
        with FileLock(str(self.manifest_path) + ".lock", timeout=30):
            with self.manifest_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, ensure_ascii=False) + "\n")

    def get_coverage(self, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM data_coverage"
        params: list[Any] = []
        if symbol:
            query += " WHERE symbol=?"
            params.append(symbol)
        query += " ORDER BY symbol,adjustment"
        with self.connect() as db:
            cursor = db.execute(query, params)
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        values = [dict(zip(columns, row)) for row in rows]
        for value in values:
            value["paths"] = json.loads(value.pop("paths_json"))
        return values

    def reconcile_daily_coverage(
        self,
        symbol: str,
        adjustment: str,
        *,
        provider: str,
    ) -> dict[str, Any] | None:
        """Rebuild one coverage row after a historical-only merge.

        A historical importer may add rows before the current canonical start.
        The normal single-run recorder cannot infer the merged date range, so
        this method recomputes it from the Parquet files and preserves the
        original canonical provider label.
        """

        with self.lock(timeout=30):
            frame = self.read_daily(symbol, adjustment=adjustment)
            if frame.empty:
                return None
            paths = sorted(
                str(path)
                for path in (self.warehouse_root / "daily" / symbol / adjustment).glob("*.parquet")
                if path.exists()
            )
            with self.connect(lock=False) as db:
                current = db.execute(
                    "SELECT quality_status FROM data_coverage WHERE symbol=? AND dataset='daily' AND adjustment=?",
                    [symbol, adjustment],
                ).fetchone()
                if current is None:
                    return None
                db.execute(
                    """
                    UPDATE data_coverage
                    SET provider=?,start_date=?,end_date=?,row_count=?,quality_status=?,paths_json=?,updated_at=?
                    WHERE symbol=? AND dataset='daily' AND adjustment=?
                    """,
                    [
                        provider,
                        frame["trade_date"].min().isoformat(),
                        frame["trade_date"].max().isoformat(),
                        len(frame),
                        str(current[0] or "warning"),
                        json.dumps(paths, ensure_ascii=False),
                        now_iso(),
                        symbol,
                        adjustment,
                    ],
                )
        return next(
            (
                item
                for item in self.get_coverage(symbol)
                if item.get("dataset") == "daily" and item.get("adjustment") == adjustment
            ),
            None,
        )

    def preserve_coverage_provider(self, symbol: str, adjustment: str, provider: str) -> None:
        """Keep the canonical coverage label on the first successful source.

        Fallback rows may extend a canonical file, but the coverage label must
        not imply that the fallback supplied the entire historical series.
        Per-run provenance remains available in ``data_runs`` and manifests.
        """
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                "UPDATE data_coverage SET provider=?,updated_at=? WHERE symbol=? AND dataset='daily' AND adjustment=?",
                [provider, now_iso(), symbol, adjustment],
            )

    def recent_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as db:
            cursor = db.execute("SELECT * FROM data_runs ORDER BY started_at DESC LIMIT ?", [limit])
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        values = [dict(zip(columns, row)) for row in rows]
        for value in values:
            value["normalized_paths"] = json.loads(value.pop("normalized_paths_json"))
            value["parameters"] = json.loads(value.pop("parameters_json"))
        return values

    def quality_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            cursor = db.execute("SELECT * FROM quality_issues ORDER BY created_at DESC LIMIT ?", [limit])
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def read_daily(self, symbol: str, *, adjustment: str = "raw") -> pd.DataFrame:
        paths = sorted((self.warehouse_root / "daily" / symbol / adjustment).glob("*.parquet"))
        if not paths:
            return pd.DataFrame()
        frames = [pd.read_parquet(path) for path in paths]
        frame = pd.concat(frames, ignore_index=True)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        return frame.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)

    def write_daily(self, frame: pd.DataFrame, *, symbol: str, adjustment: str) -> list[str]:
        output_paths: list[str] = []
        frame = frame.copy()
        years = pd.to_datetime(frame["trade_date"]).dt.year
        with self.lock(timeout=30):
            for year in sorted(years.unique()):
                incoming = frame.loc[years == year].copy()
                destination = self.warehouse_root / "daily" / symbol / adjustment / f"{year}.parquet"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    existing = pd.read_parquet(destination)
                    incoming = pd.concat([existing, incoming], ignore_index=True)
                incoming["trade_date"] = pd.to_datetime(incoming["trade_date"]).dt.date
                incoming = incoming.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
                temporary = destination.with_suffix(".parquet.tmp")
                incoming.to_parquet(temporary, index=False, engine="pyarrow")
                os.replace(temporary, destination)
                output_paths.append(str(destination))
        return output_paths

    def health(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        coverage = self.get_coverage()
        instruments = self.list_instruments()
        issues = self.quality_issues(limit=20)
        runs = self.recent_runs(limit=1)
        current = as_of or datetime.now(MARKET_TIMEZONE)
        if current.tzinfo is None:
            current = current.replace(tzinfo=MARKET_TIMEZONE)
        else:
            current = current.astimezone(MARKET_TIMEZONE)
        today = current.date()
        with self.connect() as db:
            today_open_row = db.execute(
                "SELECT is_open FROM trading_calendar WHERE trade_date=?",
                [today.isoformat()],
            ).fetchone()
            today_is_open = bool(today_open_row and today_open_row[0])
            expected_cutoff = (
                today - timedelta(days=1)
                if today_is_open and current.time() < MARKET_CLOSE_TIME
                else today
            )
            latest_open_row = db.execute(
                "SELECT MAX(trade_date) FROM trading_calendar WHERE is_open=true AND trade_date<=?",
                [expected_cutoff.isoformat()],
            ).fetchone()
        latest_open_date = str(latest_open_row[0] or "") if latest_open_row else ""
        if today_is_open and current.time() < MARKET_OPEN_TIME:
            market_session_status = "pre_open"
        elif today_is_open and current.time() < MARKET_CLOSE_TIME:
            market_session_status = "trading"
        elif today_is_open:
            market_session_status = "post_close"
        else:
            market_session_status = "closed"
        raw_daily_end = {
            str(item["symbol"]): str(item["end_date"])
            for item in coverage
            if item["dataset"] == "daily" and item["adjustment"] == "raw"
        }
        active_symbols = [
            str(item["symbol"])
            for item in instruments
            if item["lifecycle"] in {"pinned", "tracking"}
        ]
        lagging_symbols = sorted(
            symbol for symbol in active_symbols
            if latest_open_date and raw_daily_end.get(symbol, "") < latest_open_date
        )
        latest_daily_date = max(raw_daily_end.values(), default="")
        return {
            "ok": True,
            "database": str(self.db_path),
            "instrument_count": len(instruments),
            "instrument_catalog_count": self.instrument_catalog_count(),
            "active_instruments": sum(item["lifecycle"] in {"pinned", "tracking"} for item in instruments),
            "coverage_count": len(coverage),
            "warning_count": sum(item["quality_status"] != "valid" for item in coverage) + len(issues),
            "recent_issue_count": len(issues),
            "market_session_status": market_session_status,
            "latest_open_date": latest_open_date,
            "latest_daily_date": latest_daily_date,
            "daily_data_status": "provider_pending" if lagging_symbols else "current",
            "lagging_symbols": lagging_symbols,
            "lagging_symbol_count": len(lagging_symbols),
            "last_run": runs[0] if runs else None,
        }

    def save_auxiliary_snapshot(
        self,
        *,
        provider: str,
        dataset: str,
        symbol: str,
        as_of: date,
        frame: pd.DataFrame,
        adjustment: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> SyncResult:
        run_id = uuid.uuid4().hex
        started_at = now_iso()
        if frame.empty:
            result = SyncResult(run_id, symbol, provider, adjustment, 0, "quarantined", error="empty dataframe")
            self._record_auxiliary(result, dataset, started_at, as_of, "", parameters or {})
            return result
        raw_directory = self.raw_root / provider / dataset / run_id
        raw_directory.mkdir(parents=True, exist_ok=True)
        raw_path = raw_directory / f"{symbol}.csv.gz"
        frame.to_csv(raw_path, index=False, encoding="utf-8", compression="gzip")
        content_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        suffix = f"_{adjustment}" if adjustment else ""
        destination = self.warehouse_root / dataset / symbol / f"{as_of.isoformat()}{suffix}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".parquet.tmp")
        normalized = frame.copy()
        if "symbol" not in normalized.columns:
            normalized["symbol"] = symbol
        normalized["provider"] = provider
        normalized["snapshot_date"] = as_of.isoformat()
        normalized.to_parquet(temporary, index=False, engine="pyarrow")
        os.replace(temporary, destination)
        result = SyncResult(
            run_id,
            symbol,
            provider,
            adjustment,
            len(frame),
            "valid",
            str(raw_path),
            [str(destination)],
        )
        self._record_auxiliary(result, dataset, started_at, as_of, content_hash, parameters or {})
        return result

    def save_minute_snapshot(
        self,
        *,
        provider: str,
        symbol: str,
        as_of: date,
        frequency: str,
        adjustment: str,
        frame: pd.DataFrame,
        parameters: dict[str, Any] | None = None,
    ) -> SyncResult:
        return self.save_auxiliary_snapshot(
            provider=provider,
            dataset=f"minute_{frequency}",
            symbol=symbol,
            as_of=as_of,
            frame=frame,
            adjustment=adjustment,
            parameters={
                **(parameters or {}),
                "frequency": frequency,
                "adjustment": adjustment,
            },
        )

    def read_minute(
        self,
        symbol: str,
        *,
        frequency: str = "1m",
        adjustment: str = "raw",
        dates: Iterable[date] | None = None,
    ) -> pd.DataFrame:
        directory = self.warehouse_root / f"minute_{frequency}" / symbol
        if not directory.exists():
            return pd.DataFrame()
        wanted = {current.isoformat() for current in dates or []}
        frames: list[pd.DataFrame] = []
        for path in sorted(directory.glob("*.parquet")):
            name = path.stem
            if adjustment and not name.endswith(f"_{adjustment}"):
                continue
            snapshot_date = name.split("_", 1)[0]
            if wanted and snapshot_date not in wanted:
                continue
            try:
                frames.append(pd.read_parquet(path))
            except (OSError, ValueError, ImportError):
                continue
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def save_instrument_snapshot(
        self,
        provider: str,
        instruments: Iterable[Instrument],
        as_of: date,
    ) -> SyncResult:
        values = list(instruments)
        frame = pd.DataFrame([asdict(item) for item in values])
        invalid_reason = "empty instrument master"
        if not frame.empty:
            invalid = (
                ~frame["symbol"].astype(str).str.fullmatch(r"\d{6}")
                | ~frame["instrument_type"].isin(["stock", "etf", "index"])
                | ~frame["exchange"].isin(["SH", "SZ", "BJ"])
            )
            duplicate_contract = frame.duplicated(["exchange", "symbol", "instrument_type"])
            invalid_reason = (
                "instrument master contains invalid or duplicate contracts"
                if bool(invalid.any()) or bool(duplicate_contract.any())
                else ""
            )
        if invalid_reason:
            run_id = uuid.uuid4().hex
            started_at = now_iso()
            raw_directory = self.raw_root / provider / "instrument_master" / run_id
            raw_directory.mkdir(parents=True, exist_ok=True)
            raw_path = raw_directory / "ALL.csv.gz"
            frame.to_csv(raw_path, index=False, encoding="utf-8", compression="gzip")
            content_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            result = SyncResult(
                run_id,
                "ALL",
                provider,
                "",
                len(frame),
                "quarantined",
                str(raw_path),
                [],
                invalid_reason,
            )
            (self.audit_root / f"{run_id}.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "dataset": "instrument_master",
                        "status": "quarantined",
                        "issues": [
                            asdict(QualityIssue("invalid_master", "error", invalid_reason, len(frame)))
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._record_auxiliary(
                result,
                "instrument_master",
                started_at,
                as_of,
                content_hash,
                {"provider": provider},
            )
            return result
        return self.save_auxiliary_snapshot(
            provider=provider,
            dataset="instrument_master",
            symbol="ALL",
            as_of=as_of,
            frame=frame,
            parameters={"provider": provider},
        )

    def _record_auxiliary(
        self,
        result: SyncResult,
        dataset: str,
        started_at: str,
        as_of: date,
        content_hash: str,
        parameters: dict[str, Any],
    ) -> None:
        completed = now_iso()
        with self.lock(timeout=30), self.connect(lock=False) as db:
            db.execute(
                "INSERT INTO data_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    result.run_id, dataset, result.provider, result.symbol, result.adjustment, started_at,
                    completed, "failed" if result.error else "completed", result.rows,
                    as_of.isoformat(), as_of.isoformat(), result.raw_path,
                    json.dumps(result.normalized_paths, ensure_ascii=False), content_hash,
                    result.quality_status, json.dumps(parameters, ensure_ascii=False), result.error,
                ],
            )
            if result.normalized_paths:
                db.execute(
                    """
                    INSERT INTO data_coverage VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,dataset,adjustment) DO UPDATE SET
                        provider=excluded.provider,start_date=excluded.start_date,end_date=excluded.end_date,
                        row_count=excluded.row_count,quality_status=excluded.quality_status,
                        paths_json=excluded.paths_json,updated_at=excluded.updated_at
                    """,
                    [
                        result.symbol, dataset, result.adjustment, result.provider, as_of.isoformat(), as_of.isoformat(),
                        result.rows, result.quality_status,
                        json.dumps(result.normalized_paths, ensure_ascii=False), completed,
                    ],
                )
        manifest = {
            **asdict(result),
            "dataset": dataset,
            "started_at": started_at,
            "completed_at": completed,
            "snapshot_date": as_of.isoformat(),
            "content_hash": content_hash,
            "parameters": parameters,
        }
        with FileLock(str(self.manifest_path) + ".lock", timeout=30):
            with self.manifest_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, ensure_ascii=False) + "\n")

    def record_quality_issues(self, run_id: str, symbol: str, issues: list[QualityIssue]) -> None:
        with self.lock(timeout=30), self.connect(lock=False) as db:
            for issue in issues:
                db.execute(
                    "INSERT INTO quality_issues VALUES (?,?,?,?,?,?,?)",
                    [run_id, symbol, issue.code, issue.severity, issue.message, issue.rows, now_iso()],
                )


def normalise_daily_bars(
    frame: pd.DataFrame,
    *,
    symbol: str,
    instrument_type: str,
    provider: str,
    adjustment: str,
) -> pd.DataFrame:
    aliases = {
        "date": "trade_date",
        "日期": "trade_date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "昨收": "preclose",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
        "turn": "turnover",
        "tradestatus": "trade_status",
    }
    value = frame.rename(columns={column: aliases.get(str(column), str(column).lower()) for column in frame.columns}).copy()
    required = {"trade_date", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(value.columns))
    if missing:
        raise ValueError(f"missing daily columns: {','.join(missing)}")
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="coerce").dt.date
    for column in (
        "open", "high", "low", "close", "preclose", "volume", "amount", "turnover",
        "adj_factor", "first_adj", "last_adj",
    ):
        if column not in value:
            value[column] = pd.NA
        value[column] = pd.to_numeric(value[column], errors="coerce")
    if "trade_status" not in value:
        value["trade_status"] = ""
    value.insert(0, "symbol", symbol)
    value.insert(1, "instrument_type", instrument_type)
    value["adjustment"] = adjustment
    value["provider"] = provider
    columns = [
        "symbol", "instrument_type", "trade_date", "open", "high", "low", "close",
        "preclose", "volume", "amount", "turnover", "trade_status", "adjustment", "provider",
        "adj_factor", "first_adj", "last_adj",
    ]
    return value[columns].sort_values("trade_date").reset_index(drop=True)


def audit_daily_bars(frame: pd.DataFrame) -> DataAudit:
    issues: list[QualityIssue] = []
    if frame.empty:
        return DataAudit("quarantined", [QualityIssue("empty_result", "error", "provider returned no rows")])
    invalid_dates = int(frame["trade_date"].isna().sum())
    if invalid_dates:
        issues.append(QualityIssue("invalid_date", "error", "rows contain invalid trading dates", invalid_dates))
    duplicates = int(frame.duplicated(["symbol", "trade_date", "adjustment"]).sum())
    if duplicates:
        issues.append(QualityIssue("duplicate_date", "error", "duplicate symbol/date rows", duplicates))
    prices = frame[["open", "high", "low", "close"]]
    invalid_prices = int((prices.isna() | (prices <= 0)).any(axis=1).sum())
    if invalid_prices:
        issues.append(QualityIssue("invalid_price", "error", "OHLC must be positive numbers", invalid_prices))
    inconsistent = int(
        (
            (frame["high"] < frame[["open", "low", "close"]].max(axis=1))
            | (frame["low"] > frame[["open", "high", "close"]].min(axis=1))
        ).sum()
    )
    if inconsistent:
        issues.append(QualityIssue("ohlc_inconsistent", "error", "high/low does not contain open and close", inconsistent))
    negative_volume = int(((frame["volume"] < 0) | (frame["amount"].fillna(0) < 0)).sum())
    if negative_volume:
        issues.append(QualityIssue("negative_liquidity", "error", "volume or amount is negative", negative_volume))
    if len(frame) > 1:
        returns = frame["close"].pct_change().abs()
        suspicious = int((returns > 0.35).sum())
        if suspicious:
            issues.append(QualityIssue("suspicious_jump", "warning", "daily close changed by more than 35%", suspicious))
    status = "quarantined" if any(issue.severity == "error" for issue in issues) else "warning" if issues else "valid"
    return DataAudit(status, issues)


def sync_daily_bars(
    store: MarketStore,
    provider: DailyBarProvider,
    symbol: str,
    start: date,
    end: date,
    *,
    adjustment: str = "raw",
    promote: bool = True,
    preserve_existing_before: date | None = None,
) -> SyncResult:
    instrument = store.get_instrument(symbol)
    if instrument is None:
        raise KeyError(f"instrument not found: {symbol}")
    if adjustment not in ADJUST_FLAGS:
        raise ValueError(f"unsupported adjustment: {adjustment}")
    run_id = uuid.uuid4().hex
    started_at = now_iso()
    raw_path = ""
    content_hash = ""
    issues: list[QualityIssue] = []
    try:
        raw = provider.fetch_daily(symbol, instrument["instrument_type"], start, end, adjustment)
        raw_directory = store.raw_root / provider.name / "daily" / run_id
        raw_directory.mkdir(parents=True, exist_ok=True)
        raw_file = raw_directory / f"{symbol}_{adjustment}.csv.gz"
        raw.to_csv(raw_file, index=False, encoding="utf-8", compression="gzip")
        raw_path = str(raw_file)
        content_hash = hashlib.sha256(raw_file.read_bytes()).hexdigest()
        normalised = normalise_daily_bars(
            raw,
            symbol=symbol,
            instrument_type=instrument["instrument_type"],
            provider=provider.name,
            adjustment=adjustment,
        )
        before_range_filter = len(normalised)
        normalised = normalised[
            normalised["trade_date"].between(start, end, inclusive="both")
        ].copy()
        if preserve_existing_before is not None:
            normalised = normalised[normalised["trade_date"] > preserve_existing_before].copy()
        normalised["run_id"] = run_id
        normalised["fetched_at"] = now_iso()
        audit = audit_daily_bars(normalised)
        range_issues = (
            [
                QualityIssue(
                    "provider_rows_outside_requested_range",
                    "warning",
                    "provider rows outside the requested date range were excluded",
                    before_range_filter - len(normalised),
                )
            ]
            if before_range_filter != len(normalised)
            else []
        )
        issues = [*range_issues, *audit.issues]
        if not promote and audit.status != "quarantined":
            issues = [
                *issues,
                QualityIssue(
                    "fallback_not_promoted",
                    "warning",
                    "fallback snapshot retained for audit; existing passing canonical data was not replaced",
                ),
            ]
        quality_status = audit.status
        if quality_status == "valid" and (range_issues or not promote):
            quality_status = "warning"
        paths = (
            store.write_daily(normalised, symbol=symbol, adjustment=adjustment)
            if promote and audit.status != "quarantined"
            else []
        )
        result = SyncResult(
            run_id,
            symbol,
            provider.name,
            adjustment,
            len(normalised),
            quality_status,
            raw_path,
            paths,
        )
    except Exception as exc:
        issues = [QualityIssue("provider_failure", "error", str(exc)[:2000])]
        result = SyncResult(
            run_id,
            symbol,
            provider.name,
            adjustment,
            0,
            "quarantined",
            raw_path,
            [],
            str(exc)[:2000],
        )
    audit_path = store.audit_root / f"{run_id}.json"
    audit_path.write_text(
        json.dumps(
            {"run_id": run_id, "symbol": symbol, "status": result.quality_status, "issues": [asdict(item) for item in issues]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    store._record_result(
        result,
        started_at=started_at,
        start=start,
        end=end,
        content_hash=content_hash,
        issues=issues,
    )
    return result


class BaoStockMarketProvider:
    name = "baostock"

    @staticmethod
    def provider_code(symbol: str, instrument_type: str) -> str:
        if instrument_type == "index":
            exchange = "sz" if symbol.startswith("399") else "sh"
        elif symbol.startswith(("4", "8", "92")):
            exchange = "bj"
        else:
            exchange = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
        return f"{exchange}.{symbol}"

    def fetch_daily(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        adjustment: str,
    ) -> pd.DataFrame:
        import baostock as bs  # type: ignore

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        try:
            code = self.provider_code(symbol, instrument_type)
            fields = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg"
            query = bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag=ADJUST_FLAGS[adjustment],
            )
            rows: list[list[str]] = []
            while query.error_code == "0" and query.next():
                rows.append(query.get_row_data())
            if query.error_code != "0":
                raise RuntimeError(f"BaoStock query failed for {code}: {query.error_msg}")
            return pd.DataFrame(rows, columns=query.fields)
        finally:
            bs.logout()

    def fetch_calendar(self, start: date, end: date) -> list[date]:
        import baostock as bs  # type: ignore

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        try:
            query = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
            values: list[date] = []
            while query.error_code == "0" and query.next():
                row = query.get_row_data()
                if len(row) >= 2 and row[1] == "1":
                    values.append(date.fromisoformat(row[0]))
            if query.error_code != "0":
                raise RuntimeError(f"BaoStock calendar query failed: {query.error_msg}")
            return values
        finally:
            bs.logout()

    def fetch_instruments(self) -> list[Instrument]:
        import baostock as bs  # type: ignore

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        try:
            query = bs.query_stock_basic()
            rows: list[dict[str, str]] = []
            while query.error_code == "0" and query.next():
                rows.append(dict(zip(query.fields, query.get_row_data())))
            if query.error_code != "0":
                raise RuntimeError(f"BaoStock instrument query failed: {query.error_msg}")
        finally:
            bs.logout()
        instruments: list[Instrument] = []
        for row in rows:
            code = str(row.get("code") or "")
            if "." not in code:
                continue
            exchange, symbol = code.split(".", 1)
            name = str(row.get("code_name") or "")
            type_code = str(row.get("type") or "")
            instrument_type = _classify_baostock_instrument(exchange, symbol, name, type_code)
            if not instrument_type:
                continue
            instruments.append(
                Instrument(
                    symbol=symbol,
                    name=name or symbol,
                    instrument_type=instrument_type,
                    exchange=exchange.upper(),
                    status="active" if str(row.get("status") or "1") == "1" else "inactive",
                    list_date=str(row.get("ipoDate") or ""),
                    lifecycle="archived",
                    source="baostock_master",
                )
            )
        return instruments


class AKShareMarketProvider:
    name = "akshare"

    def fetch_daily(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        adjustment: str,
    ) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        start_value = start.strftime("%Y%m%d")
        end_value = end.strftime("%Y%m%d")
        adjust = "" if adjustment == "raw" else adjustment
        if instrument_type == "etf":
            frame = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=start_value,
                end_date=end_value,
                adjust=adjust,
            )
        elif instrument_type == "index":
            frame = ak.index_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_value,
                end_date=end_value,
            )
        else:
            frame = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_value,
                end_date=end_value,
                adjust=adjust,
            )
        if frame.empty:
            raise RuntimeError(f"AKShare returned no data for {symbol}")
        return frame

    def fetch_etf_instruments(self) -> list[Instrument]:
        import akshare as ak  # type: ignore

        frame = ak.fund_etf_spot_em()
        if frame.empty:
            return []
        code_column = "代码" if "代码" in frame.columns else frame.columns[0]
        name_column = "名称" if "名称" in frame.columns else frame.columns[1]
        values: list[Instrument] = []
        for _, row in frame.iterrows():
            symbol = str(row[code_column]).zfill(6)
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            exchange = "SH" if symbol.startswith("5") else "SZ"
            values.append(
                Instrument(
                    symbol,
                    str(row[name_column]),
                    "etf",
                    exchange,
                    lifecycle="archived",
                    source="akshare_etf_master",
                )
            )
        return values

    def fetch_auxiliary(self, symbol: str, exchange: str, dataset: str, as_of: date) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        if dataset == "financial_summary":
            return ak.stock_financial_analysis_indicator(symbol=symbol, start_year=str(as_of.year - 3))
        if dataset == "announcements":
            frame = ak.stock_notice_report(symbol="全部", date=as_of.strftime("%Y%m%d"))
            code_column = "代码" if "代码" in frame.columns else "股票代码"
            return frame[frame[code_column].astype(str).str.zfill(6) == symbol] if code_column in frame else frame.iloc[0:0]
        if dataset == "fund_flow":
            return ak.stock_individual_fund_flow(stock=symbol, market=exchange.lower())
        if dataset == "valuation":
            return ak.stock_individual_info_em(symbol=symbol, timeout=15)
        raise ValueError(f"unsupported auxiliary dataset: {dataset}")


_FREESTOCKDB_DAILY_TABLE = "\u65e5k"
_FREESTOCKDB_MINUTE_TABLE = "\u5206\u949fk"
_FREESTOCKDB_ADJUSTMENT_TABLE = "\u590d\u6743"


def _free_stockdb_records(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("data", "rows", "items", "result"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return candidate
        if payload and all(isinstance(value, (dict, list, tuple)) for value in payload.values()):
            return list(payload.values())
        return []
    return payload if isinstance(payload, list) else []


def _free_stockdb_catalog_stats(payload: Any) -> tuple[int, int]:
    """Return catalog group count and distinct six-digit symbol count.

    The service's catalog endpoint returns several top-level groups.  Counting
    those groups as symbols was the source of the old misleading health value.
    """
    groups = len(payload) if isinstance(payload, dict) else 0
    symbols: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                text = str(key)
                for match in re.findall(r"(?<!\d)(\d{6})(?!\d)", text):
                    symbols.add(match)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            symbols.update(re.findall(r"(?<!\d)(\d{6})(?!\d)", value))

    visit(payload)
    return groups, len(symbols)


def _free_stockdb_date_key(value: Any, *, minute: bool = False) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    if minute:
        if len(digits) == 8:
            return digits + "000000"
        if len(digits) == 12:
            return digits + "00"
        return digits[-14:]
    return digits[-8:]


def _free_stockdb_rows_frame(payload: Any) -> pd.DataFrame:
    rows = _free_stockdb_records(payload)
    if not rows:
        return pd.DataFrame()
    if not all(isinstance(row, dict) for row in rows):
        return pd.DataFrame()
    aliases = {
        "trade_date": "date",
        "pre_close": "preclose",
        "pct_chg": "pctChg",
        "vol": "volume",
        "turn": "turnover",
    }
    return pd.DataFrame(rows).rename(
        columns={str(column): aliases.get(str(column), str(column)) for column in pd.DataFrame(rows).columns}
    )


def _free_stockdb_factor_rows(payload: Any) -> list[tuple[str, float]]:
    factors: list[tuple[str, float]] = []
    if isinstance(payload, dict) and not any(key in payload for key in ("data", "rows", "items", "result")):
        items: list[Any] = [[key, value] for key, value in payload.items()]
    else:
        items = _free_stockdb_records(payload)
    for item in items:
        key: Any = ""
        value: Any = item
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            key, value = item[0], item[1]
        elif isinstance(item, dict):
            key = item.get("key") or item.get("date") or item.get("trade_date") or ""
        if not isinstance(value, dict):
            continue
        date_key = _free_stockdb_date_key(
            value.get("date") or value.get("trade_date") or key,
            minute=False,
        )
        factor_value = value.get("cum") or value.get("factor") or value.get("adj_factor")
        try:
            factor = float(factor_value)
        except (TypeError, ValueError):
            continue
        if date_key and factor > 0:
            factors.append((date_key, factor))
    return sorted(factors)


def _apply_free_stockdb_adjustment(
    frame: pd.DataFrame,
    factors: list[tuple[str, float]],
    adjustment: str,
) -> pd.DataFrame:
    if adjustment == "raw":
        return frame
    if adjustment not in {"qfq", "hfq"}:
        raise ValueError(f"unsupported FreeStockDB adjustment: {adjustment}")
    if not factors:
        raise RuntimeError("FreeStockDB returned no adjustment factors")
    latest_factor = factors[-1][1]
    value = frame.copy()
    for column in ("open", "high", "low", "close", "preclose"):
        if column in value.columns:
            value[column] = pd.to_numeric(value[column], errors="coerce").astype(float)
    for index, row in value.iterrows():
        row_date = _free_stockdb_date_key(row.get("date") or row.get("trade_datetime"))
        current_factor = next((factor for factor_date, factor in reversed(factors) if factor_date <= row_date), None)
        if current_factor is None:
            continue
        ratio = latest_factor / current_factor if adjustment == "qfq" else 1.0 / current_factor
        for column in ("open", "high", "low", "close", "preclose"):
            if column in value.columns and pd.notna(row.get(column)):
                value.at[index, column] = float(row[column]) / ratio
    return value


def _aggregate_free_stockdb_minutes(frame: pd.DataFrame, frequency: str) -> pd.DataFrame:
    if frequency == "1m":
        return frame
    minutes = int(frequency.removesuffix("m"))
    value = frame.sort_values("trade_datetime").set_index("trade_datetime")
    aggregated = value.resample(
        f"{minutes}min", origin="start_day", label="right", closed="left"
    ).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "preclose": "first",
            "volume": "sum",
            "amount": "sum",
            "turnover": "sum",
        }
    )
    aggregated = aggregated.dropna(subset=["close"]).reset_index()
    for column in ("symbol", "instrument_type", "adjustment", "provider"):
        aggregated[column] = frame[column].iloc[0]
    if len(aggregated) > 1:
        aggregated["preclose"] = aggregated["close"].shift(1)
    return aggregated[
        [
            "symbol", "instrument_type", "trade_datetime", "open", "high", "low", "close",
            "preclose", "volume", "amount", "turnover", "adjustment", "provider",
        ]
    ]


class FreeStockDBMarketProvider:
    """Read-only adapter for an optional local free-stockdb HTTP service.

    The adapter never starts the service or downloads its dataset.  It only
    reads the loopback API and returns the standard market-provider frames.
    """

    name = "freestockdb"

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = 8.0,
        *,
        client: Any | None = None,
    ):
        self.base_url = (base_url or os.environ.get("FREESTOCKDB_URL", "http://127.0.0.1:7899")).rstrip("/")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self._client = client
        self._owns_client = client is None

    def _http_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx is required for FreeStockDB") from exc
        self._client = httpx.Client(
            timeout=self.timeout_seconds,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=30),
            headers={"Connection": "keep-alive"},
        )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "FreeStockDBMarketProvider":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request_json(self, params: dict[str, str]) -> Any:
        try:
            response = self._http_client().get(
                f"{self.base_url}/",
                params=params,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"FreeStockDB request failed: {exc}") from exc

    def health(self) -> dict[str, Any]:
        try:
            payload = self._request_json({"cmd": "get", "t": "\u80a1\u7968\u4ee3\u7801"})
            catalog_groups, catalog_symbols = _free_stockdb_catalog_stats(payload)
            transport_warning = "untrusted_transport" if self.base_url.lower().startswith("http://") else ""
            return {
                "ok": True,
                "provider": self.name,
                "base_url": self.base_url,
                "catalog_groups": catalog_groups,
                "catalog_symbols": catalog_symbols,
                "symbol_catalog_rows": catalog_symbols,
                "capabilities": ["daily_raw", "daily_qfq", "daily_hfq", "minute_1m", "minute_5m", "minute_15m", "minute_30m", "minute_60m"],
                "transport_warning": transport_warning,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.name, "base_url": self.base_url, "error": str(exc)}

    def _fetch_table(self, table: str, symbol: str, start_key: str, end_key: str) -> pd.DataFrame:
        payload = self._request_json(
            {
                "cmd": "vals",
                "t": table,
                "k1": f"key:{symbol}",
                "k2": f"fwd:{start_key},{end_key}",
            }
        )
        frame = _free_stockdb_rows_frame(payload)
        if frame.empty:
            raise RuntimeError(f"FreeStockDB returned no rows for {symbol} {table}")
        return frame

    def _fetch_factors(self, symbol: str) -> list[tuple[str, float]]:
        payload = self._request_json(
            {
                "cmd": "get",
                "t": _FREESTOCKDB_ADJUSTMENT_TABLE,
                "k1": f"key:{symbol}",
                "k2": "all:",
            }
        )
        return _free_stockdb_factor_rows(payload)

    def fetch_daily(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        adjustment: str,
    ) -> pd.DataFrame:
        frame = self._fetch_table(
            _FREESTOCKDB_DAILY_TABLE,
            symbol,
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
        )
        if "date" not in frame.columns:
            raise RuntimeError("FreeStockDB daily response is missing date")
        frame["date"] = frame["date"].map(
            lambda value: datetime.strptime(_free_stockdb_date_key(value), "%Y%m%d").date().isoformat()
        )
        if adjustment != "raw":
            frame = _apply_free_stockdb_adjustment(frame, self._fetch_factors(symbol), adjustment)
        return frame.sort_values("date").reset_index(drop=True)

    def fetch_daily_cross_section(self, as_of: date) -> pd.DataFrame:
        payload = self._request_json(
            {
                "cmd": "vals",
                "t": _FREESTOCKDB_DAILY_TABLE,
                "k1": "all:",
                "k2": f"key:{as_of.strftime('%Y%m%d')}",
            }
        )
        frame = _free_stockdb_rows_frame(payload)
        if frame.empty:
            raise RuntimeError(
                f"FreeStockDB returned no cross-section rows for {as_of.isoformat()}"
            )
        aliases = {
            "date": "trade_date",
            "code": "symbol",
            "pre_close": "preclose",
            "pct_chg": "pct_change_pct",
            "pctChg": "pct_change_pct",
        }
        frame = frame.rename(
            columns={key: value for key, value in aliases.items() if key in frame}
        )
        if "trade_date" not in frame or "symbol" not in frame:
            raise RuntimeError("FreeStockDB cross-section response is missing date or code")
        frame["trade_date"] = frame["trade_date"].map(
            lambda value: datetime.strptime(
                _free_stockdb_date_key(value), "%Y%m%d"
            ).date().isoformat()
        )
        frame["symbol"] = frame["symbol"].astype(str).str.extract(
            r"(\d{6})", expand=False
        )
        frame["provider"] = self.name
        return frame.sort_values("symbol").reset_index(drop=True)

    def fetch_minute(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        frequency: str = "1m",
        adjustment: str = "raw",
    ) -> pd.DataFrame:
        allowed = {"1m", "5m", "15m", "30m", "60m"}
        if frequency not in allowed:
            raise ValueError(f"unsupported FreeStockDB minute frequency: {frequency}")
        frame = self._fetch_table(
            _FREESTOCKDB_MINUTE_TABLE,
            symbol,
            start.strftime("%Y%m%d") + "000000",
            end.strftime("%Y%m%d") + "235959",
        )
        if "date" not in frame.columns:
            raise RuntimeError("FreeStockDB minute response is missing date")
        frame["trade_datetime"] = pd.to_datetime(
            frame["date"].map(lambda value: _free_stockdb_date_key(value, minute=True)),
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )
        frame = frame.dropna(subset=["trade_datetime"]).copy()
        for column in ("open", "high", "low", "close", "preclose", "volume", "amount", "turnover"):
            if column not in frame:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.insert(0, "symbol", symbol)
        frame.insert(1, "instrument_type", instrument_type)
        frame["adjustment"] = adjustment
        frame["provider"] = self.name
        if adjustment != "raw":
            frame["date"] = frame["trade_datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
            frame = _apply_free_stockdb_adjustment(frame, self._fetch_factors(symbol), adjustment)
        value = frame[
            [
                "symbol", "instrument_type", "trade_datetime", "open", "high", "low", "close",
                "preclose", "volume", "amount", "turnover", "adjustment", "provider",
            ]
        ].sort_values("trade_datetime").reset_index(drop=True)
        return _aggregate_free_stockdb_minutes(value, frequency)


# Short alias for callers that prefer the provider name without the market-layer suffix.
FreeStockDBProvider = FreeStockDBMarketProvider


def default_sync_start(store: MarketStore, symbol: str, adjustment: str, *, fallback: date) -> date:
    coverage = [item for item in store.get_coverage(symbol) if item["adjustment"] == adjustment]
    if not coverage:
        return fallback
    return max(fallback, date.fromisoformat(coverage[0]["end_date"]) - timedelta(days=10))


def compare_daily_frames(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    *,
    threshold: float = 0.005,
) -> list[QualityIssue]:
    left = primary[["trade_date", "close"]].rename(columns={"close": "primary_close"})
    right = secondary[["trade_date", "close"]].rename(columns={"close": "secondary_close"})
    merged = left.merge(right, on="trade_date", how="inner")
    if merged.empty:
        return [QualityIssue("cross_source_empty", "warning", "providers have no overlapping dates")]
    denominator = merged["primary_close"].abs().replace(0, pd.NA)
    difference = ((merged["primary_close"] - merged["secondary_close"]).abs() / denominator).fillna(0)
    conflicts = int((difference > threshold).sum())
    if conflicts:
        return [
            QualityIssue(
                "data_conflict",
                "error",
                f"provider close prices differ by more than {threshold:.2%}",
                conflicts,
            )
        ]
    return []
