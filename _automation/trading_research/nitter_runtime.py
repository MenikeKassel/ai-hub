from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class NitterSessionStore(Protocol):
    def session(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class NitterRuntimeFiles:
    root: Path
    config_path: Path
    sessions_path: Path
    hmac_path: Path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def materialize_nitter_runtime(
    runtime_root: Path,
    template_path: Path,
    credentials: NitterSessionStore,
) -> NitterRuntimeFiles:
    root = Path(runtime_root)
    template = Path(template_path)
    if not template.is_file():
        raise FileNotFoundError(f"Nitter config template not found: {template}")
    root.mkdir(parents=True, exist_ok=True)
    hmac_path = root / "hmac.key"
    if hmac_path.exists():
        hmac_key = hmac_path.read_text(encoding="ascii").strip()
    else:
        hmac_key = secrets.token_hex(32)
        _atomic_write(hmac_path, hmac_key + "\n")

    session = credentials.session()
    sessions_path = root / "sessions.jsonl"
    config_path = root / "nitter.conf"
    _atomic_write(sessions_path, json.dumps(session, ensure_ascii=True, separators=(",", ":")) + "\n")
    _atomic_write(config_path, template.read_text(encoding="utf-8").replace("{HMAC_KEY}", hmac_key))
    return NitterRuntimeFiles(root, config_path, sessions_path, hmac_path)


def command_version(command: Path | str, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [str(command), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or completed.stderr).strip().splitlines()[0][:200]


def xtf_version(command: Path | str) -> str:
    executable = Path(command)
    marker = executable.parent.parent / ".ai-hub-version"
    if marker.is_file():
        return marker.read_text(encoding="ascii").strip()[:200]
    python = executable.with_name("python.exe") if os.name == "nt" else executable.with_name("python")
    if not python.exists():
        return ""
    return command_version(
        python,
        "-c",
        "import importlib.metadata; print(importlib.metadata.version('x-tweet-fetcher'))",
    )


def docker_command() -> str:
    command = shutil.which("docker.exe" if os.name == "nt" else "docker") or ""
    if command:
        return command
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        if candidate.is_file():
            return str(candidate)
    return ""


def docker_health() -> dict[str, Any]:
    command = docker_command()
    if not command:
        return {"installed": False, "ready": False, "command": "", "version": ""}
    version = command_version(command, "version", "--format", "{{.Server.Version}}")
    return {"installed": True, "ready": bool(version), "command": command, "version": version}


def docker_container_health(name: str) -> dict[str, Any]:
    command = docker_command()
    if not command:
        return {"ready": False, "status": "missing", "health": ""}
    try:
        completed = subprocess.run(
            [
                command,
                "inspect",
                "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                name,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ready": False, "status": "error", "health": ""}
    if completed.returncode != 0:
        return {"ready": False, "status": "missing", "health": ""}
    status, _, health = completed.stdout.strip().partition("|")
    return {
        "ready": status == "running" and health in {"", "healthy"},
        "status": status,
        "health": health,
    }


def http_health(url: str, timeout: float = 3.0) -> dict[str, Any]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ai-hub-nitter-doctor/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ready": 200 <= response.status < 500, "status": response.status, "error": ""}
    except urllib.error.HTTPError as exc:
        return {"ready": exc.code < 500, "status": exc.code, "error": str(exc)[:300]}
    except (OSError, urllib.error.URLError) as exc:
        return {"ready": False, "status": 0, "error": str(exc)[:300]}


def timeline_health(
    url: str,
    *,
    handle: str = "github",
    timeout: float = 8.0,
) -> dict[str, Any]:
    target = f"{url.rstrip('/')}/{handle.strip().lstrip('@')}"
    try:
        request = urllib.request.Request(
            target,
            headers={"User-Agent": "ai-hub-nitter-timeline-doctor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(1_000_000).decode("utf-8", errors="replace")
            has_timeline = (
                "timeline-item" in content
                and ("tweet-content" in content or "tweet-date" in content)
            )
            return {
                "ready": 200 <= response.status < 400 and has_timeline,
                "status": "timeline_ok" if has_timeline else "timeline_empty",
                "http_status": response.status,
                "url": target,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        return {
            "ready": False,
            "status": "http_error",
            "http_status": exc.code,
            "url": target,
            "error": str(exc)[:300],
        }
    except (OSError, urllib.error.URLError) as exc:
        return {
            "ready": False,
            "status": "network_error",
            "http_status": 0,
            "url": target,
            "error": str(exc)[:300],
        }
