from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


# 从环境变量推导 Hermes 主目录, 避免硬编码用户名
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_USERPROFILE = os.environ.get("USERPROFILE", "")
APPDATA_HOME = (
    Path(_LOCALAPPDATA) / "hermes"
    if _LOCALAPPDATA
    else Path(r"C:\Users\YOUR_USER\AppData\Local\hermes")
)
LEGACY_HOME = (
    Path(_USERPROFILE) / ".hermes"
    if _USERPROFILE
    else Path(r"C:\Users\YOUR_USER\.hermes")
)
PIPELINE = Path(__file__).resolve().parent / "capture_pipeline.py"
CONFIG_FILE = PIPELINE.parent / "config.yaml"
DEFAULT_VAULT = Path(r"D:\aiworkspace\obsidian-vaults")
DEFAULT_INBOX = "00-Inbox"


def check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def extract_config_value(config: str, key: str) -> str:
    match = re.search(rf'^\s*{re.escape(key)}\s*:\s*(.+?)\s*(?:#.*)?$', config, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip('"').strip("'")


def probe_python(agent_dir: Path) -> str:
    """插件注册探测优先用 hermes-agent 自带的 venv python（有 hermes_cli 依赖）。"""
    candidate = agent_dir / "venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else sys.executable


def run_python(code: str, cwd: Path, hermes_home: Path, python_exe: str) -> tuple[int, str, str]:
    completed = subprocess.run(
        [python_exe, "-c", code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
        env={**os.environ, "HERMES_HOME": str(hermes_home)},
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def inspect_hermes_home(home: Path) -> list[dict[str, Any]]:
    config = read_text(home / "config.yaml")
    plugin_dir = home / "plugins" / "hermes-capture-commands"
    agent_dir = APPDATA_HOME / "hermes-agent"
    checks: list[dict[str, Any]] = [
        check(f"{home} exists", home.exists(), str(home)),
        check(f"{home} config exists", bool(config), str(home / "config.yaml")),
        check(
            f"{home} has no capture quick_commands",
            re.search(r'target:\s*["\']?/hermes-capture\s+/(?:clip|idea|readlater)', config) is None,
            "remove stale /clip aliases",
        ),
        check(f"{home} plugin installed", (plugin_dir / "plugin.yaml").exists() and (plugin_dir / "__init__.py").exists(), str(plugin_dir)),
    ]
    if agent_dir.exists():
        python_exe = probe_python(agent_dir)
        code = (
            "from hermes_cli.plugins import discover_plugins, get_plugin_commands, has_hook;"
            "discover_plugins(force=True);"
            "cmds=get_plugin_commands();"
            "print(','.join(sorted(k for k in cmds if k in {'clip','idea','readlater','log','day','auto','kol','event','concept','holding','c','i','rl','j','a','k','e','x','h'})));"
            "print('hook=' + str(has_hook('pre_gateway_dispatch')).lower())"
        )
        rc, out, err = run_python(code, agent_dir, home, python_exe)
        lines = out.splitlines()
        expected = {
            "clip",
            "idea",
            "readlater",
            "log",
            "day",
            "auto",
            "kol",
            "event",
            "concept",
            "holding",
            "c",
            "i",
            "rl",
            "j",
            "a",
            "k",
            "e",
            "x",
            "h",
        }
        found = set(filter(None, lines[0].split(","))) if lines else set()
        checks.append(check(f"{home} plugin commands registered", rc == 0 and expected <= found, out or err))
        checks.append(check(f"{home} gateway auto-capture hook registered", rc == 0 and "hook=true" in out.lower(), out or err))
    return checks


def inspect_pipeline() -> list[dict[str, Any]]:
    config = read_text(CONFIG_FILE)
    vault_value = extract_config_value(config, "vault_path")
    inbox_dir = extract_config_value(config, "obsidian_inbox_dir") or DEFAULT_INBOX
    vault = Path(vault_value) if vault_value else DEFAULT_VAULT
    checks = [
        check("pipeline exists", PIPELINE.exists(), str(PIPELINE)),
        check("config vault_path set (obsidian 写入已启用)", bool(vault_value), vault_value or str(DEFAULT_VAULT)),
        check("vault exists", vault.exists(), str(vault)),
        check("obsidian inbox exists", (vault / inbox_dir).exists(), str(vault / inbox_dir)),
    ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks = []
    checks.extend(inspect_hermes_home(APPDATA_HOME))
    if (LEGACY_HOME / "config.yaml").exists():
        checks.extend(inspect_hermes_home(LEGACY_HOME))
    checks.extend(inspect_pipeline())
    ok = all(item["ok"] for item in checks)
    payload = {"ok": ok, "checks": checks}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            mark = "OK" if item["ok"] else "FAIL"
            print(f"[{mark}] {item['name']} - {item['detail']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
