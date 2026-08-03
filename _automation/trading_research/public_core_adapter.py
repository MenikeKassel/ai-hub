"""Thin private adapter for the versioned kol-audit-workbench package."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "_runtime" / "trading" / "public-core"


def main() -> int:
    try:
        from kol_audit.cli import run
    except ImportError as exc:
        raise SystemExit(
            "kol-audit-workbench is not installed. Install requirements-public-core.txt first."
        ) from exc

    data_dir = Path(os.environ.get("KAW_DATA_DIR", DEFAULT_DATA_DIR))
    return run(["--data-dir", str(data_dir), *(sys.argv[1:] or ["doctor"])])


if __name__ == "__main__":
    raise SystemExit(main())
