"""Legacy KOL leaderboard: per-KOL tier/rank over verified long-only checkpoints.

Return samples (samples/median_excess/win_rate/rank) use long events only.
Row-level counts are directional and share the kol_performance caliber:
- event_count / executable_event_count: legacy totals (all active/completed
  events; executable = long + is_executable_event), kept for compatibility.
- long_event_count / short_event_count: active/completed events split by
  direction; short events are audit-retained but never enter samples or rank.
- executable_long_event_count: long events that are executable and free of
  primary execution warnings (same rule as kol_performance).
- audit_event_count = long_event_count + short_event_count.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean, median
from typing import Any, Iterable

from kol_tracker import EventRecord, PRIMARY_WARNINGS, is_executable_event, is_long_event


HORIZONS = ("1W", "1M", "3M", "6M")


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _warning_tokens(event: EventRecord) -> set[str]:
    return {item.strip() for item in event.execution_warning.split(";") if item.strip()}


def _is_executable_long(event: EventRecord) -> bool:
    """Long events that are executable and free of primary execution warnings.

    Same caliber as kol_performance._is_executable_long so both pipelines count
    the executable long universe identically.
    """
    return (
        is_long_event(event)
        and is_executable_event(event)
        and not (_warning_tokens(event) & PRIMARY_WARNINGS)
    )


def _metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        return {
            "samples": 0,
            "median_return": None,
            "mean_return": None,
            "median_excess": None,
            "mean_excess": None,
            "win_rate": None,
            "median_adverse": None,
        }
    returns = [_number(row.get("directional_return")) for row in rows]
    excess = [_number(row.get("directional_excess_return")) for row in rows]
    adverse = [_number(row.get("max_adverse_return")) for row in rows]
    return {
        "samples": len(rows),
        "median_return": median(returns),
        "mean_return": fmean(returns),
        "median_excess": median(excess),
        "mean_excess": fmean(excess),
        "win_rate": sum(value > 0 for value in excess) / len(excess),
        "median_adverse": median(adverse),
    }


def _tier(horizons: dict[str, dict[str, Any]]) -> tuple[str, str]:
    if horizons["6M"]["samples"] >= 10:
        return "long_term", "6M"
    if horizons["3M"]["samples"] >= 20:
        return "reliable", "3M"
    if horizons["1M"]["samples"] >= 10:
        return "provisional", "1M"
    if max(value["samples"] for value in horizons.values()) >= 5:
        return "watch", max(HORIZONS, key=lambda item: horizons[item]["samples"])
    return "collecting", max(HORIZONS, key=lambda item: horizons[item]["samples"])


def _score(metrics: dict[str, Any]) -> float | None:
    samples = int(metrics["samples"] or 0)
    if samples == 0:
        return None
    shrinkage = samples / (samples + 10)
    performance = (
        0.60 * float(metrics["median_excess"])
        + 0.25 * float(metrics["mean_excess"])
        + 0.15 * (float(metrics["win_rate"]) - 0.5)
    )
    downside = 0.20 * abs(min(float(metrics["median_adverse"]), 0.0))
    return round(100 * shrinkage * (performance - downside), 4)


def build_kol_leaderboard(
    events: Iterable[EventRecord],
    checkpoints: Iterable[dict[str, str]],
    *,
    kol_names: Iterable[str] = (),
) -> list[dict[str, Any]]:
    event_rows = list(events)
    eligible = {
        event.event_id: event
        for event in event_rows
        if event.status in {"active", "completed"} and is_long_event(event) and is_executable_event(event)
    }
    all_events: dict[str, int] = defaultdict(int)
    executable_events: dict[str, int] = defaultdict(int)
    long_events: dict[str, int] = defaultdict(int)
    short_events: dict[str, int] = defaultdict(int)
    executable_long_events: dict[str, int] = defaultdict(int)
    for event in event_rows:
        if event.status not in {"active", "completed"}:
            continue
        all_events[event.kol_name] += 1
        if is_long_event(event):
            long_events[event.kol_name] += 1
            if event.event_id in eligible and _is_executable_long(event):
                executable_long_events[event.kol_name] += 1
        else:
            short_events[event.kol_name] += 1
        if event.event_id in eligible:
            executable_events[event.kol_name] += 1

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for checkpoint in checkpoints:
        event = eligible.get(str(checkpoint.get("event_id") or ""))
        horizon = str(checkpoint.get("horizon") or "")
        if event is None or horizon not in HORIZONS:
            continue
        if checkpoint.get("verification_status") != "verified":
            continue
        grouped[(event.kol_name, horizon)].append(checkpoint)

    names = set(kol_names) | set(all_events)
    result: list[dict[str, Any]] = []
    for name in sorted(names):
        horizons = {horizon: _metrics(grouped[(name, horizon)]) for horizon in HORIZONS}
        tier, rank_horizon = _tier(horizons)
        result.append(
            {
                "kol_name": name,
                "tier": tier,
                "rank": None,
                "rank_horizon": rank_horizon,
                "score": _score(horizons[rank_horizon]),
                "event_count": all_events[name],
                "executable_event_count": executable_events[name],
                "long_event_count": long_events[name],
                "short_event_count": short_events[name],
                "executable_long_event_count": executable_long_events[name],
                "audit_event_count": long_events[name] + short_events[name],
                "horizons": horizons,
            }
        )

    qualified = [item for item in result if item["tier"] in {"provisional", "reliable", "long_term"}]
    qualified.sort(
        key=lambda item: (
            item["score"] if item["score"] is not None else float("-inf"),
            item["horizons"][item["rank_horizon"]]["samples"],
        ),
        reverse=True,
    )
    for rank, item in enumerate(qualified, 1):
        item["rank"] = rank

    tier_order = {"long_term": 0, "reliable": 1, "provisional": 2, "watch": 3, "collecting": 4}
    return sorted(
        result,
        key=lambda item: (
            tier_order[item["tier"]],
            item["rank"] or 10_000,
            -item["executable_event_count"],
            item["kol_name"],
        ),
    )
