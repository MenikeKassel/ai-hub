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
    market_update_enabled: bool = True
    returns_update_enabled: bool = True
    research_update_enabled: bool = True
    publication_mode: str = "atomic_daily"

    @property
    def historical_read_only(self) -> bool:
        return self.mode == "historical" and not self.write_enabled

    @property
    def market_writes_enabled(self) -> bool:
        return self.write_enabled and self.market_update_enabled

    @property
    def returns_writes_enabled(self) -> bool:
        return self.write_enabled and self.returns_update_enabled

    @property
    def research_writes_enabled(self) -> bool:
        return self.write_enabled and self.research_update_enabled

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "as_of": self.as_of,
            "write_enabled": self.write_enabled,
            "market_update_enabled": self.market_update_enabled,
            "returns_update_enabled": self.returns_update_enabled,
            "research_update_enabled": self.research_update_enabled,
            "publication_mode": self.publication_mode,
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
    mode = str(value.get("mode") or "live")
    write_enabled = bool(value.get("write_enabled", True))
    return MarketRecoveryMode(
        mode=mode,
        as_of=str(value.get("as_of") or ""),
        write_enabled=write_enabled,
        market_update_enabled=bool(value.get("market_update_enabled", write_enabled or mode != "historical")),
        returns_update_enabled=bool(value.get("returns_update_enabled", write_enabled or mode != "historical")),
        research_update_enabled=bool(value.get("research_update_enabled", write_enabled or mode != "historical")),
        publication_mode=str(value.get("publication_mode") or "atomic_daily"),
    )


def market_runtime_writes_enabled(path: Path) -> bool:
    return load_market_recovery_mode(path).market_writes_enabled


def assert_market_runtime_writes_allowed(path: Path) -> None:
    mode = load_market_recovery_mode(path)
    if mode.market_writes_enabled:
        return
    raise MarketWriteBlockedError(
        "market runtime is historical/read-only as of "
        f"{mode.as_of or 'unknown'}; forward market writes are disabled"
    )


def assert_returns_writes_allowed(path: Path) -> None:
    mode = load_market_recovery_mode(path)
    if mode.returns_writes_enabled:
        return
    raise MarketWriteBlockedError(
        "returns updates are disabled by the current market mode"
    )


def assert_research_writes_allowed(path: Path) -> None:
    mode = load_market_recovery_mode(path)
    if mode.research_writes_enabled:
        return
    raise MarketWriteBlockedError(
        "research updates are disabled by the current market mode"
    )
