from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "scripts" / "hermes_ai_hub_source_guard.py"


class HermesSourceGuardTests(unittest.TestCase):
    """The ai-hub source guard was removed on 2026-08-06.

    The user granted Hermes direct write access to ai-hub, so the guard script,
    its agent hook, and its installer references must not come back.
    """

    def test_guard_script_is_removed(self) -> None:
        self.assertFalse(GUARD.exists(), "guard script must stay removed")

    def test_guard_installer_references_are_removed(self) -> None:
        installer = ROOT / "scripts" / "install-hermes-kol-research.ps1"
        if installer.exists():
            content = installer.read_text(encoding="utf-8")
            self.assertNotIn("ai-hub-source-guard", content)
            self.assertNotIn("source_guard", content)

    def test_guard_hook_config_is_removed(self) -> None:
        config = Path.home() / "AppData" / "Local" / "hermes" / "config.yaml"
        if config.exists():
            content = config.read_text(encoding="utf-8")
            self.assertNotIn("ai-hub-source-guard", content)


if __name__ == "__main__":
    unittest.main()
