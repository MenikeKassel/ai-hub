from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _resolve_paths() -> tuple[Path, Path]:
    """Resolve (WORKSPACE_ROOT, DEFAULT_WORKSPACE).

    Priority: AI_HUB_HOME (repo root) → AI_WORKSPACE_ROOT (legacy parent
    directory) → derive from this script's own location. When AI_HUB_HOME
    is set but does not exist, fail loudly with a clear error.
    """
    hub = os.environ.get("AI_HUB_HOME")
    if hub:
        root = Path(hub).resolve()
        if not root.is_dir():
            raise RuntimeError(f"AI_HUB_HOME is set but does not exist: {hub}")
        return root, root
    workspace_root = os.environ.get("AI_WORKSPACE_ROOT")
    if workspace_root:
        root = Path(workspace_root).resolve()
        return root, root / "ai-hub"
    root = Path(__file__).resolve().parents[2]
    return root, root


WORKSPACE_ROOT, DEFAULT_WORKSPACE = _resolve_paths()
DELEGATE = DEFAULT_WORKSPACE / "_automation" / "codex-delegate" / "invoke_codex.py"


CODEX_DELEGATE_SCHEMA = {
    "name": "codex_delegate",
    "description": (
        "Delegate source inspection, debugging, implementation, tests, Git work, or "
        "other maintenance to the local Codex CLI. Hermes must use this tool instead "
        "of terminal, execute_code, write_file, patch, or delegate_task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "minLength": 1, "maxLength": 20000},
            "mode": {
                "type": "string",
                "enum": ["read-only", "write"],
                "default": "write",
            },
            "workspace": {
                "type": "string",
                "description": "Absolute workspace path under <AI_HUB_HOME>.",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    },
}


def _json_result(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _workspace_path(raw: Any) -> Path:
    workspace = Path(str(raw or DEFAULT_WORKSPACE)).resolve()
    root = WORKSPACE_ROOT.resolve()
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"workspace must be under {root}") from exc
    if not workspace.is_dir():
        raise FileNotFoundError(f"workspace not found: {workspace}")
    return workspace


def _build_command(args: dict[str, Any]) -> list[str]:
    task = str(args.get("task") or "").strip()
    if not task:
        raise ValueError("delegated task is empty")
    if len(task) > 20000:
        raise ValueError("delegated task exceeds 20000 characters")
    mode = str(args.get("mode") or "write")
    if mode not in {"read-only", "write"}:
        raise ValueError(f"unsupported mode: {mode}")
    if not DELEGATE.is_file():
        raise FileNotFoundError(f"Codex delegate is missing: {DELEGATE}")
    encoded = base64.b64encode(task.encode("utf-8")).decode("ascii")
    return [
        sys.executable,
        str(DELEGATE),
        "--task-base64",
        encoded,
        "--workspace",
        str(_workspace_path(args.get("workspace"))),
        "--mode",
        mode,
    ]


def _parse_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Codex delegate returned no JSON result")


def run_codex_delegate(args: dict[str, Any], **_: Any) -> str:
    try:
        completed = subprocess.run(
            _build_command(args),
            cwd=str(_workspace_path(args.get("workspace"))),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1830,
            check=False,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
        result = _parse_result(completed.stdout)
        if completed.returncode != 0:
            result["ok"] = False
            if not result.get("error"):
                result["error"] = (
                    completed.stderr.strip()
                    or f"Codex delegate exited with {completed.returncode}"
                )[-4000:]
        return _json_result(result)
    except subprocess.TimeoutExpired:
        return _json_result({"ok": False, "error": "Codex delegation timed out"})
    except Exception as exc:
        return _json_result({"ok": False, "error": str(exc)})


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="codex_delegate",
        toolset="codex-delegate",
        schema=CODEX_DELEGATE_SCHEMA,
        handler=run_codex_delegate,
    )
