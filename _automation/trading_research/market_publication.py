from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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


_RUNTIME_STATE_TABLES = (
    "board_catalog",
    "board_daily",
    "board_fetch_queue",
    "board_memberships",
    "board_rank",
    "board_rps",
    "board_runs",
    "event_dossier_snapshots",
    "event_intraday_context",
    "event_method_interpretations",
    "event_method_research",
    "event_technical_context",
    "instrument_catalog",
    "market_cross_section_snapshots",
    "sync_queue",
)


class MarketDailyPublisher:
    """Build and atomically publish a latest-completed daily market snapshot."""

    def __init__(self, market_root: Path, post_store: Any, *, free_stockdb_url: str = "http://127.0.0.1:7899"):
        self.market_root = Path(market_root)
        self.post_store = post_store
        self.free_stockdb_url = free_stockdb_url

    def resolve_target_date(self, requested: str) -> date:
        if requested and requested != "auto":
            return date.fromisoformat(requested)
        provider = BaoStockMarketProvider()
        try:
            values = provider.fetch_calendar(date.today() - timedelta(days=730), date.today())
        finally:
            provider.close()
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

    def _validated_candidate_path(self, candidate: Path) -> Path:
        candidate = Path(candidate).resolve()
        expected_parent = self.market_root.resolve().parent
        if candidate.parent != expected_parent or not re.fullmatch(
            r"market-live-candidate-\d{8}-\d{6}", candidate.name
        ):
            raise RuntimeError(
                "candidate must be a dated market-live-candidate directory beside the live market root"
            )
        return candidate

    def _refresh_candidate_runtime_state(
        self, candidate: Path, *, refresh_instruments: bool
    ) -> dict[str, int]:
        """Copy mutable research state from live before resuming or publishing.

        A market candidate can take hours to build.  Board/event tables and
        instrument lifecycle fields may change meanwhile, but daily bars in
        the candidate remain valid.  Refreshing only the non-price state lets
        a crashed candidate resume without publishing stale research data.
        """

        candidate = self._validated_candidate_path(candidate)
        live_store = MarketStore(self.market_root)
        candidate_store = MarketStore(candidate)
        live_db = str(live_store.db_path.resolve()).replace("'", "''")
        copied: dict[str, int] = {}
        with live_store.lock(timeout=30), candidate_store.lock(timeout=30):
            with candidate_store.connect(lock=False) as db:
                local = {str(row[0]) for row in db.execute("SHOW TABLES").fetchall()}
                db.execute(f"ATTACH '{live_db}' AS live_state (READ_ONLY)")
                try:
                    available = {
                        str(row[0])
                        for row in db.execute(
                            "SELECT table_name FROM information_schema.tables WHERE table_catalog='live_state'"
                        ).fetchall()
                    }
                    for table in _RUNTIME_STATE_TABLES:
                        if table not in available or table not in local:
                            continue
                        quoted = '"' + table.replace('"', '""') + '"'
                        db.execute(f"DELETE FROM {quoted}")
                        db.execute(
                            f"INSERT INTO {quoted} SELECT * FROM live_state.{quoted}"
                        )
                        copied[table] = int(
                            db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                        )
                    if refresh_instruments:
                        db.execute(
                            """
                            UPDATE instruments SET lifecycle='archived',updated_at=?
                            WHERE symbol NOT IN (SELECT symbol FROM live_state.instruments)
                              AND lifecycle IN ('pinned','tracking')
                            """,
                            [_now_iso()],
                        )
                    db.execute(
                        "INSERT OR REPLACE INTO instruments SELECT * FROM live_state.instruments"
                    )
                    copied["instruments"] = int(
                        db.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
                    )
                finally:
                    db.execute("DETACH live_state")
        return copied

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
                # Tencent's public BJ endpoint occasionally returns an empty
                # payload for one request even though an immediate retry has
                # the completed bar. Treat it like the other network fallback.
                provider_attempts = 3 if provider.name in {"akshare", "tencent"} else 1
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

    @staticmethod
    def _run_powershell(command: str) -> subprocess.CompletedProcess[str]:
        # Explicit decoding keeps localized PowerShell errors from crashing a
        # subprocess reader thread before the publication report is written.
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _stop_ui_for_publication(self, ui_pid_path: Path | None) -> None:
        if not ui_pid_path or not ui_pid_path.exists():
            return
        try:
            pid = int(ui_pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return
        command = (
            f"$process=Get-Process -Id {pid} -ErrorAction SilentlyContinue;"
            "if($process){$process|Stop-Process -Force -ErrorAction Stop;"
            "$process|Wait-Process -Timeout 15 -ErrorAction Stop};"
            f"if(Get-Process -Id {pid} -ErrorAction SilentlyContinue){{throw 'process {pid} is still running'}}"
        )
        stopped = self._run_powershell(command)
        if stopped.returncode == 0:
            time.sleep(2)
            return

        direct_error = (stopped.stderr or stopped.stdout or "unknown stop error").strip()
        token = f"market-publish-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        request_path = ui_pid_path.parent / "stop.request.json"
        result_path = ui_pid_path.parent / "stop.result.json"
        request_path.write_text(json.dumps({"token": token}), encoding="utf-8")
        triggered = self._run_powershell(
            "Start-ScheduledTask -TaskName 'KOL_UI_Start' -ErrorAction Stop"
        )
        deadline = time.monotonic() + 30
        managed_result: dict[str, Any] = {}
        while triggered.returncode == 0 and time.monotonic() < deadline:
            if result_path.exists():
                try:
                    candidate_result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    candidate_result = {}
                if candidate_result.get("token") == token:
                    managed_result = candidate_result
                    break
            time.sleep(0.5)
        if managed_result.get("status") == "stopped":
            time.sleep(2)
            return
        request_path.unlink(missing_ok=True)
        trigger_error = (triggered.stderr or triggered.stdout or "managed stop timed out").strip()
        managed_error = str(managed_result.get("error") or trigger_error)
        raise RuntimeError(
            f"cannot stop KOL UI process {pid}: {direct_error[:500]}; "
            f"managed stop failed: {managed_error[:500]}"
        )

    def _publish_candidate(
        self,
        candidate: Path,
        previous: Path,
        *,
        target: date,
        stamp: str,
        baseline: set[str],
        admissions: MarketAdmissionRepository,
        ui_pid_path: Path | None,
    ) -> dict[str, int]:
        self._stop_ui_for_publication(ui_pid_path)
        self._refresh_candidate_runtime_state(candidate, refresh_instruments=False)
        self._preserve_runtime_state(candidate)
        os.replace(self.market_root, previous)
        try:
            os.replace(candidate, self.market_root)
            path_rewrites = self._rebase_market_paths()
            mode_path = self.market_root / "recovery-mode.json"
            mode_path.write_text(
                json.dumps(
                    {
                        "mode": "live",
                        "as_of": target.isoformat(),
                        "write_enabled": True,
                        "market_update_enabled": True,
                        "returns_update_enabled": True,
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
                    symbol
                    for symbol in final_raw
                    if final_raw.get(symbol) == target.isoformat()
                    and final_qfq.get(symbol) == target.isoformat()
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
        return path_rewrites

    def _rebase_market_paths(self) -> dict[str, int]:
        """Rewrite paths left behind by atomic candidate directory names."""

        parent = self.market_root.resolve().parent

        def rebase(value: str) -> str:
            if not value:
                return value
            path = Path(value)
            try:
                relative = path.resolve(strict=False).relative_to(parent)
            except (OSError, ValueError):
                return value
            if len(relative.parts) < 2:
                return value
            generation = relative.parts[0]
            if "candidate" not in generation.casefold():
                return value
            destination = self.market_root.joinpath(*relative.parts[1:])
            return str(destination) if destination.exists() else value

        rewrites = {"data_runs": 0, "data_coverage": 0}
        store = MarketStore(self.market_root)
        with store.lock(timeout=30), store.connect(lock=False) as db:
            for run_id, raw_path, normalized_json in db.execute(
                "SELECT run_id,raw_path,normalized_paths_json FROM data_runs"
            ).fetchall():
                new_raw = rebase(str(raw_path or ""))
                try:
                    normalized = json.loads(str(normalized_json or "[]"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(normalized, list):
                    continue
                new_normalized = [rebase(str(value)) for value in normalized]
                new_normalized_json = json.dumps(new_normalized, ensure_ascii=False)
                if new_raw != raw_path or new_normalized_json != normalized_json:
                    db.execute(
                        "UPDATE data_runs SET raw_path=?,normalized_paths_json=? WHERE run_id=?",
                        [new_raw, new_normalized_json, run_id],
                    )
                    rewrites["data_runs"] += 1
            for symbol, dataset, adjustment, paths_json in db.execute(
                "SELECT symbol,dataset,adjustment,paths_json FROM data_coverage"
            ).fetchall():
                try:
                    paths = json.loads(str(paths_json or "[]"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(paths, list):
                    continue
                new_paths_json = json.dumps(
                    [rebase(str(value)) for value in paths], ensure_ascii=False
                )
                if new_paths_json != paths_json:
                    db.execute(
                        """
                        UPDATE data_coverage SET paths_json=?
                        WHERE symbol=? AND dataset=? AND adjustment=?
                        """,
                        [new_paths_json, symbol, dataset, adjustment],
                    )
                    rewrites["data_coverage"] += 1
        return rewrites

    @staticmethod
    def _table_digest(store: MarketStore, table: str) -> dict[str, Any]:
        with store.connect() as db:
            present = db.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?",
                [table],
            ).fetchone()[0]
            if not present:
                return {"present": False, "count": 0, "sha256": ""}
            quoted = '"' + table.replace('"', '""') + '"'
            rows = db.execute(f"SELECT * FROM {quoted} ORDER BY ALL").fetchall()
        return {
            "present": True,
            "count": len(rows),
            "sha256": hashlib.sha256(repr(rows).encode("utf-8")).hexdigest(),
        }

    def _preserve_runtime_state(self, candidate: Path) -> None:
        for source in self.market_root.glob("freestockdb-*"):
            if source.is_file():
                shutil.copy2(source, candidate / source.name)
        live_logs = self.market_root / "logs"
        if live_logs.is_dir():
            shutil.copytree(live_logs, candidate / "logs", dirs_exist_ok=True)

    def promote_existing_candidate(
        self,
        candidate: Path,
        *,
        apply: bool,
        report_path: Path,
        ui_pid_path: Path | None = None,
    ) -> dict[str, Any]:
        candidate = Path(candidate).resolve()
        expected_parent = self.market_root.resolve().parent
        if candidate.parent != expected_parent or not re.fullmatch(
            r"market-live-candidate-\d{8}-\d{6}", candidate.name
        ):
            raise RuntimeError("candidate must be a dated market-live-candidate directory")
        if not (candidate / "market.duckdb").is_file():
            raise RuntimeError("candidate market.duckdb is missing")

        live_manifest = read_published_manifest(self.market_root)
        manifest = read_published_manifest(candidate)
        try:
            target = date.fromisoformat(str(manifest.get("as_of") or ""))
        except ValueError as exc:
            raise RuntimeError("candidate manifest has no valid as_of date") from exc
        baseline = {
            str(value)
            for value in manifest.get("baseline_symbols", [])
            if _valid_symbol(value)
        }
        extensions = {
            str(value)
            for value in manifest.get("extension_symbols", [])
            if _valid_symbol(value)
        }
        published = sorted(baseline | extensions)
        digest = hashlib.sha256("\n".join(published).encode("ascii")).hexdigest()
        failures: list[dict[str, Any]] = []
        if len(baseline) != 562:
            failures.append({"error": "candidate baseline count is not 562"})
        if int(manifest.get("published_count") or 0) != len(published):
            failures.append({"error": "candidate published count does not match symbols"})
        if str(manifest.get("symbols_sha256") or "").casefold() != digest:
            failures.append({"error": "candidate symbol digest does not match manifest"})
        live_baseline = {
            str(value)
            for value in live_manifest.get("baseline_symbols", [])
            if _valid_symbol(value)
        }
        if live_baseline != baseline:
            failures.append({"error": "candidate baseline differs from live baseline"})

        live_marker_before = (self.market_root / "market.duckdb").stat().st_mtime_ns
        live_store = MarketStore(self.market_root)
        candidate_store = MarketStore(candidate)
        live_active = {
            str(item["symbol"])
            for item in live_store.list_instruments()
            if item.get("lifecycle") in {"pinned", "tracking"}
            and _valid_symbol(item.get("symbol"))
        }
        candidate_active = {
            str(item["symbol"])
            for item in candidate_store.list_instruments()
            if item.get("lifecycle") in {"pinned", "tracking"}
            and _valid_symbol(item.get("symbol"))
        }
        if candidate_active != set(published):
            failures.append({"error": "candidate active set differs from its manifest"})
        if live_active != candidate_active:
            failures.append({"error": "live active set changed since candidate creation"})
        raw, qfq = _coverage_end_dates(candidate_store)
        incomplete = [
            symbol
            for symbol in published
            if raw.get(symbol) != target.isoformat()
            or qfq.get(symbol) != target.isoformat()
        ]
        if incomplete:
            failures.append(
                {
                    "error": "candidate raw/qfq coverage is incomplete",
                    "count": len(incomplete),
                    "sample": incomplete[:20],
                }
            )

        protected_state: dict[str, Any] = {}
        for table in _RUNTIME_STATE_TABLES:
            live_value = self._table_digest(live_store, table)
            candidate_value = self._table_digest(candidate_store, table)
            protected_state[table] = {
                "live_count": live_value["count"],
                "candidate_count": candidate_value["count"],
                "equal": live_value == candidate_value,
            }
            if live_value != candidate_value:
                failures.append({"error": f"protected table changed: {table}"})
        if (self.market_root / "market.duckdb").stat().st_mtime_ns != live_marker_before:
            failures.append({"error": "live market database changed during validation"})

        payload: dict[str, Any] = {
            "ok": not failures,
            "command": "market-daily-publish-repair-promote",
            "dry_run": not apply,
            "target_date": target.isoformat(),
            "candidate_root": str(candidate),
            "baseline_count": len(baseline),
            "published_count": len(published),
            "raw_current": sum(raw.get(symbol) == target.isoformat() for symbol in published),
            "qfq_current": sum(qfq.get(symbol) == target.isoformat() for symbol in published),
            "protected_state": protected_state,
            "failures": failures,
            "published": False,
        }
        if not failures and apply:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-recovery")
            previous = self.market_root.parent / f"market-live-previous-{stamp}"
            admissions = MarketAdmissionRepository(self.post_store)
            self._preserve_runtime_state(candidate)
            path_rewrites = self._publish_candidate(
                candidate,
                previous,
                target=target,
                stamp=stamp,
                baseline=baseline,
                admissions=admissions,
                ui_pid_path=ui_pid_path,
            )
            payload.update(
                {
                    "published": True,
                    "rollback_root": str(previous),
                    "mode": "live",
                    "market_update_enabled": True,
                    "returns_update_enabled": True,
                    "research_update_enabled": False,
                    "path_rewrites": path_rewrites,
                }
            )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    def apply(
        self,
        target: date,
        *,
        report_path: Path,
        runtime_root: Path,
        ui_pid_path: Path | None = None,
        candidate_root: Path | None = None,
    ) -> dict[str, Any]:
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
        candidate = (
            self._validated_candidate_path(candidate_root)
            if candidate_root is not None
            else self.market_root.parent / f"market-live-candidate-{stamp}"
        )
        previous = self.market_root.parent / f"market-live-previous-{stamp}"
        if previous.exists():
            raise RuntimeError(f"rollback directory already exists: {previous}")
        resumed = candidate.exists()
        resume_state_path = candidate / ".market-publish-resume.json"
        if resumed:
            if not (candidate / "market.duckdb").is_file():
                raise RuntimeError(f"candidate market database is missing: {candidate}")
            if resume_state_path.exists():
                try:
                    resume_state = json.loads(
                        resume_state_path.read_text(encoding="utf-8-sig")
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError("candidate resume metadata is invalid") from exc
                if str(resume_state.get("target_date") or "") != target.isoformat():
                    raise RuntimeError(
                        "candidate target date differs from the requested resume target"
                    )
            self._refresh_candidate_runtime_state(candidate, refresh_instruments=True)
        else:
            with live_store.lock(timeout=30):
                shutil.copytree(
                    self.market_root,
                    candidate,
                    ignore=shutil.ignore_patterns(".market.lock", "*.tmp", "*.tmp.*"),
                )
            resume_state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "target_date": target.isoformat(),
                        "created_at": _now_iso(),
                        "source_manifest_sha256": str(
                            manifest_before.get("symbols_sha256") or ""
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        candidate_store = MarketStore(candidate)
        providers = self._providers()
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        calendar_end = max(date.today(), target)
        calendar_start = calendar_end - timedelta(days=730)
        calendar_dates: list[date] = []
        try:
            calendar_dates = providers[0].fetch_calendar(calendar_start, calendar_end)
            if not calendar_dates:
                raise RuntimeError("BaoStock returned no trading calendar rows")
            candidate_store.replace_calendar(
                calendar_dates,
                provider="baostock",
                start=calendar_start,
                end=calendar_end,
            )
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
        live_active_after = {
            str(item["symbol"])
            for item in live_store.list_instruments()
            if item.get("lifecycle") in {"pinned", "tracking"}
            and _valid_symbol(item.get("symbol"))
        }
        if live_active_after != active:
            failures.append(
                {
                    "error": "live active set changed while candidate was building",
                    "active_before": len(active),
                    "active_after": len(live_active_after),
                }
            )
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
            "resumed": resumed,
            "calendar_start": calendar_start.isoformat(),
            "calendar_end": calendar_end.isoformat(),
            "calendar_open_dates": len(calendar_dates),
            "results_count": len(results),
            "failures": failures[:200],
            "published": False,
        }
        if failures:
            payload["candidate_preserved"] = candidate_root is not None
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if candidate_root is None:
                shutil.rmtree(candidate, ignore_errors=True)
            return payload
        write_published_manifest(
            candidate,
            as_of=target.isoformat(),
            baseline_symbols=baseline,
            extension_symbols=ready_extensions,
            source="market-daily-publish:baostock+tencent_fallbacks",
        )
        try:
            path_rewrites = self._publish_candidate(
                candidate,
                previous,
                target=target,
                stamp=stamp,
                baseline=baseline,
                admissions=admissions,
                ui_pid_path=ui_pid_path,
            )
        except Exception as exc:
            payload.update(
                {
                    "ok": False,
                    "failure_stage": "promotion",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raise
        payload.update({
            "published": True,
            "rollback_root": str(previous),
            "mode": "live",
            "market_update_enabled": True,
            "returns_update_enabled": True,
            "research_update_enabled": False,
            "path_rewrites": path_rewrites,
        })
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
