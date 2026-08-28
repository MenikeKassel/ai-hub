from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


LEADER_RULE_VERSION = "short-term-leader-v1"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def normalise_cross_section(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    value = frame.copy()
    aliases = {
        "date": "trade_date",
        "code": "symbol",
        "pre_close": "preclose",
        "pct_chg": "pct_change_pct",
        "pctChg": "pct_change_pct",
    }
    value = value.rename(columns={key: target for key, target in aliases.items() if key in value})
    required = {"trade_date", "symbol", "close", "amount"}
    missing = sorted(required - set(value.columns))
    if missing:
        raise ValueError("cross section missing columns: " + ", ".join(missing))
    value["trade_date"] = pd.to_datetime(
        value["trade_date"].astype(str),
        format="mixed",
        errors="coerce",
    ).dt.date
    value["symbol"] = value["symbol"].astype(str).str.extract(r"(\d{6})", expand=False)
    for column in (
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "turnover",
        "pct_change_pct",
        "float_mv",
        "total_mv",
    ):
        if column in value:
            value[column] = pd.to_numeric(value[column], errors="coerce")
    if "pct_change_pct" in value:
        value["pct_change"] = value["pct_change_pct"] / 100.0
    elif "preclose" in value:
        value["pct_change"] = value["close"] / value["preclose"] - 1.0
    else:
        value["pct_change"] = np.nan
    # preclose of zero (suspended or bad data) yields inf; normalise to NaN.
    value["pct_change"] = value["pct_change"].replace(
        [float("inf"), float("-inf")], np.nan
    )
    if "is_st" not in value:
        value["is_st"] = False
    value["is_st"] = value["is_st"].map(
        lambda item: (
            item
            if isinstance(item, bool)
            else bool(float(item))
            if isinstance(item, (int, float)) and not isinstance(item, bool)
            else str(item).strip().lower() in {"1", "true", "yes", "y", "st"}
        )
    )
    value = value.dropna(subset=["trade_date", "symbol", "close", "amount"])
    value = value[value["symbol"].str.match(r"^(?:[03468]\d{5}|92\d{4})$")]
    return (
        value.sort_values(["trade_date", "symbol"])
        .drop_duplicates(["trade_date", "symbol"], keep="last")
        .reset_index(drop=True)
    )


def _limit_threshold(symbol: str, is_st: bool) -> float:
    if is_st:
        return 0.048
    if symbol.startswith(("300", "301", "688", "689")):
        return 0.195
    if symbol.startswith(("4", "8", "92")):
        return 0.295
    return 0.095


def _consecutive_limit_moves(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype="int64")
    values: dict[str, int] = {}
    for symbol, rows in frame.groupby("symbol", sort=False):
        streak = 0
        for row in rows.sort_values("trade_date", ascending=False).itertuples(index=False):
            change = _finite(getattr(row, "pct_change", None))
            if change is None or change < _limit_threshold(symbol, bool(getattr(row, "is_st", False))):
                break
            streak += 1
        values[str(symbol)] = streak
    return pd.Series(values, dtype="int64")


def _rank_percentile(values: pd.Series) -> pd.Series:
    return values.rank(method="average", ascending=True, pct=True) * 100.0


def _competition_rank(values: pd.Series) -> pd.Series:
    return values.rank(method="min", ascending=False, na_option="bottom").astype("Int64")


def _candidate_status(value: bool | None) -> str:
    if value is True:
        return "candidate"
    if value is False:
        return "not_candidate"
    return "not_determined"


def prepare_short_term_leader_universe(
    snapshots: pd.DataFrame,
) -> dict[str, Any]:
    """Compute date-level cross-sectional metrics once for many event symbols."""
    frame = normalise_cross_section(snapshots)
    if frame.empty:
        return {
            "dates": [],
            "as_of": None,
            "current": pd.DataFrame(),
            "warnings": ["full_market_cross_section_unavailable"],
        }
    dates = sorted(frame["trade_date"].unique())
    as_of = dates[-1]
    current = (
        frame[frame["trade_date"] == as_of]
        .set_index("symbol", drop=False)
        .copy()
    )
    warnings: list[str] = []
    if len(dates) >= 21:
        previous = (
            frame[frame["trade_date"] == dates[-21]]
            .set_index("symbol")["close"]
            .astype(float)
        )
        returns = (
            current["close"].astype(float)
            / previous.reindex(current.index)
            - 1.0
        )
        current["return_20d"] = returns
        current["return_20d_percentile"] = _rank_percentile(returns)
    else:
        current["return_20d"] = np.nan
        current["return_20d_percentile"] = np.nan
        warnings.append("cross_section_history_less_than_21_sessions")

    recent_five = frame[frame["trade_date"].isin(dates[-5:])]
    amount_5d = recent_five.groupby("symbol")["amount"].mean()
    current["amount_5d"] = amount_5d.reindex(current.index)
    current["amount_percentile"] = _rank_percentile(current["amount_5d"])

    streaks = _consecutive_limit_moves(
        frame[frame["trade_date"].isin(dates[-10:])]
    )
    current["limit_streak"] = (
        streaks.reindex(current.index).fillna(0).astype(int)
    )
    current["limit_streak_rank"] = _competition_rank(
        current["limit_streak"]
    )
    return {
        "dates": dates,
        "as_of": as_of,
        "current": current,
        "warnings": warnings,
    }


def build_short_term_leader_lens(
    *,
    symbol: str,
    snapshots: pd.DataFrame,
    latest_daily: dict[str, Any],
    board_rows: list[dict[str, Any]] | None = None,
    board_members: dict[str, list[str]] | None = None,
    prepared_universe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build auditable leader-candidate evidence without producing a score."""
    prepared = (
        prepared_universe
        if prepared_universe is not None
        else prepare_short_term_leader_universe(snapshots)
    )
    board_rows = board_rows or []
    board_members = board_members or {}
    current = prepared.get("current")
    dates = list(prepared.get("dates") or [])
    as_of = prepared.get("as_of")
    if not isinstance(current, pd.DataFrame) or current.empty or as_of is None:
        return {
            "status": "partial",
            "label": "短线龙头",
            "conclusion": "not_determined",
            "facts": {
                "rule_version": LEADER_RULE_VERSION,
                "candidate_types": {},
            },
            "observations": [],
            "warnings": ["full_market_cross_section_unavailable"],
        }
    latest_daily_date = latest_daily.get("trade_date")
    if hasattr(latest_daily_date, "isoformat"):
        latest_daily_date = latest_daily_date.isoformat()
    else:
        latest_daily_date = str(latest_daily_date or "")
    if latest_daily_date and as_of.isoformat() != latest_daily_date[:10]:
        return {
            "status": "partial",
            "label": "短线龙头",
            "conclusion": "not_determined",
            "facts": {
                "rule_version": LEADER_RULE_VERSION,
                "as_of_trade_date": as_of.isoformat(),
                "required_trade_date": latest_daily_date[:10],
                "candidate_types": {},
            },
            "observations": [],
            "warnings": ["cross_section_stale_for_event"],
        }
    if symbol not in current.index:
        return {
            "status": "partial",
            "label": "短线龙头",
            "conclusion": "not_determined",
            "facts": {
                "rule_version": LEADER_RULE_VERSION,
                "as_of_trade_date": as_of.isoformat(),
                "candidate_types": {},
            },
            "observations": [],
            "warnings": ["symbol_missing_from_cross_section"],
        }

    target = current.loc[symbol]
    if isinstance(target, pd.DataFrame):
        target = target.iloc[-1]
    warnings: list[str] = [
        *list(prepared.get("warnings") or []),
        "limit_status_is_approximate_without_exchange_limit_metadata",
    ]
    target_current = current.loc[symbol]

    ma20 = _finite(latest_daily.get("ma20"))
    ma60 = _finite(latest_daily.get("ma60"))
    close = _finite(latest_daily.get("close"))
    distance_60d = _finite(latest_daily.get("distance_60d_high"))
    return_percentile = _finite(target_current.get("return_20d_percentile"))
    amount_percentile = _finite(target_current.get("amount_percentile"))
    limit_streak = int(target_current.get("limit_streak") or 0)
    limit_rank = int(target_current.get("limit_streak_rank") or len(current))

    strongest_board = max(
        board_rows,
        key=lambda row: _finite(row.get("rps_50")) or float("-inf"),
        default={},
    )
    strongest_board_code = str(strongest_board.get("board_code") or "")
    board_rps50 = _finite(strongest_board.get("rps_50"))
    members = {
        member
        for member in board_members.get(strongest_board_code, [])
        if member in current.index
    }
    board_return_percentile = None
    board_amount_percentile = None
    board_limit_rank = None
    if members:
        board = current.loc[sorted(members)]
        if symbol in board.index:
            if board["return_20d"].notna().any():
                board_return_percentile = _finite(
                    _rank_percentile(board["return_20d"]).loc[symbol]
                )
            board_amount_percentile = _finite(
                _rank_percentile(board["amount_5d"]).loc[symbol]
            )
            board_limit_rank = int(
                _competition_rank(board["limit_streak"]).loc[symbol]
            )
    elif strongest_board_code:
        warnings.append("board_membership_snapshot_unavailable")

    above_ma20_ma60 = bool(
        close is not None
        and ma20 is not None
        and ma60 is not None
        and close > ma20
        and close > ma60
    )
    limit_candidate = limit_streak >= 2 and (limit_rank <= 10 or board_limit_rank == 1)
    trend_inputs_ready = all(
        value is not None
        for value in (return_percentile, ma20, ma60, close, distance_60d)
    )
    trend_candidate = (
        bool(
            return_percentile >= 95
            and above_ma20_ma60
            and distance_60d >= -0.03
        )
        if trend_inputs_ready
        else None
    )
    capacity_inputs_ready = all(
        value is not None
        for value in (amount_percentile, return_percentile, board_rps50)
    )
    capacity_candidate = (
        bool(
            amount_percentile >= 95
            and return_percentile >= 80
            and board_rps50 >= 87
        )
        if capacity_inputs_ready
        else None
    )
    board_inputs_ready = all(
        value is not None
        for value in (
            board_rps50,
            board_return_percentile,
            board_amount_percentile,
        )
    )
    board_candidate = (
        bool(
            board_rps50 >= 87
            and board_return_percentile >= 90
            and board_amount_percentile >= 80
        )
        if board_inputs_ready
        else None
    )
    if board_rps50 is None:
        warnings.append("board_context_unavailable")

    candidates = {
        "limit_up_leader": {
            "candidate": limit_candidate,
            "status": _candidate_status(limit_candidate),
            "limit_streak": limit_streak,
            "market_rank": limit_rank,
            "board_rank": board_limit_rank,
        },
        "trend_leader": {
            "candidate": trend_candidate,
            "status": _candidate_status(trend_candidate),
            "return_20d_percentile": return_percentile,
            "above_ma20_ma60": above_ma20_ma60,
            "distance_60d_high": distance_60d,
        },
        "liquidity_core": {
            "candidate": capacity_candidate,
            "status": _candidate_status(capacity_candidate),
            "amount_5d_percentile": amount_percentile,
            "return_20d_percentile": return_percentile,
            "board_rps50": board_rps50,
        },
        "board_leader": {
            "candidate": board_candidate,
            "status": _candidate_status(board_candidate),
            "board_code": strongest_board_code,
            "board_name": str(strongest_board.get("board_name") or ""),
            "board_rps50": board_rps50,
            "board_return_20d_percentile": board_return_percentile,
            "board_amount_5d_percentile": board_amount_percentile,
        },
    }
    selected = [
        key
        for key, value in candidates.items()
        if value["candidate"] is True
    ]
    undetermined = any(
        value["candidate"] is None
        for value in candidates.values()
    )
    return {
        "status": (
            "ready"
            if len(dates) >= 21 and not undetermined
            else "partial"
        ),
        "label": "短线龙头",
        "conclusion": (
            "candidate"
            if selected
            else "not_determined"
            if undetermined
            else "not_candidate"
        ),
        "facts": {
            "rule_version": LEADER_RULE_VERSION,
            "as_of_trade_date": as_of.isoformat(),
            "universe_size": int(len(current)),
            "is_st": bool(target.get("is_st", False)),
            "candidate_types": candidates,
        },
        "observations": selected,
        "warnings": list(dict.fromkeys(warnings)),
    }
