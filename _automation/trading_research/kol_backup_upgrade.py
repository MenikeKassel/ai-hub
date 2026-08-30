from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


_POST_ID = re.compile(r"^\d{10,25}$")
_MEDIA_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if not path.is_file():
        return values
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
    return values


def _merge_jsonl(current: Path, source: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    current_rows = _read_jsonl(current)
    source_rows = _read_jsonl(source)
    merged: dict[str, dict[str, Any]] = {}
    for row in [*source_rows, *current_rows]:
        merged[_json_hash(row)] = row

    def sort_key(row: dict[str, Any]) -> tuple[str, str]:
        value = str(row.get("created_at") or row.get("ts") or row.get("completed_at") or "")
        return value, _json_hash(row)

    result = sorted(merged.values(), key=sort_key)
    return result, {
        "current": len(current_rows),
        "source": len(source_rows),
        "merged": len(result),
        "added": len(result) - len({_json_hash(row) for row in current_rows}),
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".upgrade.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _read_events(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not path.is_file():
        return [], {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or [])
        return fields, {str(row.get("event_id") or ""): dict(row) for row in reader if row.get("event_id")}


def _write_events(path: Path, fields: list[str], rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".upgrade.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))
    os.replace(temporary, path)


def _merge_event_metadata(current_path: Path, source_path: Path) -> tuple[list[str], dict[str, dict[str, str]], dict[str, int]]:
    current_fields, current = _read_events(current_path)
    source_fields, source = _read_events(source_path)
    fields = current_fields or source_fields
    for field in source_fields:
        if field not in fields:
            fields.append(field)
    changed = {"source_note": 0, "activation": 0, "execution_warning": 0, "security_name": 0}
    for event_id, backup in source.items():
        row = current.get(event_id)
        if row is None:
            current[event_id] = backup
            continue
        source_note = str(row.get("source_note") or "")
        backup_note = str(backup.get("source_note") or "")
        source_url = str(row.get("source_url") or "")
        if source_note == "public-dataset-recovery" and backup_note.startswith("post:") and backup_note[5:] in source_url:
            row["source_note"] = backup_note
            changed["source_note"] += 1
        for field in ("activated_at", "activation_notified_at"):
            if not str(row.get(field) or "").strip() and str(backup.get(field) or "").strip():
                row[field] = backup[field]
                changed["activation"] += 1
        current_warnings = {item for item in str(row.get("execution_warning") or "").split(";") if item}
        backup_warnings = {item for item in str(backup.get("execution_warning") or "").split(";") if item}
        union = ";".join(sorted(current_warnings | backup_warnings))
        if union != str(row.get("execution_warning") or ""):
            row["execution_warning"] = union
            changed["execution_warning"] += 1
        if str(row.get("symbol") or "") == "002036" and str(backup.get("security_name") or "") == "联创电子" and str(row.get("security_name") or "") != "联创电子":
            row["security_name"] = "联创电子"
            changed["security_name"] += 1
    return fields, current, {**changed, "events": len(current)}


def _media_files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not root.is_dir():
        return result
    for directory, _, files in os.walk(root):
        for filename in files:
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            parts = relative.split("/", 1)
            if len(parts) == 2 and _POST_ID.fullmatch(parts[0]) and _MEDIA_NAME.fullmatch(parts[1]):
                result[relative] = path
    return result


def _source_manifest(source: Path, selected: list[Path]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(selected):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        files.append({"path": relative, "bytes": path.stat().st_size, "sha256": _hash_file(path)})
    root_hash = hashlib.sha256(
        "\n".join(f"{item['path']}\t{item['bytes']}\t{item['sha256']}" for item in files).encode("utf-8")
    ).hexdigest()
    return {"file_count": len(files), "bytes": sum(int(item["bytes"]) for item in files), "root_sha256": root_hash, "files": files}


class KolBackupUpgrade:
    """Merge selected, auditable backup material without replacing live data."""

    def __init__(self, current_root: Path, source_root: Path, *, backup_root: Path | None = None):
        self.current_root = Path(current_root).resolve()
        self.source_root = Path(source_root).resolve()
        self.current_kol = self.current_root / "_runtime" / "trading" / "kol"
        self.source_kol = self.source_root / "_runtime" / "trading" / "kol"
        self.current_media = self.current_kol / "media"
        self.source_media = self.source_kol / "media"
        self.backup_root = Path(backup_root or self.current_root.parent / "_kol-repair-backups")

    def _inventory(self) -> dict[str, Any]:
        selected = [
            self.source_kol / name
            for name in ("runs.jsonl", "checkpoint_revisions.jsonl", "event_revisions.jsonl", "events.csv")
        ]
        selected.extend(self.source_media_files().values())
        manifest = _source_manifest(self.source_root, selected)
        current_media = _media_files(self.current_media)
        source_media = _media_files(self.source_media)
        backup_only = sorted(set(source_media) - set(current_media))
        current_db = sqlite3.connect(self.current_kol / "posts.db")
        current_db.row_factory = sqlite3.Row
        present = 0
        candidates = 0
        public_recovery = 0
        known_post_ids: set[str] = set()
        for post_id in {item.split("/", 1)[0] for item in backup_only}:
            row = current_db.execute(
                "SELECT c.is_candidate,c.model_name FROM posts p LEFT JOIN classifications c ON c.post_id=p.post_id WHERE p.post_id=?",
                (post_id,),
            ).fetchone()
            if row:
                known_post_ids.add(post_id)
                present += 1
                candidates += int(row["is_candidate"] or 0)
                public_recovery += int(str(row["model_name"] or "") == "public-dataset-recovery")
        current_db.close()
        return {
            "source_manifest": manifest,
            "media_source_files": len(source_media),
            "media_current_files": len(current_media),
            "media_backup_only_files": sum(item.split("/", 1)[0] in known_post_ids for item in backup_only),
            "media_orphan_files": sum(item.split("/", 1)[0] not in known_post_ids for item in backup_only),
            "media_backup_only_post_ids": len({item.split("/", 1)[0] for item in backup_only}),
            "media_current_post_ids": present,
            "media_candidate_posts": candidates,
            "public_recovery_candidates": public_recovery,
            "media_orphan_post_ids": len({item.split("/", 1)[0] for item in backup_only}) - present,
            "media_backup_only_paths": backup_only,
            "media_orphan_paths": [item for item in backup_only if item.split("/", 1)[0] not in known_post_ids],
            "media_applicable_paths": [item for item in backup_only if item.split("/", 1)[0] in known_post_ids],
        }

    def source_media_files(self) -> dict[str, Path]:
        return _media_files(self.source_media)

    def preview(self) -> dict[str, Any]:
        inventory = self._inventory()
        event_fields, event_rows, event_changes = _merge_event_metadata(
            self.current_kol / "events.csv", self.source_kol / "events.csv"
        )
        jsonl: dict[str, Any] = {}
        for name in ("runs.jsonl", "checkpoint_revisions.jsonl", "event_revisions.jsonl"):
            _, stats = _merge_jsonl(self.current_kol / name, self.source_kol / name)
            jsonl[name] = stats
        return {
            "ok": True,
            "dry_run": True,
            "source_root": str(self.source_root),
            "current_root": str(self.current_root),
            "inventory": {key: value for key, value in inventory.items() if key != "media_backup_only_paths"},
            "event_metadata": event_changes,
            "event_count_after": len(event_rows),
            "jsonl": jsonl,
            "skipped": {
                "old_source_code": True,
                "virtual_environments": True,
                "market_raw_warehouse": True,
                "private_config": True,
            },
        }

    def apply(self, *, report_path: Path) -> dict[str, Any]:
        preview = self.preview()
        inventory = self._inventory()
        source_manifest_before = inventory["source_manifest"]["root_sha256"]
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = self.backup_root / f"backup-upgrade-{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        db_path = self.current_kol / "posts.db"
        # SQLite's backup API produces a consistent copy even while the API is
        # serving read requests.  Event files are copied before atomic merge.
        source_db = sqlite3.connect(db_path)
        backup_db = sqlite3.connect(backup_dir / "posts.db")
        source_db.backup(backup_db)
        backup_db.close()
        source_db.close()
        for name in ("events.csv", "runs.jsonl", "checkpoint_revisions.jsonl", "event_revisions.jsonl"):
            path = self.current_kol / name
            if path.is_file():
                shutil.copy2(path, backup_dir / name)

        source_media = self.source_media_files()
        current_media = _media_files(self.current_media)
        backup_only_all = sorted(set(source_media) - set(current_media))
        id_db = sqlite3.connect(self.current_kol / "posts.db")
        current_db_ids = {str(row[0]) for row in id_db.execute("SELECT post_id FROM posts")}
        id_db.close()
        orphan_media_paths = [item for item in backup_only_all if item.split("/", 1)[0] not in current_db_ids]
        backup_only = [item for item in backup_only_all if item.split("/", 1)[0] in current_db_ids]
        candidate = Path(tempfile.mkdtemp(prefix="kol-media-upgrade-", dir=str(self.current_media.parent)))
        copied: list[str] = []
        try:
            for relative in backup_only:
                source = source_media[relative]
                destination = candidate / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if destination.stat().st_size != source.stat().st_size or _hash_file(destination) != _hash_file(source):
                    raise RuntimeError(f"media hash verification failed: {relative}")
                copied.append(relative)

            # Recheck all selected source inputs immediately before publishing.
            if self._inventory()["source_manifest"]["root_sha256"] != source_manifest_before:
                raise RuntimeError("backup source changed during upgrade")

            current_db = sqlite3.connect(db_path, timeout=60)
            current_db.row_factory = sqlite3.Row
            current_db.execute("BEGIN IMMEDIATE")
            for relative in copied:
                post_id, filename = relative.split("/", 1)
                row = current_db.execute(
                    "SELECT p.media_json,p.local_media_json,c.model_name,c.model_status,c.ocr_status,c.ocr_text,c.is_candidate FROM posts p LEFT JOIN classifications c ON c.post_id=p.post_id WHERE p.post_id=?",
                    (post_id,),
                ).fetchone()
                if not row:
                    continue
                path = self.current_media / relative
                destination = path
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(candidate / relative, destination)
                try:
                    local = json.loads(row["local_media_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    local = []
                if not isinstance(local, list):
                    local = []
                if any(isinstance(item, dict) and str(item.get("path") or "") == str(destination) for item in local):
                    continue
                try:
                    remote = json.loads(row["media_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    remote = []
                source_url = ""
                match = re.search(r"^(\d+)", filename)
                index = int(match.group(1)) - 1 if match else -1
                if isinstance(remote, list) and 0 <= index < len(remote) and isinstance(remote[index], dict):
                    source_url = str(remote[index].get("url") or "")
                data = destination.read_bytes()
                local.append({
                    "bytes": len(data),
                    "path": str(destination),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "source_url": source_url,
                    "type": "image",
                    "recovery_source": "quark-backup-20260830",
                })
                current_db.execute(
                    "UPDATE posts SET local_media_json=? WHERE post_id=?",
                    (json.dumps(local, ensure_ascii=False), post_id),
                )
                if int(row["is_candidate"] or 0) and str(row["model_name"] or "") == "public-dataset-recovery":
                    current_db.execute(
                        "UPDATE classifications SET ocr_status='not_requested',ocr_attempts=0,ocr_error='',ocr_next_retry_at='',model_status='not_requested',model_error='',updated_at=datetime('now','localtime') WHERE post_id=?",
                        (post_id,),
                    )
                elif int(row["is_candidate"] or 0) and not str(row["ocr_text"] or "").strip():
                    current_db.execute(
                        "UPDATE classifications SET ocr_status='not_requested',ocr_attempts=0,ocr_error='',ocr_next_retry_at='' WHERE post_id=?",
                        (post_id,),
                    )
            current_db.commit()
            current_db.close()

            fields, events, event_changes = _merge_event_metadata(
                self.current_kol / "events.csv", self.source_kol / "events.csv"
            )
            _write_events(self.current_kol / "events.csv", fields, events)
            jsonl_stats: dict[str, Any] = {}
            for name in ("runs.jsonl", "checkpoint_revisions.jsonl", "event_revisions.jsonl"):
                merged, stats = _merge_jsonl(self.current_kol / name, self.source_kol / name)
                _write_jsonl(self.current_kol / name, merged)
                jsonl_stats[name] = stats
            payload = {
                "ok": True,
                "dry_run": False,
                "backup_dir": str(backup_dir),
                "copied_media_files": len(copied),
                "orphan_media_paths": orphan_media_paths,
                "event_metadata": event_changes,
                "jsonl": jsonl_stats,
                "source_manifest_sha256": source_manifest_before,
            }
        except Exception:
            current_db = locals().get("current_db")
            if current_db is not None:
                try:
                    current_db.rollback()
                    current_db.close()
                except Exception:
                    pass
            raise
        finally:
            shutil.rmtree(candidate, ignore_errors=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
