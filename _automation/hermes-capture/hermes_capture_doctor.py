from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


APPDATA_HOME = Path(r"<USER_HOME>\AppData\Local\hermes")
LEGACY_HOME = Path(r"<USER_HOME>\.hermes")
PIPELINE = Path(__file__).resolve().parent / "capture_pipeline.py"
VAULT = Path(r"<OBSIDIAN_VAULT>")


def check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def run_python(code: str, cwd: Path, hermes_home: Path) -> tuple[int, str, str]:
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env={**__import__("os").environ, "HERMES_HOME": str(hermes_home)},
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
        code = (
            "from hermes_cli.plugins import discover_plugins, get_plugin_commands, has_hook;"
            "discover_plugins(force=True);"
            "cmds=get_plugin_commands();"
            "print(','.join(sorted(k for k in cmds if k in {'clip','idea','readlater','log','day','auto','kol','event','concept','holding','c','i','rl','j','a','k','e','x','h'})));"
            "print('hook=' + str(has_hook('pre_gateway_dispatch')).lower())"
        )
        rc, out, err = run_python(code, agent_dir, home)
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
    checks = [
        check("pipeline exists", PIPELINE.exists(), str(PIPELINE)),
        check("vault exists", VAULT.exists(), str(VAULT)),
        check("obsidian inbox exists", (VAULT / "00_Inbox").exists(), str(VAULT / "00_Inbox")),
        check("obsidian AGENTS exists", (VAULT / "AGENTS.md").exists(), str(VAULT / "AGENTS.md")),
        check("obsidian index exists", (VAULT / "index.md").exists(), str(VAULT / "index.md")),
    ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks = []
    checks.extend(inspect_hermes_home(APPDATA_HOME))
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
