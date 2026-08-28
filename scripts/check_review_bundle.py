"""Validate the source set intended for local Git/GLM review.

This check is intentionally narrower than the public-mirror check: local D-drive
paths are allowed in private operational source, while runtime data and secrets
are never allowed in Git.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PARTS = {
    "_runtime",
    "_external",
    "node_modules",
    "dist",
    "test-results",
    "playwright-report",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
FORBIDDEN_NAMES = {
    ".env",
    "config.yaml",  # private Hermes capture configuration
    "credentials.json",
    "cookies.json",
    "auth.json",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".duckdb",
    ".sqlite",
    ".sqlite3",
    ".parquet",
    ".log",
    ".pid",
    ".jsonl",
    ".pem",
    ".p12",
    ".pfx",
}

SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|ghp)_[a-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bxox[baprs]-[a-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:auth_token|ct0|api[_-]?key|password|secret)\s*[:=]\s*"
        r"['\"][a-z0-9._-]{16,}['\"]"
    ),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._-]{16,}\b"),
)

ALLOWED_SECRET_PATTERN_FILES = {
    "scripts/check_public_mirror.py",
    "scripts/check_review_bundle.py",
    "scripts/publish-public-system.ps1",
}


def candidate_files() -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("git ls-files failed; initialize the repository first")
    return [
        ROOT / value.decode("utf-8", errors="replace")
        for value in completed.stdout.split(b"\0")
        if value
    ]


def main() -> int:
    problems: list[str] = []
    files = candidate_files()
    for path in files:
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        parts = set(path.relative_to(ROOT).parts)
        suffix = path.suffix.casefold()
        if parts & FORBIDDEN_PARTS:
            problems.append(f"{relative}: forbidden generated/runtime directory")
            continue
        if path.name.casefold() in FORBIDDEN_NAMES:
            problems.append(f"{relative}: forbidden private file")
            continue
        if suffix in FORBIDDEN_SUFFIXES:
            problems.append(f"{relative}: forbidden runtime/binary suffix")
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            problems.append(f"{relative}: source file exceeds 2 MiB")
            continue
        if relative in ALLOWED_SECRET_PATTERN_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{relative}: cannot read ({exc})")
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{relative}: possible literal credential/token")
                break

    diff_check = subprocess.run(
        ["git", "diff", "--check", "--cached"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if diff_check.returncode:
        problems.append("git diff --check --cached failed: " + diff_check.stdout.strip())

    if problems:
        print(f"Review bundle check FAILED ({len(problems)} issue(s))")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Review bundle check OK ({len(files)} source files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
