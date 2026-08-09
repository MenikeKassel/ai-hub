"""Thin private adapter for the versioned kol-audit-workbench package."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "_runtime" / "trading" / "public-core"


def main() -> int:
    if sys.argv[1:2] == ["serve-private"]:
        try:
            import uvicorn
            from kol_discovery_runtime import create_private_app
        except ImportError as exc:
            raise SystemExit(
                "private KOL runtime dependencies are not installed; install "
                "requirements-public-core.txt and requirements.txt first"
            ) from exc
        host = os.environ.get("KAW_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("private KOL runtime must bind to loopback")
        uvicorn.run(
            create_private_app(),
            host=host,
            port=int(os.environ.get("KAW_PORT", "8125")),
        )
        return 0
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
