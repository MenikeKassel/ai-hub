from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kol_backup_upgrade import KolBackupUpgrade  # noqa: E402


EVENT_FIELDS = [
    "event_id", "kol_name", "platform", "source_url", "source_note", "posted_at",
    "symbol", "security_name", "direction", "thesis", "status", "exclusion_reason",
    "activated_at", "activation_notified_at", "execution_warning", "updated_at",
]


def write_event(path: Path, note: str, activated: str, warning: str, security_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "event_id": "KOL-0071",
        "kol_name": "Example",
        "platform": "X",
        "source_url": "https://x.com/example/status/2079733210825785420",
        "source_note": note,
        "posted_at": "2026-08-12T00:00:00+08:00",
        "symbol": "002036",
        "security_name": security_name,
        "direction": "long",
        "thesis": "traceable thesis",
        "status": "active",
        "exclusion_reason": "",
        "activated_at": activated,
        "activation_notified_at": "",
        "execution_warning": warning,
        "updated_at": "2026-08-12T00:00:00+08:00",
    }
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS)
        writer.writeheader()
        writer.writerow(row)


def make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE posts(
            post_id TEXT PRIMARY KEY, media_json TEXT NOT NULL DEFAULT '[]',
            local_media_json TEXT NOT NULL DEFAULT '[]', review_status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE classifications(
            post_id TEXT PRIMARY KEY, is_candidate INTEGER NOT NULL DEFAULT 0,
            model_name TEXT NOT NULL DEFAULT '', model_status TEXT NOT NULL DEFAULT 'not_requested',
            model_error TEXT NOT NULL DEFAULT '',
            ocr_status TEXT NOT NULL DEFAULT 'not_needed', ocr_text TEXT NOT NULL DEFAULT '',
            ocr_attempts INTEGER NOT NULL DEFAULT 0, ocr_error TEXT NOT NULL DEFAULT '',
            ocr_next_retry_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    con.execute(
        "INSERT INTO posts(post_id,media_json,local_media_json) VALUES(?,?,?)",
        ("2071758216380444828", "[]", "[]"),
    )
    con.execute(
        "INSERT INTO classifications(post_id,is_candidate,model_name,model_status,ocr_status) VALUES(?,?,?,?,?)",
        ("2071758216380444828", 1, "public-dataset-recovery", "completed", "not_needed"),
    )
    con.commit()
    con.close()


class KolBackupUpgradeTests(unittest.TestCase):
    def test_preview_apply_and_second_preview_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            source = root / "source"
            current_kol = current / "_runtime" / "trading" / "kol"
            source_kol = source / "_runtime" / "trading" / "kol"
            make_db(current_kol / "posts.db")
            write_event(current_kol / "events.csv", "public-dataset-recovery", "", "", "联创科技")
            (current_kol / "runs.jsonl").write_text('{"ts":"2026-08-01"}\n', encoding="utf-8")
            (source_kol / "runs.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (source_kol / "runs.jsonl").write_text('{"ts":"2026-07-01"}\n', encoding="utf-8")
            (source_kol / "checkpoint_revisions.jsonl").write_text('{"event_id":"KOL-0071"}\n', encoding="utf-8")
            (source_kol / "event_revisions.jsonl").write_text('{"event_id":"KOL-0071","reason":"backup"}\n', encoding="utf-8")
            write_event(source_kol / "events.csv", "post:2079733210825785420", "2026-08-12T21:00:00+08:00", "delayed_baseline", "联创电子")
            media = source_kol / "media" / "2071758216380444828"
            media.mkdir(parents=True)
            (media / "1.jpg").write_bytes(b"backup-image")
            (source_kol / "media" / "2099999999999999999").mkdir(parents=True)
            (source_kol / "media" / "2099999999999999999" / "1.jpg").write_bytes(b"orphan")

            upgrade = KolBackupUpgrade(current, source, backup_root=root / "backups")
            preview = upgrade.preview()
            self.assertEqual(1, preview["inventory"]["media_backup_only_files"])
            self.assertEqual(1, preview["inventory"]["media_orphan_files"])
            result = upgrade.apply(report_path=root / "report.json")
            self.assertTrue(result["ok"])
            self.assertEqual(1, result["copied_media_files"])
            self.assertEqual(1, len(result["orphan_media_paths"]))

            con = sqlite3.connect(current_kol / "posts.db")
            local = json.loads(con.execute("SELECT local_media_json FROM posts").fetchone()[0])
            state = con.execute("SELECT model_status,ocr_status FROM classifications").fetchone()
            con.close()
            self.assertEqual("quark-backup-20260830", local[0]["recovery_source"])
            self.assertEqual(("not_requested", "not_requested"), tuple(state))
            fields, rows = None, None
            with (current_kol / "events.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual("post:2079733210825785420", rows[0]["source_note"])
            self.assertEqual("联创电子", rows[0]["security_name"])
            self.assertEqual("delayed_baseline", rows[0]["execution_warning"])
            second = upgrade.preview()
            self.assertEqual(0, second["inventory"]["media_backup_only_files"])
            self.assertEqual(0, second["jsonl"]["runs.jsonl"]["added"])
            self.assertEqual(0, second["event_metadata"]["source_note"])


if __name__ == "__main__":
    unittest.main()
