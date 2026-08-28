from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from event_context import event_context_cutoff, event_context_input_hash
from event_research_service import EventMethodResearchService
from kol_tracker import EventRecord, KolStore
from market_data import MarketStore


def _safe(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _hash(value: Any) -> str:
    payload = json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _frame_records(frame: pd.DataFrame, *, limit: int | None = None) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    value = frame.copy()
    if "trade_date" in value.columns:
        value["trade_date"] = pd.to_datetime(value["trade_date"], errors="coerce").dt.date
    if "snapshot_date" in value.columns:
        value["snapshot_date"] = pd.to_datetime(value["snapshot_date"], errors="coerce").dt.date
    if limit is not None:
        value = value.tail(limit)
    return [_safe(row) for row in value.to_dict(orient="records")]


def _enrich_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """Add transparent, event-safe daily facts without changing stored bars."""
    if frame.empty:
        return frame
    value = frame.sort_values("trade_date").copy()
    close = pd.to_numeric(value.get("close"), errors="coerce")
    preclose = pd.to_numeric(value.get("preclose"), errors="coerce") if "preclose" in value else close.shift(1)
    preclose = preclose.where(preclose.notna() & (preclose > 0), close.shift(1))
    high = pd.to_numeric(value.get("high"), errors="coerce")
    low = pd.to_numeric(value.get("low"), errors="coerce")
    value["change"] = close - preclose
    value["change_pct"] = close / preclose - 1
    value["amplitude_pct"] = (high - low) / preclose
    volume = pd.to_numeric(value.get("volume"), errors="coerce")
    value["volume_ratio_5"] = volume / volume.shift(1).rolling(5, min_periods=5).mean()
    # Zero preclose or zero trailing volume yields inf/-inf; downstream
    # consumers expect NaN for missing values.
    for column in ("change_pct", "amplitude_pct", "volume_ratio_5"):
        value[column] = value[column].replace([float("inf"), float("-inf")], float("nan"))
    return value


def _post_id(event: EventRecord) -> str:
    if event.source_note.startswith("post:"):
        value = event.source_note.removeprefix("post:").strip()
        if value:
            return value
    match = re.search(r"/status/(\d{5,25})", event.source_url or "")
    return match.group(1) if match else ""


def _section_status(*, ready: bool, pending: bool = False, failed: bool = False) -> str:
    if failed:
        return "failed"
    if ready:
        return "ready"
    if pending:
        return "pending"
    return "unavailable"


class EventDossierService:
    """Aggregate event evidence and market facts without changing return data."""

    AUXILIARY_DATASETS = ("valuation", "financial_summary", "announcements", "fund_flow")

    def __init__(
        self,
        post_store: Any,
        event_store: KolStore,
        market_store: MarketStore,
        method_research_service: EventMethodResearchService | None = None,
    ):
        self.post_store = post_store
        self.event_store = event_store
        self.market_store = market_store
        self.method_research_service = (
            method_research_service
            or EventMethodResearchService(event_store, market_store)
        )

    def _event(self, event_id: str) -> EventRecord:
        event = next((item for item in self.event_store.load_events() if item.event_id == event_id), None)
        if event is None:
            raise KeyError(f"event not found: {event_id}")
        return event

    @staticmethod
    def _event_cutoff(event: EventRecord) -> tuple[date | None, list[str]]:
        if not event.posted_at:
            return None, ["posted_at_missing"]
        try:
            return event_context_cutoff(event.posted_at), []
        except (TypeError, ValueError):
            try:
                return date.fromisoformat(event.posted_at[:10]), ["posted_at_cutoff_fallback"]
            except (TypeError, ValueError):
                return None, ["posted_at_invalid"]

    @staticmethod
    def _filter_to_cutoff(frame: pd.DataFrame, cutoff: date) -> tuple[pd.DataFrame, list[str]]:
        """Keep only rows whose collection and effective dates are point-in-time safe."""
        if frame.empty:
            return frame, []
        value = frame.copy()
        warnings: list[str] = []
        date_columns = (
            "snapshot_date",
            "trade_date",
            "date",
            "announcement_date",
            "published_at",
            "report_date",
            "end_date",
        )
        mask = pd.Series(True, index=value.index)
        for column in date_columns:
            if column not in value.columns:
                continue
            parsed = pd.to_datetime(value[column], errors="coerce", utc=True).dt.date
            if not parsed.notna().any():
                warnings.append(f"{column}_unparseable")
                mask &= False
                continue
            if parsed.isna().any():
                warnings.append(f"{column}_missing")
            mask &= parsed.notna() & (parsed <= cutoff)
        return value.loc[mask].copy(), warnings

    def _drafts(self, event_id: str) -> list[dict[str, Any]]:
        with self.post_store.connect() as db:
            rows = db.execute(
                "SELECT id FROM recommendation_drafts WHERE event_id=? ORDER BY id",
                (event_id,),
            ).fetchall()
        values: list[dict[str, Any]] = []
        repository = None
        if rows:
            from recommendation_drafts import RecommendationDraftRepository

            repository = RecommendationDraftRepository(self.post_store)
        for row in rows:
            if repository is not None:
                try:
                    draft = repository.get_draft(int(row["id"]))
                    draft["revisions"] = repository.list_revisions(int(row["id"]))
                    values.append(_safe(draft))
                except KeyError:
                    continue
        return values

    def _recommendation(self, event: EventRecord) -> dict[str, Any]:
        drafts = self._drafts(event.event_id)
        if not drafts:
            return {
                "status": "partial",
                "drafts": [],
                "data": {
                    "direction": event.direction,
                    "thesis": event.thesis,
                },
                "warnings": ["recommendation_draft_not_mapped"],
            }
        return {
            "status": "ready",
            "drafts": drafts,
            "data": {
                "direction": event.direction,
                "thesis": event.thesis,
            },
            "warnings": [],
        }

    def _evidence(self, event: EventRecord) -> dict[str, Any]:
        post_id = _post_id(event)
        if not post_id:
            return {"status": "unavailable", "post_id": "", "post": None, "drafts": [], "warnings": ["source_post_not_mapped"]}
        try:
            post = self.post_store.get_post(post_id)
        except KeyError:
            if "legacy_evidence_partial" in event.execution_warning.split(";"):
                return {
                    "status": "partial",
                    "post_id": post_id,
                    "post": None,
                    "drafts": self._drafts(event.event_id),
                    "legacy_metadata": {
                        "source_url": event.source_url,
                        "posted_at": event.posted_at,
                        "source_note": event.source_note,
                        "confirmed_thesis": event.thesis,
                    },
                    "warnings": [
                        "source_post_snapshot_missing",
                        "legacy_human_confirmation_only",
                    ],
                }
            return {"status": "failed", "post_id": post_id, "post": None, "drafts": [], "warnings": ["source_post_not_found"]}
        value = dict(post)
        value.pop("raw_json", None)
        media = []
        for item in value.get("local_media", []) or []:
            if not item.get("path"):
                continue
            media.append({
                **item,
                "api_url": f"/media/{post_id}/{Path(str(item['path'])).name}",
            })
        value["local_media"] = media
        return {
            "status": "ready",
            "post_id": post_id,
            "post": _safe(value),
            "drafts": self._drafts(event.event_id),
            "warnings": [item for item in [str(post.get("provider_warning") or "")] if item],
        }

    def _market_snapshot(self, event: EventRecord) -> dict[str, Any]:
        cutoff, warnings = self._event_cutoff(event)
        if cutoff is None:
            return {
                "status": "unavailable",
                "effective_trade_date": "",
                "raw_at_event": None,
                "qfq_at_event": None,
                "coverage": self.market_store.get_coverage(event.symbol),
                "warnings": warnings,
            }
        as_of = self.market_store.latest_open_date(cutoff)
        raw = _enrich_daily(self.market_store.read_daily(event.symbol, adjustment="raw"))
        qfq = _enrich_daily(self.market_store.read_daily(event.symbol, adjustment="qfq"))
        effective_as_of = as_of or cutoff
        if as_of is None:
            warnings.append("calendar_missing_or_no_open_date")
        if not raw.empty:
            raw = raw[pd.to_datetime(raw["trade_date"], errors="coerce").dt.date <= effective_as_of]
        if not qfq.empty:
            qfq = qfq[pd.to_datetime(qfq["trade_date"], errors="coerce").dt.date <= effective_as_of]
        raw_at_event = _frame_records(raw.tail(1))
        qfq_at_event = _frame_records(qfq.tail(1))
        selected_dates = [
            str(item.get("trade_date"))
            for item in [*(raw_at_event or []), *(qfq_at_event or [])]
            if item.get("trade_date")
        ]
        effective_trade_date = as_of.isoformat() if as_of else max(selected_dates, default="")
        if not raw_at_event and not qfq_at_event:
            warnings.append("daily_data_unavailable")
        elif not raw_at_event or not qfq_at_event:
            warnings.append("daily_adjustment_partial")
        coverage = self.market_store.get_coverage(event.symbol)
        market_status = (
            "ready"
            if raw_at_event and qfq_at_event
            else "partial"
            if raw_at_event or qfq_at_event
            else "unavailable"
        )
        return {
            "status": market_status,
            "effective_trade_date": effective_trade_date,
            "raw_at_event": raw_at_event[0] if raw_at_event else None,
            "qfq_at_event": qfq_at_event[0] if qfq_at_event else None,
            "coverage": _safe(coverage),
            "warnings": warnings,
        }

    def _minute_context(self, event: EventRecord) -> dict[str, Any]:
        cutoff, cutoff_warnings = self._event_cutoff(event)
        if cutoff is None:
            return {
                "status": "unavailable",
                "datasets": {},
                "warnings": cutoff_warnings,
            }
        datasets: dict[str, Any] = {}
        event_day = pd.Timestamp(event.posted_at[:10])
        start = event_day - pd.Timedelta(days=1)
        end = event_day + pd.Timedelta(days=7)
        read_errors: list[str] = []
        for frequency in ("1m", "5m", "15m", "30m", "60m"):
            directory = self.market_store.warehouse_root / f"minute_{frequency}" / event.symbol
            paths = sorted(directory.glob("*.parquet"))
            frames: list[pd.DataFrame] = []
            for path in paths:
                try:
                    frame = pd.read_parquet(path)
                    if "trade_datetime" in frame.columns:
                        frame["trade_datetime"] = pd.to_datetime(frame["trade_datetime"], errors="coerce")
                        frame = frame[(frame["trade_datetime"] >= start) & (frame["trade_datetime"] <= end)]
                    frames.append(frame)
                except (OSError, ValueError, ImportError) as exc:
                    read_errors.append(f"{frequency}:{path.name}:{type(exc).__name__}")
            merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            if "trade_datetime" in merged.columns:
                merged = merged.sort_values("trade_datetime").drop_duplicates("trade_datetime", keep="last")
            latest = _frame_records(merged, limit=1)
            available_from = ""
            available_to = ""
            if not merged.empty and "trade_datetime" in merged.columns:
                values = pd.to_datetime(merged["trade_datetime"], errors="coerce").dropna()
                if not values.empty:
                    available_from = values.iloc[0].isoformat()
                    available_to = values.iloc[-1].isoformat()
            datasets[frequency] = {
                "status": "ready" if not merged.empty else "unavailable",
                "row_count": len(merged),
                "available_from": available_from,
                "available_to": available_to,
                "latest_bar": latest[0] if latest else None,
                "paths": [str(path) for path in paths],
                "warnings": [] if not merged.empty else ["minute_data_not_collected"],
            }
        ready = any(item["status"] == "ready" for item in datasets.values())
        if read_errors:
            warnings = [*cutoff_warnings, "minute_data_read_failed", *read_errors[:20]]
            status = "partial" if ready else "failed"
        else:
            warnings = cutoff_warnings if ready else [*cutoff_warnings, "minute_data_not_collected"]
            status = "ready" if ready else "unavailable"
        return {
            "status": status,
            "scope": "post_event_window",
            "window_start": start.date().isoformat(),
            "window_end": end.date().isoformat(),
            "datasets": datasets,
            "warnings": warnings,
        }

    def _auxiliary(self, event: EventRecord) -> dict[str, Any]:
        cutoff, cutoff_warnings = self._event_cutoff(event)
        if cutoff is None:
            return {
                "status": "unavailable",
                "cutoff_date": "",
                "datasets": {
                    dataset: {
                        "status": "unavailable",
                        "row_count": 0,
                        "rows": [],
                        "paths": [],
                        "warnings": cutoff_warnings,
                    }
                    for dataset in self.AUXILIARY_DATASETS
                },
                "warnings": cutoff_warnings,
            }
        datasets: dict[str, Any] = {}
        ready = False
        warnings: list[str] = list(cutoff_warnings)
        read_errors: list[str] = []
        for dataset in self.AUXILIARY_DATASETS:
            directory = self.market_store.warehouse_root / dataset / event.symbol
            paths = sorted(directory.glob("*.parquet"))
            frames: list[pd.DataFrame] = []
            for path in paths:
                try:
                    frame = pd.read_parquet(path)
                except (OSError, ValueError, ImportError) as exc:
                    read_errors.append(f"{dataset}:{path.name}:{type(exc).__name__}")
                    continue
                if "snapshot_date" not in frame.columns:
                    warnings.append(f"{dataset}:snapshot_date_missing")
                    continue
                filtered, frame_warnings = self._filter_to_cutoff(frame, cutoff)
                warnings.extend(f"{dataset}:{item}" for item in frame_warnings)
                if not filtered.empty:
                    filtered["_snapshot_file"] = path.name
                    frames.append(filtered)
            merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            rows = _frame_records(merged, limit=None)
            dataset_warnings = [] if rows else ["not_collected"]
            if not rows and paths:
                dataset_warnings = ["no_point_in_time_rows"]
            datasets[dataset] = {
                "status": "ready" if rows else "unavailable",
                "row_count": len(merged),
                "rows": rows,
                "paths": [str(path) for path in paths],
                "warnings": dataset_warnings,
            }
            ready = ready or bool(rows)
        if read_errors:
            warnings.extend(["auxiliary_data_read_failed", *read_errors[:20]])
        status = "partial" if ready and read_errors else "ready" if ready else "failed" if read_errors else "unavailable"
        return {
            "status": status,
            "cutoff_date": cutoff.isoformat(),
            "datasets": datasets,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def _board_context(self, event: EventRecord) -> dict[str, Any]:
        cutoff_date, cutoff_warnings = self._event_cutoff(event)
        if cutoff_date is None:
            return {
                "status": "unavailable",
                "rows": [],
                "warnings": cutoff_warnings,
            }
        cutoff = cutoff_date.isoformat()
        with self.market_store.connect() as db:
            cursor = db.execute(
                """
                WITH selected_version AS (
                    SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM board_rps WHERE formula_version='board-rps-v2'
                    ) THEN 'board-rps-v2' ELSE 'board-rps-v1' END AS formula_version
                ), membership AS (
                    SELECT m.board_key,m.snapshot_date,
                           ROW_NUMBER() OVER(PARTITION BY m.board_key ORDER BY m.snapshot_date DESC) AS rn
                    FROM board_memberships m WHERE m.symbol=? AND m.snapshot_date<=?
                ), latest_rps AS (
                    SELECT r.*,ROW_NUMBER() OVER(PARTITION BY r.board_key ORDER BY r.trade_date DESC) AS rn
                    FROM board_rps r CROSS JOIN selected_version v
                    WHERE r.trade_date<=? AND r.formula_version=v.formula_version
                )
                SELECT c.board_code,c.board_name,c.board_type,m.snapshot_date,
                       r.trade_date,r.rps_50,r.rps_120,r.rps_250,r.status
                FROM membership m
                JOIN board_catalog c USING(board_key)
                LEFT JOIN latest_rps r ON r.board_key=m.board_key AND r.rn=1
                WHERE m.rn=1
                ORDER BY c.board_type,c.board_name
                """,
                (event.symbol, cutoff, cutoff),
            )
            columns = [item[0] for item in cursor.description]
            rows = [_safe(dict(zip(columns, row))) for row in cursor.fetchall()]
        return {
            "status": _section_status(ready=bool(rows)),
            "rows": rows,
            "warnings": [*cutoff_warnings, "membership_uses_latest_available_snapshot"] if rows else [*cutoff_warnings, "board_membership_unavailable"],
        }

    def build(self, event_id: str) -> dict[str, Any]:
        event = self._event(event_id)
        evidence = self._evidence(event)
        market = self._market_snapshot(event)
        technical = self.market_store.get_event_technical_context(
            event.event_id,
            input_hash=event_context_input_hash(event.symbol, event.posted_at),
        )
        intraday = self.market_store.get_event_intraday_context(event.event_id)
        marks = [row for row in self.event_store.load_marks() if row.get("event_id") == event.event_id]
        checkpoints = [row for row in self.event_store.load_checkpoints() if row.get("event_id") == event.event_id]
        auxiliary = self._auxiliary(event)
        board_context = self._board_context(event)
        sections = {
            "evidence": evidence,
            "recommendation": self._recommendation(event),
            "event_market": market,
            "technical": {
                "status": str((technical or {}).get("status") or "pending"),
                "data": _safe(technical),
                "warnings": (technical or {}).get("warnings", ["not_computed"]),
            },
            "intraday": {
                "status": str((intraday or {}).get("status") or "pending"),
                "data": _safe(intraday),
                "warnings": (intraday or {}).get("warnings", ["not_computed"]),
            },
            "minute_data": self._minute_context(event),
            "fundamentals": auxiliary,
            "board_context": board_context,
            "performance": {
                "status": _section_status(ready=bool(marks)),
                "summary": {
                    "mark_count": len(marks),
                    "available_from": marks[0].get("trade_date", "") if marks else "",
                    "available_to": marks[-1].get("trade_date", "") if marks else "",
                    "latest_mark": _safe(marks[-1]) if marks else None,
                },
                "checkpoints": _safe(checkpoints),
                "warnings": [] if marks else ["returns_not_computed"],
            },
        }
        statuses = [str(section.get("status")) for section in sections.values()]
        overall = "failed" if "failed" in statuses else "partial" if any(item in {"partial", "pending", "unavailable"} for item in statuses) else "ready"
        section_status = {key: value.get("status", "unavailable") for key, value in sections.items()}
        warnings = [warning for section in sections.values() for warning in section.get("warnings", [])]
        return {
            "event": _safe(event.to_row()),
            "status": overall,
            "sections": sections,
            "section_status": section_status,
            "completeness": {
                "ready": sum(value == "ready" for value in section_status.values()),
                "total": len(section_status),
            },
            "warnings": list(dict.fromkeys(warnings)),
            "generated_at": now_iso(),
        }

    def market_series(
        self,
        event_id: str,
        *,
        adjustment: str = "qfq",
        frequency: str = "d",
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        event = self._event(event_id)
        if adjustment not in {"raw", "qfq", "hfq"}:
            raise ValueError("invalid adjustment")
        if frequency == "d":
            frame = _enrich_daily(self.market_store.read_daily(event.symbol, adjustment=adjustment))
            date_column = "trade_date"
            # Daily series defaults to the event-time point-in-time view. A caller
            # may pass an explicit later ``end`` when it wants a performance chart.
            if end is None:
                cutoff, _ = self._event_cutoff(event)
                if cutoff is not None:
                    end = cutoff
        elif frequency in {"1m", "5m", "15m", "30m", "60m"}:
            directory = self.market_store.warehouse_root / f"minute_{frequency}" / event.symbol
            frames = []
            for path in sorted(directory.glob("*.parquet")):
                try:
                    frames.append(pd.read_parquet(path))
                except (OSError, ValueError, ImportError):
                    continue
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            if "adjustment" in frame.columns:
                frame = frame[frame["adjustment"].fillna("raw") == adjustment]
            date_column = "trade_datetime"
        else:
            raise ValueError("invalid frequency")
        if end is None:
            cutoff, _ = self._event_cutoff(event)
            if cutoff is not None:
                # Point-in-time is the safe default. Pass an explicit later end
                # date when requesting post-event performance windows.
                end = cutoff
        if start is not None:
            frame = frame[pd.to_datetime(frame[date_column], errors="coerce").dt.date >= start]
        if end is not None:
            frame = frame[pd.to_datetime(frame[date_column], errors="coerce").dt.date <= end]
        return _frame_records(frame, limit=None)

    def minute_series(
        self,
        event_id: str,
        *,
        frequency: str = "1m",
        adjustment: str = "raw",
        start: date | None = None,
        end: date | None = None,
        page: int = 1,
        page_size: int = 500,
    ) -> dict[str, Any]:
        if frequency not in {"1m", "5m", "15m", "30m", "60m"}:
            raise ValueError("invalid frequency")
        rows = self.market_series(
            event_id,
            adjustment=adjustment,
            frequency=frequency,
            start=start,
            end=end,
        )
        total = len(rows)
        total_pages = max(1, (total + page_size - 1) // page_size)
        offset = (page - 1) * page_size
        return {
            "event_id": event_id,
            "frequency": frequency,
            "adjustment": adjustment,
            "items": rows[offset : offset + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    def refresh(self, event_id: str) -> dict[str, Any]:
        self.method_research_service.refresh(event_id)
        dossier = self.build(event_id)
        event = self._event(event_id)
        payload = dict(dossier)
        input_hash = _hash({"event": event.to_row(), "sections": dossier["sections"]})
        snapshot_id = f"{event_id}:{input_hash}"
        self.market_store.save_event_dossier_snapshot({
            "snapshot_id": snapshot_id,
            "event_id": event_id,
            "input_hash": input_hash,
            "status": dossier["status"],
            "section_status": dossier["section_status"],
            "payload": payload,
            "warnings": dossier["warnings"],
            "created_at": now_iso(),
        })
        dossier["snapshot"] = {
            "snapshot_id": snapshot_id,
            "input_hash": input_hash,
            "status": dossier["status"],
            "created_at": now_iso(),
        }
        return dossier

    def latest_snapshot(self, event_id: str) -> dict[str, Any] | None:
        values = self.market_store.list_event_dossier_snapshots(event_id)
        return values[0] if values else None


def now_iso() -> str:
    from market_data import now_iso as market_now_iso

    return market_now_iso()
