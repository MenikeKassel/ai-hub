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
import uuid
from pathlib import Path


def _default_workspace() -> Path:
    """Repo root: AI_HUB_HOME → AI_WORKSPACE_ROOT/ai-hub → script location."""
    hub = os.environ.get("AI_HUB_HOME")
    if hub:
        return Path(hub).resolve()
    workspace_root = os.environ.get("AI_WORKSPACE_ROOT")
    if workspace_root:
        return Path(workspace_root).resolve() / "ai-hub"
    return Path(__file__).resolve().parents[2]


def _workspace_arg(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"workspace not found: {path}")
    return path


DEFAULT_WORKSPACE = _default_workspace()
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
    parser.add_argument(
        "--workspace",
        type=_workspace_arg,
        default=None,
        help=(
            "Absolute workspace path (default: $AI_HUB_HOME, else "
            "$AI_WORKSPACE_ROOT\\ai-hub, else the repository root derived "
            "from this script's location)."
        ),
    )
    parser.add_argument("--mode", choices=("read-only", "write"), default="write")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.workspace is None:
        try:
            args.workspace = _workspace_arg(str(_default_workspace()))
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    return args


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


def build_prompt(task: str, *, workspace: Path, branch: str = "") -> str:
    worktree_rule = ""
    if branch:
        worktree_rule = f"""
This is a Hermes source-maintenance worktree. Work only in this worktree on
branch `{branch}`. Do not edit the base checkout, runtime databases,
credentials, or scheduled-task definitions. Commit the tested change and open
a PR if GitHub access is available; never merge it automatically.
"""
    return f"""You are executing a task delegated by the user's local Hermes agent.

Work autonomously through implementation and proportionate verification. Read the
workspace instructions (AGENTS.md and referenced docs) before changing files.
Preserve unrelated user changes. Never expose secrets. Do not place trades or make
irreversible external changes unless the task explicitly says the user already
confirmed that exact action. End with a concise report of the outcome, changed
files, verification, and any blocker.
{worktree_rule}
Delegated workspace: {workspace}

Delegated user request:
""" + task


def _git_root(workspace: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value).resolve() if value else None


def prepare_hermes_write_worktree(workspace: Path) -> tuple[Path, str]:
    """Keep Hermes write delegations out of the live ai-hub checkout."""
    root = _git_root(workspace)
    if root is None or root.name.casefold() != "ai-hub":
        return workspace, ""
    task_id = os.environ.get("HERMES_TASK_ID", "").strip()
    if not task_id:
        task_id = f"task-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    safe_task_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in task_id)
    safe_task_id = safe_task_id.strip("-")[:80] or f"task-{uuid.uuid4().hex[:8]}"
    branch = f"hermes/{safe_task_id}"
    worktree_root = root.parent / "_worktrees" / "ai-hub-hermes"
    worktree = worktree_root / safe_task_id
    if worktree.exists():
        existing = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip() == branch:
            return worktree, branch
        raise RuntimeError(f"Hermes worktree already exists with another branch: {worktree}")
    worktree_root.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-b", branch, str(worktree), "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git worktree add failed").strip()
        raise RuntimeError(detail[-2000:])
    return worktree, branch


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
        branch = ""
        if args.mode == "write" and not args.dry_run:
            workspace, branch = prepare_hermes_write_worktree(workspace)

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
                        "worktree_branch": branch,
                    }
                )
                return 0

            env = os.environ.copy()
            env["NO_COLOR"] = "1"
            completed = subprocess.run(
                command,
                input=build_prompt(task, workspace=workspace, branch=branch),
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
                        "worktree_branch": branch,
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
                    "workspace": str(workspace),
                    "worktree_branch": branch,
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
