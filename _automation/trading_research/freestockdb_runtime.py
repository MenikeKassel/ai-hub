from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from filelock import FileLock

from market_data import (
    AKShareMarketProvider,
    BaoStockMarketProvider,
    FreeStockDBMarketProvider,
)


_FILE = Path(__file__).resolve()
_REPO_ROOT = _FILE.parents[2]
_WORKSPACE_ROOT = _FILE.parents[3]

def _infer_data_root(root: Path) -> Path:
    """Prefer the resolved stockdb/data junction over a stale E: live folder."""
    linked_data = root / "data"
    try:
        resolved = linked_data.resolve(strict=False)
        if linked_data.exists() and resolved != linked_data:
            # The compatibility junction may target the canonical ``live``
            # directory itself; the runtime root is its parent so Paths.from_root
            # can consistently derive live/staging/previous siblings.
            return resolved.parent if resolved.name.casefold() == "live" else resolved
    except OSError:
        pass
    return root / "live"


DEFAULT_ROOT = Path(os.environ.get("FREESTOCKDB_ROOT", _WORKSPACE_ROOT / "stockdb"))
DEFAULT_DATA_ROOT = Path(
    os.environ.get("FREESTOCKDB_DATA_ROOT", _infer_data_root(DEFAULT_ROOT))
)
DEFAULT_URL = os.environ.get("FREESTOCKDB_URL", "http://127.0.0.1:7899")
DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("TRADING_RUNTIME_ROOT", Path(__file__).resolve().parents[2] / "_runtime" / "trading")
)
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
EXPECTED_RELEASE = "v0.2.1"
EXPECTED_SERVER_SHA256 = "ccd847e9221f57eafc4c1c995ed52b2e9e0d3172bfe5ee8ebce5251b9f4ea0bb"
EXPECTED_UPDATER_SHA256 = "011ef6c6b620126db7e1cc8d5fc9214da13faf4cc66ef4da0484987d7ad48b1a"
MAX_CLOSE_WAIT = 20
MIN_CATALOG_SYMBOLS = 5_000


@dataclass(frozen=True)
class FreeStockDBPaths:
    root: Path
    storage_root: Path
    server: Path
    updater: Path
    config: Path
    source: Path
    data: Path
    live: Path
    staging: Path
    previous: Path
    logs: Path
    state: Path
    update_state: Path
    lock: Path

    @classmethod
    def from_root(
        cls,
        root: Path,
        runtime_root: Path,
        data_root: Path,
    ) -> "FreeStockDBPaths":
        external_storage = data_root.resolve() != root.resolve()
        return cls(
            root=root,
            storage_root=data_root,
            server=root / "stockdb.exe",
            updater=root / "数据更新.exe",
            config=root / "stockdb.conf",
            source=root / "sync_url.txt",
            data=root / "data",
            live=data_root / "live" if external_storage else root / "data",
            staging=data_root / "staging" if external_storage else root / "update.next",
            previous=data_root / "previous" if external_storage else root / "data.prev",
            logs=runtime_root / "market" / "logs",
            state=runtime_root / "market" / "freestockdb-state.json",
            update_state=runtime_root / "market" / "freestockdb-update-state.json",
            lock=root / ".freestockdb-runtime.lock",
        )


@dataclass(frozen=True)
class VendorUpdateResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    completion: str = "process_exit"


def _iso_from_epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FreeStockDBRuntime:
    """Manage the local FreeStockDB process and its vendor dataset safely.

    The vendor updater is not trusted to report failed files through its exit
    code.  Every non-dry update therefore performs a separate verification and
    a loopback smoke query before the staged directory becomes active.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        base_url: str | None = None,
        runtime_root: Path | str | None = None,
        *,
        data_root: Path | str | None = None,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        socket_probe: Callable[[str, int, float], bool] | None = None,
        server_sha256: str = EXPECTED_SERVER_SHA256,
        updater_sha256: str = EXPECTED_UPDATER_SHA256,
    ) -> None:
        resolved_root = Path(root or os.environ.get("FREESTOCKDB_ROOT", DEFAULT_ROOT)).expanduser()
        if data_root is not None:
            resolved_data_root = Path(data_root).expanduser()
        elif root is None:
            resolved_data_root = Path(
                os.environ.get("FREESTOCKDB_DATA_ROOT", _infer_data_root(resolved_root))
            ).expanduser()
        elif resolved_root.resolve() == DEFAULT_ROOT.resolve():
            resolved_data_root = Path(
                os.environ.get("FREESTOCKDB_DATA_ROOT", _infer_data_root(resolved_root))
            ).expanduser()
        else:
            resolved_data_root = resolved_root
        inferred_data_root = _infer_data_root(resolved_root)
        self.configuration_conflict = (
            str(resolved_data_root.resolve()) != str(inferred_data_root.resolve())
            and bool(data_root or os.environ.get("FREESTOCKDB_DATA_ROOT"))
        )
        self.paths = FreeStockDBPaths.from_root(
            resolved_root,
            Path(runtime_root or DEFAULT_RUNTIME_ROOT).expanduser(),
            resolved_data_root,
        )
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self._process_runner = process_runner or subprocess.run
        self._command_runner = command_runner or subprocess.run
        self._socket_probe = socket_probe or self._probe_socket
        self.expected_server_sha256 = server_sha256.casefold()
        self.expected_updater_sha256 = updater_sha256.casefold()

    @property
    def host(self) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        return parsed.hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        return int(parsed.port or (443 if parsed.scheme == "https" else 80))

    def _probe_socket(self, host: str, port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _is_directory_link(self, path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
            return bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        except OSError:
            return False

    def _same_directory(self, left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return False

    def _storage_layout(self) -> dict[str, Any]:
        if self.paths.storage_root.resolve() == self.paths.root.resolve():
            return {"status": "local", "canonical": True}

        data_exists = self.paths.data.is_dir()
        live_exists = self.paths.live.is_dir()
        data_link = data_exists and self._is_directory_link(self.paths.data)
        live_link = live_exists and self._is_directory_link(self.paths.live)
        same_target = False
        if data_exists and live_exists:
            same_target = self._same_directory(self.paths.data, self.paths.live)

        if data_link and not live_link and same_target:
            status = "canonical"
        elif live_link and not data_link and same_target:
            status = "reversed"
        elif data_exists and not live_exists:
            status = "not_migrated"
        elif not data_exists and live_exists:
            status = "missing_compatibility_link"
        else:
            status = "conflict"
        return {
            "status": status,
            "canonical": status == "canonical",
            "data_exists": data_exists,
            "live_exists": live_exists,
            "data_is_link": data_link,
            "live_is_link": live_link,
            "same_target": same_target,
        }

    def _processes(self) -> list[dict[str, Any]]:
        if os.name == "nt":
            script = (
                "Get-CimInstance Win32_Process -Filter \"Name='stockdb.exe'\" | "
                "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
            )
            try:
                result = self._process_runner(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                output = (result.stdout or "").strip()
                if not output:
                    return []
                decoded = json.loads(output)
                items = decoded if isinstance(decoded, list) else [decoded]
                return [dict(item) for item in items if isinstance(item, dict)]
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                return []
        try:
            result = self._process_runner(
                ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return []
        values: list[dict[str, Any]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2 and self.paths.server.name in parts[1]:
                values.append({"ProcessId": parts[0], "CommandLine": parts[1], "ExecutablePath": ""})
        return values

    def _listening_addresses(self) -> dict[str, Any]:
        if os.name != "nt":
            return {"addresses": [], "verified": False, "error": "unsupported_platform"}
        script = (
            f"Get-NetTCPConnection -LocalPort {self.port} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalAddress "
            "-Unique | ConvertTo-Json -Compress"
        )
        try:
            result = self._process_runner(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                return {
                    "addresses": [],
                    "verified": False,
                    "error": (result.stderr or result.stdout or "listener query failed")[:500],
                }
            output = (result.stdout or "").strip()
            if not output:
                return {"addresses": [], "verified": True}
            decoded = json.loads(output)
            addresses = decoded if isinstance(decoded, list) else [decoded]
            return {
                "addresses": sorted({str(item) for item in addresses if item}),
                "verified": True,
            }
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return {"addresses": [], "verified": False, "error": str(exc)[:500]}

    def _config_security(self) -> dict[str, Any]:
        if not self.paths.config.is_file():
            return {"safe": False, "ip": "", "readonly": "", "error": "missing_config"}
        try:
            content = self.paths.config.read_text(encoding="utf-8")
        except OSError as exc:
            return {"safe": False, "ip": "", "readonly": "", "error": str(exc)[:500]}
        ip_match = re.search(r"(?mi)^\s*ip\s*:\s*([^\s#]+)", content)
        readonly_match = re.search(r"(?mi)^\s*readonly\s*:\s*([^\s#]+)", content)
        configured_ip = ip_match.group(1).strip() if ip_match else ""
        readonly = readonly_match.group(1).strip().lower() if readonly_match else ""
        return {
            "safe": configured_ip in {"127.0.0.1", "::1"}
            and readonly in {"1", "true", "yes", "on"},
            "ip": configured_ip,
            "readonly": readonly,
        }

    def exact_processes(self) -> list[dict[str, Any]]:
        expected = str(self.paths.server.resolve()).lower()
        values: list[dict[str, Any]] = []
        for item in self._processes():
            executable = str(item.get("ExecutablePath") or "")
            command_line = str(item.get("CommandLine") or "")
            if executable and str(Path(executable).resolve()).lower() == expected:
                values.append(item)
            elif not executable and expected in command_line.lower():
                values.append(item)
        return values

    def _manifest(self, data_root: Path | None = None) -> dict[str, Any]:
        path = (data_root or self.paths.data) / ".sync_manifest.json"
        if not path.is_file():
            return {"path": str(path), "exists": False, "file_count": 0}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            files = value.get("files", []) if isinstance(value, dict) else []
            return {
                "path": str(path),
                "exists": True,
                "version": value.get("version", "") if isinstance(value, dict) else "",
                "file_count": len(files) if isinstance(files, list) else 0,
                "generated_at": _iso_from_epoch(value.get("generated_at")) if isinstance(value, dict) else "",
                "size_bytes": path.stat().st_size,
            }
        except (OSError, json.JSONDecodeError) as exc:
            return {"path": str(path), "exists": True, "file_count": 0, "error": str(exc)}

    def _binary(self) -> dict[str, Any]:
        if not self.paths.server.is_file():
            return {
                "path": str(self.paths.server),
                "exists": False,
                "release": EXPECTED_RELEASE,
                "sha256": "",
                "expected_sha256": self.expected_server_sha256,
                "verified": False,
                "updater_sha256": "",
                "expected_updater_sha256": self.expected_updater_sha256,
                "updater_verified": False,
            }
        server_sha256 = _sha256(self.paths.server)
        updater_sha256 = _sha256(self.paths.updater) if self.paths.updater.is_file() else ""
        return {
            "path": str(self.paths.server),
            "exists": True,
            "release": EXPECTED_RELEASE,
            "size_bytes": self.paths.server.stat().st_size,
            "sha256": server_sha256,
            "expected_sha256": self.expected_server_sha256,
            "verified": server_sha256.casefold() == self.expected_server_sha256,
            "updater_sha256": updater_sha256,
            "expected_updater_sha256": self.expected_updater_sha256,
            "updater_verified": bool(
                updater_sha256
                and updater_sha256.casefold() == self.expected_updater_sha256
            ),
        }

    def _close_wait_count(self) -> int:
        if os.name != "nt":
            return 0
        script = (
            f"@(Get-NetTCPConnection -LocalPort {self.port} -State CloseWait "
            "-ErrorAction SilentlyContinue).Count"
        )
        try:
            result = self._process_runner(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return int((result.stdout or "0").strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0

    def _disk(self) -> dict[str, Any]:
        try:
            usage_root = self.paths.storage_root
            usage_root.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(usage_root)
            data_bytes = sum(
                item.stat().st_size
                for item in self.paths.data.rglob("*")
                if item.is_file()
            ) if self.paths.data.is_dir() else 0
            staged_data = self.paths.staging / "data"
            staged_marker = self.paths.staging / ".aihub-staging.json"
            reusable_path = (
                staged_data
                if staged_data.is_dir() and staged_marker.is_file()
                else self.paths.previous
                if self.paths.previous.is_dir()
                and not self._is_directory_link(self.paths.previous)
                else None
            )
            candidate_bytes = (
                sum(
                    item.stat().st_size
                    for item in reusable_path.rglob("*")
                    if item.is_file()
                )
                if reusable_path
                else 0
            )
            required_bytes = max(data_bytes - candidate_bytes, 0) + MIN_FREE_BYTES
            return {
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "free_gb": round(usage.free / 1024**3, 2),
                "data_bytes": data_bytes,
                "data_gb": round(data_bytes / 1024**3, 2),
                "candidate_bytes": candidate_bytes,
                "candidate_gb": round(candidate_bytes / 1024**3, 2),
                "required_for_safe_update_bytes": required_bytes,
                "required_for_safe_update_gb": round(required_bytes / 1024**3, 2),
                "staging_strategy": (
                    "resume_or_previous" if reusable_path else "initial_copy"
                ),
                "guard_ok": usage.free >= MIN_FREE_BYTES,
                "update_guard_ok": usage.free >= required_bytes,
                "path": str(usage_root),
            }
        except (OSError, PermissionError) as exc:
            return {"free_bytes": 0, "free_gb": 0, "guard_ok": False, "update_guard_ok": False, "error": str(exc)}

    def _source_url(self) -> str:
        try:
            for line in self.paths.source.read_text(encoding="utf-8").splitlines():
                value = line.strip()
                if value and not value.startswith("#"):
                    return value
            return ""
        except (OSError, IndexError):
            return ""

    def _read_state(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return {"status": "invalid"}

    def _update_state(self) -> dict[str, Any] | None:
        current = self._read_state(self.paths.update_state)
        if current is not None:
            return current
        legacy = self._read_state(self.paths.state)
        if legacy and legacy.get("status") in {"running", "failed", "updated", "dry_run"}:
            return legacy
        return None

    def expected_trade_date(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        explicit = os.environ.get("FREESTOCKDB_EXPECTED_DATE", "").strip()
        if explicit:
            try:
                return {"date": date.fromisoformat(explicit), "source": "environment"}
            except ValueError:
                return {"date": None, "source": "invalid_environment", "error": explicit}

        current = as_of or datetime.now().astimezone()
        cutoff = current.date() - timedelta(days=1) if current.hour < 15 else current.date()
        foundation_root = Path(
            os.environ.get("ASHARE_DATA_ROOT", r"F:\ai-data\ashare")
        ).expanduser()
        pointer = foundation_root / "current.json"
        if pointer.is_file():
            try:
                release = json.loads(pointer.read_text(encoding="utf-8"))
                value = date.fromisoformat(str(release.get("as_of")))
                if value <= cutoff:
                    return {
                        "date": value,
                        "source": "ashare_foundation_current",
                        "release_id": str(release.get("release_id") or ""),
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        database = self.paths.state.parent / "market.duckdb"
        if database.is_file():
            try:
                import duckdb

                with duckdb.connect(str(database), read_only=True) as connection:
                    row = connection.execute(
                        "SELECT MAX(CAST(trade_date AS DATE)) FROM trading_calendar "
                        "WHERE is_open=true AND CAST(trade_date AS DATE)<=?",
                        [cutoff],
                    ).fetchone()
                if row and row[0]:
                    value = row[0]
                    if isinstance(value, datetime):
                        value = value.date()
                    return {"date": value, "source": "local_market_calendar"}
            except Exception as exc:
                return {
                    "date": None,
                    "source": "local_market_calendar_unavailable",
                    "error": str(exc)[:500],
                }
        return {"date": None, "source": "unknown"}

    def _sample_health(
        self,
        *,
        expected_trade_date: date | None = None,
    ) -> dict[str, Any]:
        provider = FreeStockDBMarketProvider(base_url=self.base_url)
        try:
            result = provider.health()
            samples: dict[str, Any] = {}
            for symbol in ("600900", "600519", "159139"):
                try:
                    end = datetime.now().date()
                    start = end - timedelta(days=30)
                    frame = provider.fetch_daily(
                        symbol,
                        "etf" if symbol == "159139" else "stock",
                        start,
                        end,
                        "raw",
                    )
                    samples[symbol] = {
                        "rows": len(frame),
                        "latest": (
                            str(frame["date"].max())
                            if not frame.empty and "date" in frame
                            else ""
                        ),
                    }
                except Exception as exc:
                    samples[symbol] = {"rows": 0, "error": str(exc)[:300]}
            sample_errors = [symbol for symbol, item in samples.items() if not item.get("rows")]
            stale_symbols = [
                symbol
                for symbol, item in samples.items()
                if expected_trade_date
                and item.get("latest")
                and str(item["latest"]) < expected_trade_date.isoformat()
            ]
            freshness_status = (
                "stale"
                if stale_symbols or (expected_trade_date and sample_errors)
                else "current"
                if expected_trade_date
                else "unknown"
            )
            return {
                **result,
                "samples": samples,
                "sample_errors": sample_errors,
                "freshness": {
                    "status": freshness_status,
                    "expected_trade_date": (
                        expected_trade_date.isoformat() if expected_trade_date else ""
                    ),
                    "stale_symbols": stale_symbols,
                },
                "ok": bool(
                    result.get("ok")
                    and not sample_errors
                    and freshness_status != "stale"
                ),
            }
        finally:
            provider.close()

    def _basic_provider_health(self) -> dict[str, Any]:
        provider = FreeStockDBMarketProvider(base_url=self.base_url)
        try:
            return provider.health()
        finally:
            provider.close()

    def _external_sample_audit(
        self,
        expected_trade_date: date | None = None,
    ) -> dict[str, Any]:
        local = FreeStockDBMarketProvider(base_url=self.base_url)
        sources = [BaoStockMarketProvider(), AKShareMarketProvider()]
        comparisons: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            end = datetime.now().date()
            start = end - timedelta(days=90)
            for symbol, instrument_type in (("600900", "stock"), ("159139", "etf")):
                for adjustment in ("raw", "qfq"):
                    try:
                        local_frame = local.fetch_daily(
                            symbol,
                            instrument_type,
                            start,
                            end,
                            adjustment,
                        )
                    except Exception as exc:
                        errors.append(f"freestockdb:{symbol}:{adjustment}:{exc}")
                        continue
                    for source in sources:
                        try:
                            external = source.fetch_daily(
                                symbol,
                                instrument_type,
                                start,
                                end,
                                adjustment,
                            )
                            left = local_frame[["date", "close"]].copy()
                            right = external[["date", "close"]].copy()
                            left["date"] = left["date"].astype(str)
                            right["date"] = right["date"].astype(str)
                            merged = left.merge(
                                right,
                                on="date",
                                suffixes=("_local", "_external"),
                            ).dropna()
                            if expected_trade_date:
                                merged = merged[
                                    merged["date"] == expected_trade_date.isoformat()
                                ]
                            if merged.empty:
                                raise RuntimeError(
                                    "no overlapping sample date"
                                    + (
                                        f" for {expected_trade_date.isoformat()}"
                                        if expected_trade_date
                                        else ""
                                    )
                                )
                            row = merged.iloc[-1]
                            local_close = float(row["close_local"])
                            external_close = float(row["close_external"])
                            difference = abs(local_close - external_close) / max(
                                abs(external_close),
                                1e-12,
                            )
                            comparisons.append(
                                {
                                    "symbol": symbol,
                                    "adjustment": adjustment,
                                    "provider": source.name,
                                    "date": str(row["date"]),
                                    "local_close": local_close,
                                    "external_close": external_close,
                                    "difference": difference,
                                    "ok": difference <= 0.005,
                                }
                            )
                        except Exception as exc:
                            errors.append(
                                f"{source.name}:{symbol}:{adjustment}:{str(exc)[:300]}"
                            )
            required_pairs = {
                (item["symbol"], item["adjustment"])
                for item in comparisons
                if item["ok"]
            }
            conflicts = [item for item in comparisons if not item["ok"]]
            required_pairs_expected = {
                ("600900", "raw"),
                ("600900", "qfq"),
                ("159139", "raw"),
            }
            optional_pairs = {("159139", "qfq")}
            missing_required = required_pairs_expected - required_pairs
            missing_optional = optional_pairs - required_pairs
            return {
                "ok": not conflicts and not missing_required,
                "comparisons": comparisons,
                "conflicts": conflicts,
                "missing_pairs": sorted(missing_required),
                "optional_missing_pairs": sorted(missing_optional),
                "warnings": (
                    ["optional_etf_qfq_factor_unavailable"]
                    if missing_optional
                    else []
                ),
                "errors": errors,
            }
        finally:
            local.close()
            for source in sources:
                close = getattr(source, "close", None)
                if callable(close):
                    close()

    def doctor(
        self,
        *,
        include_samples: bool = True,
        expected_trade_date: date | None = None,
    ) -> dict[str, Any]:
        processes = self.exact_processes()
        port_open = self._socket_probe(self.host, self.port, 0.5)
        source_url = self._source_url()
        disk = self._disk()
        service_status = "running" if processes and port_open else "unhealthy" if processes or port_open else "stopped"
        port_conflict = port_open and not processes
        expected = (
            {"date": expected_trade_date, "source": "argument"}
            if expected_trade_date
            else self.expected_trade_date()
        )
        provider = (
            self._sample_health(expected_trade_date=expected.get("date"))
            if port_open and include_samples
            else self._basic_provider_health()
            if port_open
            else {"ok": False, "error": "loopback port is not open"}
        )
        provider["sampled"] = include_samples
        manifest = self._manifest()
        binary = self._binary()
        close_wait_count = self._close_wait_count()
        source_exists = self.paths.source.is_file() and bool(source_url)
        storage_layout = self._storage_layout()
        config_security = self._config_security()
        listener = (
            self._listening_addresses()
            if port_open
            else {"addresses": [], "verified": True}
        )
        listener_addresses = listener.get("addresses", [])
        loopback_only = (
            bool(
                self.host in {"127.0.0.1", "::1", "localhost"}
                and listener.get("verified")
                and listener_addresses
                and all(
                    address in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
                    for address in listener_addresses
                )
            )
            if port_open
            else self.host in {"127.0.0.1", "::1", "localhost"}
        )
        service_ok = service_status == "running" and not port_conflict
        checks = {
            "server": bool(binary.get("verified")),
            "updater": bool(binary.get("updater_verified")),
            "config": bool(config_security.get("safe")),
            "source": source_exists,
            "data": self.paths.data.is_dir(),
            "manifest": bool(manifest.get("exists") and manifest.get("file_count", 0)),
            "disk": bool(disk.get("guard_ok")),
            "storage_layout": bool(storage_layout.get("canonical")),
            "service": service_ok,
            "loopback": loopback_only,
            "provider": bool(provider.get("ok")),
            "freshness": (
                provider.get("freshness", {}).get("status") == "current"
                if include_samples
                else None
            ),
            "update_disk": bool(disk.get("update_guard_ok")),
        }
        service_checks = {
            key: value
            for key, value in checks.items()
            if key not in {"update_disk"} and value is not None
        }
        update_prerequisites = {
            key: value
            for key, value in checks.items()
            if key in {
                "server",
                "updater",
                "config",
                "source",
                "data",
                "manifest",
                "disk",
                "storage_layout",
                "loopback",
                "update_disk",
            }
        }
        return {
            "ok": all(service_checks.values()),
            "service_ok": service_ok,
            "data_fresh": provider.get("freshness", {}).get("status") == "current",
            "update_ready": all(update_prerequisites.values()),
            "checks": checks,
            "root": str(self.paths.root),
            "server": str(self.paths.server),
            "updater": str(self.paths.updater),
            "config": str(self.paths.config),
            "source": source_url,
            "data": str(self.paths.data),
            "storage_root": str(self.paths.storage_root),
            "configuration_conflict": self.configuration_conflict,
            "inferred_data_root": str(_infer_data_root(self.paths.root)),
            "live": str(self.paths.live),
            "staging": str(self.paths.staging),
            "previous": str(self.paths.previous),
            "storage_migrated": bool(storage_layout.get("canonical")),
            "storage_layout": storage_layout,
            "server_exists": self.paths.server.is_file(),
            "updater_exists": self.paths.updater.is_file(),
            "config_exists": self.paths.config.is_file(),
            "config_security": config_security,
            "data_exists": self.paths.data.is_dir(),
            "service_status": service_status,
            "processes": processes,
            "port": self.port,
            "port_open": port_open,
            "port_conflict": port_conflict,
            "listener": {
                **listener,
                "loopback_only": loopback_only,
            },
            "close_wait_count": close_wait_count,
            "connection_leak": close_wait_count > MAX_CLOSE_WAIT,
            "disk": disk,
            "manifest": manifest,
            "binary": binary,
            "swap_pending": (self.paths.storage_root / ".freestockdb-swap.json").is_file(),
            "provider": provider,
            "catalog_symbols": int(provider.get("catalog_symbols") or 0),
            "catalog_groups": int(provider.get("catalog_groups") or 0),
            "freshness": {
                **provider.get("freshness", {"status": "unknown", "stale_symbols": []}),
                "expected_trade_date_source": expected.get("source", "unknown"),
                **({"expected_trade_date_error": expected.get("error")} if expected.get("error") else {}),
            },
            "transport_warning": "untrusted_transport" if source_url.lower().startswith("http://") else "",
            "last_update": self._update_state(),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _write_log(self, event: str, payload: dict[str, Any]) -> None:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        path = self.paths.logs / "freestockdb-update.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "at": datetime.now().astimezone().isoformat(), **payload}, ensure_ascii=False) + "\n")

    def _save_state(self, payload: dict[str, Any]) -> None:
        try:
            self.paths.state.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.paths.state.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.paths.state)
        except OSError:
            # The state file is observability only; it must not turn a verified
            # dataset update into a false failure.
            pass

    def _save_update_state(self, payload: dict[str, Any]) -> None:
        try:
            self.paths.update_state.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.paths.update_state.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, self.paths.update_state)
        except OSError:
            pass

    def _run_command(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._command_runner(
            command,
            cwd=str(cwd or self.paths.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _tree_activity(self, data_root: Path) -> tuple[int, int, int, int]:
        file_count = 0
        total_size = 0
        latest_mtime = 0
        partials = 0
        try:
            candidates = data_root.rglob("*")
            for item in candidates:
                try:
                    if not item.is_file():
                        continue
                    item_stat = item.stat()
                except OSError:
                    # LevelDB compaction can replace a file between rglob and
                    # stat while the vendor updater is still running.
                    continue
                file_count += 1
                total_size += item_stat.st_size
                latest_mtime = max(latest_mtime, item_stat.st_mtime_ns)
                if item.name.endswith((".part", ".merge.part", ".tmp")):
                    partials += 1
        except OSError:
            # A transient directory replacement is itself activity. Returning
            # an empty snapshot prevents the updater from being closed early.
            return (0, 0, time.time_ns(), 1)
        return (file_count, total_size, latest_mtime, partials)

    def _windows_process_cpu_seconds(self, pid: int) -> float | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                kernel_ticks = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
                user_ticks = (user.dwHighDateTime << 32) | user.dwLowDateTime
                return (kernel_ticks + user_ticks) / 10_000_000
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return None

    def _close_windows_for_pid(self, pid: int) -> bool:
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            windows: list[int] = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.IsWindow.argtypes = [wintypes.HWND]
            user32.IsWindow.restype = wintypes.BOOL
            user32.PostMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostMessageW.restype = wintypes.BOOL

            @callback_type
            def collect(window: int, _parameter: int) -> bool:
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
                if owner.value == pid and user32.IsWindow(window):
                    windows.append(window)
                return True

            user32.EnumWindows(collect, 0)
            for window in windows:
                user32.PostMessageW(window, 0x0010, 0, 0)
            return bool(windows)
        except (AttributeError, OSError):
            return False

    def _run_vendor_updater(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        cwd: Path,
    ) -> VendorUpdateResult:
        if os.name != "nt" or self._command_runner is not subprocess.run:
            result = self._run_command(command, timeout=timeout, cwd=cwd)
            return VendorUpdateResult(
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

        minimum_runtime = max(
            30.0,
            float(os.environ.get("FREESTOCKDB_UPDATER_MIN_RUNTIME_SECONDS", "300")),
        )
        quiet_seconds = max(
            30.0,
            float(os.environ.get("FREESTOCKDB_UPDATER_QUIESCENCE_SECONDS", "120")),
        )
        poll_seconds = 5.0
        stdout_path = cwd / ".aihub-updater.stdout.log"
        stderr_path = cwd / ".aihub-updater.stderr.log"
        stdout_file = stdout_path.open("w", encoding="utf-8", errors="replace")
        stderr_file = stderr_path.open("w", encoding="utf-8", errors="replace")
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )
            started = time.monotonic()
            previous_activity = self._tree_activity(cwd / "data")
            previous_cpu = self._windows_process_cpu_seconds(process.pid)
            quiet_since: float | None = None
            completion = "process_exit"
            while process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout:
                    self._close_windows_for_pid(process.pid)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    raise subprocess.TimeoutExpired(list(command), timeout)

                time.sleep(poll_seconds)
                activity = self._tree_activity(cwd / "data")
                cpu = self._windows_process_cpu_seconds(process.pid)
                cpu_delta = (
                    cpu - previous_cpu
                    if cpu is not None and previous_cpu is not None
                    else 0.0
                )
                busy = activity != previous_activity or activity[3] > 0 or cpu_delta > 0.5
                if busy:
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = now
                previous_activity = activity
                previous_cpu = cpu

                if (
                    now - started >= minimum_runtime
                    and quiet_since is not None
                    and now - quiet_since >= quiet_seconds
                ):
                    completion = "quiescent_gui_close"
                    self._close_windows_for_pid(process.pid)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        completion = "quiescent_terminate"
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                    break
            process.wait(timeout=10)
            stdout_file.flush()
            stderr_file.flush()
            stdout_file.close()
            stderr_file.close()
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")[-10_000:]
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-10_000:]
            return VendorUpdateResult(
                returncode=(
                    1
                    if completion == "quiescent_terminate"
                    else int(process.returncode or 0)
                ),
                stdout=stdout or "",
                stderr=stderr or "",
                completion=completion,
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if not stdout_file.closed:
                stdout_file.close()
            if not stderr_file.closed:
                stderr_file.close()

    def _stop_exact_service(self) -> None:
        for item in self.exact_processes():
            pid = str(item.get("ProcessId") or item.get("pid") or "")
            if not pid:
                continue
            if os.name == "nt":
                self._run_command(["taskkill", "/PID", pid, "/T", "/F"], timeout=20)
            else:
                self._run_command(["kill", pid], timeout=10)
        for _ in range(40):
            if not self.exact_processes() and not self._socket_probe(self.host, self.port, 0.2):
                return
            time.sleep(0.25)
        raise RuntimeError("FreeStockDB service did not stop cleanly")

    def _start_service(self, data_root: Path | None = None) -> dict[str, Any]:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("FreeStockDB service must use a loopback-only URL")
        binary = self._binary()
        if not binary.get("verified") or not binary.get("updater_verified"):
            raise RuntimeError("FreeStockDB v0.2.1 binary SHA-256 verification failed")
        if self._socket_probe(self.host, self.port, 0.3):
            if self.exact_processes():
                return {"status": "already_running"}
            raise RuntimeError(f"port conflict on {self.host}:{self.port}")
        data = data_root or self.paths.data
        # The released Windows binary bundled with free-stockdb reads
        # stockdb.conf and rejects the upstream source-build flags as file
        # arguments.  Keep the configured working directory authoritative for
        # this binary; an explicit-argument mode remains available for builds
        # that implement main_server.cpp.
        explicit = os.environ.get("FREESTOCKDB_SERVER_ARGUMENTS", "").lower() in {"1", "true", "yes"}
        command = (
            [str(self.paths.server), "--host", self.host, "--port", str(self.port), "--data", str(data)]
            if explicit
            else [str(self.paths.server)]
        )
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(command, cwd=str(self.paths.root), creationflags=creationflags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(command, cwd=str(self.paths.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if self.exact_processes() and self._socket_probe(self.host, self.port, 0.2):
                return {"status": "started", "pid": self.exact_processes()[0].get("ProcessId", "")}
            time.sleep(0.25)
        raise RuntimeError("FreeStockDB service did not become ready")

    def _ensure_readonly_config(self) -> bool:
        if not self.paths.config.is_file():
            return False
        content = self.paths.config.read_text(encoding="utf-8")
        updated = re.sub(
            r"(?mi)^(?P<indent>[ \t]+)#?\s*readonly\s*:\s*[^\r\n#]*(?:#.*)?$",
            r"\g<indent>readonly: yes",
            content,
        )
        updated = re.sub(
            r"(?mi)^(?P<indent>[ \t]+)#?\s*ip\s*:\s*[^\r\n#]*(?:#.*)?$",
            r"\g<indent>ip: 127.0.0.1",
            updated,
        )
        if updated == content:
            return False
        temporary = self.paths.config.with_suffix(".tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, self.paths.config)
        return True

    def _create_directory_link(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            junction = self._run_command(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                timeout=30,
            )
            if junction.returncode != 0:
                raise RuntimeError(
                    "failed to create FreeStockDB compatibility junction: "
                    + (junction.stderr or junction.stdout or "").strip()
                )
            return
        link.symlink_to(target, target_is_directory=True)

    def _remove_directory_link(self, path: Path) -> None:
        if not self._is_directory_link(path):
            raise RuntimeError(f"refusing to remove a real directory as a link: {path}")
        if os.name == "nt":
            path.rmdir()
        else:
            path.unlink()

    def migrate_storage(self) -> dict[str, Any]:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.paths.lock), timeout=30):
            self._recover_interrupted_swap()
            return self._migrate_storage_unlocked()

    def _migrate_storage_unlocked(self) -> dict[str, Any]:
        layout = self._storage_layout()
        if layout["status"] == "local":
            return {"ok": True, "status": "local_storage", "live": str(self.paths.live)}
        self.paths.storage_root.mkdir(parents=True, exist_ok=True)
        if layout["status"] == "canonical":
            return {"ok": True, "status": "already_migrated", "live": str(self.paths.live)}
        reversed_layout = layout["status"] == "reversed"
        if not self.paths.data.is_dir():
            raise RuntimeError(f"FreeStockDB source data directory is missing: {self.paths.data}")
        if self.paths.live.exists() and not reversed_layout:
            raise RuntimeError(f"FreeStockDB live destination already exists: {self.paths.live}")
        source_bytes = sum(
            item.stat().st_size for item in self.paths.data.rglob("*") if item.is_file()
        )
        free_bytes = shutil.disk_usage(self.paths.storage_root).free
        if free_bytes < source_bytes + MIN_FREE_BYTES:
            raise RuntimeError("D drive does not have enough space for migration plus the 5GB guard")

        migration_root = self.paths.storage_root / "migration.next"
        if migration_root.exists():
            shutil.rmtree(migration_root)
        migration_root.mkdir(parents=True)
        backup = self.paths.root / (
            "data.before-migration-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        copied = migration_root / "data"
        stopped = False
        removed_reversed_link = False
        try:
            if self.exact_processes():
                self._stop_exact_service()
                stopped = True
            if reversed_layout:
                self._remove_directory_link(self.paths.live)
                removed_reversed_link = True
            snapshot = self._tree_snapshot(self.paths.data)
            shutil.copytree(self.paths.data, copied)
            self._verify_snapshot(copied, snapshot)
            self._write_manifest(copied)
            verify = self._verify_staged_data(migration_root)
            os.replace(copied, self.paths.live)
            os.replace(self.paths.data, backup)
            self._create_directory_link(self.paths.data, self.paths.live)
            self._ensure_readonly_config()
            if stopped:
                self._start_service()
            return {
                "ok": True,
                "status": "migrated",
                "live": str(self.paths.live),
                "compatibility_link": str(self.paths.data),
                "backup": str(backup),
                "verified_files": verify["verified_files"],
            }
        except Exception:
            if self.paths.data.exists() and self.paths.data.resolve() == self.paths.live.resolve():
                if os.name == "nt":
                    self.paths.data.rmdir()
                else:
                    self.paths.data.unlink()
            if backup.exists() and not self.paths.data.exists():
                os.replace(backup, self.paths.data)
            if self.paths.live.exists() and not copied.exists():
                os.replace(self.paths.live, copied)
            if (
                reversed_layout
                and removed_reversed_link
                and self.paths.data.is_dir()
                and not self.paths.live.exists()
            ):
                self._create_directory_link(self.paths.live, self.paths.data)
            if stopped and not self.exact_processes():
                self._start_service()
            raise
        finally:
            if migration_root.exists():
                shutil.rmtree(migration_root, ignore_errors=True)

    def repair(
        self,
        *,
        migrate: bool = False,
        force_restart: bool = False,
    ) -> dict[str, Any]:
        migration = self.migrate_storage() if migrate else None
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.paths.lock), timeout=30):
            recovery = None if migrate else self._recover_interrupted_swap()
            return self._repair_locked(
                migration=migration,
                recovery=recovery,
                force_restart=force_restart,
            )

    def _repair_locked(
        self,
        *,
        migration: dict[str, Any] | None,
        recovery: dict[str, Any] | None,
        force_restart: bool,
    ) -> dict[str, Any]:
        config_changed = self._ensure_readonly_config()
        health = self.doctor(include_samples=True)
        if health.get("ok") and not health.get("connection_leak"):
            self._save_state(
                {
                    "status": "healthy",
                    "repair_failures": 0,
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            return {
                "ok": True,
                "status": "healthy",
                "migration": migration,
                "recovery": recovery,
                "config_changed": config_changed,
                "health": health,
            }

        # Repair failures have their own compact state. Update state is kept in
        # a separate file and must not reset the consecutive-failure counter.
        previous = self._read_state(self.paths.state) or {}
        failures = int(previous.get("repair_failures") or 0) + 1
        should_restart = force_restart or bool(health.get("connection_leak")) or failures >= 2
        if health.get("port_conflict"):
            should_restart = False
        restarted = False
        if should_restart:
            if self.exact_processes():
                self._stop_exact_service()
                self._start_service()
                restarted = True
            elif not self._socket_probe(self.host, self.port, 0.3):
                self._start_service()
                restarted = True
        result_health = self.doctor(include_samples=True) if restarted else health
        provider_details = result_health.get("provider") or {}
        service_ready = bool(
            result_health.get("service_ok", result_health.get("ok"))
            and not result_health.get("connection_leak")
            and not provider_details.get("sample_errors")
        )
        data_fresh = bool(result_health.get("data_fresh", result_health.get("ok")))
        result = {
            # Repair is a service operation.  A stale vendor dataset is an
            # update concern and must not make a healthy HTTP service look
            # unrepairable.
            "ok": service_ready,
            "service_ready": service_ready,
            "data_fresh": data_fresh,
            "status": (
                "repaired"
                if restarted and service_ready and data_fresh
                else "repaired_stale"
                if restarted and service_ready
                else "restart_failed"
                if restarted
                else "waiting_for_second_failure"
                if not should_restart
                else "unhealthy"
            ),
            "repair_failures": 0 if service_ready else failures,
            "restarted": restarted,
            "migration": migration,
            "recovery": recovery,
            "config_changed": config_changed,
            "health": result_health,
        }
        self._save_state(
            {
                "ok": result["ok"],
                "status": result["status"],
                "repair_failures": result["repair_failures"],
                "restarted": result["restarted"],
                "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        return result

    def _stage_data(self) -> Path:
        """Create or resume a private updater workspace.

        The vendor updater is explicitly resumable. Retaining a partial stage
        is therefore essential: deleting it after each timeout restarts a
        multi-gigabyte sync from zero. After the first successful update, the
        previous verified generation becomes the next staging candidate. This
        A/B rotation avoids another full copy while keeping the active dataset
        untouched. Hard links are forbidden because LevelDB recovery and
        compaction can mutate files even under the read-only API setting.
        """
        staged_root = self.paths.staging
        marker = staged_root / ".aihub-staging.json"
        if (staged_root / "data").is_dir() and marker.is_file():
            for source in (self.paths.updater, self.paths.config, self.paths.source):
                shutil.copy2(source, staged_root / source.name)
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {"version": 1}
            metadata.update(
                {
                    "status": "resumed",
                    "resumed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            marker.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            return staged_root
        if staged_root.exists():
            shutil.rmtree(staged_root)
        reused_previous = bool(
            self.paths.previous.is_dir()
            and not self._is_directory_link(self.paths.previous)
        )
        staged_root.mkdir(parents=True)
        if reused_previous:
            os.replace(self.paths.previous, staged_root / "data")
        for source in (self.paths.updater, self.paths.config, self.paths.source):
            shutil.copy2(source, staged_root / source.name)
        if not reused_previous:
            source_data = self.paths.live if self.paths.live.is_dir() else self.paths.data
            shutil.copytree(source_data, staged_root / "data")
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "reused_previous" if reused_previous else "created",
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source_manifest": self._manifest(staged_root / "data"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return staged_root

    def _tree_snapshot(self, root: Path) -> dict[str, dict[str, Any]]:
        return {
            item.relative_to(root).as_posix(): {
                "size": item.stat().st_size,
                "sha256": _sha256(item),
            }
            for item in root.rglob("*")
            if item.is_file() and item.name != ".sync_manifest.json"
        }

    def _write_manifest(self, data_root: Path) -> dict[str, Any]:
        snapshot = self._tree_snapshot(data_root)
        payload = {
            "version": 2,
            "generated_at": time.time(),
            "files": [
                {
                    "path": relative,
                    "size": int(value["size"]),
                    "sha256": str(value["sha256"]),
                }
                for relative, value in sorted(snapshot.items())
            ],
        }
        manifest = data_root / ".sync_manifest.json"
        temporary = data_root / ".sync_manifest.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, manifest)
        return payload

    @property
    def _swap_journal(self) -> Path:
        return self.paths.storage_root / ".freestockdb-swap.json"

    @property
    def _retired_previous(self) -> Path:
        return self.paths.storage_root / "previous.retired"

    def _write_swap_journal(self, phase: str) -> None:
        payload = {
            "phase": phase,
            "live": str(self.paths.live),
            "previous": str(self.paths.previous),
            "retired_previous": str(self._retired_previous),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        temporary = self._swap_journal.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self._swap_journal)

    def _recover_interrupted_swap(self) -> dict[str, Any]:
        if not self._swap_journal.is_file():
            return {"recovered": False}
        try:
            state = json.loads(self._swap_journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"FreeStockDB swap journal cannot be read: {exc}") from exc
        phase = str(state.get("phase") or "")
        retired = self._retired_previous
        service_was_running = bool(self.exact_processes())
        port_open = self._socket_probe(self.host, self.port, 0.2)
        if port_open and not service_was_running:
            raise RuntimeError(
                "cannot recover FreeStockDB swap while its port is owned by another process"
            )
        if service_was_running:
            self._stop_exact_service()
        recovery_succeeded = False
        try:
            if phase in {"old_moved", "new_live"} and self.paths.live.exists():
                shutil.rmtree(self.paths.live)
            if (
                phase in {"old_moved", "new_live"}
                or (phase == "prepared" and not self.paths.live.exists())
            ) and self.paths.previous.exists():
                os.replace(self.paths.previous, self.paths.live)
            if retired.exists():
                if self.paths.previous.exists():
                    shutil.rmtree(self.paths.previous)
                os.replace(retired, self.paths.previous)
            self._swap_journal.unlink(missing_ok=True)
            recovery_succeeded = True
        finally:
            if (
                recovery_succeeded
                and service_was_running
                and not self.exact_processes()
            ):
                self._start_service()
        return {"recovered": True, "phase": phase, "service_restarted": service_was_running}

    def _verify_snapshot(
        self,
        destination: Path,
        snapshot: dict[str, dict[str, Any]],
    ) -> None:
        for relative, expected in snapshot.items():
            path = destination / relative
            if not path.is_file():
                raise RuntimeError(f"FreeStockDB copied snapshot is missing: {relative}")
            if path.stat().st_size != int(expected["size"]):
                raise RuntimeError(f"FreeStockDB copied snapshot size mismatch: {relative}")
            if _sha256(path) != expected["sha256"]:
                raise RuntimeError(f"FreeStockDB copied snapshot SHA-256 mismatch: {relative}")

    def _verify_staged_data(self, staged_root: Path) -> dict[str, Any]:
        manifest = self._manifest(staged_root / "data")
        if not manifest.get("exists") or not manifest.get("file_count"):
            raise RuntimeError("FreeStockDB staged dataset has no usable sync manifest")
        files = manifest.get("file_count", 0)
        missing = 0
        verified = 0
        try:
            payload = json.loads((staged_root / "data" / ".sync_manifest.json").read_text(encoding="utf-8"))
            for item in payload.get("files", []):
                relative = item.get("path") if isinstance(item, dict) else None
                if not relative:
                    continue
                file_path = (staged_root / "data" / relative).resolve()
                data_root = (staged_root / "data").resolve()
                if data_root not in file_path.parents:
                    raise RuntimeError(f"FreeStockDB manifest path escapes data root: {relative}")
                if not file_path.is_file():
                    missing += 1
                    continue
                expected_size = item.get("size") if isinstance(item, dict) else None
                if expected_size is not None and file_path.stat().st_size != int(expected_size):
                    raise RuntimeError(
                        f"FreeStockDB staged file size mismatch: {relative}"
                    )
                expected_sha256 = str(item.get("sha256") or "") if isinstance(item, dict) else ""
                if expected_sha256 and _sha256(file_path).casefold() != expected_sha256.casefold():
                    raise RuntimeError(
                        f"FreeStockDB staged file SHA-256 mismatch: {relative}"
                    )
                verified += 1
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"FreeStockDB staged manifest cannot be read: {exc}") from exc
        if missing:
            raise RuntimeError(f"FreeStockDB staged dataset is missing {missing} manifest files")
        return {
            "file_count": files,
            "verified_files": verified,
            "missing_files": missing,
            "manifest": manifest,
        }

    def _dataset_acceptance(
        self,
        expected_trade_date: date,
        *,
        baseline_catalog_symbols: int = 0,
    ) -> dict[str, Any]:
        samples = self._sample_health(expected_trade_date=expected_trade_date)
        provider = FreeStockDBMarketProvider(base_url=self.base_url, timeout_seconds=30)
        try:
            health = provider.health()
            catalog_symbols = int(health.get("catalog_symbols") or 0)
            cross_section = provider.fetch_daily_cross_section(expected_trade_date)
            unique_symbols = (
                int(cross_section["symbol"].dropna().astype(str).nunique())
                if "symbol" in cross_section
                else 0
            )
            coverage_ratio = (
                unique_symbols / catalog_symbols if catalog_symbols else 0.0
            )
            minimum_catalog_symbols = max(
                MIN_CATALOG_SYMBOLS,
                int(baseline_catalog_symbols * 0.90),
            )
            catalog_ok = catalog_symbols >= minimum_catalog_symbols
            cross_section_ok = bool(
                catalog_ok
                and unique_symbols >= minimum_catalog_symbols
                and coverage_ratio >= 0.90
            )
            return {
                "ok": bool(samples.get("ok") and cross_section_ok),
                "expected_trade_date": expected_trade_date.isoformat(),
                "samples": samples,
                "catalog_symbols": catalog_symbols,
                "baseline_catalog_symbols": baseline_catalog_symbols,
                "minimum_catalog_symbols": minimum_catalog_symbols,
                "cross_section_symbols": unique_symbols,
                "cross_section_coverage": round(coverage_ratio, 6),
                "minimum_cross_section_coverage": 0.90,
                "warnings": (
                    []
                    if cross_section_ok
                    else [
                        "catalog_or_cross_section_below_minimum"
                        if not catalog_ok or unique_symbols < minimum_catalog_symbols
                        else "cross_section_coverage_below_90pct"
                    ]
                ),
            }
        except Exception as exc:
            return {
                "ok": False,
                "expected_trade_date": expected_trade_date.isoformat(),
                "samples": samples,
                "catalog_symbols": 0,
                "cross_section_symbols": 0,
                "cross_section_coverage": 0.0,
                "minimum_cross_section_coverage": 0.90,
                "error": str(exc)[:1000],
            }
        finally:
            provider.close()

    def update(
        self,
        *,
        dry_run: bool = False,
        timeout: float = 3300,
        expected_trade_date: date | None = None,
    ) -> dict[str, Any]:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.paths.lock), timeout=30):
            recovery = self._recover_interrupted_swap()
            started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            expected = (
                {"date": expected_trade_date, "source": "argument"}
                if expected_trade_date
                else self.expected_trade_date()
            )
            expected_date = expected.get("date")

            def persist_update_state(payload: dict[str, Any]) -> None:
                if not dry_run:
                    self._save_update_state(payload)

            def save_phase(phase: str, **extra: Any) -> None:
                persist_update_state(
                    {
                        "ok": None,
                        "status": "running",
                        "phase": phase,
                        "started_at": started_at,
                        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "dry_run": dry_run,
                        "expected_trade_date": (
                            expected_date.isoformat() if expected_date else ""
                        ),
                        "expected_trade_date_source": expected.get("source", "unknown"),
                        **extra,
                    }
                )

            save_phase("preflight")
            before = self.doctor(include_samples=False)
            preflight = {
                "server_exists": before["server_exists"],
                "updater_exists": before["updater_exists"],
                "data_exists": before["data_exists"],
                "disk_guard_ok": before["disk"]["guard_ok"],
                "update_guard_ok": before["disk"].get("update_guard_ok", False),
                "port_conflict": before["port_conflict"],
                "transport_warning": before["transport_warning"],
                "binary": before.get("binary", {}),
                "recovery": recovery,
                "checks": before.get("checks", {}),
                "expected_trade_date": (
                    expected_date.isoformat() if expected_date else ""
                ),
                "expected_trade_date_source": expected.get("source", "unknown"),
            }
            if not before["server_exists"] or not before["updater_exists"] or not before["config_exists"] or not before["data_exists"]:
                persist_update_state({"ok": False, "status": "failed", "error": "FreeStockDB runtime files are incomplete", "started_at": started_at})
                raise RuntimeError("FreeStockDB runtime files are incomplete")
            if not before.get("binary", {}).get("verified") or not before.get("binary", {}).get("updater_verified"):
                persist_update_state({"ok": False, "status": "failed", "error": "FreeStockDB v0.2.1 binary SHA-256 verification failed", "started_at": started_at})
                raise RuntimeError("FreeStockDB v0.2.1 binary SHA-256 verification failed")
            failed_security_checks = [
                name
                for name in ("config", "loopback")
                if before.get("checks", {}).get(name) is not True
            ]
            if failed_security_checks:
                message = (
                    "FreeStockDB security preflight failed: "
                    + ",".join(failed_security_checks)
                )
                persist_update_state(
                    {
                        "ok": False,
                        "status": "failed",
                        "error": message,
                        "started_at": started_at,
                    }
                )
                raise RuntimeError(message)
            if not before["source"]:
                persist_update_state({"ok": False, "status": "failed", "error": "FreeStockDB sync_url.txt is missing or empty", "started_at": started_at})
                raise RuntimeError("FreeStockDB sync_url.txt is missing or empty")
            if not before.get("storage_migrated", False):
                message = (
                    f"FreeStockDB data has not been migrated to {self.paths.live}; "
                    "run market-freestockdb-repair --migrate first"
                )
                persist_update_state(
                    {
                        "ok": False,
                        "status": "failed",
                        "error": message,
                        "started_at": started_at,
                    }
                )
                raise RuntimeError(message)
            if not before["disk"]["guard_ok"]:
                persist_update_state({"ok": False, "status": "failed", "error": "disk free space is below the 5GB update guard", "started_at": started_at})
                raise RuntimeError("disk free space is below the 5GB update guard")
            if not before["disk"].get("update_guard_ok", False):
                message = "safe staged update needs at least the current dataset size plus 5GB free space"
                if dry_run:
                    result = {"ok": False, "status": "dry_run", "preflight": preflight, "error": message}
                    persist_update_state({**result, "started_at": started_at, "completed_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                    return result
                persist_update_state({"ok": False, "status": "failed", "error": message, "started_at": started_at})
                raise RuntimeError(message)
            if before["port_conflict"]:
                persist_update_state({"ok": False, "status": "failed", "error": "port is occupied by a process other than the configured stockdb.exe", "started_at": started_at})
                raise RuntimeError("port is occupied by a process other than the configured stockdb.exe")
            if dry_run:
                result = {"ok": True, "status": "dry_run", "preflight": preflight}
                persist_update_state({**result, "started_at": started_at, "completed_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                return result
            if not expected_date:
                message = "expected trade date is unavailable; refresh the local trading calendar or pass --expected-date"
                persist_update_state({"ok": False, "status": "failed", "error": message, "started_at": started_at})
                raise RuntimeError(message)

            staged_root: Path | None = None
            service_was_running = bool(self.exact_processes())
            service_paused_for_update = False
            stopped_for_swap = False
            preserve_staging = False
            try:
                save_phase("staging")
                reusable_stage = bool(
                    (self.paths.staging / "data").is_dir()
                    and (self.paths.staging / ".aihub-staging.json").is_file()
                )
                reusable_previous = bool(
                    self.paths.previous.is_dir()
                    and not self._is_directory_link(self.paths.previous)
                )
                needs_live_snapshot = not reusable_stage and not reusable_previous
                if service_was_running and needs_live_snapshot:
                    self._stop_exact_service()
                    service_paused_for_update = True
                staged_root = self._stage_data()
                if service_paused_for_update:
                    self._start_service()
                    service_paused_for_update = False
                updater = staged_root / self.paths.updater.name
                configured_arguments = os.environ.get("FREESTOCKDB_UPDATER_ARGUMENTS", "").strip()
                updater_arguments = shlex.split(configured_arguments, posix=os.name != "nt") if configured_arguments else []
                save_phase("vendor_update", timeout_seconds=timeout)
                sync = self._run_vendor_updater(
                    [str(updater), *updater_arguments],
                    timeout=timeout,
                    cwd=staged_root,
                )
                self._write_log(
                    "sync",
                    {
                        "returncode": sync.returncode,
                        "completion": sync.completion,
                        "stdout": (sync.stdout or "")[-1000:],
                        "stderr": (sync.stderr or "")[-1000:],
                    },
                )
                if sync.returncode != 0:
                    raise RuntimeError(f"FreeStockDB updater sync failed: {sync.returncode}")
                save_phase("manifest")
                self._write_manifest(staged_root / "data")
                save_phase("verify_files")
                verify = self._verify_staged_data(staged_root)
                self._write_log("verify", verify)
                baseline_files = int(before.get("manifest", {}).get("file_count") or 0)
                staged_files = int(verify.get("file_count") or 0)
                minimum_files = int(baseline_files * 0.90)
                if baseline_files and staged_files < minimum_files:
                    raise RuntimeError(
                        f"FreeStockDB staged file coverage dropped from {baseline_files} to {staged_files}"
                    )

                retired_previous = self._retired_previous
                if retired_previous.exists():
                    shutil.rmtree(retired_previous)
                if self.paths.previous.exists():
                    os.replace(self.paths.previous, retired_previous)
                save_phase("swap")
                if self.exact_processes():
                    self._stop_exact_service()
                    stopped_for_swap = True
                self._write_swap_journal("prepared")
                os.replace(self.paths.live, self.paths.previous)
                self._write_swap_journal("old_moved")
                os.replace(staged_root / "data", self.paths.live)
                self._write_swap_journal("new_live")
                self._start_service()
                service_paused_for_update = False
                save_phase("acceptance")
                acceptance = self._dataset_acceptance(
                    expected_date,
                    baseline_catalog_symbols=int(
                        before.get("provider", {}).get("catalog_symbols") or 0
                    ),
                )
                if not acceptance.get("ok"):
                    raise RuntimeError(
                        "FreeStockDB freshness or coverage acceptance failed: "
                        + json.dumps(acceptance, ensure_ascii=False)[:1500]
                    )
                external_audit = self._external_sample_audit(expected_date)
                if not external_audit.get("ok"):
                    raise RuntimeError(
                        "FreeStockDB cross-source sample audit failed: "
                        + json.dumps(external_audit, ensure_ascii=False)[:1500]
                    )
                if retired_previous.exists():
                    shutil.rmtree(retired_previous)
                self._swap_journal.unlink(missing_ok=True)
                self._write_log(
                    "completed",
                    {"acceptance": acceptance, "external_audit": external_audit},
                )
                result = {
                    "ok": True,
                    "status": "updated",
                    "expected_trade_date": expected_date.isoformat(),
                    "acceptance": acceptance,
                    "external_audit": external_audit,
                    "rollback_available": self.paths.previous.exists(),
                }
                persist_update_state({**result, "started_at": started_at, "completed_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                return result
            except Exception as exc:
                preserve_staging = bool(
                    staged_root
                    and (staged_root / "data").is_dir()
                    and not self._swap_journal.is_file()
                )
                self._write_log("failed", {"error": str(exc)[:2000]})
                persist_update_state({"ok": False, "status": "failed", "error": str(exc)[:2000], "started_at": started_at, "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"), "expected_trade_date": expected_date.isoformat(), "resume_available": preserve_staging, "staging": str(staged_root) if preserve_staging else ""})
                try:
                    if self._swap_journal.is_file():
                        if self.exact_processes() or self._socket_probe(self.host, self.port, 0.2):
                            self._stop_exact_service()
                        self._recover_interrupted_swap()
                    if (
                        service_paused_for_update
                        or service_was_running
                        or stopped_for_swap
                    ) and not self.exact_processes():
                        self._start_service()
                except Exception as rollback_error:
                    self._write_log("rollback_failed", {"error": str(rollback_error)[:2000]})
                raise RuntimeError(str(exc)) from exc
            finally:
                if (
                    not preserve_staging
                    and staged_root is not None
                    and staged_root.exists()
                ):
                    shutil.rmtree(staged_root, ignore_errors=True)
