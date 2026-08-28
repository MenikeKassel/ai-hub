"""Read-only bridge from the KOL workflow to the shared A-share foundation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from kol_audit.market.store import Instrument


def _foundation_symbol(symbol: str) -> str:
    value = str(symbol).strip().upper()
    if "." in value:
        return value
    if not value.isdigit() or len(value) != 6:
        raise ValueError(f"invalid A-share symbol: {symbol}")
    if value == "000300":
        return "000300.SH"
    if value.startswith(("4", "8", "9")):
        exchange = "BJ"
    elif value.startswith(("5", "6")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return f"{value}.{exchange}"


class FoundationMarketReader:
    """Pin every read to the release selected by the atomic current pointer."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self._active_release: ContextVar[dict[str, Any] | None] = ContextVar(
            "ashare_foundation_release",
            default=None,
        )

    def _read_current_release(self) -> dict[str, Any]:
        pointer = self.root / "current.json"
        if not pointer.is_file():
            raise FileNotFoundError(f"A-share foundation pointer is missing: {pointer}")
        value = json.loads(pointer.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or not value.get("release_id")
            or not value.get("as_of")
        ):
            raise ValueError(f"invalid A-share foundation pointer: {pointer}")
        return value

    def release(self) -> dict[str, Any]:
        return self._active_release.get() or self._read_current_release()

    @contextmanager
    def pinned_release(self) -> Iterator[dict[str, Any]]:
        active = self._active_release.get()
        if active is not None:
            yield active
            return
        release = self._read_current_release()
        token = self._active_release.set(release)
        try:
            yield release
        finally:
            self._active_release.reset(token)

    def dataset_path(self, dataset: str, release: dict[str, Any] | None = None) -> Path:
        selected = release or self.release()
        source_release = (selected.get("dataset_sources") or {}).get(
            dataset
        ) or selected["release_id"]
        path = self.root / "warehouse" / dataset / f"release_id={source_release}"
        if dataset not in selected.get("datasets", []) or not path.is_dir():
            raise FileNotFoundError(
                f"foundation dataset is unavailable: {dataset} at {path}"
            )
        return path

    def health(self) -> dict[str, Any]:
        try:
            with self.pinned_release() as release:
                return {
                    "ok": True,
                    "provider": "ashare-data-foundation",
                    "read_only": True,
                    "root": str(self.root),
                    "release_id": str(release["release_id"]),
                    "as_of": str(release["as_of"]),
                    "release_stage": str(release.get("release_stage") or "legacy"),
                    "primary_provider": str(release.get("primary_provider") or ""),
                    "verification_provider": str(release.get("verification_provider") or ""),
                    "coverage_ratio": release.get("coverage_ratio"),
                    "verification_status": str(release.get("verification_status") or ""),
                    "datasets": list(release.get("datasets") or []),
                }
        except (FileNotFoundError, KeyError, TypeError, ValueError, OSError) as error:
            return {
                "ok": False,
                "provider": "ashare-data-foundation",
                "read_only": True,
                "root": str(self.root),
                "error": str(error),
            }

    def coverage(self, trade_date: str | None = None) -> dict[str, Any]:
        """Measure daily cross-section coverage without changing the foundation."""
        release = self.release()
        selected_date = trade_date or str(release["as_of"])
        try:
            instruments = pd.read_parquet(
                self.dataset_path("instruments", release),
                columns=["symbol", "status"],
            )
            active = int(instruments["status"].fillna("active").eq("active").sum())
            daily = pd.read_parquet(
                self.dataset_path("daily_raw", release),
                filters=[("trade_date", "=", date.fromisoformat(selected_date))],
                columns=["symbol"],
            )
            observed = int(daily["symbol"].nunique())
            threshold = 0.90
            return {
                "trade_date": selected_date,
                "active_catalog": active,
                "observed": observed,
                "coverage_ratio": round(observed / active, 4) if active else 0.0,
                "threshold": threshold,
                "complete": bool(active and observed >= int(active * threshold)),
            }
        except (FileNotFoundError, OSError, ValueError, KeyError) as error:
            return {
                "trade_date": selected_date,
                "active_catalog": 0,
                "observed": 0,
                "coverage_ratio": 0.0,
                "threshold": 0.90,
                "complete": False,
                "error": str(error),
            }

    def read_daily(self, symbol: str, *, adjustment: str = "raw") -> pd.DataFrame:
        values = self.read_daily_many([symbol], adjustment=adjustment)
        return values.get(str(symbol).split(".", 1)[0], pd.DataFrame())

    def read_daily_many(
        self,
        symbols: list[str] | set[str] | tuple[str, ...],
        *,
        adjustment: str = "raw",
    ) -> dict[str, pd.DataFrame]:
        if adjustment == "hfq":
            return {str(symbol).split(".", 1)[0]: pd.DataFrame() for symbol in symbols}
        if adjustment not in {"raw", "qfq"}:
            raise ValueError(f"unsupported adjustment: {adjustment}")
        dataset = "daily_raw" if adjustment == "raw" else "daily_adjusted"
        requested = {
            str(symbol).strip().upper().split(".", 1)[0]: _foundation_symbol(str(symbol))
            for symbol in symbols
        }
        if not requested:
            return {}
        with self.pinned_release() as release:
            frame = pd.read_parquet(
                self.dataset_path(dataset, release),
                filters=[("symbol", "in", list(requested.values()))],
            )
        if frame.empty:
            return {symbol: pd.DataFrame() for symbol in requested}
        output = frame.copy()
        output["foundation_symbol"] = output["symbol"].astype(str)
        output["symbol"] = output["foundation_symbol"].str.split(".").str[0]
        output["trade_date"] = pd.to_datetime(output["trade_date"]).dt.date
        if "suspended" not in output.columns:
            output["suspended"] = False
        output["suspended"] = output["suspended"].fillna(False).astype(bool)
        output["market_open"] = ~output["suspended"]
        output["last_trade_date"] = output["trade_date"].where(output["market_open"])
        output["provider"] = "ashare-data-foundation"
        output["foundation_release_id"] = str(release["release_id"])
        output["foundation_release_stage"] = str(release.get("release_stage") or "legacy")
        return {
            symbol: (
                output[output["symbol"] == symbol]
                .sort_values("trade_date")
                .drop_duplicates("trade_date", keep="last")
                .reset_index(drop=True)
            )
            for symbol in requested
        }

    def instruments(self, release: dict[str, Any] | None = None) -> list[Instrument]:
        release = release or self.release()
        frame = pd.read_parquet(self.dataset_path("instruments", release))
        values: list[Instrument] = []
        for row in frame.to_dict(orient="records"):
            foundation_symbol = str(row.get("symbol") or "")
            symbol = foundation_symbol.split(".", 1)[0]
            if not symbol.isdigit() or len(symbol) != 6:
                continue
            list_date = row.get("list_date")
            values.append(
                Instrument(
                    symbol=symbol,
                    name=str(row.get("name") or symbol),
                    instrument_type="stock",
                    exchange=str(
                        row.get("exchange") or foundation_symbol.rsplit(".", 1)[-1]
                    ),
                    status=str(row.get("status") or "active"),
                    list_date=list_date.isoformat()
                    if isinstance(list_date, date)
                    else str(list_date or ""),
                    lifecycle="archived",
                    source=f"ashare-foundation:{release['release_id']}",
                )
            )
        return values

    def trading_dates(self, release: dict[str, Any] | None = None) -> list[date]:
        release = release or self.release()
        frame = pd.read_parquet(
            self.dataset_path("trading_calendar", release),
            filters=[("is_open", "==", True)],
            columns=["trade_date"],
        )
        return sorted(set(pd.to_datetime(frame["trade_date"]).dt.date))


class FoundationBackedMarketStore:
    """Keep KOL workflow state local while sourcing all daily facts centrally."""

    def __init__(self, local_store: Any, foundation_root: Path):
        self.local_store = local_store
        self.foundation = FoundationMarketReader(foundation_root)
        self._daily_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
        self._reference_instruments: dict[str, dict[str, Any]] | None = None
        self._reference_release_id = ""
        self._trading_dates: list[date] | None = None
        self._calendar_release_id = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self.local_store, name)

    def bootstrap_reference_data(self) -> dict[str, Any]:
        initial_health = self.foundation.health()
        if not initial_health["ok"]:
            return {**initial_health, "instrument_count": 0, "calendar_count": 0}
        with self.foundation.pinned_release():
            health = self.foundation.health()
            instruments = self._reference_map()
            calendar = self._calendar()
            return {
                **health,
                "instrument_count": len(instruments),
                "calendar_count": len(calendar),
                "unchanged": True,
                "materialized": False,
            }

    def _reference_map(self) -> dict[str, dict[str, Any]]:
        release = self.foundation.release()
        release_id = str(release["release_id"])
        if self._reference_instruments is None or self._reference_release_id != release_id:
            self._reference_instruments = {
                instrument.symbol: asdict(instrument)
                for instrument in self.foundation.instruments(release)
            }
            self._reference_release_id = release_id
        return self._reference_instruments

    def _calendar(self) -> list[date]:
        release = self.foundation.release()
        release_id = str(release["release_id"])
        if self._trading_dates is None or self._calendar_release_id != release_id:
            self._trading_dates = self.foundation.trading_dates(release)
            self._calendar_release_id = release_id
        return self._trading_dates

    def get_instrument(self, symbol: str) -> dict[str, Any] | None:
        reference = self._reference_map().get(str(symbol))
        workflow = self.local_store.get_instrument(str(symbol))
        return self._merge_instrument(reference, workflow)

    @staticmethod
    def _merge_instrument(
        reference: dict[str, Any] | None,
        workflow: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if reference is None:
            return workflow
        if workflow is None:
            return dict(reference)
        merged = {**workflow, **reference}
        for key in ("lifecycle", "first_seen_at", "last_mentioned_at", "created_at", "updated_at"):
            if key in workflow:
                merged[key] = workflow[key]
        merged["workflow_source"] = workflow.get("source", "")
        return merged

    def list_instruments(self, lifecycle: str | None = None) -> list[dict[str, Any]]:
        with self.foundation.pinned_release():
            references = self._reference_map()
            workflows = {
                item["symbol"]: item for item in self.local_store.list_instruments()
            }
            symbols = set(references)
            symbols.update(
                symbol
                for symbol, item in workflows.items()
                if item.get("lifecycle") in {"pinned", "tracking"}
            )
            values = [
                self._merge_instrument(references.get(symbol), workflows.get(symbol))
                for symbol in symbols
            ]
            selected = [item for item in values if item is not None]
            if lifecycle:
                selected = [item for item in selected if item.get("lifecycle") == lifecycle]
            return sorted(
                selected,
                key=lambda item: (
                    {"pinned": 0, "tracking": 1, "archived": 2}.get(
                        item.get("lifecycle"), 3
                    ),
                    item["symbol"],
                ),
            )

    def instrument_map(self) -> dict[str, dict[str, Any]]:
        return {item["symbol"]: item for item in self.list_instruments()}

    def instrument_catalog_count(self) -> int:
        return len(self._reference_map())

    def latest_open_date(self, as_of: date) -> date | None:
        eligible = [value for value in self._calendar() if value <= as_of]
        return eligible[-1] if eligible else None

    def open_dates_between(self, start: date, end: date) -> list[date]:
        return [value for value in self._calendar() if start <= value <= end]

    def read_daily(self, symbol: str, *, adjustment: str = "raw") -> pd.DataFrame:
        release_id = str(self.foundation.release().get("release_id", ""))
        cache_key = (release_id, str(symbol).split(".", 1)[0], adjustment)
        if cache_key in self._daily_cache:
            return self._daily_cache[cache_key].copy()
        frame = self.foundation.read_daily(symbol, adjustment=adjustment)
        frame = self._merge_local_suspension_rows(symbol, frame, release_id, adjustment)
        self._daily_cache[cache_key] = frame.copy()
        return frame

    def prefetch_daily(
        self,
        symbols: set[str] | list[str] | tuple[str, ...],
        *,
        adjustments: tuple[str, ...] = ("raw", "qfq"),
    ) -> dict[str, int]:
        release_id = str(self.foundation.release().get("release_id", ""))
        counts: dict[str, int] = {}
        for adjustment in adjustments:
            frames = self.foundation.read_daily_many(list(symbols), adjustment=adjustment)
            rows = 0
            for symbol in symbols:
                normalized = str(symbol).split(".", 1)[0]
                frame = frames.get(normalized, pd.DataFrame())
                frame = self._merge_local_suspension_rows(symbol, frame, release_id, adjustment)
                self._daily_cache[(release_id, normalized, adjustment)] = frame.copy()
                rows += len(frame)
            counts[adjustment] = rows
        return counts

    def _merge_local_suspension_rows(
        self,
        symbol: str,
        frame: pd.DataFrame,
        release_id: str,
        adjustment: str,
    ) -> pd.DataFrame:
        # Early foundation releases omit rows for suspended securities. The
        # validated local warehouse retains those rows with trade_status=0;
        # import only that suspension observation so all consumers share the
        # foundation release while keeping the missing-market-state visible.
        local = self.local_store.read_daily(symbol, adjustment=adjustment)
        if not local.empty and not frame.empty:
            if "date" not in local.columns and "trade_date" in local.columns:
                local = local.rename(columns={"trade_date": "date"})
            local["date"] = pd.to_datetime(local["date"])
            local_status = (
                pd.to_numeric(local["trade_status"], errors="coerce")
                if "trade_status" in local.columns
                else pd.Series(pd.NA, index=local.index)
            )
            local_suspended = local.get("suspended", pd.Series(False, index=local.index)).astype(bool)
            suspension_rows = local[local_status.eq(0) | local_suspended].copy()
            if not suspension_rows.empty:
                known_dates = set(pd.to_datetime(frame["trade_date"]))
                suspension_rows = suspension_rows[~suspension_rows["date"].isin(known_dates)]
                if not suspension_rows.empty:
                    suspension_rows["trade_date"] = suspension_rows["date"].dt.date
                    suspension_rows["suspended"] = True
                    suspension_rows["market_open"] = False
                    suspension_rows["last_trade_date"] = pd.NaT
                    suspension_rows["provider"] = "ashare-data-foundation:suspension-observation"
                    suspension_rows["foundation_release_id"] = release_id
                    frame = pd.concat([frame, suspension_rows], ignore_index=True, sort=False)
                    frame = frame.sort_values("trade_date").drop_duplicates("trade_date", keep="first").reset_index(drop=True)
        # A missing foundation frame is an explicit data-quality state.  Never
        # fall back to the local warehouse: it is workflow state, not a second
        # daily fact source.  Suspension overlays above are intentionally
        # limited to status observations and never provide replacement prices.
        return frame

    def fetch_stock(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjusted: bool,
    ) -> pd.DataFrame:
        frame = self.read_daily(symbol, adjustment="qfq" if adjusted else "raw")
        if frame.empty:
            raise RuntimeError(f"foundation has no {symbol} data")
        if "trade_date" in frame.columns:
            if "date" not in frame.columns:
                frame = frame.rename(columns={"trade_date": "date"})
            else:
                frame["date"] = frame["date"].fillna(pd.to_datetime(frame["trade_date"]))
        frame["date"] = pd.to_datetime(frame["date"])
        return frame[(frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))].reset_index(drop=True)

    def fetch_benchmark(self, start: date, end: date) -> pd.DataFrame:
        return self.fetch_stock("000300", start, end, adjusted=False)

    @property
    def name(self) -> str:
        return "ashare-data-foundation"

    def get_coverage(self, symbol: str | None = None) -> list[dict[str, Any]]:
        release = self.foundation.release()
        selected_symbol = str(symbol or "ALL")
        paths = {
            "raw": self.foundation.dataset_path("daily_raw", release),
            "qfq": self.foundation.dataset_path("daily_adjusted", release),
        }
        return [
            {
                "symbol": selected_symbol,
                "dataset": "daily",
                "adjustment": adjustment,
                "provider": "ashare-data-foundation",
                "start_date": "2016-01-04",
                "end_date": str(release["as_of"]),
                "row_count": 0,
                "quality_status": "valid",
                "paths": [str(path)],
                "updated_at": str(release.get("created_at") or ""),
                "release_id": str(release["release_id"]),
            }
            for adjustment, path in paths.items()
        ]

    def health(self) -> dict[str, Any]:
        initial_health = self.foundation.health()
        if not initial_health["ok"]:
            return {
                "ok": False,
                "database": str(self.local_store.db_path),
                "provider": "ashare-data-foundation",
                "read_only": True,
                "foundation": initial_health,
                "instrument_count": 0,
                "instrument_catalog_count": 0,
                "active_instruments": 0,
                "coverage_count": 0,
                "warning_count": 1,
                "recent_issue_count": 1,
                "latest_open_date": "",
                "latest_daily_date": "",
                "daily_data_status": "provider_pending",
                "lagging_symbols": [],
                "lagging_symbol_count": 0,
                "last_run": None,
            }
        with self.foundation.pinned_release():
            foundation = self.foundation.health()
            instruments = self.list_instruments()
            active_count = sum(
                item["lifecycle"] in {"pinned", "tracking"} for item in instruments
            )
            return {
                "ok": bool(foundation["ok"]),
                "database": str(self.local_store.db_path),
                "provider": "ashare-data-foundation",
                "read_only": True,
                "foundation": foundation,
                "instrument_count": len(instruments),
                "instrument_catalog_count": self.instrument_catalog_count(),
                "active_instruments": active_count,
                "coverage_count": 2 if foundation["ok"] else 0,
                "warning_count": 0 if foundation["ok"] else 1,
                "recent_issue_count": 0,
                "latest_open_date": str(foundation.get("as_of") or ""),
                "latest_daily_date": str(foundation.get("as_of") or ""),
                "daily_data_status": "current" if foundation["ok"] else "provider_pending",
                "lagging_symbols": [],
                "lagging_symbol_count": 0,
                "last_run": None,
            }

    def enqueue_sync(
        self,
        symbol: str,
        *,
        dataset: str = "daily",
        priority: int = 50,
        start: date | None = None,
        end: date | None = None,
        reason: str = "manual",
    ) -> None:
        del dataset, priority, start, end, reason
        if self.get_instrument(symbol) is None:
            raise KeyError(f"instrument not found: {symbol}")
        # The shared foundation owns refresh and publication; consumers never enqueue
        # provider work or write a second copy of the daily facts.

    def pending_sync(self) -> list[dict[str, Any]]:
        return []

    def recent_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        del limit
        return []

    def quality_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        health = self.foundation.health()
        if health["ok"]:
            return []
        return [
            {
                "code": "foundation_unavailable",
                "severity": "error",
                "message": str(health.get("error") or "foundation unavailable"),
                "rows": 0,
            }
        ]

    def write_daily(
        self, frame: pd.DataFrame, *, symbol: str, adjustment: str
    ) -> list[str]:
        del frame, symbol, adjustment
        raise PermissionError("A-share foundation consumers are read-only")
