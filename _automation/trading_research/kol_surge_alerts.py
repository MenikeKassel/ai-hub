"""Alert on posts whose mentioned stocks surged within a week of the post.

Every post the console ingests yields stock leads (``stock_leads``).  This
module re-checks each recent (post, symbol) pair against the local market
warehouse and reports pairs whose price gained at least ``threshold``
(default 10%) within ``window_days`` (default 7) calendar days after the
post.  A small JSON state file deduplicates alerts and parks pairs whose
window has closed or whose symbol has no warehouse data.

Baseline rule: a post published before the close anchors on the previous
trading day's close (the post day's move counts); a post published after the
close anchors on the post day's own close.  Gains use the adjusted (qfq)
close series.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
CLOSE_TIME = clock_time(15, 0)
DEFAULT_THRESHOLD = 0.10
DEFAULT_WINDOW_DAYS = 7
DEFAULT_LOOKBACK_DAYS = 21
PARK_AFTER_GRACE_DAYS = 10


def _now_local() -> datetime:
    return datetime.now(SHANGHAI)


def _parse_posted_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _bars(frame: Any) -> list[tuple[date, float]]:
    """Normalise a price frame into an ordered (date, close) list."""
    dates = list(frame["date"])
    closes = list(frame["close"])
    bars: list[tuple[date, float]] = []
    for raw_date, raw_close in zip(dates, closes):
        try:
            day = raw_date.date() if hasattr(raw_date, "date") else date.fromisoformat(str(raw_date)[:10])
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if close > 0:
            bars.append((day, close))
    bars.sort(key=lambda item: item[0])
    return bars


def baseline_index(bars: list[tuple[date, float]], posted: datetime) -> int | None:
    """Index of the close that anchors the post's forward move."""
    if not bars:
        return None
    if posted.time() >= CLOSE_TIME:
        same_day = [index for index, (day, _) in enumerate(bars) if day == posted.date()]
        if same_day:
            return same_day[-1]
    earlier = [index for index, (day, _) in enumerate(bars) if day < posted.date()]
    return earlier[-1] if earlier else None


def evaluate_pair(
    bars: list[tuple[date, float]],
    posted: datetime,
    *,
    window_days: int,
    threshold: float,
    today: date,
) -> dict[str, Any]:
    """Evaluate one (post, symbol) pair against its forward bar window."""
    index = baseline_index(bars, posted)
    if index is None:
        return {"status": "no_data"}
    baseline_day, baseline_close = bars[index]
    window_end = posted.date() + timedelta(days=window_days)
    forward = [(day, close) for day, close in bars[index + 1 :] if day <= window_end]
    if not forward:
        closed = today > window_end + timedelta(days=PARK_AFTER_GRACE_DAYS)
        return {"status": "no_data" if closed else "pending"}
    best_day, best_close = max(forward, key=lambda item: item[1])
    max_gain = best_close / baseline_close - 1.0
    return {
        "status": "alert" if max_gain >= threshold else "checked",
        "max_gain": max_gain,
        "baseline_date": baseline_day.isoformat(),
        "baseline_close": baseline_close,
        "peak_date": best_day.isoformat(),
        "peak_close": best_close,
        "window_end": window_end.isoformat(),
        "window_closed": today > window_end,
    }


def recent_leads(post_store: Any, *, since: datetime, limit: int = 5000) -> list[dict[str, Any]]:
    """Recent stock leads joined with their post and KOL for context."""
    with post_store.connect() as db:
        rows = db.execute(
            """
            SELECT l.post_id, l.symbol, l.security_name, l.direction, l.status AS lead_status,
                   p.posted_at, p.url, substr(p.text, 1, 160) AS text_snippet,
                   k.display_name, k.handle
            FROM stock_leads l
            JOIN posts p ON p.post_id = l.post_id
            JOIN kols k ON k.id = l.kol_id
            WHERE p.posted_at >= ? AND l.status != 'ignored'
            ORDER BY p.posted_at DESC
            LIMIT ?
            """,
            (since.isoformat(timespec="seconds"), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def scan_surge_alerts(
    post_store: Any,
    price_provider: Any,
    *,
    state_path: Path,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Report posts whose stock gained ``threshold`` within ``window_days``."""
    moment = (now or _now_local()).astimezone(SHANGHAI)
    today = moment.date()
    state = _load_state(state_path)
    leads = recent_leads(post_store, since=moment - timedelta(days=max(1, lookback_days)))
    new_alerts: list[dict[str, Any]] = []
    errors: list[str] = []
    pending = expired = no_data = 0
    for lead in leads:
        key = f"{lead['post_id']}|{lead['symbol']}"
        existing = state.get(key)
        if isinstance(existing, dict) and existing.get("status") in {"alerted", "expired", "no_data"}:
            continue
        try:
            posted = _parse_posted_at(lead["posted_at"])
        except (TypeError, ValueError):
            errors.append(f"{lead['post_id']}: invalid posted_at")
            state[key] = {"status": "no_data", "checked_at": moment.isoformat(timespec="seconds")}
            no_data += 1
            continue
        try:
            frame = price_provider.fetch_stock(
                str(lead["symbol"]),
                posted.date() - timedelta(days=15),
                posted.date() + timedelta(days=window_days),
                adjusted=True,
            )
            outcome = evaluate_pair(
                _bars(frame),
                posted,
                window_days=window_days,
                threshold=threshold,
                today=today,
            )
        except Exception as exc:  # noqa: BLE001 - unknown symbols are expected
            errors.append(f"{lead['symbol']}: {type(exc).__name__}: {str(exc)[:120]}")
            if today > posted.date() + timedelta(days=window_days + PARK_AFTER_GRACE_DAYS):
                outcome = {"status": "no_data"}
            else:
                outcome = {"status": "pending"}
        status = outcome["status"]
        if status == "alert":
            record = {
                "post_id": lead["post_id"],
                "symbol": lead["symbol"],
                "security_name": lead.get("security_name") or "",
                "direction": lead.get("direction") or "",
                "kol": lead.get("display_name") or "",
                "handle": lead.get("handle") or "",
                "url": lead.get("url") or "",
                "posted_at": lead["posted_at"],
                "text_snippet": lead.get("text_snippet") or "",
                **{field: outcome[field] for field in ("max_gain", "baseline_date", "baseline_close", "peak_date", "peak_close", "window_end", "window_closed")},
            }
            new_alerts.append(record)
            state[key] = {
                "status": "alerted",
                "checked_at": moment.isoformat(timespec="seconds"),
                "max_gain": outcome["max_gain"],
                "baseline_date": outcome["baseline_date"],
                "peak_date": outcome["peak_date"],
            }
        elif status == "checked" and outcome.get("window_closed"):
            state[key] = {
                "status": "expired",
                "checked_at": moment.isoformat(timespec="seconds"),
                "max_gain": outcome["max_gain"],
            }
            expired += 1
        elif status == "checked":
            # Still inside the window and below threshold: re-check next run.
            continue
        elif status == "no_data":
            state[key] = {"status": "no_data", "checked_at": moment.isoformat(timespec="seconds")}
            no_data += 1
        else:
            pending += 1
    if not dry_run:
        _save_state(state_path, state)
    new_alerts.sort(key=lambda item: item.get("max_gain") or 0.0, reverse=True)
    return {
        "ok": True,
        "generated_at": moment.isoformat(timespec="seconds"),
        "state_path": str(state_path),
        "lookback_days": lookback_days,
        "window_days": window_days,
        "threshold": threshold,
        "pairs": len(leads),
        "new_alerts": new_alerts,
        "pending": pending,
        "expired": expired,
        "no_data": no_data,
        "errors": errors[:10],
        "dry_run": dry_run,
    }


def format_surge_message(alerts: list[dict[str, Any]], *, window_days: int, threshold: float) -> str:
    """Build the Hermes/Feishu push message for new surge alerts."""
    lines = [f"[KOL研究台] 帖子兑现提醒（帖后{window_days}日内涨幅≥{threshold:.0%}）："]
    shown = alerts[:5]
    for index, item in enumerate(shown, start=1):
        gain = item.get("max_gain")
        pct = f"+{gain:.1%}" if isinstance(gain, float) else "?"
        lines.append(
            f"{index}. @{item.get('handle') or item.get('kol')} {item.get('security_name') or ''}({item.get('symbol')}) "
            f"{pct}｜峰值 {item.get('peak_date')} 收 {item.get('peak_close')}（基准 {item.get('baseline_date')} 收 {item.get('baseline_close')}）\n"
            f"   帖子 {str(item.get('posted_at'))[:16]} {item.get('url')}"
        )
    if len(alerts) > len(shown):
        lines.append(f"共 {len(alerts)} 条，其余见状态文件；阈值 {threshold:.0%}/{window_days}天，价格源=market_warehouse(qfq)")
    return "\n".join(lines)
