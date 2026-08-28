"""Idempotently restore the recoverable public KOL snapshot into a private runtime.

The public snapshot is intentionally lossy.  This importer therefore treats the
existing private runtime as authoritative for rich fields (post text, media,
raw provider payloads, private identity and review notes) and only fills missing
records or public classification/market-history fields.  It is safe to run in
dry-run mode and safe to run repeatedly with ``apply=True``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RECOVERY_PROVIDER = "public-dataset-recovery"
RECOVERY_RUN_ID = "public-snapshot-recovery:2026-08-21"
RECOVERY_AS_OF = "2026-08-21"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _runtime_root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if (root / "kol" / "posts.db").is_file():
        return root
    if (root / "_runtime" / "trading" / "kol" / "posts.db").is_file():
        return root / "_runtime" / "trading"
    if (root / "trading" / "kol" / "posts.db").is_file():
        return root / "trading"
    return root


def _required_data_files(root: Path) -> list[Path]:
    return [
        root / "manifest.json",
        root / "catalog" / "kols.csv",
        root / "catalog" / "instruments.csv",
        root / "events" / "events.csv",
        root / "recommendations" / "drafts.csv",
        root / "returns" / "daily_marks-2026.csv",
        root / "returns" / "checkpoints.csv",
        root / "research" / "technical_context.csv",
        root / "research" / "method_research.jsonl",
        root / "performance" / "series-2026.csv",
    ]


def _verify_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "errors": ["manifest.json missing"], "warnings": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    for entry in manifest.get("files", []):
        path = root / str(entry.get("path", ""))
        if not path.is_file():
            errors.append(f"missing:{entry.get('path')}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(entry.get("sha256", "")):
            # The publisher's three markdown files have a known CRLF/LF
            # discrepancy.  Business data remains a hard failure.
            if path.suffix.lower() in {".md", ".txt"}:
                warnings.append(f"document_hash_mismatch:{entry.get('path')}")
            else:
                errors.append(f"hash_mismatch:{entry.get('path')}")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _upsert_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else row.get(field, "") for field in fields})


def _merge_csv_by_key(
    path: Path,
    public_rows: list[dict[str, str]],
    keys: tuple[str, ...],
    *,
    overwrite_public: bool = False,
) -> dict[str, int]:
    existing = _read_csv(path)
    fields: list[str] = []
    for row in existing + public_rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    index = {tuple(str(row.get(key, "")) for key in keys): row for row in existing}
    added = overwritten = 0
    for public in public_rows:
        key = tuple(str(public.get(k, "")) for k in keys)
        if key not in index:
            row = {field: public.get(field, "") for field in fields}
            index[key] = row
            added += 1
        elif overwrite_public:
            index[key].update({field: public.get(field, "") for field in fields if field in public})
            overwritten += 1
    ordered = sorted(index.values(), key=lambda row: tuple(str(row.get(k, "")) for k in keys))
    _upsert_csv(path, fields, ordered)
    return {"added": added, "overwritten": overwritten, "total": len(ordered)}


def _restore_posts(root: Path, runtime: Path, *, apply: bool, counts: dict[str, Any]) -> None:
    snapshot_posts: list[dict[str, Any]] = []
    for path in sorted((root / "sources").glob("posts-*.jsonl")):
        snapshot_posts.extend(_read_jsonl(path))
    snapshot_by_id = {str(row.get("post_id", "")): row for row in snapshot_posts if row.get("post_id")}
    posts_db = runtime / "kol" / "posts.db"
    con = sqlite3.connect(posts_db)
    con.row_factory = sqlite3.Row
    existing_post_ids = {str(row[0]) for row in con.execute("SELECT post_id FROM posts")}
    existing_kol_ids = {int(row[0]) for row in con.execute("SELECT id FROM kols")}
    new_kols = [row for row in _read_csv(root / "catalog" / "kols.csv") if int(row["kol_id"]) not in existing_kol_ids]
    new_posts = [row for key, row in snapshot_by_id.items() if key not in existing_post_ids]
    counts["posts_snapshot"] = len(snapshot_by_id)
    counts["posts_added"] = len(new_posts)
    counts["kols_added"] = len(new_kols)
    counts["post_classifications_updated"] = 0
    if not apply:
        con.close()
        return
    timestamp = _now()
    for row in new_kols:
        kol_id = int(row["kol_id"])
        con.execute(
            """INSERT OR IGNORE INTO kols(
                id,display_name,platform,handle,profile_url,domain,status,tracking_mode,
                created_at,updated_at,identity_status,availability_status,availability_checked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kol_id, row.get("display_name", ""), row.get("platform", "X"), row.get("handle", ""),
                row.get("profile_url", ""), "", row.get("status", "active"), row.get("tracking_mode", "all"),
                timestamp, timestamp, "verified", "active", timestamp,
            ),
        )
    for post in snapshot_by_id.values():
        post_id = str(post["post_id"])
        posted_at = str(post.get("posted_at", ""))
        if post_id not in existing_post_ids:
            raw = _json(post)
            content_hash = _hash({"post_id": post_id, "url": post.get("source_url", ""), "posted_at": posted_at})
            con.execute(
                """INSERT INTO posts(
                    post_id,kol_id,platform,handle,author_name,url,text,article_title,article_text,
                    quoted_id,quoted_text,quoted_author,posted_at,posted_at_utc,post_type,language,
                    media_json,local_media_json,metrics_json,raw_json,content_hash,fetched_at,
                    review_status,source_note,updated_at,canonical_provider,metrics_provider,provider_warning
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    post_id, int(post.get("kol_id") or 0), post.get("platform", ""), post.get("handle", ""),
                    post.get("author_name", ""), post.get("source_url", ""), "", "", "", "", "", "",
                    posted_at, posted_at, post.get("post_type", "post"), "", "[]", "[]", "{}", raw,
                    content_hash, str(post.get("posted_at") or timestamp), post.get("review_status", "pending"),
                    "public snapshot link-only recovery", timestamp, RECOVERY_PROVIDER, RECOVERY_PROVIDER,
                    "public snapshot has no private text/media/raw provider payload",
                ),
            )
            con.execute(
                "INSERT OR IGNORE INTO post_sources(post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json) VALUES(?,?,?,?,?,?,?)",
                (post_id, RECOVERY_PROVIDER, str(post.get("posted_at") or timestamp), content_hash, raw, "{}", _json(["link_only_recovery"])),
            )
            existing_post_ids.add(post_id)
        else:
            # Only public classification/review state is newer; never replace
            # private rich post fields or private review notes.
            current_post = con.execute("SELECT review_status FROM posts WHERE post_id=?", (post_id,)).fetchone()
            if current_post and str(current_post[0]) != str(post.get("review_status", "pending")):
                con.execute(
                    "UPDATE posts SET review_status=?,updated_at=? WHERE post_id=?",
                    (post.get("review_status", "pending"), timestamp, post_id),
                )
        classification = post
        present = con.execute(
            "SELECT content_type,evidence_type,is_candidate,model_summary FROM classifications WHERE post_id=?",
            (post_id,),
        ).fetchone()
        if present:
            desired = (
                classification.get("content_type", "other"), classification.get("evidence_type", "ambiguous"),
                int(classification.get("is_candidate") or 0),
            )
            current = (str(present[0]), str(present[1]), int(present[2] or 0))
            if current != desired or (not str(present[3] or "") and classification.get("model_summary", "")):
                con.execute(
                    """UPDATE classifications SET content_type=?,evidence_type=?,is_candidate=?,
                       model_summary=CASE WHEN model_summary='' THEN ? ELSE model_summary END,updated_at=?
                       WHERE post_id=?""",
                    (*desired, classification.get("model_summary", ""), timestamp, post_id),
                )
                counts["post_classifications_updated"] += 1
        else:
            con.execute(
                """INSERT INTO classifications(
                    post_id,content_type,evidence_type,is_candidate,model_status,model_name,
                    model_summary,updated_at,draft_generation_status,draft_generation_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    post_id, classification.get("content_type", "other"), classification.get("evidence_type", "ambiguous"),
                    int(classification.get("is_candidate") or 0), "completed", RECOVERY_PROVIDER,
                    classification.get("model_summary", ""), timestamp, "not_applicable", RECOVERY_PROVIDER,
                ),
            )
    con.commit()
    con.close()


def _restore_drafts(root: Path, runtime: Path, *, apply: bool, counts: dict[str, Any]) -> None:
    rows = _read_csv(root / "recommendations" / "drafts.csv")
    db = sqlite3.connect(runtime / "kol" / "posts.db")
    existing = {int(row[0]) for row in db.execute("SELECT id FROM recommendation_drafts")}
    new = [row for row in rows if int(row["draft_id"]) not in existing]
    counts["drafts_snapshot"] = len(rows)
    counts["drafts_added"] = len(new)
    if not apply:
        db.close()
        return
    timestamp = _now()
    for row in new:
        draft_id = int(row["draft_id"])
        signature = _hash({"post_id": row.get("post_id", ""), "symbol": row.get("symbol", ""), "thesis": row.get("thesis", ""), "source": RECOVERY_PROVIDER})
        db.execute(
            """INSERT OR IGNORE INTO recommendation_drafts(
                id,post_id,symbol,security_name,direction,thesis,evidence_type,evidence_spans_json,
                conditions_json,mention_kind,confidence,model_name,extraction_version,source_signature,
                status,queue_scope,review_date,event_id,reviewed_at,created_at,updated_at,evidence_source,
                depends_on_ocr,action,horizon,strength
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                draft_id, row.get("post_id", ""), row.get("symbol", ""), row.get("security_name", ""), row.get("direction", ""),
                row.get("thesis", ""), row.get("evidence_type", "ambiguous"), _json([row.get("evidence_excerpt", "")]),
                row.get("conditions", "[]") or "[]", row.get("mention_kind", "recommendation"), float(row.get("confidence") or 0),
                RECOVERY_PROVIDER, row.get("extraction_version", RECOVERY_PROVIDER), signature, row.get("status", "ready"),
                row.get("queue_scope", "morning"), str(row.get("reviewed_at", ""))[:10], row.get("event_id", ""),
                row.get("reviewed_at", ""), timestamp, timestamp, RECOVERY_PROVIDER, 0, row.get("action", "watch"),
                row.get("horizon", "unspecified"), "unspecified",
            ),
        )
    db.commit()
    db.close()


def _restore_events(root: Path, runtime: Path, *, apply: bool, counts: dict[str, Any]) -> None:
    public = _read_csv(root / "events" / "events.csv")
    path = runtime / "kol" / "events.csv"
    existing = _read_csv(path)
    fields: list[str] = []
    for row in existing + public:
        for field in row:
            if field not in fields:
                fields.append(field)
    by_id = {str(row.get("event_id", "")): row for row in existing}
    added = 0
    for row in public:
        event_id = str(row.get("event_id", ""))
        if event_id not in by_id:
            merged = {field: row.get(field, "") for field in fields}
            merged["source_note"] = RECOVERY_PROVIDER
            by_id[event_id] = merged
            added += 1
    counts["events_snapshot"] = len(public)
    counts["events_added"] = added
    if apply:
        _upsert_csv(path, fields, sorted(by_id.values(), key=lambda row: str(row.get("event_id", ""))))


def _restore_returns(root: Path, runtime: Path, *, apply: bool, counts: dict[str, Any]) -> None:
    marks = _read_csv(root / "returns" / "daily_marks-2026.csv")
    checkpoints = _read_csv(root / "returns" / "checkpoints.csv")
    mark_result = _merge_csv_by_key(runtime / "kol" / "daily_marks.csv", marks, ("event_id", "trade_date"), overwrite_public=True) if apply else {"total": len(set((r.get("event_id", ""), r.get("trade_date", "")) for r in _read_csv(runtime / "kol" / "daily_marks.csv") + marks))}
    checkpoint_result = _merge_csv_by_key(runtime / "kol" / "checkpoints.csv", checkpoints, ("event_id", "horizon", "target_days"), overwrite_public=True) if apply else {"total": len(set((r.get("event_id", ""), r.get("horizon", ""), r.get("target_days", "")) for r in _read_csv(runtime / "kol" / "checkpoints.csv") + checkpoints))}
    counts["daily_marks_snapshot"] = len(marks)
    counts["daily_marks_total"] = mark_result["total"]
    counts["checkpoints_snapshot"] = len(checkpoints)
    counts["checkpoints_total"] = checkpoint_result["total"]


def _restore_market(root: Path, runtime: Path, *, apply: bool, counts: dict[str, Any]) -> None:
    instruments = _read_csv(root / "catalog" / "instruments.csv")
    technical = _read_csv(root / "research" / "technical_context.csv")
    research = _read_jsonl(root / "research" / "method_research.jsonl")
    counts.update({"instruments_snapshot": len(instruments), "technical_snapshot": len(technical), "method_research_snapshot": len(research)})
    if not apply:
        return
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from market_data import Instrument, MarketStore  # type: ignore
    market_root = runtime / "market"
    store = MarketStore(market_root)
    grouped: dict[str, list[Instrument]] = defaultdict(list)
    for row in instruments:
        grouped[row.get("snapshot_date", RECOVERY_AS_OF)].append(
            Instrument(
                symbol=row.get("symbol", ""), name=row.get("name", ""), instrument_type=row.get("instrument_type", "stock"),
                exchange=row.get("exchange", ""), status=row.get("status", "active"), list_date=row.get("list_date", ""),
                lifecycle="tracking", source=RECOVERY_PROVIDER,
            )
        )
    with store.connect() as db:
        existing_catalog = {str(row[0]) for row in db.execute("SELECT instrument_key FROM instrument_catalog").fetchall()}
    instrument_changes = 0
    for snapshot_date, values in grouped.items():
        try:
            store.upsert_instrument_catalog(values, provider=RECOVERY_PROVIDER, snapshot_date=date.fromisoformat(snapshot_date))
        except ValueError:
            store.upsert_instrument_catalog(values, provider=RECOVERY_PROVIDER, snapshot_date=date.fromisoformat(RECOVERY_AS_OF))
    instrument_changes = sum(
        1 for row in instruments
        if f"{str(row.get('exchange', '')).upper()}:{row.get('symbol', '')}:{row.get('instrument_type', '')}" not in existing_catalog
    )
    # Private technical rows are already represented in the public export by
    # (event_id, computed_at).  Keep the private row and import only the
    # genuinely additional historical snapshots.
    with store.connect() as db:
        existing_context_keys = {
            (str(row[0]), str(row[1]))
            for row in db.execute("SELECT event_id,computed_at FROM event_technical_context").fetchall()
        }
    technical_changes = 0
    for row in technical:
        event_id = row.get("event_id", "")
        source_hash = row.get("source_hash", "")
        context_key = (event_id, row.get("computed_at", ""))
        if context_key in existing_context_keys:
            continue
        record = {
            # ``computed_at`` is part of the public row identity.  The same
            # event/source hash can legitimately have several historical
            # feature snapshots, and collapsing them would lose records.
            "snapshot_id": f"{RECOVERY_PROVIDER}:{event_id}:{_hash(row)}", "event_id": event_id,
            "feature_version": row.get("feature_version", "technical-context-v1"), "input_hash": _hash({"symbol": row.get("symbol", ""), "posted_at": row.get("posted_at", "")}),
            "symbol": row.get("symbol", ""), "posted_at": row.get("posted_at", ""), "expected_trade_date": row.get("as_of_trade_date", ""),
            "as_of_trade_date": row.get("as_of_trade_date", ""), "adjustment": row.get("adjustment", "qfq"),
            "rsi14": _number(row.get("rsi14")), "macd_dif": _number(row.get("macd_dif")), "macd_dea": _number(row.get("macd_dea")),
            "macd_hist": _number(row.get("macd_hist")), "macd_hist_pct": _number(row.get("macd_hist_pct")), "atr14": _number(row.get("atr14")),
            "atr14_pct": _number(row.get("atr14_pct")), "volume_ratio_5": _number(row.get("volume_ratio_5")), "return_20d": _number(row.get("return_20d")),
            "distance_60d_high": _number(row.get("distance_60d_high")), "history_bars": int(float(row.get("history_bars") or 0)),
            "status": row.get("status", "complete"), "warnings_json": row.get("warnings_json", "[]"), "source_hash": source_hash,
            "error": "", "computed_at": row.get("computed_at", _now()), "foundation_release_id": row.get("foundation_release_id", ""),
        }
        if store.save_event_technical_context(record):
            technical_changes += 1
            existing_context_keys.add(context_key)
    research_changes = 0
    for row in research:
        if store.save_event_method_research({
            "snapshot_id": row.get("snapshot_id") or f"{RECOVERY_PROVIDER}:{_hash(row)}", "event_id": row.get("event_id", ""),
            "method_version": row.get("method_version", "event-method-research-v1"), "input_hash": row.get("input_hash", _hash(row)),
            "symbol": row.get("symbol", ""), "posted_at": row.get("posted_at", ""), "as_of_trade_date": row.get("as_of_trade_date", ""),
            "status": row.get("status", "complete"), "payload": row.get("research", {}), "warnings": row.get("warnings", []),
            "computed_at": row.get("computed_at", _now()),
        }):
            research_changes += 1
    counts["instruments_added_or_updated"] = instrument_changes
    counts["technical_added"] = technical_changes
    counts["method_research_added"] = research_changes


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _restore_performance(root: Path, runtime: Path, *, apply: bool, counts: dict[str, Any]) -> None:
    rows = _read_csv(root / "performance" / "series-2026.csv")
    latest = _read_csv(root / "performance" / "latest.csv")
    counts["performance_series_snapshot"] = len(rows)
    counts["performance_latest_snapshot"] = len(latest)
    if not apply:
        return
    db = sqlite3.connect(runtime / "kol" / "performance.db")
    timestamp = _now()
    input_hash = _hash({"as_of": RECOVERY_AS_OF, "series_count": len(rows), "latest_count": len(latest)})
    db.execute(
        """INSERT OR IGNORE INTO performance_runs(run_id,as_of,mode,algorithm_version,input_hash,status,started_at,completed_at,error)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (RECOVERY_RUN_ID, RECOVERY_AS_OF, "historical_recovery", "public-dataset-recovery", input_hash, "completed", timestamp, timestamp, ""),
    )
    existing_hashes = {str(row[0]) for row in db.execute("SELECT input_hash FROM performance_snapshots WHERE run_id=?", (RECOVERY_RUN_ID,))}
    inserted = 0
    for index, row in enumerate(rows):
        # Keep duplicate-looking public rows: their position is stable in the
        # snapshot and represents a separate historical series record.
        row_hash = _hash({"index": index, "row": row})
        if row_hash in existing_hashes:
            continue
        db.execute(
            """INSERT INTO performance_snapshots(run_id,as_of,platform,kol_key,horizon,window_name,tier,rank,input_hash,payload_json,created_at,foundation_release_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (RECOVERY_RUN_ID, row.get("as_of", RECOVERY_AS_OF), row.get("platform", ""), row.get("kol_key", ""), row.get("horizon", ""),
             row.get("window_name", "all"), row.get("tier", ""), int(row["rank"]) if row.get("rank") else None, row_hash, _json(row),
             timestamp, ""),
        )
        existing_hashes.add(row_hash)
        inserted += 1
    db.commit()
    counts["performance_series_added"] = inserted
    db.close()


def restore_public_dataset(input_root: str | Path, target_runtime: str | Path, *, apply: bool = False) -> dict[str, Any]:
    root = Path(input_root).resolve()
    runtime = _runtime_root(target_runtime)
    validation = _verify_manifest(root)
    result: dict[str, Any] = {
        "ok": bool(validation["ok"]), "apply": bool(apply), "input": str(root), "runtime": str(runtime),
        "as_of": RECOVERY_AS_OF, "provider": RECOVERY_PROVIDER, "validation": validation, "counts": {},
    }
    if not validation["ok"]:
        return result
    for path in _required_data_files(root):
        if not path.is_file():
            result["ok"] = False
            result.setdefault("errors", []).append(f"required_file_missing:{path.relative_to(root)}")
    if not result["ok"]:
        return result
    runtime.mkdir(parents=True, exist_ok=True)
    _restore_posts(root, runtime, apply=apply, counts=result["counts"])
    _restore_drafts(root, runtime, apply=apply, counts=result["counts"])
    _restore_events(root, runtime, apply=apply, counts=result["counts"])
    _restore_returns(root, runtime, apply=apply, counts=result["counts"])
    _restore_market(root, runtime, apply=apply, counts=result["counts"])
    _restore_performance(root, runtime, apply=apply, counts=result["counts"])
    result["counts"]["kols_total"] = len(_read_csv(root / "catalog" / "kols.csv"))
    result["counts"]["events_total"] = len(_read_csv(runtime / "kol" / "events.csv")) if apply else result["counts"].get("events_snapshot", 0)
    if apply:
        marker = runtime / "market" / "recovery-mode.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "mode": "historical", "as_of": RECOVERY_AS_OF, "write_enabled": False,
            "reason": "restored public snapshot; market source files are not available for forward sync",
            "provider": RECOVERY_PROVIDER, "updated_at": _now(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["historical_mode"] = str(marker)
    return result
