from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


SHANGHAI = ZoneInfo("Asia/Shanghai")
FEATURE_VERSION = "technical-context-v1"
ADJUSTMENT = "qfq"


@dataclass(frozen=True)
class EventTechnicalContext:
    snapshot_id: str
    event_id: str
    feature_version: str
    input_hash: str
    symbol: str
    posted_at: str
    expected_trade_date: str
    as_of_trade_date: str
    adjustment: str
    rsi14: float | None
    macd_dif: float | None
    macd_dea: float | None
    macd_hist: float | None
    macd_hist_pct: float | None
    atr14: float | None
    atr14_pct: float | None
    volume_ratio_5: float | None
    return_20d: float | None
    distance_60d_high: float | None
    history_bars: int
    status: str
    warnings: list[str]
    source_hash: str
    error: str
    computed_at: str
    foundation_release_id: str = ""

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["warnings_json"] = json.dumps(record.pop("warnings"), ensure_ascii=False)
        return record


def event_context_input_hash(symbol: str, posted_at: str) -> str:
    payload = json.dumps(
        {"symbol": str(symbol).strip(), "posted_at": str(posted_at).strip()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_context_snapshot_id(
    *,
    event_id: str,
    input_hash: str,
    source_hash: str,
    status: str,
    warnings: list[str],
    error: str = "",
    foundation_release_id: str = "",
) -> str:
    payload = json.dumps(
        {
            "event_id": event_id,
            "feature_version": FEATURE_VERSION,
            "input_hash": input_hash,
            "source_hash": source_hash,
            "status": status,
            "warnings": warnings,
            "error": error,
            "foundation_release_id": foundation_release_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_context_cutoff(posted_at: str) -> date:
    posted = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    if posted.tzinfo is None:
        raise ValueError("posted_at must include timezone")
    local_posted = posted.astimezone(SHANGHAI)
    return local_posted.date() if local_posted.time() >= time(15, 0) else local_posted.date() - timedelta(days=1)


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    values = frame.copy()
    if "trade_date" not in values.columns and "date" in values.columns:
        values = values.rename(columns={"date": "trade_date"})
    required = {"trade_date", "high", "low", "close", "volume"}
    missing = sorted(required - set(values.columns))
    if missing:
        raise ValueError("qfq history missing columns: " + ", ".join(missing))
    values["trade_date"] = pd.to_datetime(values["trade_date"], errors="coerce").dt.date
    for column in ("high", "low", "close", "volume"):
        values[column] = pd.to_numeric(values[column], errors="coerce")
    values = values.dropna(subset=["trade_date", "high", "low", "close", "volume"])
    return values.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)


def _wilder_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    changes = closes.diff().iloc[1:]
    gains = changes.clip(lower=0.0)
    losses = -changes.clip(upper=0.0)
    average_gain = float(gains.iloc[:period].mean())
    average_loss = float(losses.iloc[:period].mean())
    for index in range(period, len(changes)):
        average_gain = ((period - 1) * average_gain + float(gains.iloc[index])) / period
        average_loss = ((period - 1) * average_loss + float(losses.iloc[index])) / period
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _wilder_atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    if len(frame) < period:
        return None
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    average = float(true_range.iloc[:period].mean())
    for value in true_range.iloc[period:]:
        average = ((period - 1) * average + float(value)) / period
    return average


def _macd(closes: pd.Series) -> tuple[float | None, float | None, float | None]:
    if len(closes) < 35:
        return None, None, None
    dif = closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    return float(dif.iloc[-1]), float(dea.iloc[-1]), float((dif - dea).iloc[-1])


def _source_hash(frame: pd.DataFrame) -> str:
    payload = frame[["trade_date", "high", "low", "close", "volume"]].to_csv(
        index=False,
        date_format="%Y-%m-%d",
        float_format="%.12g",
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_event_technical_context(
    *,
    event_id: str,
    symbol: str,
    posted_at: str,
    qfq_prices: pd.DataFrame,
    expected_trade_date: date | str | None = None,
    computed_at: str | None = None,
    foundation_release_id: str = "",
) -> EventTechnicalContext:
    cutoff = event_context_cutoff(posted_at)
    expected = date.fromisoformat(expected_trade_date) if isinstance(expected_trade_date, str) else expected_trade_date
    expected_value = expected.isoformat() if expected else ""
    frame = _normalise_frame(qfq_prices)
    history = frame[frame["trade_date"] <= cutoff].reset_index(drop=True) if not frame.empty else frame
    timestamp = computed_at or datetime.now().astimezone().isoformat(timespec="seconds")
    input_hash = event_context_input_hash(symbol, posted_at)
    if history.empty:
        warnings = ["no_qfq_history"]
        status = "pending"
        snapshot_id = event_context_snapshot_id(
            event_id=event_id,
            input_hash=input_hash,
            source_hash="",
            status=status,
            warnings=warnings,
            foundation_release_id=foundation_release_id,
        )
        return EventTechnicalContext(
            snapshot_id, event_id, FEATURE_VERSION, input_hash, symbol, posted_at, expected_value, "", ADJUSTMENT,
            None, None, None, None, None, None, None, None, None, None,
            0, status, warnings, "", "", timestamp,
            foundation_release_id,
        )

    warnings: list[str] = []
    as_of = history.iloc[-1]["trade_date"]
    if as_of < cutoff:
        warnings.append("prior_trade_date_used")
    closes = history["close"].astype(float)
    latest_close = float(closes.iloc[-1])
    rsi14 = _wilder_rsi(closes)
    if rsi14 is None:
        warnings.append("insufficient_rsi14")
    macd_dif, macd_dea, macd_hist = _macd(closes)
    if macd_hist is None:
        warnings.append("insufficient_macd")
    atr14 = _wilder_atr(history)
    if atr14 is None:
        warnings.append("insufficient_atr14")

    volume_ratio_5: float | None = None
    if len(history) >= 6:
        reference_volume = float(history.iloc[-6:-1]["volume"].mean())
        if reference_volume > 0:
            volume_ratio_5 = float(history.iloc[-1]["volume"]) / reference_volume
        else:
            warnings.append("zero_reference_volume")
    else:
        warnings.append("insufficient_volume_ratio_5")

    with np.errstate(divide="ignore", invalid="ignore"):
        return_20d = float(closes.iloc[-1] / closes.iloc[-21] - 1.0) if len(history) >= 21 else None
    if return_20d is None or not np.isfinite(return_20d):
        return_20d = None
    if return_20d is None:
        warnings.append("insufficient_return_20d")
    with np.errstate(divide="ignore", invalid="ignore"):
        distance_60d_high = (
            float(latest_close / closes.iloc[-60:].max() - 1.0) if len(history) >= 60 else None
        )
    if distance_60d_high is None or not np.isfinite(distance_60d_high):
        distance_60d_high = None
    if distance_60d_high is None:
        warnings.append("insufficient_distance_60d_high")

    values = [rsi14, macd_hist, atr14, volume_ratio_5, return_20d, distance_60d_high]
    status = "complete" if all(value is not None for value in values) else "partial"
    if expected and as_of < expected:
        warnings.append("expected_trade_date_missing")
        status = "pending"
    source_hash = _source_hash(history)
    snapshot_id = event_context_snapshot_id(
        event_id=event_id,
        input_hash=input_hash,
        source_hash=source_hash,
        status=status,
        warnings=warnings,
        foundation_release_id=foundation_release_id,
    )
    return EventTechnicalContext(
        snapshot_id=snapshot_id,
        event_id=event_id,
        feature_version=FEATURE_VERSION,
        input_hash=input_hash,
        symbol=symbol,
        posted_at=posted_at,
        expected_trade_date=expected_value,
        as_of_trade_date=as_of.isoformat(),
        adjustment=ADJUSTMENT,
        rsi14=rsi14,
        macd_dif=macd_dif,
        macd_dea=macd_dea,
        macd_hist=macd_hist,
        macd_hist_pct=(macd_hist / latest_close) if macd_hist is not None and latest_close else None,
        atr14=atr14,
        atr14_pct=(atr14 / latest_close) if atr14 is not None and latest_close else None,
        volume_ratio_5=volume_ratio_5,
        return_20d=return_20d,
        distance_60d_high=distance_60d_high,
        history_bars=len(history),
        status=status,
        warnings=warnings,
        source_hash=source_hash,
        error="",
        computed_at=timestamp,
        foundation_release_id=foundation_release_id,
    )


def failed_event_technical_context(
    *,
    event_id: str,
    symbol: str,
    posted_at: str,
    error: str,
    expected_trade_date: date | str | None = None,
    computed_at: str | None = None,
    foundation_release_id: str = "",
) -> EventTechnicalContext:
    expected = date.fromisoformat(expected_trade_date) if isinstance(expected_trade_date, str) else expected_trade_date
    expected_value = expected.isoformat() if expected else ""
    input_hash = event_context_input_hash(symbol, posted_at)
    clean_error = str(error).strip()[:1000] or "unknown calculation error"
    warnings = ["calculation_failed"]
    snapshot_id = event_context_snapshot_id(
        event_id=event_id,
        input_hash=input_hash,
        source_hash="",
        status="failed",
        warnings=warnings,
        error=clean_error,
        foundation_release_id=foundation_release_id,
    )
    return EventTechnicalContext(
        snapshot_id, event_id, FEATURE_VERSION, input_hash, symbol, posted_at, expected_value,
        "", ADJUSTMENT, None, None, None, None, None, None, None, None, None,
        None, 0, "failed", warnings, "", clean_error,
        computed_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        foundation_release_id,
    )
