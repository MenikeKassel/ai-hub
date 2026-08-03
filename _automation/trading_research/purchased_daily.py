"""Read-only importer for the purchased 2026-07-31 daily archive.

The archive is treated as an untrusted, dated snapshot.  This module never
modifies the source directory and only writes through the existing MarketStore
when an explicit import command is used.
"""

from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from market_data import MarketStore, SyncResult, sync_daily_bars


PURCHASED_DAILY_PROVIDER = "purchased_daily_20260731"
EXPECTED_COLUMNS = (
    "ts_code",
    "trade_date",
    "name",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
    "adj_factor",
    "first_adj",
    "last_adj",
    "open_qfq",
    "high_qfq",
    "low_qfq",
    "close_qfq",
    "pre_close_qfq",
    "change_qfq",
    "pct_chg_qfq",
    "open_hfq",
    "high_hfq",
    "low_hfq",
    "close_hfq",
    "pre_close_hfq",
    "change_hfq",
    "pct_chg_hfq",
)
REFERENCE_FILES = ("159139.SZ", "000300.SH")
ADJUSTMENT_COLUMNS = {
    "raw": ("open", "high", "low", "close", "pre_close"),
    "qfq": ("open_qfq", "high_qfq", "low_qfq", "close_qfq", "pre_close_qfq"),
    "hfq": ("open_hfq", "high_hfq", "low_hfq", "close_hfq", "pre_close_hfq"),
}


def _exchange_for_symbol(symbol: str) -> str:
    code = symbol.split(".", 1)[0].zfill(6)
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("5", "6", "9")):
        return "SH"
    return "SZ"


def _normalise_symbol(value: str) -> str:
    text = str(value).strip().upper()
    code = text.split(".", 1)[0]
    if not code.isdigit() or len(code) > 6:
        raise ValueError(f"invalid purchased archive symbol: {value}")
    return code.zfill(6)


def _archive_path(root: Path, symbol: str) -> Path:
    code = _normalise_symbol(symbol)
    return root / f"{code}.{_exchange_for_symbol(code)}.csv"


def _read_last_line(path: Path) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        if position == 0:
            return ""
        block_size = 8192
        data = b""
        while position > 0 and b"\n" not in data:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data
        lines = [line for line in data.splitlines() if line.strip()]
        return lines[-1].decode("utf-8-sig", errors="replace") if lines else ""


def _last_trade_date(path: Path) -> str:
    line = _read_last_line(path)
    try:
        row = next(csv.reader([line]))
        value = str(row[1]).strip() if len(row) > 1 else ""
        datetime.strptime(value, "%Y%m%d")
        return value
    except (csv.Error, IndexError):
        return ""
    except ValueError:
        return ""


def audit_purchased_daily_archive(
    root: Path,
    *,
    expected_date: date | None = None,
) -> dict[str, Any]:
    """Audit archive metadata without loading the multi-gigabyte CSV bodies."""

    root = Path(root)
    errors: list[str] = []
    warnings: list[str] = []
    header_variants: dict[str, int] = {}
    header_errors: list[str] = []
    code_errors: list[str] = []
    latest_counts: dict[str, int] = {}
    files = sorted(root.glob("*.csv")) if root.is_dir() else []

    if not root.is_dir():
        errors.append(f"archive root does not exist: {root}")
    for path in files:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                header = tuple(next(reader))
                first_row = next(reader, [])
        except (OSError, StopIteration, csv.Error) as exc:
            header_errors.append(f"{path.name}: {exc}")
            continue
        header_key = ",".join(header)
        header_variants[header_key] = header_variants.get(header_key, 0) + 1
        if header != EXPECTED_COLUMNS:
            header_errors.append(path.name)
        if not path.stem.upper().endswith((".SH", ".SZ", ".BJ")) or not path.stem[:6].isdigit():
            code_errors.append(path.name)
        elif not first_row or str(first_row[0]).strip().upper() != path.stem.upper():
            code_errors.append(f"{path.name}:ts_code_mismatch")
        last_date = _last_trade_date(path)
        if not last_date:
            code_errors.append(f"{path.name}:missing_last_trade_date")
        latest_counts[last_date] = latest_counts.get(last_date, 0) + 1

    if header_errors:
        errors.append(f"header_errors={len(header_errors)}")
    if code_errors:
        errors.append(f"file_errors={len(code_errors)}")
    if not files:
        errors.append("no CSV files found")

    latest_date = max(latest_counts, default="")
    expected_text = expected_date.strftime("%Y%m%d") if expected_date else latest_date
    latest_date_file_count = latest_counts.get(expected_text, 0)
    stale_file_count = len(files) - latest_date_file_count
    source_status = "unknown" if not latest_date else "current"
    if expected_date and latest_date and latest_date != expected_text:
        source_status = "stale" if latest_date < expected_text else "unexpected_future"
        warnings.append(f"archive_latest_date={latest_date}, expected_date={expected_text}")
    if stale_file_count:
        warnings.append(f"stale_or_suspended_files={stale_file_count}")

    missing_reference_files = [
        name for name in REFERENCE_FILES if not (root / f"{name}.csv").exists()
    ]
    if missing_reference_files:
        warnings.append("missing_reference_files=" + ",".join(missing_reference_files))

    return {
        "ok": not errors,
        "provider": PURCHASED_DAILY_PROVIDER,
        "root": str(root),
        "file_count": len(files),
        "audit_scope": "metadata_only; row_content_validated_on_import",
        "latest_date": latest_date,
        "expected_date": expected_text,
        "source_status": source_status,
        "latest_date_file_count": latest_date_file_count,
        "stale_file_count": stale_file_count,
        "header_variants": header_variants,
        "header_errors": header_errors[:100],
        "file_errors": code_errors[:100],
        "missing_reference_files": missing_reference_files,
        "warnings": warnings,
        "errors": errors,
    }


class PurchasedDailyProvider:
    """Read the purchased CSV snapshot and convert Tushare units."""

    name = PURCHASED_DAILY_PROVIDER

    def __init__(self, root: Path):
        self.root = Path(root)

    def has_symbol(self, symbol: str) -> bool:
        return _archive_path(self.root, symbol).is_file()

    def available_range(self, symbol: str) -> tuple[date, date] | None:
        path = _archive_path(self.root, symbol)
        if not path.exists():
            return None
        dates = pd.read_csv(path, usecols=["trade_date"], dtype={"trade_date": "string"})
        values = pd.to_datetime(dates["trade_date"], errors="coerce").dropna()
        if values.empty:
            return None
        return values.min().date(), values.max().date()

    def fetch_daily(
        self,
        symbol: str,
        instrument_type: str,
        start: date,
        end: date,
        adjustment: str,
    ) -> pd.DataFrame:
        del instrument_type
        if adjustment not in ADJUSTMENT_COLUMNS:
            raise ValueError(f"unsupported purchased archive adjustment: {adjustment}")
        if end < start:
            return pd.DataFrame()
        path = _archive_path(self.root, symbol)
        if not path.exists():
            raise FileNotFoundError(f"purchased archive file not found: {path.name}")
        selected = [
            "ts_code", "trade_date", "vol", "amount",
            "adj_factor", "first_adj", "last_adj",
            *ADJUSTMENT_COLUMNS[adjustment],
        ]
        frame = pd.read_csv(path, usecols=selected, dtype={"ts_code": "string", "trade_date": "string"})
        expected_code = f"{_normalise_symbol(symbol)}.{_exchange_for_symbol(symbol)}"
        codes = frame["ts_code"].astype("string").str.strip().str.upper()
        if not bool(codes.eq(expected_code).all()):
            mismatched = sorted(set(codes[codes.ne(expected_code)].dropna().tolist()))
            raise ValueError(
                f"archive symbol mismatch for {path.name}: expected {expected_code}, found {mismatched[:5]}"
            )
        raw_dates = frame["trade_date"].astype("string").str.strip()
        valid_date_shape = raw_dates.str.fullmatch(r"\d{8}").fillna(False)
        if not bool(valid_date_shape.all()):
            raise ValueError(f"archive contains malformed trade_date values: {path.name}")
        frame["trade_date"] = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce").dt.date
        if bool(frame["trade_date"].isna().any()):
            raise ValueError(f"archive contains invalid trade_date values: {path.name}")
        frame = frame[frame["trade_date"].between(start, end, inclusive="both")].copy()
        columns = ADJUSTMENT_COLUMNS[adjustment]
        frame = frame.rename(
            columns={
                columns[0]: "open",
                columns[1]: "high",
                columns[2]: "low",
                columns[3]: "close",
                columns[4]: "preclose",
                "vol": "volume",
            }
        )
        for column in (
            "open", "high", "low", "close", "preclose", "volume", "amount",
            "adj_factor", "first_adj", "last_adj",
        ):
            if column not in frame:
                frame[column] = pd.NA
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["volume"] = frame["volume"] * 100
        frame["amount"] = frame["amount"] * 1000
        frame["turnover"] = pd.NA
        frame["trade_status"] = ""
        return frame[
            [
                "trade_date", "open", "high", "low", "close", "preclose", "volume", "amount",
                "turnover", "trade_status", "adj_factor", "first_adj", "last_adj",
            ]
        ].sort_values("trade_date").reset_index(drop=True)


def _coverage(store: MarketStore, symbol: str, adjustment: str) -> dict[str, Any] | None:
    return next(
        (
            value
            for value in store.get_coverage(symbol)
            if value.get("dataset") == "daily" and value.get("adjustment") == adjustment
        ),
        None,
    )


def import_historical_daily(
    store: MarketStore,
    provider: PurchasedDailyProvider,
    symbols: Iterable[str],
    *,
    start: date,
    end: date,
    adjustments: Iterable[str] = ("raw",),
) -> dict[str, Any]:
    """Merge only dates older than the canonical series for each symbol."""

    requested_symbols = sorted({_normalise_symbol(value) for value in symbols})
    selected_adjustments = tuple(adjustments)
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    missing: list[str] = []
    for symbol in requested_symbols:
        path = _archive_path(provider.root, symbol)
        if not path.exists():
            missing.append(f"{symbol}.{_exchange_for_symbol(symbol)}")
            continue
        instrument = store.get_instrument(symbol)
        if instrument is None:
            skipped.append({"symbol": symbol, "reason": "instrument_missing"})
            continue
        for adjustment in selected_adjustments:
            existing = store.read_daily(symbol, adjustment=adjustment)
            requested_end = end
            if not existing.empty:
                first_existing = min(existing["trade_date"])
                requested_end = min(requested_end, first_existing - timedelta(days=1))
            if requested_end < start:
                skipped.append({"symbol": symbol, "adjustment": adjustment, "reason": "canonical_covers_range"})
                continue
            available = provider.available_range(symbol)
            if available is None or available[0] > requested_end or available[1] < start:
                skipped.append({"symbol": symbol, "adjustment": adjustment, "reason": "archive_no_overlap"})
                continue
            previous_provider = (_coverage(store, symbol, adjustment) or {}).get("provider")
            result: SyncResult = sync_daily_bars(
                store,
                provider,
                symbol,
                start,
                requested_end,
                adjustment=adjustment,
                promote=True,
            )
            store.reconcile_daily_coverage(
                symbol,
                adjustment,
                provider=previous_provider or provider.name,
            )
            results.append(result.__dict__)
    return {
        "provider": provider.name,
        "symbols_requested": len(requested_symbols),
        "results": results,
        "skipped": skipped,
        "missing_files": missing,
        "failed": [item for item in results if item.get("quality_status") == "quarantined"],
    }
