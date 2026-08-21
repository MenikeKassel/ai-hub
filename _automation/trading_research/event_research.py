from __future__ import annotations

import hashlib
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from event_context import event_context_cutoff
from market_indicators import compute_daily_indicators
from market_cross_section import build_short_term_leader_lens


METHOD_RESEARCH_VERSION = "event-method-research-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
PROFILE_BINS = 24


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _posted_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("posted_at must include timezone")
    return parsed.astimezone(SHANGHAI)


def _normalise_daily(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    value = frame.copy()
    if "trade_date" not in value.columns and "date" in value.columns:
        value = value.rename(columns={"date": "trade_date"})
    required = {"trade_date", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(value.columns))
    if missing:
        raise ValueError("qfq history missing columns: " + ", ".join(missing))
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close", "preclose", "volume", "amount"):
        if column in value.columns:
            value[column] = pd.to_numeric(value[column], errors="coerce")
    return (
        value.dropna(subset=["trade_date", "open", "high", "low", "close", "volume"])
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )


def _normalise_minutes(
    frame: pd.DataFrame,
    *,
    posted_at: str,
    completed_session_date: str,
) -> tuple[pd.DataFrame, str, str]:
    if frame.empty or "trade_datetime" not in frame.columns:
        return pd.DataFrame(), "", "unavailable"
    value = frame.copy()
    parsed = pd.to_datetime(value["trade_datetime"], errors="coerce")
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_convert(SHANGHAI).dt.tz_localize(None)
    value["trade_datetime"] = parsed
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in value.columns:
            value[column] = pd.to_numeric(value[column], errors="coerce")
    required = {"open", "high", "low", "close", "volume"}
    if required - set(value.columns):
        return pd.DataFrame(), "", "unavailable"
    value = value.dropna(subset=["trade_datetime", *sorted(required)])
    posted = _posted_at(posted_at).replace(tzinfo=None)
    use_current = posted.time() >= time(9, 30) and posted.weekday() < 5
    current = value[value["trade_datetime"].dt.date == posted.date()]
    if use_current and not current.empty:
        current = current[current["trade_datetime"] <= posted]
        if not current.empty:
            session = current
            session_date = posted.date().isoformat()
            mode = (
                "complete_session"
                if posted.time() >= time(15, 0)
                else "session_to_post"
            )
        else:
            session = pd.DataFrame()
            session_date = ""
            mode = "unavailable"
    else:
        session = pd.DataFrame()
        session_date = ""
        mode = "unavailable"
    if session.empty:
        session = value[
            value["trade_datetime"].dt.date.astype(str) == completed_session_date
        ]
        if not session.empty:
            session_date = completed_session_date
            mode = "prior_complete_session"
    session = (
        session.sort_values("trade_datetime")
        .drop_duplicates("trade_datetime", keep="last")
        .reset_index(drop=True)
    )
    return (
        session,
        session_date,
        mode,
    )


def _frame_hash(frame: pd.DataFrame, columns: list[str]) -> str:
    if frame.empty:
        return ""
    available = [column for column in columns if column in frame.columns]
    payload = frame[available].to_csv(index=False, float_format="%.12g")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _session_bucket(posted: datetime) -> str:
    if posted.weekday() >= 5:
        return "non_trading_day"
    current = posted.time()
    if current < time(9, 30):
        return "pre_open"
    if current < time(11, 30):
        return "morning_session"
    if current < time(13, 0):
        return "lunch_break"
    if current < time(15, 0):
        return "afternoon_session"
    return "post_close"


def _pivot_points(frame: pd.DataFrame, span: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    if len(frame) < span * 2 + 1:
        return highs, lows
    high_values = frame["high"].astype(float)
    low_values = frame["low"].astype(float)
    for index in range(span, len(frame) - span):
        high_window = high_values.iloc[index - span : index + span + 1]
        low_window = low_values.iloc[index - span : index + span + 1]
        if float(high_values.iloc[index]) >= float(high_window.max()):
            highs.append({
                "index": index,
                "date": frame.iloc[index]["trade_date"].isoformat(),
                "price": float(high_values.iloc[index]),
            })
        if float(low_values.iloc[index]) <= float(low_window.min()):
            lows.append({
                "index": index,
                "date": frame.iloc[index]["trade_date"].isoformat(),
                "price": float(low_values.iloc[index]),
            })
    return highs, lows


def _trend_structure(highs: list[dict[str, Any]], lows: list[dict[str, Any]]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "undetermined"
    higher_high = highs[-1]["price"] > highs[-2]["price"]
    higher_low = lows[-1]["price"] > lows[-2]["price"]
    lower_high = highs[-1]["price"] < highs[-2]["price"]
    lower_low = lows[-1]["price"] < lows[-2]["price"]
    if higher_high and higher_low:
        return "higher_high_higher_low"
    if lower_high and lower_low:
        return "lower_high_lower_low"
    return "mixed_or_range"


def _fib_context(
    frame: pd.DataFrame,
    highs: list[dict[str, Any]],
    lows: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if highs and lows:
        high_anchor = highs[-1]
        low_anchor = lows[-1]
    else:
        window = frame.tail(60)
        high_index = int(window["high"].astype(float).idxmax())
        low_index = int(window["low"].astype(float).idxmin())
        high_anchor = {
            "index": high_index,
            "date": frame.loc[high_index, "trade_date"].isoformat(),
            "price": float(frame.loc[high_index, "high"]),
        }
        low_anchor = {
            "index": low_index,
            "date": frame.loc[low_index, "trade_date"].isoformat(),
            "price": float(frame.loc[low_index, "low"]),
        }
        warnings.append("swing_anchors_fallback_60d")
    high = float(high_anchor["price"])
    low = float(low_anchor["price"])
    distance = high - low
    direction = "upswing" if low_anchor["index"] < high_anchor["index"] else "downswing"
    ratios = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    levels: list[dict[str, float]] = []
    if distance > 0:
        for ratio in ratios:
            price = high - distance * ratio if direction == "upswing" else low + distance * ratio
            levels.append({"ratio": ratio, "price": price})
        position = (float(frame.iloc[-1]["close"]) - low) / distance
    else:
        position = None
        warnings.append("zero_swing_range")
    return {
        "direction": direction,
        "low_anchor": low_anchor,
        "high_anchor": high_anchor,
        "levels": levels,
        "close_position": position,
        "premium_discount": (
            "premium" if position is not None and position > 0.5
            else "discount" if position is not None and position < 0.5
            else "equilibrium" if position is not None
            else "unavailable"
        ),
    }, warnings


def _overlap_ratio(frame: pd.DataFrame) -> float | None:
    if len(frame) < 2:
        return None
    values: list[float] = []
    for index in range(1, len(frame)):
        previous = frame.iloc[index - 1]
        current = frame.iloc[index]
        intersection = max(
            0.0,
            min(float(previous["high"]), float(current["high"]))
            - max(float(previous["low"]), float(current["low"])),
        )
        union = max(float(previous["high"]), float(current["high"])) - min(
            float(previous["low"]), float(current["low"])
        )
        if union > 0:
            values.append(intersection / union)
    return float(np.mean(values)) if values else None


def _price_action(frame: pd.DataFrame, trend: str) -> dict[str, Any]:
    window = frame.tail(21)
    closes = window["close"].astype(float)
    path = float(closes.diff().abs().sum())
    efficiency = abs(float(closes.iloc[-1] - closes.iloc[0])) / path if path > 0 else 0.0
    overlap = _overlap_ratio(window)
    latest = window.iloc[-1]
    previous = window.iloc[:-1]
    breakout = "none"
    if not previous.empty and float(latest["close"]) > float(previous["high"].max()):
        breakout = "up"
    elif not previous.empty and float(latest["close"]) < float(previous["low"].min()):
        breakout = "down"
    spread = float(latest["high"] - latest["low"])
    body_ratio = abs(float(latest["close"] - latest["open"])) / spread if spread > 0 else None
    close_location = (
        (float(latest["close"]) - float(latest["low"])) / spread
        if spread > 0 else None
    )
    if breakout != "none":
        regime = f"breakout_{breakout}"
    elif trend in {"higher_high_higher_low", "lower_high_lower_low"} and efficiency >= 0.3:
        regime = "trend"
    elif efficiency <= 0.25 or (overlap is not None and overlap >= 0.55):
        regime = "trading_range"
    else:
        regime = "transition"
    return {
        "status": "ready" if len(window) >= 21 else "partial",
        "label": "PA 价格行为",
        "conclusion": "descriptive_only",
        "facts": {
            "regime": regime,
            "trend_structure": trend,
            "directional_efficiency_20": efficiency,
            "bar_overlap_20": overlap,
            "breakout_vs_prior_20": breakout,
            "latest_body_ratio": body_ratio,
            "latest_close_location": close_location,
        },
        "observations": [],
        "warnings": ["price_only_interpretation_not_signal"],
    }


def _latest_fvg(frame: pd.DataFrame) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    for index in range(2, len(frame)):
        current = frame.iloc[index]
        previous_two = frame.iloc[index - 2]
        if float(current["low"]) > float(previous_two["high"]):
            result = {
                "kind": "bullish_gap",
                "date": current["trade_date"].isoformat(),
                "low": float(previous_two["high"]),
                "high": float(current["low"]),
            }
        elif float(current["high"]) < float(previous_two["low"]):
            result = {
                "kind": "bearish_gap",
                "date": current["trade_date"].isoformat(),
                "low": float(current["high"]),
                "high": float(previous_two["low"]),
            }
    return result


def _value_area(
    counts: np.ndarray,
    edges: np.ndarray,
    *,
    percentage: float = 0.7,
) -> tuple[float | None, float | None, float | None]:
    total = float(counts.sum())
    if total <= 0:
        return None, None, None
    poc = int(np.argmax(counts))
    selected: list[int] = []
    cumulative = 0.0
    for index in np.argsort(counts)[::-1]:
        if counts[index] <= 0:
            continue
        selected.append(int(index))
        cumulative += float(counts[index])
        if cumulative >= total * percentage:
            break
    return (
        float((edges[poc] + edges[poc + 1]) / 2),
        float(edges[min(selected)]),
        float(edges[max(selected) + 1]),
    )


def _profile_edges(frame: pd.DataFrame) -> np.ndarray | None:
    if frame.empty or not {"low", "high"}.issubset(frame.columns):
        return None
    low = float(frame["low"].min())
    high = float(frame["high"].max())
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        return None
    return np.linspace(low, high, PROFILE_BINS + 1)


def _volume_profile(frame: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "method": "bar_approximation",
        "poc": None,
        "value_area_low": None,
        "value_area_high": None,
        "bins": PROFILE_BINS,
    }
    edges = _profile_edges(frame)
    if edges is None:
        return empty
    typical = (
        frame["high"].astype(float)
        + frame["low"].astype(float)
        + frame["close"].astype(float)
    ) / 3.0
    indexes = np.clip(np.searchsorted(edges, typical, side="right") - 1, 0, PROFILE_BINS - 1)
    counts = np.bincount(
        indexes,
        weights=frame["volume"].astype(float),
        minlength=PROFILE_BINS,
    )
    poc, value_low, value_high = _value_area(counts, edges)
    return {
        **empty,
        "poc": poc,
        "value_area_low": value_low,
        "value_area_high": value_high,
    }


def _tpo_profile(frame: pd.DataFrame) -> dict[str, Any]:
    empty = {
        "method": "30m_bar_range_approximation",
        "poc": None,
        "value_area_low": None,
        "value_area_high": None,
        "initial_balance_low": None,
        "initial_balance_high": None,
        "rotation_factor": None,
        "bins": PROFILE_BINS,
    }
    edges = _profile_edges(frame)
    if edges is None:
        return empty
    blocks = (
        frame.assign(block=frame["trade_datetime"].dt.floor("30min"))
        .groupby("block", as_index=False)
        .agg(high=("high", "max"), low=("low", "min"))
        .sort_values("block")
    )
    counts = np.zeros(PROFILE_BINS, dtype=float)
    for row in blocks.itertuples(index=False):
        touched = np.where((edges[:-1] <= float(row.high)) & (edges[1:] >= float(row.low)))[0]
        counts[touched] += 1.0
    poc, value_low, value_high = _value_area(counts, edges)
    rotation = 0
    for index in range(1, len(blocks)):
        rotation += int(np.sign(float(blocks.iloc[index]["high"]) - float(blocks.iloc[index - 1]["high"])))
        rotation += int(np.sign(float(blocks.iloc[index]["low"]) - float(blocks.iloc[index - 1]["low"])))
    initial = blocks.head(2)
    return {
        **empty,
        "poc": poc,
        "value_area_low": value_low,
        "value_area_high": value_high,
        "initial_balance_low": float(initial["low"].min()) if not initial.empty else None,
        "initial_balance_high": float(initial["high"].max()) if not initial.empty else None,
        "rotation_factor": rotation if len(blocks) >= 2 else None,
    }


def _orderflow_lens(
    frame: pd.DataFrame,
    minute: pd.DataFrame,
    *,
    instrument_type: str,
) -> dict[str, Any]:
    latest = frame.iloc[-1]
    spread = float(latest["high"] - latest["low"])
    close_location_value = (
        ((float(latest["close"] - latest["low"])) - (float(latest["high"] - latest["close"]))) / spread
        if spread > 0 else None
    )
    atr = _finite(latest.get("atr14"))
    body_atr = (
        abs(float(latest["close"] - latest["open"])) / atr
        if atr and atr > 0 else None
    )
    spread_atr = spread / atr if atr and atr > 0 else None
    volume_ratio = _finite(latest.get("volume_ratio_5"))
    if volume_ratio is not None and volume_ratio >= 1.5 and body_atr is not None and body_atr < 0.5:
        effort_result = "high_effort_low_result"
    elif volume_ratio is not None and volume_ratio >= 1.5 and body_atr is not None and body_atr >= 1.0:
        effort_result = "high_effort_expansion"
    else:
        effort_result = "neutral_or_insufficient"

    warnings = [
        "wyckoff_phase_requires_manual_interpretation",
        "cvd_unavailable_without_aggressor_side_trades",
        (
            "option_wall_not_applicable_for_stock"
            if instrument_type == "stock"
            else "option_wall_unavailable_without_option_open_interest"
        ),
    ]
    session_vwap = None
    vwap_distance = None
    profile = _volume_profile(pd.DataFrame())
    tpo = _tpo_profile(pd.DataFrame())
    if minute.empty:
        warnings.append("minute_session_unavailable")
    else:
        typical = (
            minute["high"].astype(float)
            + minute["low"].astype(float)
            + minute["close"].astype(float)
        ) / 3.0
        volume = minute["volume"].astype(float)
        denominator = float(volume.sum())
        if denominator > 0:
            session_vwap = float((typical * volume).sum() / denominator)
            vwap_distance = float(minute.iloc[-1]["close"]) / session_vwap - 1.0
        profile = _volume_profile(minute)
        tpo = _tpo_profile(minute)
    return {
        "status": "partial",
        "label": "威科夫 / 订单流",
        "conclusion": "manual_phase_review",
        "facts": {
            "wyckoff": {
                "phase": "manual_required",
                "close_location_value": close_location_value,
                "spread_atr": spread_atr,
                "body_atr": body_atr,
                "volume_ratio_5": volume_ratio,
                "effort_result": effort_result,
            },
            "vwap": {
                "session_vwap": session_vwap,
                "close_distance": vwap_distance,
                "method": "typical_price_volume_weighted",
            },
            "volume_profile": profile,
            "tpo": tpo,
            "cvd": {
                "available": False,
                "status": "unavailable",
                "reason": "requires aggressor-side trade data",
            },
            "option_wall": {
                "available": False,
                "status": (
                    "not_applicable"
                    if instrument_type == "stock"
                    else "unavailable"
                ),
                "reason": (
                    "ordinary A-share stocks do not have a stock-specific option wall"
                    if instrument_type == "stock"
                    else "requires option open-interest by strike"
                ),
            },
        },
        "observations": [],
        "warnings": warnings,
    }


def _unavailable_result(
    *,
    event_id: str,
    symbol: str,
    posted_at: str,
    computed_at: str,
    warning: str,
) -> dict[str, Any]:
    labels = {
        "short_term_leader": "短线龙头",
        "dow_wave_gann": "道氏 / 波浪 / 江恩",
        "price_action": "PA 价格行为",
        "ict": "ICT 时间与流动性",
        "wyckoff_orderflow": "威科夫 / 订单流",
    }
    return {
        "version": METHOD_RESEARCH_VERSION,
        "event_id": event_id,
        "symbol": symbol,
        "posted_at": posted_at,
        "as_of_trade_date": "",
        "status": "unavailable",
        "lenses": {
            key: {
                "status": "unavailable",
                "label": label,
                "conclusion": "not_determined",
                "facts": {},
                "observations": [],
                "warnings": [warning],
            }
            for key, label in labels.items()
        },
        "data_lineage": {
            "daily_adjustment": "qfq",
            "daily_bars": 0,
            "daily_source_hash": "",
            "minute_session_date": "",
            "minute_bars": 0,
            "minute_source_hash": "",
        },
        "warnings": [warning],
        "computed_at": computed_at,
    }


def analyze_event_methods(
    *,
    event_id: str,
    symbol: str,
    posted_at: str,
    qfq_daily: pd.DataFrame,
    minute_bars: pd.DataFrame | None = None,
    cross_section_snapshots: pd.DataFrame | None = None,
    board_rows: list[dict[str, Any]] | None = None,
    board_members: dict[str, list[str]] | None = None,
    instrument_type: str = "stock",
    daily_lineage: dict[str, Any] | None = None,
    leader_universe: dict[str, Any] | None = None,
    computed_at: str | None = None,
) -> dict[str, Any]:
    """Build point-in-time research evidence without producing a trading signal."""
    timestamp = computed_at or datetime.now(SHANGHAI).isoformat(timespec="microseconds")
    daily = _normalise_daily(qfq_daily)
    if daily.empty:
        return _unavailable_result(
            event_id=event_id,
            symbol=symbol,
            posted_at=posted_at,
            computed_at=timestamp,
            warning="qfq_history_unavailable_at_event",
        )
    cutoff = event_context_cutoff(posted_at)
    history = daily[daily["trade_date"] <= cutoff].reset_index(drop=True)
    if history.empty:
        return _unavailable_result(
            event_id=event_id,
            symbol=symbol,
            posted_at=posted_at,
            computed_at=timestamp,
            warning="qfq_history_unavailable_at_event",
        )
    history = compute_daily_indicators(history)
    latest = history.iloc[-1]
    as_of = latest["trade_date"].isoformat()
    minute, minute_session_date, minute_mode = _normalise_minutes(
        minute_bars if minute_bars is not None else pd.DataFrame(),
        posted_at=posted_at,
        completed_session_date=as_of,
    )
    highs, lows = _pivot_points(history)
    trend = _trend_structure(highs, lows)
    fib, fib_warnings = _fib_context(history, highs, lows)
    close = float(latest["close"])
    ma20 = _finite(latest.get("ma20"))
    ma60 = _finite(latest.get("ma60"))
    volume_ratio = _finite(latest.get("volume_ratio_5"))
    return_20d = _finite(latest.get("return_20d"))
    distance_60d = _finite(latest.get("distance_60d_high"))

    changes = history["close"].astype(float).pct_change()
    if symbol.startswith(("300", "301", "688", "689")):
        approximate_limit_threshold = 0.195
    elif symbol.startswith(("4", "8", "92")):
        approximate_limit_threshold = 0.295
    else:
        approximate_limit_threshold = 0.095
    limit_streak = 0
    for value in reversed(changes.dropna().tolist()):
        if not np.isfinite(value):
            # pct_change can yield inf when the previous close is zero, or
            # NaN from suspended days; neither is a limit move.
            continue
        if float(value) >= approximate_limit_threshold:
            limit_streak += 1
        else:
            break
    board_values = board_rows or []
    board_rps50 = max(
        (_finite(item.get("rps_50")) for item in board_values),
        default=None,
        key=lambda value: -float("inf") if value is None else value,
    )
    observations: list[str] = []
    if distance_60d is not None and distance_60d >= -0.03:
        observations.append("near_60d_high")
    if return_20d is not None and return_20d > 0:
        observations.append("positive_20d_momentum")
    if volume_ratio is not None and volume_ratio >= 1.5:
        observations.append("volume_expansion")
    if ma20 is not None and ma60 is not None and close > ma20 and close > ma60:
        observations.append("above_ma20_ma60")
    if board_rps50 is not None and board_rps50 >= 87:
        observations.append("strong_board_rps50")

    previous = history.iloc[:-1].tail(20)
    prior_high = float(previous["high"].max()) if not previous.empty else None
    prior_low = float(previous["low"].min()) if not previous.empty else None
    latest_high = float(latest["high"])
    latest_low = float(latest["low"])
    buy_side_sweep = bool(prior_high and latest_high > prior_high and close < prior_high)
    sell_side_sweep = bool(prior_low and latest_low < prior_low and close > prior_low)

    leader_lens = build_short_term_leader_lens(
        symbol=symbol,
        snapshots=(
            cross_section_snapshots
            if cross_section_snapshots is not None
            else pd.DataFrame()
        ),
        latest_daily=latest.to_dict(),
        board_rows=board_values,
        board_members=board_members,
        prepared_universe=leader_universe,
    )
    leader_lens["facts"] = {
        **leader_lens.get("facts", {}),
        "return_20d": return_20d,
        "distance_60d_high": distance_60d,
        "volume_ratio_5": volume_ratio,
        "above_ma20_ma60": bool(
            ma20 is not None and ma60 is not None and close > ma20 and close > ma60
        ),
        "limit_move_streak_local_approx": limit_streak,
        "limit_move_threshold_local_approx": approximate_limit_threshold,
        "strongest_board_rps50": board_rps50,
        "board_context": board_values,
    }
    leader_candidates = leader_lens["facts"].get("candidate_types", {})
    leader_lens["facts"]["return_20d_percentile"] = (
        leader_candidates.get("trend_leader", {}).get(
            "return_20d_percentile"
        )
    )
    leader_lens["facts"]["amount_5d_percentile"] = (
        leader_candidates.get("liquidity_core", {}).get(
            "amount_5d_percentile"
        )
    )
    leader_lens["observations"] = list(
        dict.fromkeys([*leader_lens.get("observations", []), *observations])
    )

    lenses = {
        "short_term_leader": leader_lens,
        "dow_wave_gann": {
            "status": "partial",
            "label": "道氏 / 波浪 / 江恩",
            "conclusion": trend,
            "facts": {
                "trend_structure": trend,
                "ma5": _finite(latest.get("ma5")),
                "ma10": _finite(latest.get("ma10")),
                "ma20": ma20,
                "ma60": ma60,
                "atr14": _finite(latest.get("atr14")),
                "atr14_pct": _finite(latest.get("atr14_pct")),
                "confirmed_pivot_highs": highs[-3:],
                "confirmed_pivot_lows": lows[-3:],
                "fib_levels": fib["levels"],
                "fib_context": fib,
                "wave_count": {
                    "available": False,
                    "reason": "wave counts are path-dependent and require analyst confirmation",
                },
                "gann_geometry": {
                    "available": False,
                    "reason": "no single deterministic Gann specification is configured",
                },
            },
            "observations": [],
            "warnings": [
                *fib_warnings,
                "wave_count_requires_manual_interpretation",
                "gann_rules_not_standardized",
            ],
        },
        "price_action": _price_action(history, trend),
        "ict": {
            "status": "partial",
            "label": "ICT 时间与流动性",
            "conclusion": "descriptive_only",
            "facts": {
                "post_session": _session_bucket(_posted_at(posted_at)),
                "prior_20_high": prior_high,
                "prior_20_low": prior_low,
                "distance_to_prior_20_high": close / prior_high - 1.0 if prior_high else None,
                "distance_to_prior_20_low": close / prior_low - 1.0 if prior_low else None,
                "buy_side_liquidity_sweep": buy_side_sweep,
                "sell_side_liquidity_sweep": sell_side_sweep,
                "dealing_range": fib,
                "latest_three_bar_gap": _latest_fvg(history.tail(80)),
            },
            "observations": [],
            "warnings": [
                *fib_warnings,
                "ict_interpretation_is_discretionary",
                "liquidity_levels_are_price_proxies_not_order_book_liquidity",
            ],
        },
        "wyckoff_orderflow": _orderflow_lens(
            history,
            minute,
            instrument_type=instrument_type,
        ),
    }
    lineage = dict(daily_lineage or {})
    warnings = list(
        dict.fromkeys(
            [
                *(
                    warning
                    for lens in lenses.values()
                    for warning in lens.get("warnings", [])
                ),
                *(lineage.get("warnings") or []),
            ]
        )
    )
    return {
        "version": METHOD_RESEARCH_VERSION,
        "event_id": event_id,
        "symbol": symbol,
        "posted_at": posted_at,
        "as_of_trade_date": as_of,
        "status": "partial",
        "lenses": lenses,
        "data_lineage": {
            "daily_adjustment": "qfq",
            "daily_price_scale_basis": lineage.get(
                "daily_price_scale_basis",
                "provider_qfq",
            ),
            "daily_price_scale": lineage.get("daily_price_scale"),
            "daily_raw_cutoff_close": lineage.get("daily_raw_cutoff_close"),
            "daily_qfq_cutoff_close": lineage.get("daily_qfq_cutoff_close"),
            "daily_bars": len(history),
            "daily_source_hash": _frame_hash(
                history,
                ["trade_date", "open", "high", "low", "close", "volume"],
            ),
            "minute_adjustment": "raw",
            "minute_profile_method": "bar_approximation",
            "minute_session_date": minute_session_date,
            "minute_session_mode": minute_mode,
            "minute_bars": len(minute),
            "minute_source_hash": _frame_hash(
                minute,
                ["trade_datetime", "open", "high", "low", "close", "volume"],
            ),
        },
        "warnings": warnings,
        "computed_at": timestamp,
    }
