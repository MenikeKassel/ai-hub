from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from kol_tracker import KolStore
from market_data import FreeStockDBMarketProvider, MarketStore


SHANGHAI = ZoneInfo("Asia/Shanghai")
INTRADAY_VERSION = "intraday-v1"
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(15, 0)


def _posted_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _window_dates(store: MarketStore, posted: datetime) -> tuple[date | None, date | None, list[str]]:
    warnings: list[str] = []
    first = store.next_open_date(posted.date(), include_value=True)
    if first is None:
        return None, None, ["calendar_missing"]
    if first == posted.date() and MARKET_OPEN <= posted.time() < MARKET_CLOSE:
        effective = first
    else:
        effective = store.next_open_date(posted.date(), include_value=False)
        if effective is None:
            return None, None, ["next_open_date_missing"]
        warnings.append("outside_market_session")
    dates = store.open_dates_between(effective, effective + timedelta(days=370))
    if len(dates) < 5:
        return effective, None, [*warnings, "insufficient_open_sessions"]
    return effective, dates[4], warnings


def _price_at_or_after(frame: pd.DataFrame, target: float) -> float | None:
    values = frame[frame["elapsed_minutes"] >= target]
    if values.empty:
        return None
    value = pd.to_numeric(values.iloc[0].get("close"), errors="coerce")
    return float(value) if pd.notna(value) and value > 0 else None


def _elapsed_minutes(value: pd.Timestamp, session_dates: list[date]) -> float | None:
    """Return exchange-session minutes, excluding lunch and overnight gaps."""
    current = value.date()
    if current not in session_dates:
        return None
    minutes = value.hour * 60 + value.minute + value.second / 60 - 570
    if 0 <= minutes <= 120:
        session_offset = minutes
    elif 210 <= minutes <= 330:
        session_offset = minutes - 90
    else:
        return None
    return session_dates.index(current) * 240 + session_offset


def _window_is_complete(frame: pd.DataFrame, session_dates: list[date], window_end: date) -> bool:
    if not session_dates or frame.empty:
        return False
    available = set(frame["trade_datetime"].dt.date)
    if not set(session_dates).issubset(available):
        return False
    final = frame[frame["trade_datetime"].dt.date == window_end]
    if final.empty:
        return False
    # A complete session should contain the closing auction vicinity. The
    # feed's last bar may be 14:59 rather than a literal 15:00 bar.
    return bool((final["trade_datetime"].dt.time >= time(14, 55)).any())


def _source_hash(frame: pd.DataFrame) -> str:
    payload = frame.to_json(orient="records", date_format="iso", double_precision=12)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record(
    *,
    event: Any,
    effective_start: date,
    window_end: date,
    frame: pd.DataFrame,
    provider: str,
    warnings: list[str],
    status: str,
) -> dict[str, Any]:
    value = frame.sort_values("trade_datetime").copy()
    posted = _posted_at(event.posted_at)
    start_filter = pd.Timestamp(posted.replace(tzinfo=None)) if effective_start == posted.date() else pd.Timestamp.combine(effective_start, MARKET_OPEN)
    end_filter = pd.Timestamp.combine(window_end, MARKET_CLOSE)
    window = value[(value["trade_datetime"] >= start_filter) & (value["trade_datetime"] <= end_filter)].copy()
    if window.empty:
        return {
            "snapshot_id": f"{event.event_id}|{INTRADAY_VERSION}|{_source_hash(value)}",
            "event_id": event.event_id,
            "symbol": event.symbol,
            "direction": event.direction,
            "posted_at": event.posted_at,
            "effective_start": effective_start.isoformat(),
            "window_end": window_end.isoformat(),
            "provider": provider,
            "frequency": "1m",
            "adjustment": "raw",
            "first_bar_at": "",
            "first_price": None,
            "close_5m": None,
            "close_15m": None,
            "close_30m": None,
            "close_60m": None,
            "close_window": None,
            "mfe": None,
            "mae": None,
            "bars": 0,
            "status": "pending",
            "warnings_json": json.dumps([*warnings, "no_bars"], ensure_ascii=False),
            "source_hash": _source_hash(value),
            "computed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        }

    session_dates = sorted(set(window["trade_datetime"].dt.date))
    window["elapsed_minutes"] = window["trade_datetime"].map(
        lambda item: _elapsed_minutes(pd.Timestamp(item), session_dates)
    )
    window = window.dropna(subset=["elapsed_minutes"]).copy()
    if window.empty:
        return {
            "snapshot_id": f"{event.event_id}|{INTRADAY_VERSION}|{_source_hash(value)}",
            "event_id": event.event_id,
            "symbol": event.symbol,
            "direction": event.direction,
            "posted_at": event.posted_at,
            "effective_start": effective_start.isoformat(),
            "window_end": window_end.isoformat(),
            "provider": provider,
            "frequency": "1m",
            "adjustment": "raw",
            "first_bar_at": "",
            "first_price": None,
            "close_5m": None,
            "close_15m": None,
            "close_30m": None,
            "close_60m": None,
            "close_window": None,
            "mfe": None,
            "mae": None,
            "bars": 0,
            "status": "pending",
            "warnings_json": json.dumps([*warnings, "no_session_bars"], ensure_ascii=False),
            "source_hash": _source_hash(value),
            "computed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        }
    first = window.iloc[0]
    first_price = float(first["open"] if pd.notna(first.get("open")) else first["close"])
    if not first_price or first_price <= 0:
        warnings.append("invalid_first_price")
        first_price = None
    closes: dict[str, float | None] = {}
    if first_price is not None:
        first_elapsed = float(first["elapsed_minutes"])
        for label, minutes in (("5", 5), ("15", 15), ("30", 30), ("60", 60)):
            closes[label] = _price_at_or_after(window, first_elapsed + minutes)
        last = pd.to_numeric(window.iloc[-1].get("close"), errors="coerce")
        closes["window"] = float(last) if pd.notna(last) else None
        prices = pd.to_numeric(window["close"], errors="coerce").dropna()
        raw_returns = prices / first_price - 1
        if event.direction == "short":
            directional = -raw_returns
        else:
            directional = raw_returns
        mfe = float(directional.max()) if not directional.empty else None
        mae = float(directional.min()) if not directional.empty else None
    else:
        closes = {key: None for key in ("5", "15", "30", "60", "window")}
        mfe = mae = None

    gaps = window["trade_datetime"].sort_values().diff().dt.total_seconds().dropna()
    if not gaps.empty and float(gaps.max()) > 10 * 60:
        warnings.append("intraday_gap")
    if len(window["close"].dropna().unique()) <= 1:
        warnings.append("one_price_or_flat")
    thesis = str(getattr(event, "thesis", ""))
    if re.search("\\u7ad9\\u7a33|\\u7a81\\u7834|\\u56de\\u8e0f|\\u786e\\u8ba4\\u540e|\\u82e5|\\u5982\\u679c|\\u6761\\u4ef6", thesis):
        warnings.append("manual_condition_review")
    warnings = list(dict.fromkeys(warnings))
    return {
        "snapshot_id": f"{event.event_id}|{INTRADAY_VERSION}|{_source_hash(value)}",
        "event_id": event.event_id,
        "symbol": event.symbol,
        "direction": event.direction,
        "posted_at": event.posted_at,
        "effective_start": effective_start.isoformat(),
        "window_end": window_end.isoformat(),
        "provider": provider,
        "frequency": "1m",
        "adjustment": "raw",
        "first_bar_at": pd.Timestamp(first["trade_datetime"]).isoformat(),
        "first_price": first_price,
        "close_5m": closes["5"],
        "close_15m": closes["15"],
        "close_30m": closes["30"],
        "close_60m": closes["60"],
        "close_window": closes["window"],
        "mfe": mfe,
        "mae": mae,
        "bars": int(len(window)),
        "status": status,
        "warnings_json": json.dumps(warnings, ensure_ascii=False),
        "source_hash": _source_hash(value),
        "computed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
    }


def backfill_event_intraday(
    market_store: MarketStore,
    event_store: KolStore,
    *,
    provider: FreeStockDBMarketProvider | None = None,
    event_ids: Iterable[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    wanted = set(event_ids) if event_ids is not None else None
    owns_provider = provider is None
    provider = provider or FreeStockDBMarketProvider()
    summary: dict[str, Any] = {"created": [], "skipped": [], "pending": [], "errors": [], "processed": 0}
    events = [
        event for event in event_store.load_events()
        if event.status in {"active", "completed"} and (wanted is None or event.event_id in wanted)
    ]
    for event in events:
        summary["processed"] += 1
        existing = market_store.get_event_intraday_context(event.event_id)
        if existing is not None and existing.get("status") == "complete" and not force:
            summary["skipped"].append(event.event_id)
            continue
        try:
            posted = _posted_at(event.posted_at)
            effective, window_end, warnings = _window_dates(market_store, posted)
            if effective is None or window_end is None:
                record = {
                    "snapshot_id": f"{event.event_id}|{INTRADAY_VERSION}|pending",
                    "event_id": event.event_id,
                    "symbol": event.symbol,
                    "direction": event.direction,
                    "posted_at": event.posted_at,
                    "effective_start": effective.isoformat() if effective else "",
                    "window_end": window_end.isoformat() if window_end else "",
                    "provider": provider.name,
                    "frequency": "1m",
                    "adjustment": "raw",
                    "first_bar_at": "",
                    "first_price": None,
                    "close_5m": None,
                    "close_15m": None,
                    "close_30m": None,
                    "close_60m": None,
                    "close_window": None,
                    "mfe": None,
                    "mae": None,
                    "bars": 0,
                    "status": "pending",
                    "warnings_json": json.dumps(warnings, ensure_ascii=False),
                    "source_hash": "",
                    "computed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                }
                market_store.save_event_intraday_context(record, force=force)
                summary["pending"].append(event.event_id)
                continue
            instrument = market_store.get_instrument(event.symbol) or {}
            frame = provider.fetch_minute(
                event.symbol,
                str(instrument.get("instrument_type") or "stock"),
                effective,
                window_end,
                frequency="1m",
                adjustment="raw",
            )
            frame = frame[(frame["trade_datetime"].dt.date >= effective) & (frame["trade_datetime"].dt.date <= window_end)].copy()
            market_store.save_minute_snapshot(
                provider=provider.name,
                symbol=event.symbol,
                as_of=window_end,
                frequency="1m",
                adjustment="raw",
                frame=frame,
                parameters={"event_id": event.event_id, "effective_start": effective.isoformat(), "window_end": window_end.isoformat()},
            )
            session_dates = market_store.open_dates_between(effective, window_end)
            record = _record(
                event=event,
                effective_start=effective,
                window_end=window_end,
                frame=frame,
                provider=provider.name,
                warnings=warnings,
                status="complete" if _window_is_complete(frame, session_dates, window_end) else "pending",
            )
            if record["status"] == "pending" and frame is not None and not frame.empty:
                warnings_value = json.loads(record["warnings_json"])
                if "window_incomplete" not in warnings_value:
                    warnings_value.append("window_incomplete")
                    record["warnings_json"] = json.dumps(warnings_value, ensure_ascii=False)
            changed = market_store.save_event_intraday_context(record, force=force)
            if record["status"] == "pending":
                summary["pending"].append(event.event_id)
            elif changed:
                summary["created"].append(event.event_id)
            else:
                summary["skipped"].append(event.event_id)
        except Exception as exc:
            summary["errors"].append({"event_id": event.event_id, "error": str(exc)[:1000]})
    summary["ok"] = not summary["errors"]
    if owns_provider:
        provider.close()
    return summary


def audit_event_intraday(market_store: MarketStore, event_store: KolStore) -> dict[str, Any]:
    events = [event for event in event_store.load_events() if event.status in {"active", "completed"}]
    contexts: dict[str, dict[str, Any]] = {}
    for item in market_store.list_event_intraday_contexts():
        previous = contexts.get(item["event_id"])
        current_key = (str(item.get("computed_at") or ""), str(item.get("snapshot_id") or ""))
        previous_key = (
            (str(previous.get("computed_at") or ""), str(previous.get("snapshot_id") or ""))
            if previous
            else ("", "")
        )
        if previous is None or current_key > previous_key:
            contexts[item["event_id"]] = item
    missing = [event.event_id for event in events if event.event_id not in contexts]
    failed = [item for item in contexts.values() if item.get("status") == "failed"]
    return {
        "ok": not missing and not failed,
        "formal_events": len(events),
        "contexts": len(contexts),
        "complete": sum(item.get("status") == "complete" for item in contexts.values()),
        "pending": sum(item.get("status") == "pending" for item in contexts.values()),
        "missing_event_ids": missing,
        "failed": failed,
    }
