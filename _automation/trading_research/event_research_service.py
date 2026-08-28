from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock

from event_context import event_context_cutoff
from event_research import METHOD_RESEARCH_VERSION, analyze_event_methods
from event_research_ai import (
    EventResearchAIProvider,
    interpretation_id,
    interpretation_input_hash,
)
from kol_tracker import EventRecord, KolStore
from market_cross_section import prepare_short_term_leader_universe
from market_data import FreeStockDBMarketProvider, MarketStore, now_iso


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EventMethodResearchService:
    """Compile point-in-time event evidence and optional AI interpretations."""

    def __init__(
        self,
        event_store: KolStore,
        market_store: MarketStore,
        *,
        cross_section_provider: FreeStockDBMarketProvider | None = None,
        minute_provider: FreeStockDBMarketProvider | None = None,
        interpreter: EventResearchAIProvider | None = None,
        cache_market_data: bool = False,
    ):
        self.event_store = event_store
        self.market_store = market_store
        self.cross_section_provider = cross_section_provider
        self.minute_provider = minute_provider
        self.interpreter = interpreter
        self.cache_market_data = cache_market_data
        self._daily_cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._cross_section_cache: dict[date, pd.DataFrame] = {}
        self._minute_cache: dict[tuple[str, date], pd.DataFrame] = {}
        self._leader_universe_cache: dict[
            tuple[date, ...],
            dict[str, Any],
        ] = {}
        self._cross_section_dates_cache: list[date] | None = None
        self._cross_section_dates_cache_at: datetime | None = None

    @property
    def interpretation_provider(self) -> str:
        if self.interpreter is None:
            return ""
        return str(
            getattr(self.interpreter, "provider_name", self.interpreter.model_name)
        )

    @property
    def interpretation_model(self) -> str:
        return str(self.interpreter.model_name) if self.interpreter is not None else ""

    def has_ready_interpretation(
        self,
        event_id: str,
        research_snapshot_id: str,
    ) -> bool:
        provider = self.interpretation_provider
        expected_input_hash = (
            interpretation_input_hash(
                self._event(event_id).to_row(),
                research_snapshot_id,
                prompt_version=self.interpreter.prompt_version,
            )
            if self.interpreter is not None
            else ""
        )
        return any(
            item.get("status") == "ready"
            and (
                not provider
                or (
                    item.get("provider") == provider
                    and item.get("prompt_version")
                    == self.interpreter.prompt_version
                    and item.get("input_hash") == expected_input_hash
                )
            )
            for item in self.market_store.list_event_method_interpretations(
                event_id,
                research_snapshot_id=research_snapshot_id,
            )
        )

    def _read_daily(self, symbol: str, adjustment: str) -> pd.DataFrame:
        key = (symbol, adjustment)
        if not self.cache_market_data:
            return self.market_store.read_daily(symbol, adjustment=adjustment)
        if key not in self._daily_cache:
            self._daily_cache[key] = self.market_store.read_daily(
                symbol,
                adjustment=adjustment,
            )
        return self._daily_cache[key].copy()

    def _read_cross_sections(self, dates: Iterable[date]) -> pd.DataFrame:
        values = sorted(set(dates))
        if not self.cache_market_data:
            return self.market_store.read_cross_sections(values)
        for current in values:
            if current not in self._cross_section_cache:
                self._cross_section_cache[current] = (
                    self.market_store.read_cross_sections([current])
                )
        frames = [
            self._cross_section_cache[current]
            for current in values
            if not self._cross_section_cache[current].empty
        ]
        return (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame()
        )

    def _available_cross_section_dates(self, as_of: str) -> list[date]:
        now = datetime.now(SHANGHAI)
        if (
            self._cross_section_dates_cache is None
            or self._cross_section_dates_cache_at is None
            or (now - self._cross_section_dates_cache_at).total_seconds() > 60
        ):
            self._cross_section_dates_cache = sorted(
                {
                    (
                        value
                        if isinstance(value, date)
                        else date.fromisoformat(str(value)[:10])
                    )
                    for value in (
                        item["trade_date"]
                        for item in (
                            self.market_store.list_cross_section_snapshots()
                        )
                        if item.get("status") == "ready"
                    )
                }
            )
            self._cross_section_dates_cache_at = now
        cutoff = date.fromisoformat(as_of)
        return [
            current
            for current in self._cross_section_dates_cache
            if current <= cutoff
        ][-21:]

    def _read_minutes(
        self,
        symbol: str,
        dates: Iterable[date],
    ) -> pd.DataFrame:
        values = sorted(set(dates))
        if not self.cache_market_data:
            return self.market_store.read_minute(
                symbol,
                frequency="1m",
                adjustment="raw",
                dates=values,
            )
        for current in values:
            key = (symbol, current)
            if key not in self._minute_cache:
                self._minute_cache[key] = self.market_store.read_minute(
                    symbol,
                    frequency="1m",
                    adjustment="raw",
                    dates=[current],
                )
        frames = [
            self._minute_cache[(symbol, current)]
            for current in values
            if not self._minute_cache[(symbol, current)].empty
        ]
        return (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame()
        )

    def _event(self, event_id: str) -> EventRecord:
        event = next(
            (
                item
                for item in self.event_store.load_events()
                if item.event_id == event_id
            ),
            None,
        )
        if event is None:
            raise KeyError(f"event not found: {event_id}")
        return event

    def _daily_context(
        self,
        event: EventRecord,
    ) -> tuple[pd.DataFrame, list[date], str, dict[str, Any]]:
        daily = self._read_daily(event.symbol, "qfq")
        if daily.empty:
            return daily, [], "", {
                "daily_price_scale_basis": "unavailable",
                "warnings": ["qfq_history_unavailable_at_event"],
            }
        value = daily.copy()
        if "trade_date" not in value and "date" in value:
            value = value.rename(columns={"date": "trade_date"})
        value["trade_date"] = pd.to_datetime(
            value["trade_date"], errors="coerce"
        ).dt.date
        cutoff = event_context_cutoff(event.posted_at)
        dates = sorted(
            current
            for current in value["trade_date"].dropna().unique()
            if current <= cutoff
        )
        metadata: dict[str, Any] = {
            "daily_price_scale_basis": "provider_qfq",
            "daily_price_scale": 1.0,
            "daily_raw_cutoff_close": None,
            "daily_qfq_cutoff_close": None,
            "warnings": [],
        }
        if dates:
            as_of = dates[-1]
            qfq_row = value[value["trade_date"] == as_of].iloc[-1]
            raw = self._read_daily(event.symbol, "raw")
            if not raw.empty:
                raw_value = raw.copy()
                if "trade_date" not in raw_value and "date" in raw_value:
                    raw_value = raw_value.rename(columns={"date": "trade_date"})
                raw_value["trade_date"] = pd.to_datetime(
                    raw_value["trade_date"],
                    errors="coerce",
                ).dt.date
                raw_rows = raw_value[raw_value["trade_date"] == as_of]
            else:
                raw_rows = pd.DataFrame()
            qfq_close = float(qfq_row["close"])
            raw_close = (
                float(raw_rows.iloc[-1]["close"])
                if not raw_rows.empty
                else None
            )
            if raw_close is not None and qfq_close > 0 and raw_close > 0:
                scale = raw_close / qfq_close
                for column in ("open", "high", "low", "close", "preclose"):
                    if column in value:
                        value[column] = pd.to_numeric(
                            value[column],
                            errors="coerce",
                        ) * scale
                metadata.update(
                    {
                        "daily_price_scale_basis": "event_cutoff_raw_close",
                        "daily_price_scale": scale,
                        "daily_raw_cutoff_close": raw_close,
                        "daily_qfq_cutoff_close": qfq_close,
                    }
                )
            else:
                metadata["warnings"].append(
                    "qfq_rebase_raw_cutoff_unavailable"
                )
        return (
            value,
            dates[-21:],
            dates[-1].isoformat() if dates else "",
            metadata,
        )

    def _board_context(
        self,
        event: EventRecord,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        cutoff = event_context_cutoff(event.posted_at).isoformat()
        with self.market_store.connect() as db:
            membership_exists = db.execute(
                """
                SELECT 1
                FROM board_memberships
                WHERE symbol=? AND snapshot_date<=?
                LIMIT 1
                """,
                [event.symbol, cutoff],
            ).fetchone()
            if membership_exists is None:
                return [], {}
            cursor = db.execute(
                """
                WITH selected_version AS (
                    SELECT CASE WHEN EXISTS(
                        SELECT 1 FROM board_rps
                        WHERE formula_version='board-rps-v2'
                    ) THEN 'board-rps-v2' ELSE 'board-rps-v1' END AS formula_version
                ), membership AS (
                    SELECT m.board_key,m.snapshot_date,
                           ROW_NUMBER() OVER(
                               PARTITION BY m.board_key
                               ORDER BY m.snapshot_date DESC
                           ) AS rn
                    FROM board_memberships m
                    WHERE m.symbol=? AND m.snapshot_date<=?
                ), latest_rps AS (
                    SELECT r.*,ROW_NUMBER() OVER(
                        PARTITION BY r.board_key ORDER BY r.trade_date DESC
                    ) AS rn
                    FROM board_rps r CROSS JOIN selected_version v
                    WHERE r.trade_date<=?
                      AND r.formula_version=v.formula_version
                )
                SELECT c.board_key,c.board_code,c.board_name,c.board_type,
                       m.snapshot_date,r.trade_date,r.rps_50,r.rps_120,
                       r.rps_250,r.status
                FROM membership m
                JOIN board_catalog c USING(board_key)
                LEFT JOIN latest_rps r
                  ON r.board_key=m.board_key AND r.rn=1
                WHERE m.rn=1
                ORDER BY c.board_type,c.board_name
                """,
                [event.symbol, cutoff, cutoff],
            )
            columns = [item[0] for item in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            members: dict[str, list[str]] = {}
            for row in rows:
                member_cursor = db.execute(
                    """
                    SELECT symbol FROM board_memberships
                    WHERE board_key=? AND snapshot_date=?
                    ORDER BY symbol
                    """,
                    [row["board_key"], row["snapshot_date"]],
                )
                members[str(row["board_code"])] = [
                    str(item[0]) for item in member_cursor.fetchall()
                ]
        for row in rows:
            row.pop("board_key", None)
            for key in ("snapshot_date", "trade_date"):
                if row.get(key) is not None:
                    row[key] = row[key].isoformat()
        return rows, members

    def _minute_dates(
        self,
        event: EventRecord,
        completed_session: str,
    ) -> list[date]:
        values = [date.fromisoformat(completed_session)] if completed_session else []
        posted = datetime.fromisoformat(event.posted_at.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            raise ValueError("posted_at must include timezone")
        local = posted.astimezone(SHANGHAI)
        if local.weekday() < 5 and local.time() >= time(9, 30):
            values.append(local.date())
        return sorted(set(values))

    def ensure_cross_sections(
        self,
        event_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        event = self._event(event_id)
        _, dates, _, _ = self._daily_context(event)
        if self.cross_section_provider is None:
            return {
                "requested": len(dates),
                "created": [],
                "skipped": [current.isoformat() for current in dates],
                "errors": ["cross_section_provider_unavailable"],
            }
        existing = {
            str(item["trade_date"])
            for item in self.market_store.list_cross_section_snapshots()
            if item.get("status") == "ready"
        }
        result: dict[str, Any] = {
            "requested": len(dates),
            "created": [],
            "skipped": [],
            "errors": [],
        }
        for current in dates:
            if not force and current.isoformat() in existing:
                result["skipped"].append(current.isoformat())
                continue
            try:
                frame = self.cross_section_provider.fetch_daily_cross_section(current)
                saved = self.market_store.save_cross_section_snapshot(
                    provider=self.cross_section_provider.name,
                    as_of=current,
                    frame=frame,
                )
                self._cross_section_cache.pop(current, None)
                self._leader_universe_cache.clear()
                self._cross_section_dates_cache = None
                result["created"].append(saved)
            except Exception as exc:
                result["errors"].append(
                    {
                        "trade_date": current.isoformat(),
                        "error": str(exc)[:1000],
                    }
                )
        return result

    def ensure_minutes(
        self,
        event_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        event = self._event(event_id)
        _, _, completed_session, _ = self._daily_context(event)
        dates = self._minute_dates(event, completed_session)
        if self.minute_provider is None:
            return {
                "requested": len(dates),
                "created": [],
                "skipped": [current.isoformat() for current in dates],
                "errors": ["minute_provider_unavailable"],
            }
        instrument = self.market_store.get_instrument(event.symbol) or {}
        instrument_type = str(instrument.get("instrument_type") or "stock")
        result: dict[str, Any] = {
            "requested": len(dates),
            "created": [],
            "skipped": [],
            "errors": [],
        }
        for current in dates:
            existing = self._read_minutes(event.symbol, [current])
            if not force and not existing.empty:
                result["skipped"].append(current.isoformat())
                continue
            try:
                frame = self.minute_provider.fetch_minute(
                    event.symbol,
                    instrument_type,
                    current,
                    current,
                    frequency="1m",
                    adjustment="raw",
                )
                saved = self.market_store.save_minute_snapshot(
                    provider=self.minute_provider.name,
                    symbol=event.symbol,
                    as_of=current,
                    frequency="1m",
                    adjustment="raw",
                    frame=frame,
                    parameters={"event_id": event.event_id},
                )
                self._minute_cache.pop((event.symbol, current), None)
                result["created"].append(
                    {
                        "trade_date": current.isoformat(),
                        "rows": saved.rows,
                        "quality_status": saved.quality_status,
                    }
                )
            except Exception as exc:
                result["errors"].append(
                    {
                        "trade_date": current.isoformat(),
                        "error": str(exc)[:1000],
                    }
                )
        return result

    def build(self, event_id: str) -> dict[str, Any]:
        event = self._event(event_id)
        (
            daily,
            cross_section_dates,
            completed_session,
            daily_lineage,
        ) = self._daily_context(event)
        board_rows, board_members = self._board_context(event)
        minute_dates = self._minute_dates(event, completed_session)
        minute = self._read_minutes(event.symbol, minute_dates)
        market_cross_section_dates = (
            self._available_cross_section_dates(completed_session)
            if completed_session
            else []
        )
        if market_cross_section_dates:
            cross_section_dates = market_cross_section_dates
        cross_sections = self._read_cross_sections(cross_section_dates)
        leader_universe = None
        if self.cache_market_data:
            leader_key = tuple(cross_section_dates)
            if leader_key not in self._leader_universe_cache:
                self._leader_universe_cache[leader_key] = (
                    prepare_short_term_leader_universe(cross_sections)
                )
            leader_universe = self._leader_universe_cache[leader_key]
        instrument = self.market_store.get_instrument(event.symbol) or {}
        return analyze_event_methods(
            event_id=event.event_id,
            symbol=event.symbol,
            posted_at=event.posted_at,
            qfq_daily=daily,
            minute_bars=minute,
            cross_section_snapshots=cross_sections,
            board_rows=board_rows,
            board_members=board_members,
            instrument_type=str(instrument.get("instrument_type") or "stock"),
            daily_lineage=daily_lineage,
            leader_universe=leader_universe,
        )

    def refresh(
        self,
        event_id: str,
        *,
        fetch_cross_section: bool = False,
        fetch_minute: bool = False,
    ) -> dict[str, Any]:
        fetch_result: dict[str, Any] = {}
        if fetch_cross_section:
            fetch_result["cross_section"] = self.ensure_cross_sections(event_id)
        if fetch_minute:
            fetch_result["minute"] = self.ensure_minutes(event_id)
        event = self._event(event_id)
        research = self.build(event_id)
        payload_without_time = {
            key: value
            for key, value in research.items()
            if key != "computed_at"
        }
        input_hash = _json_hash(
            {
                "event": {
                    "event_id": event.event_id,
                    "symbol": event.symbol,
                    "posted_at": event.posted_at,
                },
                "research": payload_without_time,
            }
        )
        snapshot_id = _json_hash(
            {
                "event_id": event.event_id,
                "method_version": METHOD_RESEARCH_VERSION,
                "input_hash": input_hash,
            }
        )
        created = self.market_store.save_event_method_research(
            {
                "snapshot_id": snapshot_id,
                "event_id": event.event_id,
                "method_version": METHOD_RESEARCH_VERSION,
                "input_hash": input_hash,
                "symbol": event.symbol,
                "posted_at": event.posted_at,
                "as_of_trade_date": research.get("as_of_trade_date") or "",
                "status": research.get("status") or "partial",
                "payload": research,
                "warnings": research.get("warnings") or [],
                "computed_at": research["computed_at"],
            }
        )
        return {
            "snapshot_id": snapshot_id,
            "created": created,
            "research": research,
            "fetch": fetch_result,
        }

    def get_section(self, event_id: str) -> dict[str, Any]:
        latest = self.market_store.get_event_method_research(event_id)
        if latest is None:
            research = self.build(event_id)
            return {
                "status": str(research.get("status") or "partial"),
                "data": research,
                "interpretation": None,
                "warnings": list(research.get("warnings") or []),
                "snapshot_id": "",
            }
        interpretations = self.market_store.list_event_method_interpretations(
            event_id,
            research_snapshot_id=str(latest["snapshot_id"]),
        )
        expected_input_hash = (
            interpretation_input_hash(
                self._event(event_id).to_row(),
                str(latest["snapshot_id"]),
                prompt_version=self.interpreter.prompt_version,
            )
            if self.interpreter is not None
            else ""
        )
        current_provider = [
            item
            for item in interpretations
            if item.get("provider") == self.interpretation_provider
            and (
                self.interpreter is None
                or (
                    item.get("prompt_version")
                    == self.interpreter.prompt_version
                    and item.get("input_hash") == expected_input_hash
                )
            )
        ]
        selected = next(
            (item for item in current_provider if item.get("status") == "ready"),
            None,
        )
        if selected is None and current_provider:
            selected = current_provider[0]
        if selected is None and self.interpreter is None:
            selected = next(
                (item for item in interpretations if item.get("status") == "ready"),
                interpretations[0] if interpretations else None,
            )
        return {
            "status": str(latest.get("status") or "partial"),
            "data": latest["payload"],
            "interpretation": selected,
            "warnings": list(latest.get("warnings") or []),
            "snapshot_id": str(latest["snapshot_id"]),
        }

    def history(self, event_id: str) -> list[dict[str, Any]]:
        values = self.market_store.list_event_method_research(event_id)
        for value in values:
            value["interpretations"] = (
                self.market_store.list_event_method_interpretations(
                    event_id,
                    research_snapshot_id=str(value["snapshot_id"]),
                )
            )
        return values

    def interpret_pending(
        self,
        *,
        event_ids: Iterable[str] | None = None,
        max_items: int = 5,
    ) -> dict[str, Any]:
        lock_path = self.market_store.root / "event-method-research-ai.lock"
        with FileLock(str(lock_path), timeout=600):
            return self._interpret_pending_locked(
                event_ids=event_ids,
                max_items=max_items,
            )

    def _interpret_pending_locked(
        self,
        *,
        event_ids: Iterable[str] | None = None,
        max_items: int = 5,
    ) -> dict[str, Any]:
        if self.interpreter is None:
            raise RuntimeError("event research AI interpreter is unavailable")
        wanted = set(event_ids or [])
        events = [
            event
            for event in self.event_store.load_events()
            if event.status in {"active", "completed"}
            and (not wanted or event.event_id in wanted)
        ]
        items: list[dict[str, Any]] = []
        metadata: dict[str, dict[str, Any]] = {}
        skipped: list[str] = []
        for event in events:
            latest = self.market_store.get_event_method_research(event.event_id)
            if latest is None or latest.get("status") == "unavailable":
                skipped.append(event.event_id)
                continue
            existing = self.market_store.list_event_method_interpretations(
                event.event_id,
                research_snapshot_id=str(latest["snapshot_id"]),
            )
            expected_input_hash = interpretation_input_hash(
                event.to_row(),
                str(latest["snapshot_id"]),
                prompt_version=self.interpreter.prompt_version,
            )
            if any(
                item.get("status") == "ready"
                and item.get("provider") == self.interpretation_provider
                and item.get("prompt_version")
                == self.interpreter.prompt_version
                and item.get("input_hash") == expected_input_hash
                for item in existing
            ):
                skipped.append(event.event_id)
                continue
            item = {
                "event": event.to_row(),
                "research": latest["payload"],
            }
            items.append(item)
            metadata[event.event_id] = latest
            if max_items <= 0:
                # max_items=0 means "process none" (a pause switch), not one.
                items.pop()
                break
            if len(items) >= min(max_items, 5):
                break
        if not items:
            return {
                "ok": True,
                "processed": 0,
                "created": [],
                "failed": [],
                "skipped": skipped,
                "errors": [],
            }
        try:
            outputs = self.interpreter.interpret_many(items)
        except Exception as exc:
            error = str(exc)[:2000]
            provider = self.interpretation_provider
            model = self.interpretation_model
            prompt_version = str(self.interpreter.prompt_version)
            failed: list[dict[str, Any]] = []
            for item in items:
                event = item["event"]
                event_id = str(event["event_id"])
                research = metadata[event_id]
                input_hash = interpretation_input_hash(
                    event,
                    str(research["snapshot_id"]),
                    prompt_version=prompt_version,
                )
                payload = {"status": "failed", "error": error}
                record_id = interpretation_id(
                    event_id=event_id,
                    research_snapshot_id=str(research["snapshot_id"]),
                    provider=provider,
                    prompt_version=prompt_version,
                    input_hash=input_hash,
                    payload=payload,
                )
                inserted = self.market_store.save_event_method_interpretation(
                    {
                        "interpretation_id": record_id,
                        "event_id": event_id,
                        "research_snapshot_id": str(research["snapshot_id"]),
                        "provider": provider,
                        "model": model,
                        "prompt_version": prompt_version,
                        "input_hash": input_hash,
                        "status": "failed",
                        "payload": {},
                        "validation": {"ok": False, "errors": [error]},
                        "error": error,
                        "created_at": now_iso(),
                    }
                )
                failed.append(
                    {
                        "event_id": event_id,
                        "interpretation_id": record_id,
                        "created": inserted,
                        "provider": provider,
                    }
                )
            return {
                "ok": False,
                "processed": len(items),
                "created": [],
                "failed": failed,
                "skipped": skipped,
                "errors": [{"error": error, "event_ids": sorted(metadata)}],
            }
        created: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        provider = self.interpretation_provider
        model = self.interpretation_model
        prompt_version = str(self.interpreter.prompt_version)
        for item in items:
            event = item["event"]
            event_id = str(event["event_id"])
            research = metadata[event_id]
            try:
                payload = outputs[event_id]
                input_hash = interpretation_input_hash(
                    event,
                    str(research["snapshot_id"]),
                    prompt_version=prompt_version,
                )
                record_id = interpretation_id(
                    event_id=event_id,
                    research_snapshot_id=str(research["snapshot_id"]),
                    provider=provider,
                    prompt_version=prompt_version,
                    input_hash=input_hash,
                    payload=payload,
                )
                inserted = self.market_store.save_event_method_interpretation(
                    {
                        "interpretation_id": record_id,
                        "event_id": event_id,
                        "research_snapshot_id": str(research["snapshot_id"]),
                        "provider": provider,
                        "model": model,
                        "prompt_version": prompt_version,
                        "input_hash": input_hash,
                        "status": "ready",
                        "payload": payload,
                        "validation": {"ok": True},
                        "error": "",
                        "created_at": now_iso(),
                    }
                )
                created.append(
                    {
                        "event_id": event_id,
                        "interpretation_id": record_id,
                        "created": inserted,
                        "provider": provider,
                    }
                )
            except Exception as exc:
                errors.append({"event_id": event_id, "error": str(exc)[:1000]})
        return {
            "ok": not errors,
            "processed": len(items),
            "created": created,
            "failed": [],
            "skipped": skipped,
            "errors": errors,
        }

    def doctor(self) -> dict[str, Any]:
        events = [
            event
            for event in self.event_store.load_events()
            if event.status in {"active", "completed"}
        ]
        latest = {
            event.event_id: self.market_store.get_event_method_research(
                event.event_id
            )
            for event in events
        }
        status_counts: dict[str, int] = {}
        interpreted = 0
        for event_id, research in latest.items():
            status = str((research or {}).get("status") or "missing")
            status_counts[status] = status_counts.get(status, 0) + 1
            if research and self.has_ready_interpretation(
                event_id,
                str(research["snapshot_id"]),
            ):
                interpreted += 1
        return {
            "ok": status_counts.get("missing", 0) == 0,
            "formal_events": len(events),
            "objective_status": status_counts,
            "ai_interpreted": interpreted,
            "ai_pending": len(events) - interpreted,
            "ai_provider": self.interpretation_provider,
            "cross_section_dates": len(
                {
                    str(item["trade_date"])
                    for item in self.market_store.list_cross_section_snapshots()
                    if item.get("status") == "ready"
                }
            ),
        }
