from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DEFAULT_WORKSPACE = Path(r"<AI_HUB_HOME>\ai-hub")
MAX_DIAGNOSTIC_CHARS = 4000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Hermes request through the local Codex CLI."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task", help="Task text. Prefer --stdin for arbitrary user text.")
    source.add_argument("--task-base64", help="UTF-8 task encoded as Base64.")
    source.add_argument("--prompt-file", type=Path, help="UTF-8 file containing the task.")
    source.add_argument("--stdin", action="store_true", help="Read the task from stdin.")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--mode", choices=("read-only", "write"), default="write")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def read_task(args: argparse.Namespace) -> str:
    if args.task is not None:
        task = args.task
    elif args.task_base64 is not None:
        task = base64.b64decode(args.task_base64, validate=True).decode("utf-8")
    elif args.prompt_file is not None:
        task = args.prompt_file.read_text(encoding="utf-8")
    else:
        task = sys.stdin.read()
    task = task.strip()
    if not task:
        raise ValueError("delegated task is empty")
    return task


def find_codex() -> str:
    # The Microsoft Store executable can be discoverable but deny child-process
    # execution. The npm .cmd shim is the reliable non-interactive entry point.
    candidates = ("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError("Codex CLI was not found on PATH")


def build_prompt(task: str) -> str:
    return """You are executing a task delegated by the user's local Hermes agent.

Work autonomously through implementation and proportionate verification. Read the
workspace instructions (AGENTS.md and referenced docs) before changing files.
Preserve unrelated user changes. Never expose secrets. Do not place trades or make
irreversible external changes unless the task explicitly says the user already
confirmed that exact action. End with a concise report of the outcome, changed
files, verification, and any blocker.

Delegated user request:
""" + task


def make_command(codex: str, workspace: Path, mode: str, output_file: Path) -> list[str]:
    sandbox = "read-only" if mode == "read-only" else "danger-full-access"
    return [
        codex,
        "--ask-for-approval",
        "never",
        "exec",
        "--cd",
        str(workspace),
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--color",
        "never",
        "--output-last-message",
        str(output_file),
        "-",
    ]


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    try:
        task = read_task(args)
        workspace = args.workspace.resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"workspace not found: {workspace}")
        codex = find_codex()

        with tempfile.TemporaryDirectory(prefix="hermes-codex-") as temp_dir:
            output_file = Path(temp_dir) / "last-message.txt"
            command = make_command(codex, workspace, args.mode, output_file)
            if args.dry_run:
                emit(
                    {
                        "ok": True,
                        "dry_run": True,
                        "workspace": str(workspace),
                        "mode": args.mode,
                        "codex": codex,
                        "task_chars": len(task),
                    }
                )
                return 0

            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            completed = subprocess.run(
                command,
                input=build_prompt(task),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=str(workspace),
                env=env,
                timeout=args.timeout,
                check=False,
            )
            answer = (
                output_file.read_text(encoding="utf-8", errors="replace").strip()
                if output_file.exists()
                else ""
            )
            elapsed = round(time.monotonic() - started, 2)
            if completed.returncode == 0 and answer:
                emit(
                    {
                        "ok": True,
                        "answer": answer,
                        "workspace": str(workspace),
                        "mode": args.mode,
                        "elapsed_seconds": elapsed,
                    }
                )
                return 0

            diagnostic = (completed.stderr or completed.stdout or answer).strip()
            emit(
                {
                    "ok": False,
                    "error": diagnostic[-MAX_DIAGNOSTIC_CHARS:]
                    or f"Codex exited with code {completed.returncode}",
                    "exit_code": completed.returncode,
                    "elapsed_seconds": elapsed,
                }
            )
            return 1
    except subprocess.TimeoutExpired as exc:
        emit(
            {
                "ok": False,
                "error": f"Codex timed out after {exc.timeout} seconds",
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
        )
        return 1
    except Exception as exc:  # Keep the Hermes-facing contract as one JSON object.
        emit(
            {
                "ok": False,
                "error": str(exc),
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
