from __future__ import annotations

from datetime import datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
PHASE_RUNTIME_MINUTES = {"initial": 45.0, "refresh": 18.0, "final": 10.0}


def effective_phase(current: datetime) -> str:
    local = current.astimezone(SHANGHAI) if current.tzinfo else current.replace(tzinfo=SHANGHAI)
    if local.time() < time(8, 10):
        return "initial"
    if local.time() < time(8, 40):
        return "refresh"
    return "final"


def next_phase(completed: str, current: datetime) -> str | None:
    due = effective_phase(current)
    if completed == "initial" and due == "refresh":
        return "refresh"
    if completed in {"initial", "refresh"} and due == "final":
        return "final"
    return None


class MorningOrchestrator:
    def __init__(
        self,
        *,
        runner: Callable[[str, float], dict[str, Any]],
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.runner = runner
        self.now_provider = now_provider or (lambda: datetime.now(SHANGHAI))

    def run(self) -> dict[str, Any]:
        current = self.now_provider()
        phase = effective_phase(current)
        phases: list[str] = []
        results: list[dict[str, Any]] = []
        while phase:
            runtime = 25.0 if phase == "final" and current.time() >= time(9, 0) else PHASE_RUNTIME_MINUTES[phase]
            result = self.runner(phase, runtime)
            phases.append(phase)
            results.append(result)
            if not result.get("ok", False):
                break
            current = self.now_provider()
            phase = next_phase(phase, current)
        return {
            "ok": bool(results) and all(item.get("ok", False) for item in results),
            "phases": phases,
            "results": results,
        }
