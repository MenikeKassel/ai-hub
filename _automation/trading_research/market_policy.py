from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MarketRecoveryMode:
    mode: str = "live"
    as_of: str = ""
    write_enabled: bool = True

    @property
    def historical_read_only(self) -> bool:
        return self.mode == "historical" and not self.write_enabled

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "as_of": self.as_of,
            "write_enabled": self.write_enabled,
        }


class MarketWriteBlockedError(RuntimeError):
    pass


def load_market_recovery_mode(path: Path) -> MarketRecoveryMode:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return MarketRecoveryMode(
        mode=str(value.get("mode") or "live"),
        as_of=str(value.get("as_of") or ""),
        write_enabled=bool(value.get("write_enabled", True)),
    )


def market_runtime_writes_enabled(path: Path) -> bool:
    return not load_market_recovery_mode(path).historical_read_only


def assert_market_runtime_writes_allowed(path: Path) -> None:
    mode = load_market_recovery_mode(path)
    if not mode.historical_read_only:
        return
    raise MarketWriteBlockedError(
        "market runtime is historical/read-only as of "
        f"{mode.as_of or 'unknown'}; forward market writes are disabled"
    )
