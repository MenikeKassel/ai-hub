from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestockdb_runtime import FreeStockDBRuntime, VendorUpdateResult  # noqa: E402


class FreeStockDBRuntimeTests(unittest.TestCase):
    def test_sample_health_marks_history_stale_against_expected_trade_date(self) -> None:
        class FixtureProvider:
            def health(self):
                return {"ok": True, "provider": "freestockdb"}

            def fetch_daily(self, *_args, **_kwargs):
                return pd.DataFrame(
                    {
                        "date": ["2026-07-30"],
                        "close": [10.0],
                    }
                )

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            runtime = FreeStockDBRuntime(
                Path(tmp),
                runtime_root=Path(tmp) / "runtime",
                data_root=Path(tmp),
                socket_probe=lambda *_: True,
            )
            with patch(
                "freestockdb_runtime.FreeStockDBMarketProvider",
                return_value=FixtureProvider(),
            ):
                result = runtime._sample_health(
                    expected_trade_date=date(2026, 7, 31)
                )

        self.assertFalse(result["ok"])
        self.assertEqual("stale", result["freshness"]["status"])
        self.assertEqual("2026-07-31", result["freshness"]["expected_trade_date"])
        self.assertEqual(
            ["159139", "600519", "600900"],
            sorted(result["freshness"]["stale_symbols"]),
        )

    def test_expected_trade_date_uses_local_market_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            market_root = base / "runtime" / "market"
            market_root.mkdir(parents=True)
            import duckdb

            with duckdb.connect(str(market_root / "market.duckdb")) as db:
                db.execute(
                    "CREATE TABLE trading_calendar(trade_date VARCHAR,is_open BOOLEAN,provider VARCHAR,updated_at VARCHAR)"
                )
                db.executemany(
                    "INSERT INTO trading_calendar VALUES (?,?,?,?)",
                    [
                        ("2026-07-30", True, "fixture", ""),
                        ("2026-07-31", True, "fixture", ""),
                        ("2026-08-01", False, "fixture", ""),
                    ],
                )
            runtime = FreeStockDBRuntime(
                base,
                runtime_root=base / "runtime",
                data_root=base,
                socket_probe=lambda *_: False,
            )

            result = runtime.expected_trade_date(
                as_of=datetime(2026, 8, 1, 14, 0)
            )

        self.assertEqual(date(2026, 7, 31), result["date"])
        self.assertEqual("local_market_calendar", result["source"])

    def test_expected_trade_date_uses_previous_session_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            market_root = base / "runtime" / "market"
            market_root.mkdir(parents=True)
            import duckdb

            with duckdb.connect(str(market_root / "market.duckdb")) as db:
                db.execute(
                    "CREATE TABLE trading_calendar(trade_date VARCHAR,is_open BOOLEAN,provider VARCHAR,updated_at VARCHAR)"
                )
                db.executemany(
                    "INSERT INTO trading_calendar VALUES (?,?,?,?)",
                    [
                        ("2026-07-30", True, "fixture", ""),
                        ("2026-07-31", True, "fixture", ""),
                    ],
                )
            runtime = FreeStockDBRuntime(
                base,
                runtime_root=base / "runtime",
                data_root=base,
                socket_probe=lambda *_: False,
            )

            result = runtime.expected_trade_date(
                as_of=datetime(2026, 7, 31, 10, 0)
            )

        self.assertEqual(date(2026, 7, 30), result["date"])

    def test_doctor_does_not_treat_unknown_freshness_as_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base,
                runtime_root=base / "runtime",
                data_root=base,
                socket_probe=lambda *_: True,
            )
            with (
                patch.object(runtime, "exact_processes", return_value=[]),
                patch.object(
                    runtime,
                    "_sample_health",
                    return_value={
                        "ok": True,
                        "freshness": {"status": "unknown", "stale_symbols": []},
                    },
                ),
                patch.object(
                    runtime,
                    "expected_trade_date",
                    return_value={"date": None, "source": "unknown"},
                ),
                patch.object(
                    runtime,
                    "_listening_addresses",
                    return_value={"addresses": ["127.0.0.1"], "verified": True},
                ),
            ):
                result = runtime.doctor()

        self.assertFalse(result["checks"]["freshness"])
        self.assertFalse(result["data_fresh"])
        self.assertFalse(result["ok"])

    def test_config_repair_enforces_loopback_and_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base,
                runtime_root=base / "runtime",
                data_root=base,
                socket_probe=lambda *_: False,
            )
            runtime.paths.root.mkdir(parents=True, exist_ok=True)
            runtime.paths.config.write_text(
                "server:\n    ip: 0.0.0.0\n    # readonly: no\n",
                encoding="utf-8",
            )

            changed = runtime._ensure_readonly_config()
            security = runtime._config_security()

        self.assertTrue(changed)
        self.assertTrue(security["safe"])
        self.assertEqual("127.0.0.1", security["ip"])
        self.assertEqual("yes", security["readonly"])

    def test_dataset_acceptance_rejects_incomplete_latest_cross_section(self) -> None:
        class FixtureProvider:
            def health(self):
                return {"ok": True, "catalog_symbols": 6_000}

            def fetch_daily_cross_section(self, _as_of):
                return pd.DataFrame(
                    {"symbol": [f"{index:06d}" for index in range(5_340)]}
                )

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            runtime = FreeStockDBRuntime(
                Path(tmp),
                runtime_root=Path(tmp) / "runtime",
                data_root=Path(tmp),
                socket_probe=lambda *_: True,
            )
            with (
                patch.object(
                    runtime,
                    "_sample_health",
                    return_value={"ok": True, "freshness": {"status": "current"}},
                ),
                patch(
                    "freestockdb_runtime.FreeStockDBMarketProvider",
                    return_value=FixtureProvider(),
                ),
            ):
                result = runtime._dataset_acceptance(date(2026, 7, 31))

        self.assertFalse(result["ok"])
        self.assertEqual(0.89, result["cross_section_coverage"])
        self.assertIn("cross_section_coverage_below_90pct", result["warnings"])

    def test_dataset_acceptance_rejects_tiny_self_consistent_catalog(self) -> None:
        class FixtureProvider:
            def health(self):
                return {"ok": True, "catalog_symbols": 100}

            def fetch_daily_cross_section(self, _as_of):
                return pd.DataFrame(
                    {"symbol": [f"{index:06d}" for index in range(100)]}
                )

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            runtime = FreeStockDBRuntime(
                Path(tmp),
                runtime_root=Path(tmp) / "runtime",
                data_root=Path(tmp),
                socket_probe=lambda *_: True,
            )
            with (
                patch.object(
                    runtime,
                    "_sample_health",
                    return_value={"ok": True, "freshness": {"status": "current"}},
                ),
                patch(
                    "freestockdb_runtime.FreeStockDBMarketProvider",
                    return_value=FixtureProvider(),
                ),
            ):
                result = runtime._dataset_acceptance(date(2026, 7, 31))

        self.assertFalse(result["ok"])
        self.assertEqual(1.0, result["cross_section_coverage"])
        self.assertIn("catalog_or_cross_section_below_minimum", result["warnings"])

    def test_external_audit_treats_missing_etf_adjustment_as_optional(self) -> None:
        class LocalProvider:
            def fetch_daily(self, symbol, _kind, _start, _end, adjustment):
                if symbol == "159139" and adjustment == "qfq":
                    raise RuntimeError("adjustment factors unavailable")
                close = 29.09 if symbol == "600900" else 1.245
                return pd.DataFrame({"date": ["2026-07-31"], "close": [close]})

            def close(self):
                return None

        class MatchingProvider(LocalProvider):
            pass

        class FailingProvider:
            name = "optional-source"

            def fetch_daily(self, *_args, **_kwargs):
                raise RuntimeError("source unavailable")

            def close(self):
                return None

        MatchingProvider.name = "matching"
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FreeStockDBRuntime(
                Path(tmp),
                runtime_root=Path(tmp) / "runtime",
                data_root=Path(tmp),
                socket_probe=lambda *_: True,
            )
            with (
                patch(
                    "freestockdb_runtime.FreeStockDBMarketProvider",
                    return_value=LocalProvider(),
                ),
                patch(
                    "freestockdb_runtime.BaoStockMarketProvider",
                    return_value=MatchingProvider(),
                ),
                patch(
                    "freestockdb_runtime.AKShareMarketProvider",
                    return_value=FailingProvider(),
                ),
            ):
                result = runtime._external_sample_audit(date(2026, 7, 31))

        self.assertTrue(result["ok"])
        self.assertEqual([], result["missing_pairs"])
        self.assertEqual([("159139", "qfq")], result["optional_missing_pairs"])
        self.assertIn("optional_etf_qfq_factor_unavailable", result["warnings"])

    def test_update_only_pauses_service_for_consistent_snapshot_and_final_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "program"
            storage = base / "storage"
            root.mkdir()
            storage.mkdir()
            for name in ("stockdb.exe", "数据更新.exe", "stockdb.conf", "sync_url.txt"):
                (root / name).write_bytes(b"fixture")
            live = storage / "live"
            live.mkdir()
            (live / "old.ldb").write_bytes(b"old")
            events: list[str] = []
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=base / "runtime",
                data_root=storage,
                socket_probe=lambda *_: False,
                server_sha256=hashlib.sha256(b"fixture").hexdigest(),
                updater_sha256=hashlib.sha256(b"fixture").hexdigest(),
            )

            def stage_data() -> Path:
                events.append("stage")
                staged = storage / "staging"
                staged.mkdir()
                shutil.copy2(root / "数据更新.exe", staged / "数据更新.exe")
                shutil.copytree(live, staged / "data")
                return staged

            def stop_service() -> None:
                events.append("stop")

            def run_updater(*_args, **_kwargs):
                events.append("vendor_update")
                return VendorUpdateResult(0, "ok", "", "fixture")

            healthy = {
                "server_exists": True,
                "updater_exists": True,
                "config_exists": True,
                "data_exists": True,
                "source": "http://mirror.invalid",
                "storage_migrated": True,
                "port_conflict": False,
                "transport_warning": "untrusted_transport",
                "binary": {"verified": True, "updater_verified": True},
                "disk": {"guard_ok": True, "update_guard_ok": True},
                "checks": {"config": True, "loopback": True},
            }
            with (
                patch.object(runtime, "doctor", return_value=healthy),
                patch.object(
                    runtime,
                    "exact_processes",
                    side_effect=[[{"ProcessId": 1}], [{"ProcessId": 2}]],
                ),
                patch.object(runtime, "_stage_data", side_effect=stage_data),
                patch.object(runtime, "_stop_exact_service", side_effect=stop_service),
                patch.object(runtime, "_start_service", side_effect=lambda: events.append("start")),
                patch.object(runtime, "_run_vendor_updater", side_effect=run_updater),
                patch.object(runtime, "_write_manifest", return_value={}),
                patch.object(runtime, "_verify_staged_data", side_effect=lambda *_: events.append("verify") or {"verified_files": 1}),
                patch.object(runtime, "_dataset_acceptance", return_value={"ok": True}),
                patch.object(runtime, "_external_sample_audit", return_value={"ok": True}),
            ):
                result = runtime.update(expected_trade_date=date(2026, 7, 31))

        self.assertTrue(result["ok"])
        self.assertEqual(
            ["stop", "stage", "start", "vendor_update", "verify", "stop", "start"],
            events,
        )

    def test_stage_data_resumes_existing_partial_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "program"
            storage = base / "storage"
            root.mkdir()
            storage.mkdir()
            for name in ("数据更新.exe", "stockdb.conf", "sync_url.txt"):
                (root / name).write_bytes(b"current")
            live = storage / "live"
            live.mkdir()
            (live / "live-only.ldb").write_bytes(b"live")
            staged = storage / "staging"
            (staged / "data").mkdir(parents=True)
            (staged / "data" / "partial.ldb").write_bytes(b"partial")
            (staged / ".aihub-staging.json").write_text(
                json.dumps({"version": 1, "status": "partial"}),
                encoding="utf-8",
            )
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=base / "runtime",
                data_root=storage,
                socket_probe=lambda *_: False,
            )

            result = runtime._stage_data()

            self.assertEqual(staged, result)
            self.assertTrue((staged / "data" / "partial.ldb").is_file())
            self.assertFalse((staged / "data" / "live-only.ldb").exists())
            self.assertEqual(b"current", (staged / "数据更新.exe").read_bytes())

    def test_disk_guard_accounts_for_missing_bytes_in_partial_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base,
                runtime_root=base / "runtime",
                data_root=base,
                socket_probe=lambda *_: False,
            )
            runtime.paths.data.mkdir()
            (runtime.paths.data / "live.ldb").write_bytes(b"0123456789")
            staged_data = runtime.paths.staging / "data"
            staged_data.mkdir(parents=True)
            (staged_data / "partial.ldb").write_bytes(b"0123")
            (runtime.paths.staging / ".aihub-staging.json").write_text(
                json.dumps({"version": 1, "status": "partial"}),
                encoding="utf-8",
            )

            result = runtime._disk()

        self.assertEqual(10, result["data_bytes"])
        self.assertEqual(4, result["candidate_bytes"])
        self.assertEqual(5 * 1024**3 + 6, result["required_for_safe_update_bytes"])

    def test_stage_data_reuses_previous_generation_without_copying_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "program"
            storage = base / "storage"
            root.mkdir()
            storage.mkdir()
            for name in ("stockdb.conf", "sync_url.txt"):
                (root / name).write_bytes(b"current")
            live = storage / "live"
            live.mkdir()
            (live / "live-only.ldb").write_bytes(b"live")
            previous = storage / "previous"
            previous.mkdir()
            (previous / "previous.ldb").write_bytes(b"previous")
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=base / "runtime",
                data_root=storage,
                socket_probe=lambda *_: False,
            )
            runtime.paths.updater.write_bytes(b"current")

            result = runtime._stage_data()

            self.assertEqual(storage / "staging", result)
            self.assertTrue((result / "data" / "previous.ldb").is_file())
            self.assertFalse((result / "data" / "live-only.ldb").exists())
            self.assertFalse(previous.exists())
            marker = json.loads(
                (result / ".aihub-staging.json").read_text(encoding="utf-8")
            )
            self.assertEqual("reused_previous", marker["status"])

    def test_storage_layout_rejects_reversed_compatibility_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "program"
            storage = base / "storage"
            (root / "data").mkdir(parents=True)
            (storage / "live").mkdir(parents=True)
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=base / "runtime",
                data_root=storage,
                socket_probe=lambda *_: False,
            )

            with (
                patch.object(
                    runtime,
                    "_is_directory_link",
                    side_effect=lambda path: path == runtime.paths.live,
                ),
                patch.object(runtime, "_same_directory", return_value=True),
            ):
                result = runtime._storage_layout()

            self.assertEqual("reversed", result["status"])
            self.assertFalse(result["canonical"])

    def test_doctor_reports_exact_paths_and_does_not_count_catalog_groups_as_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stockdb.exe").write_bytes(b"")
            (root / "数据更新.exe").write_bytes(b"")
            (root / "stockdb.conf").write_text("port=7899", encoding="utf-8")
            (root / "sync_url.txt").write_text("# comment\nhttp://mirror.invalid", encoding="utf-8")
            data = root / "data"
            data.mkdir()
            (data / ".sync_manifest.json").write_text(
                json.dumps({"version": 2, "generated_at": 0, "files": [{"path": "1.ldb"}]}),
                encoding="utf-8",
            )

            runtime = FreeStockDBRuntime(
                root,
                "http://127.0.0.1:7899",
                runtime_root=root / "runtime",
                data_root=root,
                socket_probe=lambda *_: False,
            )
            result = runtime.doctor()

            self.assertEqual("stopped", result["service_status"])
            self.assertEqual("http://mirror.invalid", result["source"])
            self.assertEqual("untrusted_transport", result["transport_warning"])
            self.assertEqual(1, result["manifest"]["file_count"])

    def test_dry_run_does_not_touch_data_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("stockdb.exe", "数据更新.exe"):
                (root / name).write_bytes(b"")
            (root / "stockdb.conf").write_text(
                "server:\n\tip: 127.0.0.1\n\treadonly: yes\n",
                encoding="utf-8",
            )
            (root / "sync_url.txt").write_text("http://mirror.invalid", encoding="utf-8")
            (root / "data").mkdir()
            marker = root / "data" / "marker.txt"
            marker.write_text("old", encoding="utf-8")
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=root / "runtime",
                data_root=root,
                socket_probe=lambda *_: False,
                server_sha256=hashlib.sha256(b"").hexdigest(),
                updater_sha256=hashlib.sha256(b"").hexdigest(),
            )
            runtime._save_update_state(
                {"ok": True, "status": "updated", "expected_trade_date": "2026-07-31"}
            )

            result = runtime.update(dry_run=True)

            self.assertEqual("dry_run", result["status"])
            self.assertEqual("old", marker.read_text(encoding="utf-8"))
            self.assertFalse((root / "data.next").exists())
            persisted = json.loads(
                runtime.paths.update_state.read_text(encoding="utf-8")
            )
            self.assertEqual("updated", persisted["status"])

    def test_direct_update_rejects_unsafe_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"fixture"
            for name in ("stockdb.exe", "数据更新.exe"):
                (root / name).write_bytes(payload)
            (root / "stockdb.conf").write_text(
                "server:\n\tip: 0.0.0.0\n\treadonly: no\n",
                encoding="utf-8",
            )
            (root / "sync_url.txt").write_text(
                "http://mirror.invalid",
                encoding="utf-8",
            )
            (root / "data").mkdir()
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=root / "runtime",
                data_root=root,
                socket_probe=lambda *_: False,
                server_sha256=hashlib.sha256(payload).hexdigest(),
                updater_sha256=hashlib.sha256(payload).hexdigest(),
            )

            with self.assertRaisesRegex(RuntimeError, "security preflight failed"):
                runtime.update(dry_run=True)

    def test_doctor_rejects_an_unpinned_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stockdb.exe").write_bytes(b"unexpected")
            (root / "数据更新.exe").write_bytes(b"unexpected")
            (root / "stockdb.conf").write_text("readonly: yes", encoding="utf-8")
            (root / "sync_url.txt").write_text("http://mirror.invalid", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / ".sync_manifest.json").write_text(
                json.dumps({"version": 2, "files": [{"path": "sample.ldb"}]}),
                encoding="utf-8",
            )
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=root / "runtime",
                data_root=root,
                socket_probe=lambda *_: False,
            )

            result = runtime.doctor(include_samples=False)

            self.assertFalse(result["binary"]["verified"])
            self.assertFalse(result["binary"]["updater_verified"])
            self.assertFalse(result["checks"]["server"])
            self.assertFalse(result["checks"]["updater"])

    def test_staged_manifest_rejects_size_and_sha256_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = FreeStockDBRuntime(
                root,
                runtime_root=root / "runtime",
                data_root=root,
                socket_probe=lambda *_: False,
            )
            staged = root / "staged"
            data = staged / "data"
            data.mkdir(parents=True)
            payload = b"verified"
            (data / "sample.ldb").write_bytes(payload)
            (data / ".sync_manifest.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "files": [
                            {
                                "path": "sample.ldb",
                                "size": len(payload) + 1,
                                "sha256": hashlib.sha256(payload).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                runtime._verify_staged_data(staged)

            manifest = json.loads((data / ".sync_manifest.json").read_text(encoding="utf-8"))
            manifest["files"][0]["size"] = len(payload)
            manifest["files"][0]["sha256"] = "0" * 64
            (data / ".sync_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                runtime._verify_staged_data(staged)

    def test_external_data_root_uses_live_staging_and_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base / "program",
                runtime_root=base / "runtime",
                data_root=base / "storage",
                socket_probe=lambda *_: False,
            )

            self.assertEqual(base / "storage" / "live", runtime.paths.live)
            self.assertEqual(base / "storage" / "staging", runtime.paths.staging)
            self.assertEqual(base / "storage" / "previous", runtime.paths.previous)

    def test_repair_persists_compact_state_without_recursive_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base / "program",
                runtime_root=base / "runtime",
                data_root=base / "storage",
                socket_probe=lambda *_: False,
            )
            unhealthy = {
                "ok": False,
                "connection_leak": True,
                "port_conflict": False,
            }
            healthy = {
                "ok": True,
                "connection_leak": False,
                "port_conflict": False,
            }

            with (
                patch.object(runtime, "_ensure_readonly_config", return_value=False),
                patch.object(runtime, "doctor", side_effect=[unhealthy, healthy]),
                patch.object(runtime, "exact_processes", return_value=[]),
                patch.object(runtime, "_start_service"),
            ):
                result = runtime.repair(force_restart=True)

            state = json.loads(runtime.paths.state.read_text(encoding="utf-8"))
            self.assertEqual("repaired", result["status"])
            self.assertEqual("repaired", state["status"])
            self.assertNotIn("health", state)

    def test_repair_failure_counter_is_independent_from_update_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base / "program",
                runtime_root=base / "runtime",
                data_root=base / "storage",
                socket_probe=lambda *_: False,
            )
            runtime.paths.state.parent.mkdir(parents=True, exist_ok=True)
            runtime.paths.state.write_text(
                json.dumps({"status": "waiting_for_second_failure", "repair_failures": 1}),
                encoding="utf-8",
            )
            runtime.paths.update_state.write_text(
                json.dumps({"status": "updated", "repair_failures": 0}),
                encoding="utf-8",
            )
            unhealthy = {
                "ok": False,
                "connection_leak": False,
                "port_conflict": False,
            }
            healthy = {
                "ok": True,
                "connection_leak": False,
                "port_conflict": False,
            }

            with (
                patch.object(runtime, "_ensure_readonly_config", return_value=False),
                patch.object(runtime, "doctor", side_effect=[unhealthy, healthy]),
                patch.object(runtime, "exact_processes", return_value=[]),
                patch.object(runtime, "_start_service") as start_service,
            ):
                result = runtime.repair()

            self.assertEqual("repaired", result["status"])
            start_service.assert_called_once_with()

    def test_interrupted_swap_restores_old_live_and_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            storage = base / "storage"
            runtime = FreeStockDBRuntime(
                base / "program",
                runtime_root=base / "runtime",
                data_root=storage,
                socket_probe=lambda *_: False,
            )
            runtime.paths.live.mkdir(parents=True)
            (runtime.paths.live / "marker.txt").write_text("unverified-new", encoding="utf-8")
            runtime.paths.previous.mkdir()
            (runtime.paths.previous / "marker.txt").write_text("verified-old", encoding="utf-8")
            runtime._retired_previous.mkdir()
            (runtime._retired_previous / "marker.txt").write_text("older", encoding="utf-8")
            runtime._write_swap_journal("old_moved")

            result = runtime._recover_interrupted_swap()

            self.assertTrue(result["recovered"])
            self.assertEqual(
                "verified-old",
                (runtime.paths.live / "marker.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "older",
                (runtime.paths.previous / "marker.txt").read_text(encoding="utf-8"),
            )

    def test_interrupted_swap_stops_service_before_replacing_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            storage = base / "storage"
            runtime = FreeStockDBRuntime(
                base / "program",
                runtime_root=base / "runtime",
                data_root=storage,
                socket_probe=lambda *_: True,
            )
            runtime.paths.live.mkdir(parents=True)
            (runtime.paths.live / "marker.txt").write_text("new", encoding="utf-8")
            runtime.paths.previous.mkdir()
            (runtime.paths.previous / "marker.txt").write_text("old", encoding="utf-8")
            runtime._write_swap_journal("new_live")

            with (
                patch.object(
                    runtime,
                    "exact_processes",
                    side_effect=[[{"ProcessId": 1}], []],
                ),
                patch.object(runtime, "_stop_exact_service") as stop_service,
                patch.object(runtime, "_start_service") as start_service,
            ):
                result = runtime._recover_interrupted_swap()

            self.assertTrue(result["recovered"])
            stop_service.assert_called_once_with()
            start_service.assert_called_once_with()
            self.assertEqual(
                "old",
                (runtime.paths.live / "marker.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse(runtime._swap_journal.exists())

    def test_failed_swap_recovery_leaves_service_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = FreeStockDBRuntime(
                base / "program",
                runtime_root=base / "runtime",
                data_root=base / "storage",
                socket_probe=lambda *_: True,
            )
            runtime.paths.live.mkdir(parents=True)
            runtime.paths.previous.mkdir()
            runtime._write_swap_journal("new_live")

            with (
                patch.object(
                    runtime,
                    "exact_processes",
                    return_value=[{"ProcessId": 1}],
                ),
                patch.object(runtime, "_stop_exact_service") as stop_service,
                patch.object(runtime, "_start_service") as start_service,
                patch(
                    "freestockdb_runtime.shutil.rmtree",
                    side_effect=OSError("fixture failure"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "fixture failure"):
                    runtime._recover_interrupted_swap()

            stop_service.assert_called_once_with()
            start_service.assert_not_called()
            self.assertTrue(runtime._swap_journal.is_file())
