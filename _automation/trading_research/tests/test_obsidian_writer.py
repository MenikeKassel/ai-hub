from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hermes-capture"))

from obsidian_writer import path_is_in_output_dir, write_inbox_note  # noqa: E402


class ObsidianWriterBoundaryTests(unittest.TestCase):
    def test_existing_inbox_note_does_not_satisfy_kol_auto_output(self) -> None:
        self.assertFalse(path_is_in_output_dir("00_Inbox/source.md", "01_Sources/KOL_Auto"))
        self.assertTrue(path_is_in_output_dir("01_Sources/KOL_Auto/source.md", "01_Sources/KOL_Auto"))

    def test_prefetched_output_directory_stays_inside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            relative = write_inbox_note(
                {
                    "title": "KOL source",
                    "url": "https://x.com/example/status/12345",
                    "content": "source snapshot",
                    "obsidian_output_dir": "01_Sources/KOL_Auto",
                },
                "https://notion.so/example",
                {"vault_path": str(vault)},
            )

            self.assertTrue(relative.startswith("01_Sources/KOL_Auto/"))
            self.assertTrue((vault / relative).exists())

    def test_prefetched_output_directory_cannot_escape_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                write_inbox_note(
                    {
                        "title": "unsafe",
                        "content": "source snapshot",
                        "obsidian_output_dir": "../../outside",
                    },
                    "",
                    {"vault_path": str(Path(tmp) / "vault")},
                )


if __name__ == "__main__":
    unittest.main()
