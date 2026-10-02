"""Read-only health projections shared by quick and detailed diagnostics."""
from __future__ import annotations

from datetime import datetime, timedelta

from kol_tracker import SHANGHAI


def research_health(store, market: dict) -> dict:
    from kol_sources.coverage import surface_coverage
    from research_workflow import ResearchWorkflow
    coverage = surface_coverage(store)
    index = ResearchWorkflow(store).status()
    market_status = market.get('daily_data_status', market.get('market_status','unknown'))
    known_bad = market_status not in {'current','checking','unknown'}
    data_status = 'degraded' if coverage['gap_count'] or known_bad else 'ready' if market_status == 'current' else 'unknown'
    delivery_status = 'degraded' if index['proposal_status'] == 'degraded' else index['status']
    overall = 'degraded' if 'degraded' in {data_status,delivery_status} else 'ready' if data_status == delivery_status == 'ready' else 'pending'
    return {'process_ready':True,'ok_scope':'process','business_ok':overall=='ready','overall_status':overall,
        'data_status':data_status,'delivery_status':delivery_status,'research_index':index,
        'source_coverage':{k:v for k,v in coverage.items() if k != 'items'}}


def model_health(store, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(SHANGHAI)
    cutoff = (current - timedelta(hours=24)).isoformat(timespec="seconds")
    with store.connect() as db:
        rows = db.execute(
            "SELECT model_status,COUNT(*) count FROM classifications "
            "WHERE updated_at>=? AND model_status IN ('completed','failed') GROUP BY model_status",
            (cutoff,),
        ).fetchall()
        latest = db.execute(
            "SELECT model_status,updated_at FROM classifications "
            "WHERE model_status IN ('completed','failed') ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        usage_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_daily_usage'"
        ).fetchone()
        usage = db.execute(
            "SELECT completed,failed FROM model_daily_usage WHERE usage_date=?",
            (current.astimezone(SHANGHAI).date().isoformat(),),
        ).fetchone() if usage_table else None
    counts = {row["model_status"]: int(row["count"]) for row in rows}
    failed = max(counts.get("failed", 0), int(usage["failed"]) if usage else 0)
    completed = max(counts.get("completed", 0), int(usage["completed"]) if usage else 0)
    return {
        "status": "degraded" if failed else "ready" if completed else "unknown",
        "recent_failed": failed,
        "recent_completed": completed,
        "window_hours": 24,
        "latest_status": latest["model_status"] if latest else "never",
        "latest_at": latest["updated_at"] if latest else "",
        "source": "classification_and_daily_usage",
    }


def model_component_health(
    store,
    budget: dict,
    *,
    credentials_configured: bool,
) -> dict:
    """Project one consistent AI status for quick and detailed diagnostics."""
    recent = model_health(store)
    attempted = int(budget.get("attempted") or 0)
    completed = int(budget.get("completed") or 0)
    failed = int(budget.get("failed") or 0)
    recent_completed = int(recent.get("recent_completed") or 0)
    recent_failed = int(recent.get("recent_failed") or 0)
    if (attempted and completed == 0 and failed) or (
        recent_failed and recent_completed == 0
    ):
        status = "unavailable"
    elif failed or recent_failed:
        status = "degraded"
    elif not credentials_configured:
        status = "not_configured"
    else:
        status = "ready"
    return {
        "status": status,
        "attempted": attempted,
        "completed": completed,
        "failed": failed,
        "recent_completed": recent_completed,
        "recent_failed": recent_failed,
        "updated_at": str(budget.get("updated_at") or ""),
    }


def apply_publication_health(value: dict, manifest: dict, coverage: list[dict]) -> None:
    """A complete historical publication does not imply current daily data."""
    symbols = {str(symbol) for symbol in manifest.get("published_symbols", []) if str(symbol).isdigit()}
    cutoff = str(manifest.get("as_of") or "")
    ends = {
        adjustment: {
            str(row.get("symbol")): str(row.get("end_date") or "")
            for row in coverage
            if row.get("dataset") == "daily" and row.get("adjustment") == adjustment
        }
        for adjustment in ("raw", "qfq")
    }
    lagging = sorted(symbol for symbol in symbols if any(ends[a].get(symbol, "") < cutoff for a in ends))
    complete = bool(symbols and cutoff and not lagging)
    value.update({
        "published_as_of": cutoff,
        "published_target_sequence_count": len(symbols),
        "published_raw_current_sequence_count": sum(ends["raw"].get(s, "") >= cutoff for s in symbols) if cutoff else 0,
        "published_qfq_current_sequence_count": sum(ends["qfq"].get(s, "") >= cutoff for s in symbols) if cutoff else 0,
        "published_lagging_symbols": lagging,
        "published_lagging_symbol_count": len(lagging),
        "published_coverage_status": "complete" if complete else "incomplete" if symbols and cutoff else "unknown",
    })
    expected = str(value.get("latest_open_date") or "")
    if symbols and expected and value.get("market_session_status") != "unknown":
        current_lagging = sorted(symbol for symbol in symbols if any(ends[a].get(symbol, "") < expected for a in ends))
        value["lagging_symbols"] = current_lagging
        value["lagging_symbol_count"] = len(current_lagging)
        value["daily_data_status"] = "provider_pending" if current_lagging else "current"
    value["market_status"] = value.get("daily_data_status", "unknown")
