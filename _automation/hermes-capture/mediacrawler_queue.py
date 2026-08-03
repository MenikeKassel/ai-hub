from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mediacrawler_runner import default_queue_file, execute_job, resolve_repo


ACTIVE_STATUSES = {"pending", "running"}
FINAL_STATUSES = {"done", "failed", "cancelled"}
ALL_STATUSES = ACTIVE_STATUSES | FINAL_STATUSES


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="Manage MediaCrawler JSONL queue.")
    parser.add_argument("--queue-file", default="", help="Queue JSONL path. Defaults to _runtime/mediacrawler/queue.jsonl.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List queue jobs.")
    list_parser.add_argument("--status", default="", choices=["", *sorted(ALL_STATUSES)], help="Filter by status.")

    next_parser = subparsers.add_parser("run-next", help="Run the next pending job.")
    next_parser.add_argument("--execute", action="store_true", help="Actually execute the job. Default previews it.")
    next_parser.add_argument("--timeout", type=int, default=1800)
    next_parser.add_argument("--import-results", action="store_true", help="Import successful MediaCrawler output into Obsidian.")
    next_parser.add_argument("--import-notion", action="store_true", help="Also create/update Notion pages when importing results.")
    next_parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))

    run_parser = subparsers.add_parser("run-id", help="Run a job by id.")
    run_parser.add_argument("job_id")
    run_parser.add_argument("--execute", action="store_true", help="Actually execute the job. Default previews it.")
    run_parser.add_argument("--timeout", type=int, default=1800)
    run_parser.add_argument("--import-results", action="store_true", help="Import successful MediaCrawler output into Obsidian.")
    run_parser.add_argument("--import-notion", action="store_true", help="Also create/update Notion pages when importing results.")
    run_parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))

    mark_parser = subparsers.add_parser("mark", help="Mark a job status.")
    mark_parser.add_argument("job_id")
    mark_parser.add_argument("--status", required=True, choices=sorted(ALL_STATUSES))
    mark_parser.add_argument("--note", default="")

    args = parser.parse_args()
    queue_file = Path(args.queue_file) if args.queue_file else default_queue_file()
    queue_file = queue_file.resolve()

    if args.command == "list":
        result = list_jobs(queue_file, args.status)
    elif args.command == "run-next":
        result = run_next(
            queue_file,
            execute=args.execute,
            timeout=args.timeout,
            import_results=args.import_results,
            import_notion=args.import_notion,
            config_path=Path(args.config),
        )
    elif args.command == "run-id":
        result = run_id(
            queue_file,
            args.job_id,
            execute=args.execute,
            timeout=args.timeout,
            import_results=args.import_results,
            import_notion=args.import_notion,
            config_path=Path(args.config),
        )
    elif args.command == "mark":
        result = mark_job(queue_file, args.job_id, args.status, args.note)
    else:
        result = {"ok": False, "error": f"unsupported command: {args.command}"}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0 if result.get("ok") else 1


def configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def list_jobs(queue_file: Path, status: str = "") -> dict[str, Any]:
    jobs = read_queue(queue_file)
    if status:
        jobs = [job for job in jobs if job.get("status", "pending") == status]
    return {"ok": True, "queue_file": str(queue_file), "count": len(jobs), "jobs": jobs}


def run_next(
    queue_file: Path,
    *,
    execute: bool,
    timeout: int,
    import_results: bool = False,
    import_notion: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    jobs = read_queue(queue_file)
    for job in jobs:
        if job.get("status", "pending") == "pending":
            return run_job(
                queue_file,
                jobs,
                job["job_id"],
                execute=execute,
                timeout=timeout,
                import_results=import_results,
                import_notion=import_notion,
                config_path=config_path,
            )
    return {"ok": False, "queue_file": str(queue_file), "error": "no pending MediaCrawler jobs"}


def run_id(
    queue_file: Path,
    job_id: str,
    *,
    execute: bool,
    timeout: int,
    import_results: bool = False,
    import_notion: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    jobs = read_queue(queue_file)
    return run_job(
        queue_file,
        jobs,
        job_id,
        execute=execute,
        timeout=timeout,
        import_results=import_results,
        import_notion=import_notion,
        config_path=config_path,
    )


def run_job(
    queue_file: Path,
    jobs: list[dict[str, Any]],
    job_id: str,
    *,
    execute: bool,
    timeout: int,
    import_results: bool = False,
    import_notion: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    job = find_job(jobs, job_id)
    if not job:
        return {"ok": False, "queue_file": str(queue_file), "error": f"job not found: {job_id}"}

    executable_job = job_to_executable(job)
    if not execute:
        return {
            "ok": True,
            "execute": False,
            "queue_file": str(queue_file),
            "job": public_job(job),
            "command_text": executable_job.get("command_text", ""),
            "message": "dry run; pass --execute to run this job",
        }

    now = datetime.now().isoformat(timespec="seconds")
    job["status"] = "running"
    job["started_at"] = now
    job.pop("finished_at", None)
    write_queue(queue_file, jobs)

    result = execute_job(executable_job, timeout)
    job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    job["status"] = "done" if result.get("ok") else "failed"
    job["exit_code"] = result.get("exit_code")
    job["outputs"] = result.get("outputs", [])
    job["error"] = result.get("error", "")
    job["stdout_excerpt"] = trim(result.get("stdout", ""))
    job["stderr_excerpt"] = trim(result.get("stderr", ""))
    import_result: dict[str, Any] = {}
    if result.get("ok") and import_results:
        import_result = import_job_outputs(
            queue_file=queue_file,
            job=job,
            config_path=config_path or Path(__file__).with_name("config.yaml"),
            write_notion=import_notion,
        )
        job["imported_at"] = datetime.now().isoformat(timespec="seconds")
        job["imported_paths"] = [item.get("obsidian_path", "") for item in import_result.get("imported", []) if item.get("obsidian_path")]
        if not import_result.get("ok"):
            job["import_error"] = import_result.get("error", "unknown import error")
    write_queue(queue_file, jobs)

    return {
        "ok": bool(result.get("ok")) and (not import_results or bool(import_result.get("ok"))),
        "execute": True,
        "queue_file": str(queue_file),
        "job": public_job(job),
        "result": {
            "exit_code": result.get("exit_code"),
            "outputs": result.get("outputs", []),
            "error": result.get("error", ""),
        },
        "import_result": import_result,
    }


def import_job_outputs(queue_file: Path, job: dict[str, Any], config_path: Path, write_notion: bool) -> dict[str, Any]:
    from mediacrawler_importer import import_command

    save_path = Path(job.get("save_path") or default_queue_file().parent).resolve()
    return import_command(
        output_root=save_path,
        queue_file=queue_file,
        config_path=config_path,
        platform=str(job.get("platform", "")),
        job_id=str(job.get("job_id", "")),
        write_notion=write_notion,
        limit=0,
    )


def mark_job(queue_file: Path, job_id: str, status: str, note: str) -> dict[str, Any]:
    jobs = read_queue(queue_file)
    job = find_job(jobs, job_id)
    if not job:
        return {"ok": False, "queue_file": str(queue_file), "error": f"job not found: {job_id}"}
    job["status"] = status
    job["marked_at"] = datetime.now().isoformat(timespec="seconds")
    if note:
        job["note"] = note
    write_queue(queue_file, jobs)
    return {"ok": True, "queue_file": str(queue_file), "job": public_job(job)}


def job_to_executable(job: dict[str, Any]) -> dict[str, Any]:
    save_path = job.get("save_path") or str(default_queue_file().parent)
    platform = job.get("platform", "")
    return {
        "repo": job.get("repo") or str(resolve_repo("")),
        "platform": platform,
        "type": job.get("type", ""),
        "save_path": save_path,
        "output_hint": str(Path(save_path) / platform) if platform else save_path,
        "command": job.get("command", []),
        "command_text": job.get("command_text", ""),
        "notes": [],
    }


def read_queue(queue_file: Path) -> list[dict[str, Any]]:
    if not queue_file.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for line in queue_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "job_id" in job:
            jobs.append(job)
    return jobs


def write_queue(queue_file: Path, jobs: list[dict[str, Any]]) -> None:
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = queue_file.with_suffix(queue_file.suffix + ".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        for job in jobs:
            handle.write(json.dumps(job, ensure_ascii=False) + "\n")
    temp_file.replace(queue_file)


def find_job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    for job in jobs:
        if job.get("job_id") == job_id:
            return job
    return None


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"command"} and not key.endswith("_excerpt")
    }


def trim(text: str, limit: int = 2000) -> str:
    clean = text or ""
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "\n...[truncated]"


def render_text(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"ERROR: {result.get('error', 'unknown error')}"
    if "jobs" in result:
        lines = [f"queue: {result['queue_file']}", f"count: {result['count']}"]
        for job in result["jobs"]:
            lines.append(
                f"- {job.get('job_id')} [{job.get('status', 'pending')}] "
                f"{job.get('platform')} {job.get('type')} {job.get('command_text', '')}"
            )
        return "\n".join(lines)
    if result.get("execute") is False:
        job = result.get("job", {})
        return "\n".join(
            [
                f"job: {job.get('job_id')}",
                f"status: {job.get('status', 'pending')}",
                f"command: {result.get('command_text', '')}",
                result.get("message", ""),
            ]
        ).strip()
    job = result.get("job", {})
    run_result = result.get("result", {})
    return "\n".join(
        [
            f"job: {job.get('job_id')}",
            f"status: {job.get('status')}",
            f"exit_code: {run_result.get('exit_code')}",
            f"outputs: {len(run_result.get('outputs', []))}",
            f"error: {run_result.get('error') or ''}",
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
