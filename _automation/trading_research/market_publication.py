from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from filelock import FileLock

from market_admissions import (
    MarketAdmissionRepository,
    read_published_manifest,
    write_published_manifest,
)
from market_data import (
    AKShareMarketProvider,
    BaoStockMarketProvider,
    FreeStockDBMarketProvider,
    TencentMarketProvider,
    Instrument,
    MarketStore,
    default_sync_start,
    sync_daily_bars,
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _valid_symbol(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{6}", str(value or "")))


def _instrument_type(symbol: str) -> str:
    if symbol == "000300" or symbol.startswith(("000", "399")) and symbol in {"000001", "000016", "000300", "000688", "000905", "000852", "399001", "399006"}:
        return "index"
    if symbol.startswith(("15", "50", "51", "56", "58")):
        return "etf"
    return "stock"


def _exchange(symbol: str, instrument_type: str) -> str:
    if instrument_type == "index":
        return "SZ" if symbol.startswith(("399",)) else "SH"
    if symbol.startswith(("4", "8", "92")):
        return "BJ"
    return "SH" if symbol.startswith(("5", "6", "9")) else "SZ"


def _coverage_end_dates(store: MarketStore) -> tuple[dict[str, str], dict[str, str]]:
    raw: dict[str, str] = {}
    qfq: dict[str, str] = {}
    for row in store.get_coverage():
        if row.get("dataset") != "daily":
            continue
        target = raw if row.get("adjustment") == "raw" else qfq if row.get("adjustment") == "qfq" else None
        if target is None:
            continue
        symbol = str(row.get("symbol") or "")
        end_date = str(row.get("end_date") or "")
        if end_date > target.get(symbol, ""):
            target[symbol] = end_date
    return raw, qfq


class MarketDailyPublisher:
    """Build and atomically publish a latest-completed daily market snapshot."""

    def __init__(
        self,
        market_root: Path,
        post_store: Any,
        *,
        free_stockdb_url: str = "http://127.0.0.1:7899",
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.market_root = Path(market_root)
        self.post_store = post_store
        self.free_stockdb_url = free_stockdb_url
        self.now_provider = now_provider or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))

    def resolve_target_date(self, requested: str) -> date:
        if requested and requested != "auto":
            return date.fromisoformat(requested)
        current = self.now_provider().astimezone(ZoneInfo("Asia/Shanghai"))
        provider = BaoStockMarketProvider()
        try:
            values = provider.fetch_calendar(current.date() - timedelta(days=730), current.date())
        finally:
            provider.close()
        if current.time() < clock_time(17, 0):
            values = [value for value in values if value < current.date()]
        if not values:
            raise RuntimeError("BaoStock returned no completed trading date")
        return max(values)

    def _providers(self) -> list[Any]:
        return [
            BaoStockMarketProvider(),
            FreeStockDBMarketProvider(base_url=self.free_stockdb_url, timeout_seconds=30),
            TencentMarketProvider(),
            AKShareMarketProvider(),
        ]

    def _sync_symbol(self, store: MarketStore, symbol: str, target: date, providers: list[Any]) -> list[dict[str, Any]]:
        instrument = store.get_instrument(symbol)
        if instrument is None:
            kind = _instrument_type(symbol)
            store.upsert_instrument(
                Instrument(
                    symbol,
                    symbol,
                    kind,
                    _exchange(symbol, kind),
                    lifecycle="archived",
                    source="market_daily_publish",
                )
            )
            instrument = store.get_instrument(symbol)
        assert instrument is not None
        existing_coverage = store.get_coverage(symbol)
        existing_raw = max(
            (str(row.get("end_date") or "") for row in existing_coverage if row.get("dataset") == "daily" and row.get("adjustment") == "raw"),
            default="",
        )
        existing_qfq = max(
            (str(row.get("end_date") or "") for row in existing_coverage if row.get("dataset") == "daily" and row.get("adjustment") == "qfq"),
            default="",
        )
        if existing_raw >= target.isoformat() and existing_qfq >= target.isoformat():
            return []
        results: list[dict[str, Any]] = []
        for adjustment in ("raw", "qfq"):
            start = default_sync_start(store, symbol, adjustment, fallback=date(1990, 1, 1))
            existing = next(
                (
                    row for row in store.get_coverage(symbol)
                    if row.get("dataset") == "daily" and row.get("adjustment") == adjustment
                ),
                None,
            )
            preserve_before = date.fromisoformat(str(existing["end_date"])) if existing and existing.get("end_date") else None
            attempts: list[dict[str, Any]] = []
            for provider in providers:
                provider_attempts = 3 if provider.name == "akshare" else 1
                for attempt_number in range(provider_attempts):
                    try:
                        result = sync_daily_bars(
                            store,
                            provider,
                            symbol,
                            start,
                            target,
                            adjustment=adjustment,
                            promote=True,
                            preserve_existing_before=preserve_before,
                        )
                        item = {"symbol": symbol, "adjustment": adjustment, "provider": provider.name, "attempt": attempt_number + 1, **result.__dict__}
                        attempts.append(item)
                        if result.quality_status not in {"quarantined"} and not result.error:
                            current = next(
                                (
                                    row for row in store.get_coverage(symbol)
                                    if row.get("dataset") == "daily" and row.get("adjustment") == adjustment
                                ),
                                None,
                            )
                            if current and current.get("end_date") and date.fromisoformat(str(current["end_date"])) >= target:
                                break
                    except Exception as exc:
                        attempts.append({"symbol": symbol, "adjustment": adjustment, "provider": provider.name, "attempt": attempt_number + 1, "error": str(exc)[:1000]})
                        if attempt_number + 1 < provider_attempts:
                            time.sleep(1)
                current = next(
                    (
                        row for row in store.get_coverage(symbol)
                        if row.get("dataset") == "daily" and row.get("adjustment") == adjustment
                    ),
                    None,
                )
                if current and current.get("end_date") and date.fromisoformat(str(current["end_date"])) >= target:
                    break
            results.extend(attempts)
        return results

    def preview(self, target: date) -> dict[str, Any]:
        store = MarketStore(self.market_root)
        manifest = read_published_manifest(self.market_root)
        active = sorted(
            str(item["symbol"])
            for item in store.list_instruments()
            if item.get("lifecycle") in {"pinned", "tracking"} and _valid_symbol(item.get("symbol"))
        )
        admissions = MarketAdmissionRepository(self.post_store, ensure_schema=False)
        pending = [str(item["symbol"]) for item in admissions.list(status="pending", limit=1000)]
        raw, qfq = _coverage_end_dates(store)
        return {
            "ok": True,
            "dry_run": True,
            "target_date": target.isoformat(),
            "active_symbols": len(active),
            "pending_admissions": len(pending),
            "raw_current": sum(raw.get(symbol) == target.isoformat() for symbol in active),
            "qfq_current": sum(qfq.get(symbol) == target.isoformat() for symbol in active),
            "published_manifest": manifest,
            "source": "baostock_primary;freestockdb_tencent_and_akshare_fallback",
        }

    def apply(self, target: date, *, report_path: Path, runtime_root: Path, ui_pid_path: Path | None = None) -> dict[str, Any]:
        live_store = MarketStore(self.market_root)
        manifest_before = read_published_manifest(self.market_root)
        baseline = {
            str(value) for value in manifest_before.get("baseline_symbols", []) if _valid_symbol(value)
        }
        if len(baseline) != 562:
            raise RuntimeError(f"published baseline must contain 562 symbols; found {len(baseline)}")
        existing_extensions = {
            str(value) for value in manifest_before.get("extension_symbols", []) if _valid_symbol(value)
        }
        active = {
            str(item["symbol"])
            for item in live_store.list_instruments()
            if item.get("lifecycle") in {"pinned", "tracking"} and _valid_symbol(item.get("symbol"))
        }
        admissions = MarketAdmissionRepository(self.post_store)
        pending = [str(item["symbol"]) for item in admissions.list(status="pending", limit=1000)]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = self.market_root.parent / f"market-live-candidate-{stamp}"
        previous = self.market_root.parent / f"market-live-previous-{stamp}"
        if candidate.exists() or previous.exists():
            raise RuntimeError(f"candidate or rollback directory already exists: {candidate}")
        marker_before = (self.market_root / "market.duckdb").stat().st_mtime_ns
        with live_store.lock(timeout=30):
            shutil.copytree(
                self.market_root,
                candidate,
                ignore=shutil.ignore_patterns(".market.lock", "*.tmp", "*.tmp.*"),
            )
        candidate_store = MarketStore(candidate)
        providers = self._providers()
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        try:
            for index, symbol in enumerate(sorted(active | set(pending)), start=1):
                results.extend(self._sync_symbol(candidate_store, symbol, target, providers))
                if index == 1 or index % 25 == 0 or index == len(active | set(pending)):
                    print(f"[market-daily-publish] {index}/{len(active | set(pending))}", flush=True)
        finally:
            for provider in providers:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
        raw, qfq = _coverage_end_dates(candidate_store)
        complete = {
            symbol for symbol in (active | set(pending))
            if raw.get(symbol) == target.isoformat() and qfq.get(symbol) == target.isoformat()
        }
        ready_extensions = (complete - baseline) | (existing_extensions & complete)
        for symbol in sorted(ready_extensions - active):
            current = candidate_store.get_instrument(symbol)
            if current is not None:
                candidate_store.restore_research_state(
                    symbol,
                    lifecycle="tracking",
                    last_mentioned_at=str(current.get("last_mentioned_at") or target.isoformat()),
                )
        published = baseline | ready_extensions
        candidate_active = {
            str(item["symbol"])
            for item in candidate_store.list_instruments()
            if item.get("lifecycle") in {"pinned", "tracking"}
        }
        incomplete = sorted(
            symbol for symbol in candidate_active
            if raw.get(symbol) != target.isoformat() or qfq.get(symbol) != target.isoformat()
        )
        if incomplete:
            failures.extend({"symbol": symbol, "error": "raw/qfq did not reach target date"} for symbol in incomplete[:200])
        if candidate_active != published:
            failures.append({"error": "candidate active set differs from published manifest", "candidate_active": len(candidate_active), "published": len(published)})
        marker_after = (self.market_root / "market.duckdb").stat().st_mtime_ns
        if marker_after != marker_before:
            failures.append({"error": "live market database changed while candidate was building"})
        payload: dict[str, Any] = {
            "ok": not failures,
            "command": "market-daily-publish",
            "dry_run": False,
            "target_date": target.isoformat(),
            "baseline_count": len(baseline),
            "active_before": len(active),
            "pending_before": len(pending),
            "published_count": len(published),
            "new_extensions": len(ready_extensions - existing_extensions),
            "raw_series": len([symbol for symbol in published if raw.get(symbol)]),
            "qfq_series": len([symbol for symbol in published if qfq.get(symbol)]),
            "raw_current": sum(raw.get(symbol) == target.isoformat() for symbol in published),
            "qfq_current": sum(qfq.get(symbol) == target.isoformat() for symbol in published),
            "candidate_root": str(candidate),
            "results_count": len(results),
            "failures": failures[:200],
            "published": False,
        }
        if failures:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            shutil.rmtree(candidate, ignore_errors=True)
            return payload
        write_published_manifest(
            candidate,
            as_of=target.isoformat(),
            baseline_symbols=baseline,
            extension_symbols=ready_extensions,
            source="market-daily-publish:baostock+tencent_fallbacks",
        )
        if ui_pid_path and ui_pid_path.exists():
            try:
                pid = int(ui_pid_path.read_text(encoding="ascii").strip())
                subprocess.run(["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"], check=False, capture_output=True)
                time.sleep(2)
            except (OSError, ValueError):
                pass
        os.replace(self.market_root, previous)
        try:
            os.replace(candidate, self.market_root)
            mode_path = self.market_root / "recovery-mode.json"
            mode_path.write_text(
                json.dumps(
                    {
                        "mode": "live",
                        "as_of": target.isoformat(),
                        "write_enabled": True,
                        "market_update_enabled": True,
                        "returns_update_enabled": False,
                        "research_update_enabled": False,
                        "publication_mode": "atomic_daily",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            final_store = MarketStore(self.market_root)
            final_raw, final_qfq = _coverage_end_dates(final_store)
            admissions.reconcile_confirmed(
                as_of=target.isoformat(),
                baseline_symbols=baseline,
                complete_symbols={
                    symbol for symbol in final_raw
                    if final_raw.get(symbol) == target.isoformat() and final_qfq.get(symbol) == target.isoformat()
                },
                raw_end_dates=final_raw,
                qfq_end_dates=final_qfq,
                apply=True,
            )
        except Exception:
            if self.market_root.exists():
                failed_root = self.market_root.parent / f"market-live-failed-{stamp}"
                os.replace(self.market_root, failed_root)
            os.replace(previous, self.market_root)
            raise
        payload.update({
            "published": True,
            "rollback_root": str(previous),
            "mode": "live",
            "market_update_enabled": True,
            "returns_update_enabled": False,
            "research_update_enabled": False,
        })
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
