from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


REPO_ROOT = Path(os.environ.get("AI_HUB_HOME") or os.environ.get("AI_HUB_ROOT") or Path(__file__).resolve().parents[2])
OPERATOR = REPO_ROOT / "scripts" / "hermes-kol-operator.ps1"
ACTIONS = {
    "status",
    "doctor",
    "start",
    "open",
    "collect",
    "review",
    "market",
    "returns",
    "board-status",
    "board-sync",
    "import-zhihu",
    "onboard-zhihu",
    "list-kols",
    "add-kol",
    "set-kol-status",
    "list-drafts",
    "approve-draft",
    "reject-draft",
    "list-events",
    "event-action",
}


KOL_OPERATOR_SCHEMA = {
    "name": "kol_operator",
    "description": (
        "Operate the local KOL audit workbench through its deterministic Windows "
        "operator. Use this tool, never terminal or raw Python, to start/open the "
        "console, collect posts, inspect status, manage KOLs, review explicit draft "
        "IDs, sync market data, inspect board RPS, or update returns. This tool cannot edit source code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "id": {"type": "integer", "minimum": 1},
            "handle": {"type": "string"},
            "display_name": {"type": "string"},
            "platform": {"type": "string", "enum": ["X", "Zhihu"]},
            "profile_url": {"type": "string"},
            "domain": {"type": "string"},
            "batch_size": {"type": "integer", "minimum": 1, "maximum": 31},
            "status": {"type": "string", "enum": ["active", "paused"]},
            "review_date": {
                "type": "string",
                "description": "Review date in YYYY-MM-DD form.",
            },
            "note": {"type": "string"},
            "event_id": {"type": "string"},
            "event_action": {
                "type": "string",
                "enum": ["activate", "exclude", "restore", "archive"],
            },
            "force": {"type": "boolean", "default": False},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _json_result(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _find_powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise RuntimeError("powershell.exe is not available")
    return executable


def _build_command(args: dict[str, Any]) -> list[str]:
    action = str(args.get("action") or "")
    if action not in ACTIONS:
        raise ValueError(f"unsupported action: {action}")
    if not OPERATOR.is_file():
        raise RuntimeError(f"KOL operator is missing: {OPERATOR}")

    command = [
        _find_powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(OPERATOR),
        "-Action",
        action,
        "-RepoRoot",
        str(REPO_ROOT),
    ]
    switches = {
        "id": "-Id",
        "handle": "-Handle",
        "display_name": "-DisplayName",
        "platform": "-Platform",
        "profile_url": "-ProfileUrl",
        "domain": "-Domain",
        "batch_size": "-BatchSize",
        "status": "-Status",
        "review_date": "-ReviewDate",
        "note": "-Note",
        "event_id": "-EventId",
        "event_action": "-EventAction",
    }
    for field, switch in switches.items():
        value = args.get(field)
        if value is not None and str(value).strip():
            command.extend((switch, str(value)))
    if args.get("force") is True:
        command.append("-Force")
    return command


def _parse_operator_output(stdout: str) -> dict[str, Any] | list[Any]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    raise ValueError("operator returned no JSON result")


def run_operator(args: dict[str, Any], **_: Any) -> str:
    try:
        command = _build_command(args)
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
        if completed.returncode != 0:
            return _json_result(
                {
                    "ok": False,
                    "error": (
                        completed.stderr.strip()
                        or completed.stdout.strip()
                        or f"operator exited with {completed.returncode}"
                    )[-2000:],
                }
            )
        return _json_result(_parse_operator_output(completed.stdout))
    except subprocess.TimeoutExpired:
        return _json_result({"ok": False, "error": "KOL operator timed out after 90 seconds"})
    except Exception as exc:
        return _json_result({"ok": False, "error": str(exc)})


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="kol_operator",
        toolset="kol-research-operator",
        schema=KOL_OPERATOR_SCHEMA,
        handler=run_operator,
    )
