"""Public mirror hygiene checks.

Run on CI and before publishing. Fails when the working tree contains
patterns that must never be published:

- hardcoded Notion database/page UUIDs;
- Windows user directories or local absolute paths;
- literal placeholder paths like <AI_HUB_HOME> used as runtime config;
- common API keys, tokens, cookies;
- local database files, .env, or runtime directories tracked by git.

Ordinary git commit SHAs are not treated as secrets.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Notion UUIDs are 8-4-4-4-12 hex groups. Only flag well-formed UUIDs
# whose middle groups match the Notion layout (800e style is common but
# not universal), i.e. any 8-4-4-4-12 hex pattern is suspicious in source.
NOTION_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

# Windows user directory references (C:\Users\<name>, /c/Users/<name>).
# YOUR_USER is the documented placeholder and is allowed.
WINDOWS_USER_RE = re.compile(r"(?i)(?:[a-z]:[\\/])?[\\/]?users[\\/](?!your_user[\\/\s\"'])[^\\/\s\"']+")

# Local absolute paths under common workspaces
LOCAL_PATH_RE = re.compile(
    r"(?i)(?:E|D|C|F):[\\/]aiworkspace[\\/]|"
    r"(?:E|D|C|F):[\\/]ai-data[\\/]"
)

# Literal placeholder paths that must not be used at runtime
PLACEHOLDER_RE = re.compile(r"<[A-Z_]{3,}>")

# Common secret-like literals (bearer tokens, api keys, cookies).
# Bare words like "secret" in prose are not flagged; only assignment forms
# (secret=..., password: "...") and well-known token shapes match.
SECRET_LITERAL_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|passwd|auth[_-]?token)\s*[:=]\s*['\"][^'\"]{8,}['\"]|"
    r"bearer\s+[a-z0-9._-]{12,}|"
    r"sk-[a-z0-9]{20,}|"
    r"ghp_[a-zA-Z0-9]{20,}|"
    r"xox[baprs]-[a-zA-Z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"auth_token\s*[:=]\s*['\"][^'\"]{12,}['\"]|"
    r"ct0\s*[:=]\s*['\"][^'\"]{12,}['\"]"
)

# Files that must never be tracked by git in this repo
FORBIDDEN_TRACKED = (
    ".env",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "_runtime/",
    "_external/",
    "secrets/",
    "config.local.yaml",
    "config.private.yaml",
)

ALLOWED_UUID_CONTEXTS = (
    "test",
    "fixture",
    "example",
    "sample",
    "placeholder",
    "replace",
    "00000000-0000-0000-0000-000000000000",
)


def _looks_like_fixture(line: str) -> bool:
    low = line.lower()
    return any(token in low for token in ALLOWED_UUID_CONTEXTS)


# Files that legitimately reference real local paths and are exempt:
# - tests/test_hermes_operator_plugin.py: mocks the operator path in a fixture.
# - scripts/publish-public-system.ps1: the sanitizer's own replacement map
#   (<USER_HOME> -> <USER_HOME>) must keep the real source strings.
ALLOWED_PATH_REFERENCES = {
    "_automation/trading_research/tests/test_hermes_operator_plugin.py",
    "_automation/hermes-capture/config.yaml",
    "scripts/publish-public-system.ps1",
}


def check_file(path: Path) -> list[str]:
    """Return a list of problems found in a single file."""
    problems: list[str] = []
    if path.name in {".env", ".env.example"} and path.stat().st_size > 0:
        if ".example" not in path.name:
            problems.append(f"{path}: tracked .env file")
        return problems
    if any(path.name.endswith(suffix) for suffix in (".db", ".sqlite", ".sqlite3")):
        problems.append(f"{path}: tracked database file")
        return problems
    rel_posix = path.relative_to(ROOT).as_posix()
    if rel_posix in ALLOWED_PATH_REFERENCES:
        return problems
    if any(
        fnmatch.fnmatch(rel_posix, pat)
        or (pat.endswith("/") and rel_posix.startswith(pat))
        for pat in FORBIDDEN_TRACKED
    ):
        problems.append(f"{path}: forbidden tracked file")
        return problems
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return problems
    for lineno, line in enumerate(content.splitlines(), start=1):
        if NOTION_UUID_RE.search(line) and not _looks_like_fixture(line):
            problems.append(f"{path}:{lineno}: hardcoded Notion UUID")
        if SECRET_LITERAL_RE.search(line):
            problems.append(f"{path}:{lineno}: secret-like literal")
        # Local absolute paths and Windows user dirs are scrubbed at publish
        # time for documentation files (.md); code and config must be clean
        # in the working tree.
        if path.suffix.lower() not in {".md"}:
            if WINDOWS_USER_RE.search(line):
                problems.append(f"{path}:{lineno}: Windows user directory reference")
            if LOCAL_PATH_RE.search(line):
                problems.append(f"{path}:{lineno}: local absolute path reference")
            if PLACEHOLDER_RE.search(line) and ("config" in line.lower() or "path" in line.lower()):
                problems.append(f"{path}:{lineno}: literal placeholder path in config-like context")
    return problems


def _git_tracked_files() -> list[Path] | None:
    """All git-tracked files under ROOT, or None when git is unavailable.

    Uses `git ls-files` so the scan is limited to what would actually be
    published, and is fast even when the working tree contains huge ignored
    directories (node_modules, _runtime, .venv, ...).
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [
        ROOT / raw.decode("utf-8", errors="replace")
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def iter_source_files() -> list[Path]:
    """All tracked source/doc files, skipping vendor and binary dirs.

    Prefers `git ls-files` (fast, .gitignore-aware); falls back to a bounded
    rglob walk only when git is not available in the environment.
    """
    skip_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        "dist",
        "_runtime",
        "_external",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".mypy",
    }
    self_name = Path(__file__).resolve()
    files: list[Path] = []
    tracked = _git_tracked_files()
    if tracked is None:
        # Fallback: recursive walk (slow on huge trees; only used when git
        # is missing or this is not a git checkout).
        tracked = [p for p in ROOT.rglob("*") if p.is_file()]
    for path in tracked:
        if not path.is_file():
            continue
        if path.resolve() == self_name:
            continue  # the checker itself contains the pattern definitions
        rel = path.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pyc"}:
            continue
        files.append(path)
    return files


def main() -> int:
    problems: list[str] = []
    for path in iter_source_files():
        problems.extend(check_file(path))
    if problems:
        print(f"Public mirror hygiene check FAILED ({len(problems)} problem(s)):")
        for problem in problems[:100]:
            print(f"  - {problem}")
        if len(problems) > 100:
            print(f"  ... and {len(problems) - 100} more")
        return 1
    print("Public mirror hygiene check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
