from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_refresh_state(runtime_root: Path) -> dict[str, Any]:
    path = Path(runtime_root) / "pipeline-refresh.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"status": "idle", "symbols": [], "error": ""}


def run_post_approval_refresh(
    runtime_root: Path,
    repo_root: Path,
    symbols: list[str],
    *,
    as_of: date,
    python: str | None = None,
    timeout_seconds: float | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    clean_symbols = sorted({symbol for symbol in symbols if len(symbol) == 6 and symbol.isdigit()})
    if not clean_symbols:
        return {"status": "not_requested", "symbols": []}
    runtime_root = Path(runtime_root)
    state_path = runtime_root / "pipeline-refresh.json"
    lock = FileLock(str(runtime_root / "pipeline-refresh.lock"), timeout=120)
    run_id = uuid.uuid4().hex
    base = {
        "run_id": run_id,
        "symbols": clean_symbols,
        "as_of": as_of.isoformat(),
        "error": "",
    }
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None

    def stage_timeout(default: int) -> float:
        if deadline is None:
            return float(default)
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise TimeoutError("approval refresh exceeded its task budget")
        return min(float(default), remaining)

    def run_stage(command: list[str], default_timeout: int) -> subprocess.CompletedProcess[str]:
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(3):
            result = subprocess.run(
                command,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=stage_timeout(default_timeout),
                check=False,
            )
            detail = f"{result.stderr}\n{result.stdout}".lower()
            retryable = result.returncode != 0 and (
                "cannot open file" in detail
                or "another process" in detail
                or "另一个程序正在使用" in detail
            )
            if not retryable or attempt == 2:
                return result
            time.sleep(2 * (attempt + 1))
        assert result is not None
        return result
    try:
        lock.acquire()
    except Timeout:
        state = {**base, "status": "deferred", "phase": "busy"}
        _write_state(state_path, state)
        return state
    try:
        executable = python or sys.executable
        cli = Path(repo_root) / "_automation" / "trading_research" / "trading_cli.py"
        _write_state(state_path, {**base, "status": "running", "phase": "market_sync"})
        market = run_stage(
            [
                executable,
                str(cli),
                "market-sync",
                "--as-of",
                as_of.isoformat(),
                "--symbols",
                ",".join(clean_symbols),
            ],
            900,
        )
        if market.returncode != 0:
            detail = (market.stderr or market.stdout or "market sync failed").strip()
            raise RuntimeError(detail[-2000:])

        _write_state(state_path, {**base, "status": "running", "phase": "return_update"})
        returns = run_stage(
            [executable, str(cli), "kol-update", "--as-of", as_of.isoformat()],
            600,
        )
        if returns.returncode != 0:
            detail = (returns.stderr or returns.stdout or "return update failed").strip()
            raise RuntimeError(detail[-2000:])
        state = {**base, "status": "completed", "phase": "done"}
        _write_state(state_path, state)
        return state
    except Exception as exc:
        state = {**base, "status": "failed", "phase": "failed", "error": str(exc)[:2000]}
        _write_state(state_path, state)
        if raise_on_error:
            raise RuntimeError(state["error"]) from exc
        return state
    finally:
        lock.release()
