from __future__ import annotations

import pandas as pd


INDICATOR_VERSION = "daily-technical-v1"


def _wilder_rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    result = pd.Series(float("nan"), index=closes.index, dtype="float64")
    if len(closes) < period + 1:
        return result
    changes = closes.diff()
    gains = changes.clip(lower=0.0)
    losses = -changes.clip(upper=0.0)
    average_gain = float(gains.iloc[1 : period + 1].mean())
    average_loss = float(losses.iloc[1 : period + 1].mean())

    def rsi_value() -> float:
        if average_loss == 0:
            return 50.0 if average_gain == 0 else 100.0
        relative_strength = average_gain / average_loss
        return 100.0 - 100.0 / (1.0 + relative_strength)

    result.iloc[period] = rsi_value()
    for index in range(period + 1, len(closes)):
        average_gain = ((period - 1) * average_gain + float(gains.iloc[index])) / period
        average_loss = ((period - 1) * average_loss + float(losses.iloc[index])) / period
        result.iloc[index] = rsi_value()
    return result


def _wilder_atr_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    result = pd.Series(float("nan"), index=frame.index, dtype="float64")
    if len(frame) < period:
        return result
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
    result.iloc[period - 1] = average
    for index in range(period, len(frame)):
        average = ((period - 1) * average + float(true_range.iloc[index])) / period
        result.iloc[index] = average
    return result


def compute_daily_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Return deterministic qfq daily indicators without changing stored bars."""
    if frame.empty:
        return frame.copy()
    required = {"trade_date", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("daily history missing columns: " + ", ".join(missing))

    value = frame.copy()
    value["trade_date"] = pd.to_datetime(value["trade_date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in value.columns:
            value[column] = pd.to_numeric(value[column], errors="coerce")
    value = (
        value.dropna(subset=["trade_date", "open", "high", "low", "close", "volume"])
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
        .reset_index(drop=True)
    )
    closes = value["close"].astype(float)
    volume = value["volume"].astype(float)

    for period in (5, 10, 20, 60):
        value[f"ma{period}"] = closes.rolling(period, min_periods=period).mean()

    macd_dif = closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()
    macd_dea = macd_dif.ewm(span=9, adjust=False).mean()
    value["macd_dif"] = macd_dif
    value["macd_dea"] = macd_dea
    value["macd_hist"] = macd_dif - macd_dea
    if len(value) < 35:
        value.loc[:, ["macd_dif", "macd_dea", "macd_hist"]] = float("nan")
    else:
        value.loc[value.index < 34, ["macd_dif", "macd_dea", "macd_hist"]] = float("nan")

    value["rsi14"] = _wilder_rsi_series(closes)
    value["atr14"] = _wilder_atr_series(value)
    value["atr14_pct"] = value["atr14"] / closes.where(closes != 0)
    value["volume_ratio_5"] = volume / volume.shift(1).rolling(5, min_periods=5).mean()
    value["return_20d"] = closes / closes.shift(20) - 1.0
    value["distance_60d_high"] = closes / closes.rolling(60, min_periods=60).max() - 1.0
    value["indicator_version"] = INDICATOR_VERSION
    return value
