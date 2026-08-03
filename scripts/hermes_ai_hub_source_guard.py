from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


HERMES_HOME = Path(
    os.environ.get("HERMES_HOME")
    or Path(os.environ["LOCALAPPDATA"]) / "hermes"
)
MUTATING_SKILL_ACTIONS = {
    "create",
    "edit",
    "patch",
    "delete",
    "write_file",
    "remove_file",
}


def _block(message: str) -> None:
    print(json.dumps({"action": "block", "message": message}, ensure_ascii=True))
    raise SystemExit(0)


def _allow() -> None:
    print("{}")
    raise SystemExit(0)


def _read_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        _block(
            "Hermes source guard could not parse the tool request. "
            "Use the KOL operator or delegate the change to Codex."
        )
    return payload if isinstance(payload, dict) else {}


def main() -> None:
    payload = _read_payload()
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    args = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in {"write_file", "patch"}:
        _block(
            "Hermes is operation-only and cannot write or patch files. "
            "Use a native operation tool, or codex_delegate for source maintenance."
        )

    if tool_name == "skill_manage":
        action = str(args.get("action") or "")
        if action in MUTATING_SKILL_ACTIONS:
            _block(
                "Hermes cannot create or modify skills. Use codex_delegate."
            )
        _allow()

    if tool_name == "delegate_task":
        _block(
            "Hermes cannot delegate maintenance to a generic subagent. "
            "Use the native codex_delegate tool."
        )

    if tool_name in {"terminal", "execute_code"}:
        _block(
            "Hermes cannot use a general-purpose shell or code executor. "
            "Use a native operation tool, or codex_delegate for maintenance."
        )

    _allow()


if __name__ == "__main__":
    main()
