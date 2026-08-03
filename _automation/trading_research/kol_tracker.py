from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock


SHANGHAI = ZoneInfo("Asia/Shanghai")
CHECKPOINT_DAYS = {"1W": 5, "1M": 20, "3M": 60, "6M": 120}
EVENT_STATUSES = {"candidate", "active", "completed", "excluded", "archived"}
NON_EXECUTABLE_WARNINGS = {
    "one_price_limit_suspected",
    "conditional_intraday_entry_unverified",
}

EVENT_FIELDS = [
    "event_id",
    "kol_name",
    "platform",
    "source_url",
    "source_note",
    "posted_at",
    "symbol",
    "security_name",
    "direction",
    "thesis",
    "status",
    "exclusion_reason",
    "baseline_rule",
    "baseline_date",
    "baseline_price_raw",
    "benchmark_symbol",
    "benchmark_baseline_price",
    "execution_warning",
    "activated_at",
    "activation_notified_at",
    "updated_at",
    "kol_id",
    "kol_handle",
    "source_post_id",
]

MARK_FIELDS = [
    "event_id",
    "trade_date",
    "close_raw",
    "close_adjusted",
    "raw_return",
    "adjusted_return",
    "directional_return",
    "benchmark_close",
    "benchmark_return",
    "directional_excess_return",
    "max_adverse_return",
    "max_favorable_return",
    "tracking_days",
    "data_source",
    "data_status",
    "run_id",
    "updated_at",
]

CHECKPOINT_FIELDS = [
    "event_id",
    "horizon",
    "target_days",
    "trade_date",
    "close_raw",
    "raw_return",
    "adjusted_return",
    "directional_return",
    "benchmark_return",
    "directional_excess_return",
    "max_adverse_return",
    "max_favorable_return",
    "primary_source",
    "secondary_source",
    "secondary_close",
    "verification_status",
    "finalized_at",
]


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    kol_name: str
    platform: str
    source_url: str
    source_note: str
    posted_at: str
    symbol: str
    security_name: str
    direction: str
    thesis: str
    status: str
    exclusion_reason: str = ""
    baseline_rule: str = ""
    baseline_date: str = ""
    baseline_price_raw: str = ""
    benchmark_symbol: str = "000300"
    benchmark_baseline_price: str = ""
    execution_warning: str = ""
    activated_at: str = ""
    activation_notified_at: str = ""
    updated_at: str = ""
    kol_id: str = ""
    kol_handle: str = ""
    source_post_id: str = ""

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "EventRecord":
        return cls(**{field: str(row.get(field, "")) for field in EVENT_FIELDS})

    def to_row(self) -> dict[str, str]:
        return {field: str(asdict(self).get(field, "")) for field in EVENT_FIELDS}


@dataclass(frozen=True)
class CalculationResult:
    event: EventRecord
    marks: list[dict[str, str]]
    checkpoints: list[dict[str, str]]


@dataclass(frozen=True)
class UpdateResult:
    run_id: str
    updated_events: int
    mark_count: int
    new_checkpoints: list[dict[str, str]]
    notifications: list[dict[str, str]]
    errors: list[str]
    awaiting_market_data: list[str]


class PriceProvider(Protocol):
    name: str

    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame: ...

    def fetch_benchmark(self, start: date, end: date) -> pd.DataFrame: ...


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _merge_warnings(*values: str) -> str:
    warnings: list[str] = []
    for value in values:
        for warning in value.split(";"):
            clean = warning.strip()
            if clean and clean not in warnings:
                warnings.append(clean)
    return ";".join(warnings)


def is_executable_event(event: EventRecord) -> bool:
    return not (set(event.execution_warning.split(";")) & NON_EXECUTABLE_WARNINGS)


def validate_event(event: EventRecord) -> list[str]:
    if event.status not in EVENT_STATUSES:
        return ["status"]
    if event.status not in {"active", "completed"}:
        return []

    errors: list[str] = []
    required = {
        "event_id": event.event_id,
        "kol_name": event.kol_name,
        "platform": event.platform,
        "source_url": event.source_url,
        "source_note": event.source_note,
        "posted_at": event.posted_at,
        "symbol": event.symbol,
        "direction": event.direction,
        "thesis": event.thesis,
    }
    errors.extend(name for name, value in required.items() if not value.strip())

    if event.symbol and not re.fullmatch(r"\d{6}", event.symbol):
        errors.append("one_symbol_per_event")
    if event.direction not in {"long", "short"}:
        errors.append("direction")
    if event.source_url and not event.source_url.startswith(("https://", "http://")):
        errors.append("source_url")
    if event.posted_at:
        try:
            posted = datetime.fromisoformat(event.posted_at)
            if posted.tzinfo is None:
                errors.append("posted_at_timezone")
        except ValueError:
            errors.append("posted_at")
    return sorted(set(errors))


class KolStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.events_path = self.root / "events.csv"
        self.marks_path = self.root / "daily_marks.csv"
        self.checkpoints_path = self.root / "checkpoints.csv"
        self.runs_path = self.root / "runs.jsonl"
        self.event_revisions_path = self.root / "event_revisions.jsonl"
        self.backups_dir = self.root / "backups"
        self.logs_dir = self.root / "logs"
        self.returns_lock_path = self.root / "returns.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self._migrate_derived_return_fields()

    @staticmethod
    def _csv_fields(path: Path) -> list[str]:
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle).fieldnames or [])

    def _migrate_derived_return_fields(self) -> None:
        marks_need_migration = bool(self._csv_fields(self.marks_path)) and "max_favorable_return" not in self._csv_fields(self.marks_path)
        checkpoints_need_migration = bool(self._csv_fields(self.checkpoints_path)) and "max_favorable_return" not in self._csv_fields(self.checkpoints_path)
        if not marks_need_migration and not checkpoints_need_migration:
            return
        with FileLock(str(self.returns_lock_path), timeout=60):
            marks_need_migration = bool(self._csv_fields(self.marks_path)) and "max_favorable_return" not in self._csv_fields(self.marks_path)
            checkpoints_need_migration = bool(self._csv_fields(self.checkpoints_path)) and "max_favorable_return" not in self._csv_fields(self.checkpoints_path)
            if not marks_need_migration and not checkpoints_need_migration:
                return
            marks = self._read_csv(self.marks_path)
            running: dict[str, float] = {}
            mfe_by_date: dict[tuple[str, str], str] = {}
            invalid_rows: list[str] = []
            for row in sorted(marks, key=lambda item: (item.get("event_id", ""), item.get("trade_date", ""))):
                event_id = row.get("event_id", "")
                try:
                    directional = float(row.get("directional_return", ""))
                except (TypeError, ValueError):
                    mfe_by_date[(event_id, row.get("trade_date", ""))] = ""
                    invalid_rows.append(f"{event_id}:{row.get('trade_date', '')}")
                    continue
                running[event_id] = max(0.0, running.get(event_id, 0.0), directional)
                mfe_by_date[(event_id, row.get("trade_date", ""))] = _format_number(running[event_id])
            for row in marks:
                row["max_favorable_return"] = mfe_by_date.get(
                    (row.get("event_id", ""), row.get("trade_date", "")),
                    "",
                )
            checkpoints = self._read_csv(self.checkpoints_path)
            for row in checkpoints:
                row["max_favorable_return"] = mfe_by_date.get(
                    (row.get("event_id", ""), row.get("trade_date", "")),
                    "",
                )
            if marks_need_migration:
                self._backup_dataset(self.marks_path)
                self._atomic_write(self.marks_path, marks, MARK_FIELDS)
            if checkpoints_need_migration:
                self._backup_dataset(self.checkpoints_path)
                self._atomic_write(self.checkpoints_path, checkpoints, CHECKPOINT_FIELDS)
            if invalid_rows:
                self.log_run(
                    "mfe_migration_warning",
                    {"invalid_directional_returns": invalid_rows},
                )

    @staticmethod
    def _read_csv(path: Path) -> list[dict[str, str]]:
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    @staticmethod
    def _atomic_write(path: Path, rows: Iterable[dict[str, str]], fields: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                temp_name = handle.name
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({field: row.get(field, "") for field in fields})
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def _backup_events(self) -> None:
        if not self.events_path.exists():
            return
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(self.events_path, self.backups_dir / f"{stamp}_events.csv")

    def _backup_dataset(self, path: Path) -> None:
        if not path.exists():
            return
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, self.backups_dir / f"{stamp}_{path.name}")

    def load_events(self) -> list[EventRecord]:
        return [EventRecord.from_row(row) for row in self._read_csv(self.events_path)]

    def save_events(self, events: Iterable[EventRecord], backup: bool = True) -> None:
        if backup:
            self._backup_events()
        ordered = sorted(events, key=lambda event: event.event_id)
        self._atomic_write(self.events_path, (event.to_row() for event in ordered), EVENT_FIELDS)

    def register_event(self, event: EventRecord) -> bool:
        errors = validate_event(event)
        if errors:
            raise ValueError(f"event validation failed: {', '.join(errors)}")
        events = self.load_events()
        duplicate = any(
            existing.event_id == event.event_id
            or (
                event.source_url
                and existing.source_url == event.source_url
                and existing.symbol == event.symbol
            )
            for existing in events
        )
        if duplicate:
            return False
        self.save_events([*events, event])
        self.log_run("register_event", {"event_id": event.event_id, "status": event.status})
        return True

    def update_event(self, event: EventRecord, *, action: str) -> EventRecord:
        errors = validate_event(event)
        if errors:
            raise ValueError(f"event validation failed: {', '.join(errors)}")
        with FileLock(str(self.root / "events.lock"), timeout=60):
            events = self.load_events()
            current = next((item for item in events if item.event_id == event.event_id), None)
            if current is None:
                raise KeyError(f"event not found: {event.event_id}")
            if event.status in {"active", "completed"} and any(
                item.event_id != event.event_id
                and item.status in {"active", "completed"}
                and item.source_url == event.source_url
                and item.symbol == event.symbol
                for item in events
            ):
                raise ValueError("an active event already exists for this source and symbol")
            self.save_events(
                [event if item.event_id == event.event_id else item for item in events]
            )
            self.log_run(
                "update_event",
                {
                    "event_id": event.event_id,
                    "action": action,
                    "from_status": current.status,
                    "to_status": event.status,
                },
            )
        return event

    def amend_event(
        self,
        event_id: str,
        changes: dict[str, str],
        *,
        reason: str,
    ) -> tuple[EventRecord, dict]:
        allowed = {
            "kol_name", "platform", "source_url", "source_note", "posted_at",
            "symbol", "security_name", "direction", "thesis",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported event amendment fields: " + ", ".join(sorted(unknown)))
        if not reason.strip():
            raise ValueError("event amendment reason is required")
        with FileLock(str(self.root / "events.lock"), timeout=60):
            events = self.load_events()
            current = next((item for item in events if item.event_id == event_id), None)
            if current is None:
                raise KeyError(f"event not found: {event_id}")
            effective = {
                field: str(value).strip()
                for field, value in changes.items()
                if str(value).strip() != str(getattr(current, field))
            }
            if not effective:
                revisions = self.list_event_revisions(event_id)
                return current, revisions[0] if revisions else {
                    "revision_id": "",
                    "event_id": event_id,
                    "reason": reason.strip(),
                    "changed_fields": [],
                    "recalculation_required": False,
                }
            recalculation_required = bool(set(effective) & {"symbol", "direction", "posted_at"})
            candidate = replace(current, **effective, updated_at=now_iso())
            if recalculation_required:
                candidate = replace(
                    candidate,
                    status="active",
                    baseline_rule="",
                    baseline_date="",
                    baseline_price_raw="",
                    benchmark_baseline_price="",
                )
            errors = validate_event(candidate)
            if errors:
                raise ValueError(f"event validation failed: {', '.join(errors)}")
            if any(
                item.event_id != event_id
                and item.status in {"active", "completed"}
                and item.source_url == candidate.source_url
                and item.symbol == candidate.symbol
                for item in events
            ):
                raise ValueError("an active event already exists for this source and symbol")

            original_bytes = {
                path: path.read_bytes() if path.exists() else None
                for path in (
                    self.events_path,
                    self.marks_path,
                    self.checkpoints_path,
                    self.event_revisions_path,
                )
            }
            return_lock = FileLock(str(self.returns_lock_path), timeout=60) if recalculation_required else None
            if return_lock is not None:
                return_lock.acquire()
            try:
                self._backup_events()
                if recalculation_required:
                    self._backup_dataset(self.marks_path)
                    self._backup_dataset(self.checkpoints_path)
                    self._atomic_write(
                        self.marks_path,
                        (row for row in self.load_marks() if row.get("event_id") != event_id),
                        MARK_FIELDS,
                    )
                    self._atomic_write(
                        self.checkpoints_path,
                        (row for row in self.load_checkpoints() if row.get("event_id") != event_id),
                        CHECKPOINT_FIELDS,
                    )
                self.save_events(
                    [candidate if item.event_id == event_id else item for item in events],
                    backup=False,
                )
                revision = {
                    "revision_id": uuid.uuid4().hex,
                    "event_id": event_id,
                    "reason": reason.strip(),
                    "changed_fields": sorted(effective),
                    "before": {field: str(getattr(current, field)) for field in effective},
                    "after": {field: str(getattr(candidate, field)) for field in effective},
                    "recalculation_required": recalculation_required,
                    "created_at": now_iso(),
                }
                self.root.mkdir(parents=True, exist_ok=True)
                with self.event_revisions_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(revision, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self.log_run(
                    "amend_event",
                    {
                        "event_id": event_id,
                        "revision_id": revision["revision_id"],
                        "changed_fields": revision["changed_fields"],
                        "recalculation_required": recalculation_required,
                    },
                )
                return candidate, revision
            except Exception:
                for path, content in original_bytes.items():
                    if content is None:
                        if path.exists():
                            path.unlink()
                    else:
                        temp = path.with_suffix(path.suffix + ".restore.tmp")
                        temp.write_bytes(content)
                        os.replace(temp, path)
                raise
            finally:
                if return_lock is not None:
                    return_lock.release()

    def list_event_revisions(self, event_id: str) -> list[dict]:
        if not self.event_revisions_path.exists():
            return []
        values: list[dict] = []
        with self.event_revisions_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if value.get("event_id") == event_id:
                    values.append(value)
        return sorted(values, key=lambda value: str(value.get("created_at", "")), reverse=True)

    def load_marks(self) -> list[dict[str, str]]:
        return self._read_csv(self.marks_path)

    def upsert_marks(self, rows: Iterable[dict[str, str]]) -> None:
        with FileLock(str(self.returns_lock_path), timeout=60):
            merged = {(row.get("event_id", ""), row.get("trade_date", "")): row for row in self.load_marks()}
            for row in rows:
                merged[(row.get("event_id", ""), row.get("trade_date", ""))] = row
            ordered = [merged[key] for key in sorted(merged)]
            self._atomic_write(self.marks_path, ordered, MARK_FIELDS)

    def load_checkpoints(self) -> list[dict[str, str]]:
        return self._read_csv(self.checkpoints_path)

    def freeze_checkpoints(self, rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
        with FileLock(str(self.returns_lock_path), timeout=60):
            existing = {
                (row.get("event_id", ""), row.get("horizon", "")): row
                for row in self.load_checkpoints()
            }
            added: list[dict[str, str]] = []
            for row in rows:
                key = (row.get("event_id", ""), row.get("horizon", ""))
                if key in existing or row.get("verification_status", "") != "verified":
                    continue
                existing[key] = row
                added.append(row)
            self._atomic_write(
                self.checkpoints_path,
                [existing[key] for key in sorted(existing)],
                CHECKPOINT_FIELDS,
            )
        return added

    def log_run(self, event: str, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = {"ts": now_iso(), "event": event, **payload}
        with self.runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def notification_sent(self, key: str) -> bool:
        if not self.runs_path.exists():
            return False
        with self.runs_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == "notification_sent" and row.get("key") == key:
                    return True
        return False

    def pending_notifications(self) -> list[dict[str, str]]:
        if not self.runs_path.exists():
            return []
        queued: dict[str, dict[str, str]] = {}
        sent: set[str] = set()
        with self.runs_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(row.get("key", ""))
                if not key:
                    continue
                if row.get("event") == "notification_queued":
                    queued[key] = {
                        field: str(row.get(field, ""))
                        for field in ["kind", "key", "message", "event_id"]
                    }
                elif row.get("event") in {"notification_sent", "notification_superseded"}:
                    sent.add(key)
        return [queued[key] for key in queued if key not in sent]

    def queue_notification(self, payload: dict[str, str]) -> bool:
        key = payload["key"]
        if self.notification_sent(key) or any(row["key"] == key for row in self.pending_notifications()):
            return False
        self.log_run("notification_queued", payload)
        return True

    def record_notification(self, key: str, payload: dict[str, str]) -> None:
        self.log_run("notification_sent", {"key": key, **payload})

    def supersede_notification(self, key: str, *, reason: str) -> None:
        self.log_run("notification_superseded", {"key": key, "reason": reason})


def _normalise_prices(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "open", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"price frame missing columns: {', '.join(sorted(missing))}")
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    for field in ["open", "high", "low", "close", "volume"]:
        if field in data.columns:
            data[field] = pd.to_numeric(data[field], errors="coerce")
    data = data.dropna(subset=["date", "open", "close"])
    data = data[(data["open"] > 0) & (data["close"] > 0)]
    return data.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)


def _format_number(value: float | int | str) -> str:
    if value == "" or pd.isna(value):
        return ""
    return f"{float(value):.8f}"


def _select_baseline(event: EventRecord, raw: pd.DataFrame) -> tuple[int, str, str, str]:
    posted = datetime.fromisoformat(event.posted_at)
    if posted.tzinfo is None:
        raise ValueError("posted_at must include a timezone")
    local = posted.astimezone(SHANGHAI)
    post_date = pd.Timestamp(local.date())
    date_matches = raw.index[raw["date"] == post_date].tolist()

    if date_matches and local.time() <= time(15, 0):
        return date_matches[0], "close", "same_day_close", ""

    future = raw.index[raw["date"] > post_date].tolist()
    if not future:
        raise ValueError("no tradable price after posted_at")
    delayed = "" if date_matches else "delayed_baseline"
    return future[0], "open", "next_open", delayed


def _row_for_date(frame: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    rows = frame[frame["date"] == date]
    if rows.empty:
        return None
    return rows.iloc[0]


def calculate_event_history(
    event: EventRecord,
    raw_prices: pd.DataFrame,
    adjusted_prices: pd.DataFrame,
    benchmark_prices: pd.DataFrame,
    *,
    data_source: str = "fixture",
    run_id: str = "fixture",
) -> CalculationResult:
    errors = validate_event(event)
    if errors:
        raise ValueError(f"event validation failed: {', '.join(errors)}")

    raw = _normalise_prices(raw_prices)
    adjusted = _normalise_prices(adjusted_prices)
    benchmark = _normalise_prices(benchmark_prices)
    baseline_index, baseline_field, baseline_rule, warning = _select_baseline(event, raw)
    baseline_row = raw.iloc[baseline_index]
    baseline_date = baseline_row["date"]
    baseline_price = float(baseline_row[baseline_field])

    benchmark_baseline_row = _row_for_date(benchmark, baseline_date)
    if benchmark_baseline_row is None:
        raise ValueError(f"benchmark missing baseline date {baseline_date.date()}")
    benchmark_baseline = float(benchmark_baseline_row[baseline_field])

    adjusted_baseline_row = _row_for_date(adjusted, baseline_date)
    adjusted_baseline = (
        float(adjusted_baseline_row[baseline_field])
        if adjusted_baseline_row is not None
        else float("nan")
    )

    if all(field in raw.columns for field in ["open", "high", "low", "close"]):
        same_price = all(
            abs(float(baseline_row[field]) - baseline_price) < 1e-12
            for field in ["open", "high", "low", "close"]
        )
        if same_price:
            warning = ";".join(filter(None, [warning, "one_price_limit_suspected"]))

    updated_event = replace(
        event,
        baseline_rule=baseline_rule,
        baseline_date=baseline_date.strftime("%Y-%m-%d"),
        baseline_price_raw=_format_number(baseline_price),
        benchmark_baseline_price=_format_number(benchmark_baseline),
        execution_warning=_merge_warnings(event.execution_warning, warning),
        updated_at=now_iso(),
    )

    sign = 1.0 if event.direction == "long" else -1.0
    marks: list[dict[str, str]] = []
    max_adverse = 0.0
    max_favorable = 0.0
    corporate_action_seen = False
    tracking = raw.iloc[baseline_index:].reset_index(drop=True)
    for elapsed, (_, row) in enumerate(tracking.iterrows()):
        trade_date = row["date"]
        benchmark_row = _row_for_date(benchmark, trade_date)
        adjusted_row = _row_for_date(adjusted, trade_date)
        close_raw = float(row["close"])
        raw_return = close_raw / baseline_price - 1.0
        directional_return = sign * raw_return
        max_adverse = min(max_adverse, directional_return)
        max_favorable = max(max_favorable, directional_return)

        benchmark_return: float | str = ""
        benchmark_close: float | str = ""
        directional_excess: float | str = ""
        if benchmark_row is not None:
            benchmark_close = float(benchmark_row["close"])
            benchmark_return = benchmark_close / benchmark_baseline - 1.0
            directional_excess = sign * (raw_return - benchmark_return)

        adjusted_close: float | str = ""
        adjusted_return: float | str = ""
        if adjusted_row is not None and not pd.isna(adjusted_baseline):
            adjusted_close = float(adjusted_row["close"])
            adjusted_return = adjusted_close / adjusted_baseline - 1.0
            if abs(float(adjusted_return) - raw_return) > 0.005:
                corporate_action_seen = True

        status = "corporate_action_warning" if corporate_action_seen else "ok"
        marks.append(
            {
                "event_id": event.event_id,
                "trade_date": trade_date.strftime("%Y-%m-%d"),
                "close_raw": _format_number(close_raw),
                "close_adjusted": _format_number(adjusted_close),
                "raw_return": _format_number(raw_return),
                "adjusted_return": _format_number(adjusted_return),
                "directional_return": _format_number(directional_return),
                "benchmark_close": _format_number(benchmark_close),
                "benchmark_return": _format_number(benchmark_return),
                "directional_excess_return": _format_number(directional_excess),
                "max_adverse_return": _format_number(max_adverse),
                "max_favorable_return": _format_number(max_favorable),
                "tracking_days": str(elapsed),
                "data_source": data_source,
                "data_status": status,
                "run_id": run_id,
                "updated_at": now_iso(),
            }
        )

    if corporate_action_seen:
        warning = _merge_warnings(updated_event.execution_warning, "corporate_action_adjustment")
        updated_event = replace(updated_event, execution_warning=warning)

    checkpoints: list[dict[str, str]] = []
    for horizon, target_days in CHECKPOINT_DAYS.items():
        if len(marks) <= target_days:
            continue
        mark = marks[target_days]
        checkpoints.append(
            {
                "event_id": event.event_id,
                "horizon": horizon,
                "target_days": str(target_days),
                "trade_date": mark["trade_date"],
                "close_raw": mark["close_raw"],
                "raw_return": mark["raw_return"],
                "adjusted_return": mark["adjusted_return"],
                "directional_return": mark["directional_return"],
                "benchmark_return": mark["benchmark_return"],
                "directional_excess_return": mark["directional_excess_return"],
                "max_adverse_return": mark["max_adverse_return"],
                "max_favorable_return": mark["max_favorable_return"],
                "primary_source": data_source,
                "secondary_source": "",
                "secondary_close": "",
                "verification_status": "verified",
                "finalized_at": now_iso(),
            }
        )

    return CalculationResult(event=updated_event, marks=marks, checkpoints=checkpoints)


def _percent(value: str) -> str:
    if value in {"", None}:
        return "-"
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "-"


def _source_link(url: str) -> str:
    if not url:
        return "-"
    return f"[原帖]({url})"


def generate_dashboard(store: KolStore, output: Path) -> None:
    events = store.load_events()
    marks = store.load_marks()
    checkpoints = store.load_checkpoints()
    latest_marks: dict[str, dict[str, str]] = {}
    for mark in marks:
        event_id = mark.get("event_id", "")
        if event_id not in latest_marks or mark.get("trade_date", "") > latest_marks[event_id].get("trade_date", ""):
            latest_marks[event_id] = mark

    lines = [
        "---",
        "type: generated_kol_backtest_report",
        "status: active",
        "generated_at: \"" + now_iso() + "\"",
        "---",
        "",
        "# KOL 推荐收益看板",
        "",
        "> 自动生成，只用于研究审计，不构成投资建议。请勿手工修改本页数据。",
        "",
        "## 正在跟踪",
        "",
        "| 事件 | KOL | 标的 | 基准日/价格 | 当前日 | 方向收益 | 沪深300 | 超额 | 最大不利 | 警告 | 来源 |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for event in events:
        if event.status not in {"active", "completed"}:
            continue
        mark = latest_marks.get(event.event_id, {})
        row_template = (
            "| {event_id} | {kol} | {symbol} {name} | {date} / {price} | "
            "{current} | {ret} | {bench} | {excess} | {adverse} | {warning} | {source} |"
        )
        lines.append(
            row_template.format(
                event_id=event.event_id,
                kol=event.kol_name,
                symbol=event.symbol,
                name=event.security_name,
                date=event.baseline_date or "待取数",
                price=event.baseline_price_raw or "-",
                current=mark.get("trade_date", "-"),
                ret=_percent(mark.get("directional_return", "")),
                bench=_percent(mark.get("benchmark_return", "")),
                excess=_percent(mark.get("directional_excess_return", "")),
                adverse=_percent(mark.get("max_adverse_return", "")),
                warning=event.execution_warning or mark.get("data_status", "-") or "-",
                source=_source_link(event.source_url),
            )
        )
    if not any(event.status in {"active", "completed"} for event in events):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 已冻结节点",
            "",
            "| 事件 | 节点 | 交易日 | 方向收益 | 超额 | 最大不利 | 验证 |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    for row in sorted(checkpoints, key=lambda item: (item.get("event_id", ""), item.get("target_days", ""))):
        lines.append(
            f"| {row.get('event_id', '')} | {row.get('horizon', '')} | {row.get('trade_date', '')} | "
            f"{_percent(row.get('directional_return', ''))} | {_percent(row.get('directional_excess_return', ''))} | "
            f"{_percent(row.get('max_adverse_return', ''))} | {row.get('verification_status', '')} |"
        )
    if not checkpoints:
        lines.append("| - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 候选与排除",
            "",
            "| 事件 | KOL | 状态 | 原因 | 来源 |",
            "|---|---|---|---|---|",
        ]
    )
    for event in events:
        if event.status in {"active", "completed"}:
            continue
        lines.append(
            f"| {event.event_id} | {event.kol_name} | {event.status} | "
            f"{event.exclusion_reason or '待补六要素'} | {_source_link(event.source_url)} |"
        )

    completed_by_kol: dict[str, int] = {}
    for event in events:
        if event.status == "completed" and is_executable_event(event):
            completed_by_kol[event.kol_name] = completed_by_kol.get(event.kol_name, 0) + 1
    lines.extend(
        [
            "",
            "## 样本门槛",
            "",
            "- 疑似一字板或含未验证盘中条件的事件只展示价格表现，不计入可执行样本。",
        ]
    )
    if not completed_by_kol:
        lines.append("- 暂无完成120交易日跟踪的事件，不展示KOL排名。")
    else:
        for kol, count in sorted(completed_by_kol.items()):
            suffix = "可进入统计" if count >= 10 else "样本不足，不排名"
            lines.append(f"- {kol}: {count} 条，{suffix}。")
    lines.extend(["", "人工审核入口：http://127.0.0.1:8123/#/reviews", ""])

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.tmp")
    temp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp, output)


def _provider_frame(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
    }
    return _normalise_prices(frame.rename(columns=aliases))


class BaoStockProvider:
    name = "baostock"

    @staticmethod
    def _stock_code(symbol: str) -> str:
        market = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{market}.{symbol}"

    def _query(self, code: str, start: date, end: date, adjustflag: str) -> pd.DataFrame:
        import baostock as bs  # type: ignore

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
        try:
            result = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag=adjustflag,
            )
            rows: list[list[str]] = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            if result.error_code != "0":
                raise RuntimeError(f"BaoStock query failed: {result.error_msg}")
            frame = pd.DataFrame(rows, columns=result.fields)
            if frame.empty:
                raise RuntimeError(f"BaoStock returned no data for {code}")
            return _provider_frame(frame)
        finally:
            bs.logout()

    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame:
        # BaoStock: 2=前复权, 3=不复权.
        return self._query(self._stock_code(symbol), start, end, "2" if adjusted else "3")

    def fetch_benchmark(self, start: date, end: date) -> pd.DataFrame:
        return self._query("sh.000300", start, end, "3")


class WarehousePriceProvider:
    """Read validated daily bars from the local market warehouse."""

    name = "market_warehouse"

    def __init__(self, warehouse_root: Path):
        self.warehouse_root = Path(warehouse_root)
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _load(self, symbol: str, adjustment: str) -> pd.DataFrame:
        key = (symbol, adjustment)
        if key not in self._cache:
            paths = sorted((self.warehouse_root / "daily" / symbol / adjustment).glob("*.parquet"))
            if not paths:
                raise RuntimeError(f"local warehouse has no {adjustment} data for {symbol}")
            frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
            if "trade_date" in frame.columns and "date" not in frame.columns:
                frame = frame.rename(columns={"trade_date": "date"})
            self._cache[key] = _normalise_prices(frame)
        return self._cache[key]

    def _slice(self, symbol: str, adjustment: str, start: date, end: date) -> pd.DataFrame:
        frame = self._load(symbol, adjustment)
        selected = frame[
            (frame["date"] >= pd.Timestamp(start))
            & (frame["date"] <= pd.Timestamp(end))
        ]
        if selected.empty:
            raise RuntimeError(
                f"local warehouse has no {adjustment} rows for {symbol} between {start} and {end}"
            )
        return selected.reset_index(drop=True).copy()

    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame:
        return self._slice(symbol, "qfq" if adjusted else "raw", start, end)

    def fetch_benchmark(self, start: date, end: date) -> pd.DataFrame:
        return self._slice("000300", "raw", start, end)


class AKShareProvider:
    name = "akshare"

    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        frame = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq" if adjusted else "",
        )
        if frame.empty:
            raise RuntimeError(f"AKShare returned no data for {symbol}")
        return _provider_frame(frame)

    def fetch_benchmark(self, start: date, end: date) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        try:
            frame = ak.index_zh_a_hist(
                symbol="000300",
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        except Exception:
            frame = ak.stock_zh_index_daily_em(symbol="sh000300")
        frame = _provider_frame(frame)
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)]
        if frame.empty:
            raise RuntimeError("AKShare returned no CSI 300 data")
        return frame.reset_index(drop=True)


def _notification(kind: str, key: str, message: str, event_id: str = "") -> dict[str, str]:
    return {"kind": kind, "key": key, "message": message, "event_id": event_id}


def _secondary_checkpoint(
    event: EventRecord,
    checkpoint: dict[str, str],
    secondary_raw: pd.DataFrame,
    secondary_benchmark: pd.DataFrame,
    secondary_name: str,
) -> tuple[dict[str, str], dict[str, str] | None]:
    trade_date = pd.Timestamp(checkpoint["trade_date"])
    baseline_date = pd.Timestamp(event.baseline_date)
    raw = _normalise_prices(secondary_raw)
    benchmark = _normalise_prices(secondary_benchmark)
    secondary_row = _row_for_date(raw, trade_date)
    secondary_baseline_row = _row_for_date(raw, baseline_date)
    secondary_benchmark_row = _row_for_date(benchmark, trade_date)
    secondary_benchmark_baseline_row = _row_for_date(benchmark, baseline_date)
    verified = dict(checkpoint)
    verified["secondary_source"] = secondary_name
    if any(
        row is None
        for row in [
            secondary_row,
            secondary_baseline_row,
            secondary_benchmark_row,
            secondary_benchmark_baseline_row,
        ]
    ):
        verified["verification_status"] = "data_conflict"
        return verified, _notification(
            "data_conflict",
            f"data_conflict:{checkpoint['event_id']}:{checkpoint['horizon']}:{checkpoint['trade_date']}:missing",
            f"{checkpoint['event_id']} {checkpoint['horizon']} 在 {secondary_name} 缺少基准日或节点日行情，暂不冻结。",
            checkpoint["event_id"],
        )

    assert secondary_row is not None
    assert secondary_baseline_row is not None
    assert secondary_benchmark_row is not None
    assert secondary_benchmark_baseline_row is not None
    baseline_field = "open" if event.baseline_rule == "next_open" else "close"
    primary_close = float(checkpoint["close_raw"])
    secondary_close = float(secondary_row["close"])
    primary_baseline = float(event.baseline_price_raw)
    secondary_baseline = float(secondary_baseline_row[baseline_field])
    primary_benchmark_baseline = float(event.benchmark_baseline_price)
    secondary_benchmark_baseline = float(secondary_benchmark_baseline_row[baseline_field])
    primary_benchmark_close = primary_benchmark_baseline * (1.0 + float(checkpoint["benchmark_return"]))
    secondary_benchmark_close = float(secondary_benchmark_row["close"])
    comparisons = {
        "标的基准": (primary_baseline, secondary_baseline),
        "标的节点": (primary_close, secondary_close),
        "沪深300基准": (primary_benchmark_baseline, secondary_benchmark_baseline),
        "沪深300节点": (primary_benchmark_close, secondary_benchmark_close),
    }
    differences = {
        label: abs(primary_value / secondary_value - 1.0)
        for label, (primary_value, secondary_value) in comparisons.items()
    }
    verified["secondary_close"] = _format_number(secondary_close)
    conflict_label, difference = max(differences.items(), key=lambda item: item[1])
    if difference > 0.005:
        verified["verification_status"] = "data_conflict"
        return verified, _notification(
            "data_conflict",
            (
                f"data_conflict:{checkpoint['event_id']}:{checkpoint['horizon']}:"
                f"{checkpoint['trade_date']}:{conflict_label}:{difference:.6f}"
            ),
            (
                f"{checkpoint['event_id']} {checkpoint['horizon']} 双源价格冲突（{conflict_label}）："
                f"差异 {difference:.2%}，节点暂不冻结。"
            ),
            checkpoint["event_id"],
        )
    verified["verification_status"] = "verified"
    return verified, None


def update_kol_tracking(
    store: KolStore,
    primary: PriceProvider,
    secondary: PriceProvider,
    *,
    as_of: date,
    dashboard_path: Path,
    dry_run: bool = False,
) -> UpdateResult:
    run_id = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S%z")
    events = store.load_events()
    existing_checkpoints = {
        (row.get("event_id", ""), row.get("horizon", ""))
        for row in store.load_checkpoints()
    }
    updated: list[EventRecord] = []
    notifications: list[dict[str, str]] = []
    errors: list[str] = []
    awaiting_market_data: list[str] = []
    all_marks: list[dict[str, str]] = []
    checkpoints_to_freeze: list[dict[str, str]] = []

    tracked_event_count = sum(
        event.status in {"active", "completed"} for event in events
    )
    for event in events:
        if event.status not in {"active", "completed"}:
            updated.append(event)
            continue

        posted = datetime.fromisoformat(event.posted_at).astimezone(SHANGHAI)
        start = posted.date() - timedelta(days=7)
        selected = primary
        verifier: PriceProvider | None = secondary
        try:
            raw = primary.fetch_stock(event.symbol, start, as_of, adjusted=False)
            adjusted = primary.fetch_stock(event.symbol, start, as_of, adjusted=True)
            benchmark = primary.fetch_benchmark(start, as_of)
        except Exception as primary_error:
            notifications.append(
                _notification(
                    "source_failure",
                    f"source_failure:{as_of.isoformat()}:{event.event_id}:{primary.name}",
                    f"{event.event_id} 主行情源 {primary.name} 失败：{primary_error}",
                    event.event_id,
                )
            )
            try:
                selected = secondary
                verifier = None
                raw = secondary.fetch_stock(event.symbol, start, as_of, adjusted=False)
                adjusted = secondary.fetch_stock(event.symbol, start, as_of, adjusted=True)
                benchmark = secondary.fetch_benchmark(start, as_of)
            except Exception as secondary_error:
                message = (
                    f"{event.event_id} 无可用行情：{primary.name}={primary_error}; "
                    f"{secondary.name}={secondary_error}"
                )
                errors.append(message)
                notifications.append(
                    _notification(
                        "source_failure",
                        f"source_failure:{as_of.isoformat()}:{event.event_id}:all",
                        message,
                        event.event_id,
                    )
                )
                updated.append(event)
                continue

        try:
            result = calculate_event_history(
                event,
                raw,
                adjusted,
                benchmark,
                data_source=selected.name,
                run_id=run_id,
            )
        except Exception as exc:
            if str(exc) == "no tradable price after posted_at":
                awaiting_market_data.append(event.event_id)
                updated.append(event)
                continue
            message = f"{event.event_id} 收益计算失败：{exc}"
            errors.append(message)
            notifications.append(
                _notification(
                    "calculation_failure",
                    f"calculation_failure:{as_of.isoformat()}:{event.event_id}",
                    message,
                    event.event_id,
                )
            )
            updated.append(event)
            continue

        event_checkpoints = [
            row
            for row in result.checkpoints
            if (row["event_id"], row["horizon"]) not in existing_checkpoints
        ]
        if event_checkpoints and verifier is not None:
            try:
                secondary_raw = verifier.fetch_stock(event.symbol, start, as_of, adjusted=False)
                secondary_benchmark = verifier.fetch_benchmark(start, as_of)
                verified_rows: list[dict[str, str]] = []
                for checkpoint in event_checkpoints:
                    verified, conflict = _secondary_checkpoint(
                        result.event,
                        checkpoint,
                        secondary_raw,
                        secondary_benchmark,
                        verifier.name,
                    )
                    verified_rows.append(verified)
                    if conflict:
                        notifications.append(conflict)
                event_checkpoints = verified_rows
            except Exception as exc:
                notifications.append(
                    _notification(
                        "source_failure",
                        f"checkpoint_source_failure:{as_of.isoformat()}:{event.event_id}:{verifier.name}",
                        f"{event.event_id} 节点交叉验证源 {verifier.name} 失败：{exc}，节点暂不冻结。",
                        event.event_id,
                    )
                )
                event_checkpoints = [
                    {**row, "secondary_source": verifier.name, "verification_status": "secondary_unavailable"}
                    for row in event_checkpoints
                ]
        elif event_checkpoints:
            event_checkpoints = [
                {**row, "verification_status": "secondary_unavailable"}
                for row in event_checkpoints
            ]

        all_marks.extend(result.marks)
        checkpoints_to_freeze.extend(event_checkpoints)
        if not event.activation_notified_at:
            notifications.append(
                _notification(
                    "activation",
                    f"activation:{event.event_id}",
                    f"KOL事件 {event.event_id} 已激活：{event.kol_name} / {event.symbol} {event.security_name}。",
                    event.event_id,
                )
            )
        updated.append(result.event)

    verified_candidates = [
        row for row in checkpoints_to_freeze if row.get("verification_status") == "verified"
    ]
    if dry_run:
        new_checkpoints = verified_candidates
    else:
        if all_marks:
            store.upsert_marks(all_marks)
        new_checkpoints = store.freeze_checkpoints(verified_candidates)

    for checkpoint in new_checkpoints:
        notifications.append(
            _notification(
                "checkpoint",
                f"checkpoint:{checkpoint['event_id']}:{checkpoint['horizon']}",
                (
                    f"{checkpoint['event_id']} 到达 {checkpoint['horizon']} 节点："
                    f"方向收益 {_percent(checkpoint['directional_return'])}，"
                    f"超额 {_percent(checkpoint['directional_excess_return'])}。"
                ),
                checkpoint["event_id"],
            )
        )

    completed_ids = {
        row["event_id"] for row in [*store.load_checkpoints(), *new_checkpoints] if row.get("horizon") == "6M"
    }
    finalized_events: list[EventRecord] = []
    for event in updated:
        if event.event_id in completed_ids and event.status == "active":
            finalized_events.append(replace(event, status="completed", updated_at=now_iso()))
            notifications.append(
                _notification(
                    "completed",
                    f"completed:{event.event_id}",
                    f"KOL事件 {event.event_id} 已完成120日检查点；每日收益将继续跟踪。",
                    event.event_id,
                )
            )
        else:
            finalized_events.append(event)

    if not dry_run:
        if finalized_events != events:
            store.save_events(finalized_events)
        generate_dashboard(store, dashboard_path)
        store.log_run(
            "kol_update",
            {
                "run_id": run_id,
                "as_of": as_of.isoformat(),
                "updated_events": tracked_event_count,
                "marks": len(all_marks),
                "new_checkpoints": len(new_checkpoints),
                "awaiting_market_data": awaiting_market_data,
                "errors": errors,
            },
        )

    return UpdateResult(
        run_id=run_id,
        updated_events=tracked_event_count,
        mark_count=len(all_marks),
        new_checkpoints=new_checkpoints,
        notifications=notifications,
        errors=errors,
        awaiting_market_data=awaiting_market_data,
    )


def _seed_events() -> list[EventRecord]:
    activated = "2026-07-13T00:00:00+08:00"
    return [
        EventRecord(
            event_id="KOL-0001",
            kol_name="Serenity 白毛",
            platform="X",
            source_url="https://twitter.com/artinmemes/status/2062737603007480183",
            source_note="01_Sources/2026-06-05__X-白毛绿的谐波688017二手推荐.md",
            posted_at="",
            symbol="688017",
            security_name="绿的谐波",
            direction="long",
            thesis="二手转述称白毛推荐绿的谐波",
            status="candidate",
            exclusion_reason="缺Serenity原始推荐、精确时间和完整理由",
        ),
        EventRecord(
            event_id="KOL-0002",
            kol_name="A股点金手",
            platform="X",
            source_url="https://x.com/agudianjinshou/status/2075222049170333870",
            source_note="01_Sources/2026-07-10__X-A股点金手-(@agudianjinshou).md",
            posted_at="2026-07-09T22:14:26+08:00",
            symbol="",
            security_name="AI算力硬件瓶颈链",
            direction="long",
            thesis="转述产业瓶颈方法论并涉及英特尔",
            status="candidate",
            exclusion_reason="方法论和二手作业，不是明确A股单标的事前推荐",
        ),
        EventRecord(
            event_id="KOL-0003",
            kol_name="A股点金手",
            platform="X",
            source_url="https://x.com/agudianjinshou/status/2075097945163309070",
            source_note="01_Sources/2026-07-10__X-A股点金手-(@agudianjinshou)-2.md",
            posted_at="2026-07-09T14:01:18+08:00",
            symbol="",
            security_name="东山精密等五只股票",
            direction="long",
            thesis="上涨后评论东山精密及InP光芯片链修复",
            status="excluded",
            exclusion_reason="涨后回顾且一条内容包含多个标的",
        ),
        EventRecord(
            event_id="KOL-0004",
            kol_name="林哥-深研A股",
            platform="X",
            source_url="https://x.com/WwQQ129146/status/2074819740686795088",
            source_note="01_Sources/2026-07-10__X-林哥-深研A股-(@WwQQ129146).md",
            posted_at="2026-07-08T19:35:49+08:00",
            symbol="002414",
            security_name="高德红外",
            direction="long",
            thesis="半年报预增601%-701%，Q2利润预计环比增长147%-196%，次日关注",
            status="active",
            activated_at=activated,
        ),
        EventRecord(
            event_id="KOL-0005",
            kol_name="擒龙捉妖-泰戈",
            platform="X",
            source_url="https://x.com/sszcw/status/2075437983680090113",
            source_note="01_Sources/2026-07-10__X-擒龙捉妖.泰戈👊🏻📈🐂🔥-(@sszcw).md",
            posted_at="2026-07-10T12:32:29+08:00",
            symbol="601888",
            security_name="中国中免",
            direction="long",
            thesis="现在可以进场，周期较长，中间会震荡；标的由配图和人工确认",
            status="active",
            activated_at=activated,
        ),
    ]


def initialize_seed_events(store: KolStore) -> int:
    created = 0
    for event in _seed_events():
        if store.register_event(event):
            created += 1
    return created
