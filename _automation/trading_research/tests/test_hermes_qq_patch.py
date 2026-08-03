from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class HermesQqPatchTests(unittest.TestCase):
    def test_qq_4009_patch_is_idempotent_and_runs_before_gateway_restart(self) -> None:
        patch = (ROOT / "scripts" / "patch-hermes-qqbot-4009.ps1").read_text(
            encoding="utf-8"
        )
        installer = (
            ROOT / "scripts" / "install-hermes-kol-research.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "close_logger = logger.info if code == 4009 else logger.warning",
            patch,
        )
        self.assertIn("reconnect_elapsed > 30", patch)
        self.assertIn("QQ 4009 reconnect attempt failed after", patch)
        self.assertLess(
            installer.index("patch-hermes-qqbot-4009.ps1"),
            installer.rindex("hermes gateway restart"),
        )
