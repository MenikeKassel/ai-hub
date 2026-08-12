"""Deterministic, privacy-preserving export for the public KOL audit dataset.

The exporter deliberately reads the private runtime as a source of facts only. It
never writes to SQLite/DuckDB and never copies provider payloads, media or full
post text into the public tree.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from filelock import FileLock


EXPORT_VERSION = "public-kol-dataset-v1"
SCHEMA_VERSION = "2026-08-03"

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_LOCAL_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/][^\s,;，。；]+|\\\\[^\s,;，。；]+)")
_SECRET_RE = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{12,}|(?:bearer|token|secret|api[_ -]?key|授权码)\s*[:=：]\s*[^\s,;，。；]+)"
)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [^-]+ KEY-----.*?-----END [^-]+ KEY-----", re.S)
_CONTACT_RE = re.compile(
    r"(?i)(?:qq|微信|wechat|discord|telegram|电报|tg|飞书|联系方式)\s*[:：]?\s*[\w@._+\-]{2,}"
)
_FORBIDDEN_EXPORT_RE = re.compile(
    r"(?i)(?:auth_token|\bct0\b|appsecret|api[_ -]?key\s*[:=]|bearer\s+[a-z0-9._-]{16,}|"
    r"raw_json|local_media_json|notion_url|cookie|password|private_key|C:\\Users\\|"
    r"<AI_HUB_HOME>|<OBSIDIAN_VAULT>|<MARKET_DATA_HOME>|<PURCHASED_DATA_HOME>)"
)
_DROP_KEYS = {
    "text", "article_text", "quoted_text", "raw", "raw_json", "media", "media_json",
    "local_media_json", "ocr", "ocr_text", "prompt", "prompt_version", "error",
    "source_note", "notion_url", "local_path", "file_path", "broker", "transaction",
    "transactions", "cookie", "auth_token", "ct0", "secret", "token", "password",
}


def canonical_url(value: Any) -> str:
    """Keep a public source URL while dropping tracking/query credentials."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return sanitize_text(raw)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
    except ValueError:
        return sanitize_text(raw)


def sanitize_text(value: Any, *, limit: int | None = None) -> str:
    """Redact common contact/credential/path forms without guessing at meaning."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _PRIVATE_KEY_RE.sub("[REDACTED]", text)
    text = _PHONE_RE.sub("[CONTACT]", text)
    text = _EMAIL_RE.sub("[CONTACT]", text)
    text = _LOCAL_PATH_RE.sub("[LOCAL_PATH]", text)
    text = _SECRET_RE.sub("[REDACTED]", text)
    text = _CONTACT_RE.sub("[CONTACT]", text)

    def _url(match: re.Match[str]) -> str:
        url = canonical_url(match.group(0).rstrip(".,，。；;"))
        return "[LINK]" if url else "[LINK]"

    text = _URL_RE.sub(_url, text)
    return text[:limit] if limit is not None else text


def short_excerpt(value: Any, limit: int = 240) -> str:
    return sanitize_text(value, limit=limit)


def sanitize_public_identity(value: Any, limit: int = 160) -> str:
    """Sanitize a public display name/handle without treating ``QQ`` as a secret."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _PRIVATE_KEY_RE.sub("[REDACTED]", text)
    text = _PHONE_RE.sub("[CONTACT]", text)
    text = _EMAIL_RE.sub("[CONTACT]", text)
    text = _LOCAL_PATH_RE.sub("[LOCAL_PATH]", text)
    text = _SECRET_RE.sub("[REDACTED]", text)
    return text[:limit]


def _safe_value(value: Any, depth: int = 0) -> Any:
    """Copy derived research JSON while removing raw/private fields."""

    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(k): _safe_value(v, depth + 1)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k).lower() not in _DROP_KEYS
        }
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return sanitize_text(value, limit=2000)
    return value


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> int:
    materialized = [dict(row) for row in rows]
    materialized.sort(key=lambda row: tuple(str(row.get(field, "")) for field in fields))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in materialized:
            writer.writerow({field: "" if row.get(field) is None else row.get(field) for field in fields})
    return len(materialized)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]], key: str = "") -> int:
    materialized = [row for row in rows]
    materialized.sort(key=lambda row: str(row.get(key, "")) if key else json.dumps(row, ensure_ascii=False, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in materialized:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return len(materialized)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _year(value: Any) -> str:
    match = re.match(r"(\d{4})", str(value or ""))
    return match.group(1) if match else "unknown"


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


class PublicDatasetExporter:
    """Build a stable public snapshot from the private runtime."""

    def __init__(self, runtime_root: str | Path | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "_runtime" / "trading"
        self.runtime = Path(runtime_root or os.environ.get("TRADING_RUNTIME_ROOT") or default)
        self.kol_root = self.runtime / "kol"
        self.market_root = self.runtime / "market"
        self.posts_db = self.kol_root / "posts.db"
        self.performance_db = self.kol_root / "performance.db"
        self.market_db = self.market_root / "market.duckdb"

    def doctor(self) -> dict[str, Any]:
        paths = {
            "posts_db": self.posts_db,
            "events": self.kol_root / "events.csv",
            "daily_marks": self.kol_root / "daily_marks.csv",
            "checkpoints": self.kol_root / "checkpoints.csv",
            "performance_db": self.performance_db,
            "market_db": self.market_db,
        }
        missing = [name for name, path in paths.items() if not path.exists()]
        sizes = {name: path.stat().st_size for name, path in paths.items() if path.exists()}
        return {"ok": not missing, "export_version": EXPORT_VERSION, "missing": missing, "sizes": sizes}

    def export(self, output: str | Path) -> dict[str, Any]:
        destination = Path(output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime / "public-dataset.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=120):
            staging = Path(tempfile.mkdtemp(prefix=f"{destination.name}.staging-", dir=destination.parent))
            previous = destination.with_name(f".{destination.name}.previous")
            moved_previous = False
            try:
                counts = self._export_to(staging)
                validation = validate_public_dataset(staging)
                if not validation["ok"]:
                    raise RuntimeError("Public dataset staging validation failed: " + "; ".join(validation["errors"]))
                if previous.exists():
                    shutil.rmtree(previous)
                if destination.exists():
                    os.replace(destination, previous)
                    moved_previous = True
                try:
                    os.replace(staging, destination)
                except Exception:
                    if moved_previous and previous.exists() and not destination.exists():
                        os.replace(previous, destination)
                    raise
                if previous.exists():
                    shutil.rmtree(previous)
                return {"ok": True, "output": str(destination), "counts": counts, "manifest": str(destination / "manifest.json")}
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging)
                if moved_previous and previous.exists() and not destination.exists():
                    os.replace(previous, destination)
                raise

    def _export_to(self, root: Path) -> dict[str, int]:
        root.mkdir(parents=True, exist_ok=True)
        posts, classifications, drafts, kols = self._load_posts()
        event_rows = _read_csv(self.kol_root / "events.csv")
        mark_rows = _read_csv(self.kol_root / "daily_marks.csv")
        checkpoint_rows = _read_csv(self.kol_root / "checkpoints.csv")

        post_by_id = {row["post_id"]: row for row in posts}
        class_by_id = {row["post_id"]: row for row in classifications}
        drafts_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for draft in drafts:
            drafts_by_post[str(draft.get("post_id", ""))].append(draft)

        counts: dict[str, int] = {}
        public_kols = [
            {
                "kol_id": row["id"],
                "display_name": sanitize_public_identity(row.get("display_name")),
                "platform": sanitize_text(row.get("platform"), limit=40),
                "handle": sanitize_public_identity(row.get("handle")),
                "profile_url": canonical_url(row.get("profile_url")),
                "status": sanitize_text(row.get("status"), limit=40),
                "tracking_mode": sanitize_text(row.get("tracking_mode"), limit=80),
            }
            for row in kols
        ]
        counts["kols"] = _write_csv(
            root / "catalog" / "kols.csv",
            ["kol_id", "display_name", "platform", "handle", "profile_url", "status", "tracking_mode"],
            public_kols,
        )
        counts.update(self._export_instruments(root))

        source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for post in posts:
            classification = class_by_id.get(post["post_id"], {})
            post_drafts = drafts_by_post.get(post["post_id"], [])
            evidence = next((short_excerpt(item.get("evidence_excerpt")) for item in post_drafts if item.get("evidence_excerpt")), "")
            summary = short_excerpt(classification.get("model_summary"), 320)
            row = {
                "post_id": post["post_id"],
                "kol_id": post.get("kol_id", ""),
                "platform": sanitize_text(post.get("platform"), limit=40),
                "handle": sanitize_public_identity(post.get("handle")),
                "author_name": sanitize_public_identity(post.get("author_name")),
                "source_url": canonical_url(post.get("url")),
                "posted_at": post.get("posted_at", ""),
                "post_type": sanitize_text(post.get("post_type"), limit=60),
                "content_type": sanitize_text(classification.get("content_type"), limit=60),
                "evidence_type": sanitize_text(classification.get("evidence_type"), limit=80),
                "is_candidate": classification.get("is_candidate", ""),
                "review_status": sanitize_text(post.get("review_status"), limit=40),
                "model_summary": summary,
                "evidence_excerpt": evidence,
                "draft_count": len(post_drafts),
                "public_content_status": "summary_available" if summary or evidence else "link_only",
            }
            source_rows[_year(post.get("posted_at"))].append(row)
        for year, rows in sorted(source_rows.items()):
            counts[f"sources_{year}"] = _write_jsonl(
                root / "sources" / f"posts-{year}.jsonl", rows, key="post_id"
            )

        public_drafts = []
        for draft in drafts:
            post = post_by_id.get(str(draft.get("post_id", "")), {})
            public_drafts.append({
                "draft_id": draft.get("id", ""),
                "post_id": draft.get("post_id", ""),
                "platform": post.get("platform", ""),
                "kol_id": post.get("kol_id", ""),
                "kol_handle": sanitize_public_identity(post.get("handle")),
                "kol_name": sanitize_public_identity(post.get("author_name")),
                "source_url": canonical_url(post.get("url")),
                "posted_at": post.get("posted_at", ""),
                "symbol": sanitize_text(draft.get("symbol"), limit=20),
                "security_name": sanitize_text(draft.get("security_name"), limit=80),
                "direction": sanitize_text(draft.get("direction"), limit=20),
                "action": sanitize_text(draft.get("action"), limit=30),
                "horizon": sanitize_text(draft.get("horizon"), limit=40),
                "thesis": short_excerpt(draft.get("thesis"), 600),
                "evidence_excerpt": short_excerpt(_json(draft.get("evidence_spans_json"), [""])[0] if _json(draft.get("evidence_spans_json"), [""]) else ""),
                "conditions": short_excerpt(json.dumps(_json(draft.get("conditions_json"), []), ensure_ascii=False), 400),
                "evidence_type": sanitize_text(draft.get("evidence_type"), limit=80),
                "mention_kind": sanitize_text(draft.get("mention_kind"), limit=50),
                "confidence": draft.get("confidence", ""),
                "status": sanitize_text(draft.get("status"), limit=40),
                "queue_scope": sanitize_text(draft.get("queue_scope"), limit=40),
                "reviewed_at": draft.get("reviewed_at", ""),
                "event_id": draft.get("event_id", ""),
                "extraction_version": sanitize_text(draft.get("extraction_version"), limit=80),
            })
        counts["drafts"] = _write_csv(
            root / "recommendations" / "drafts.csv",
            list(public_drafts[0].keys()) if public_drafts else ["draft_id"],
            public_drafts,
        )

        public_events = []
        event_fields = [
            "event_id", "kol_name", "kol_id", "kol_handle", "platform", "source_url", "source_post_id",
            "posted_at", "symbol", "security_name", "direction", "thesis", "status", "baseline_rule",
            "baseline_date", "baseline_price_raw", "benchmark_symbol", "benchmark_baseline_price", "execution_warning",
        ]
        for event in event_rows:
            public_events.append({
                "event_id": event.get("event_id", ""),
                "kol_name": sanitize_public_identity(event.get("kol_name")),
                "kol_id": event.get("kol_id", ""),
                "kol_handle": sanitize_public_identity(event.get("kol_handle")),
                "platform": sanitize_text(event.get("platform"), limit=40),
                "source_url": canonical_url(event.get("source_url")),
                "source_post_id": event.get("source_post_id", ""),
                "posted_at": event.get("posted_at", ""),
                "symbol": event.get("symbol", ""),
                "security_name": sanitize_text(event.get("security_name"), limit=80),
                "direction": sanitize_text(event.get("direction"), limit=20),
                "thesis": short_excerpt(event.get("thesis"), 600),
                "status": sanitize_text(event.get("status"), limit=40),
                "baseline_rule": sanitize_text(event.get("baseline_rule"), limit=40),
                "baseline_date": event.get("baseline_date", ""),
                "baseline_price_raw": event.get("baseline_price_raw", ""),
                "benchmark_symbol": event.get("benchmark_symbol", ""),
                "benchmark_baseline_price": event.get("benchmark_baseline_price", ""),
                "execution_warning": short_excerpt(event.get("execution_warning"), 240),
            })
        counts["events"] = _write_csv(root / "events" / "events.csv", event_fields, public_events)
        counts["daily_marks"] = self._export_year_csv(root / "returns", "daily_marks", mark_rows)
        counts["checkpoints"] = _write_csv(root / "returns" / "checkpoints.csv", list(checkpoint_rows[0].keys()) if checkpoint_rows else ["event_id"], checkpoint_rows)
        counts.update(self._export_performance(root))
        counts["technical_context"] = self._export_technical_context(root)
        counts["method_research"] = self._export_method_research(root)
        counts.update(self._export_boards(root))

        self._write_docs(root, counts)
        manifest = self._build_manifest(root, counts)
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return counts

    def _load_posts(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if not self.posts_db.exists():
            return [], [], [], []
        con = _read_only_sqlite(self.posts_db)
        try:
            def rows(table: str) -> list[dict[str, Any]]:
                cursor = con.execute(f"select * from {table}")
                fields = [item[0] for item in cursor.description]
                return [dict(zip(fields, row)) for row in cursor.fetchall()]
            return rows("posts"), rows("classifications"), rows("recommendation_drafts"), rows("kols")
        finally:
            con.close()

    def _export_instruments(self, root: Path) -> dict[str, int]:
        if not self.market_db.exists():
            return {"instruments": 0}
        import duckdb
        con = duckdb.connect(str(self.market_db), read_only=True)
        try:
            rows = [dict(zip([item[0] for item in con.description], row)) for row in con.execute(
                "select symbol,name,instrument_type,exchange,status,list_date,provider,snapshot_date from instrument_catalog"
            ).fetchall()]
        finally:
            con.close()
        return {"instruments": _write_csv(root / "catalog" / "instruments.csv", list(rows[0].keys()) if rows else ["symbol"], rows)}

    def _export_year_csv(self, root: Path, stem: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[_year(row.get("trade_date"))].append(row)
        total = 0
        for year, values in sorted(groups.items()):
            fields = list(values[0].keys())
            total += _write_csv(root / f"{stem}-{year}.csv", fields, values)
        return total

    def _export_performance(self, root: Path) -> dict[str, int]:
        performance_fields = [
            "as_of", "platform", "kol_key", "kol_id", "kol_handle", "kol_name", "horizon",
            "window_name", "tier", "rank", "batch_count", "event_count", "unique_symbols",
            "recommendation_days", "median_excess", "mean_excess", "win_rate", "median_mae",
            "median_mfe", "sample_status", "unmatured_batch_count", "summary", "limitations",
            "algorithm_version",
        ]
        if not self.performance_db.exists():
            _write_csv(root / "performance" / "latest.csv", performance_fields, [])
            return {"performance_latest": 0, "performance_series": 0}
        con = _read_only_sqlite(self.performance_db)
        try:
            cursor = con.execute(
                "select platform,kol_key,horizon,window_name,tier,rank,as_of,payload_json from performance_snapshots"
            )
            fields = [item[0] for item in cursor.description]
            snapshots = [dict(zip(fields, row)) for row in cursor.fetchall()]
        finally:
            con.close()
        rows = []
        for snapshot in snapshots:
            payload = _json(snapshot.get("payload_json"), {})
            horizon_metrics = (payload.get("horizons") or {}).get(snapshot.get("horizon"), {})
            metrics = payload.get("metrics") or horizon_metrics
            narrative = payload.get("narrative") or {}
            rows.append({
                "as_of": snapshot.get("as_of", ""),
                "platform": snapshot.get("platform", ""),
                "kol_key": snapshot.get("kol_key", ""),
                "kol_id": payload.get("kol_id", ""),
                "kol_handle": sanitize_public_identity(payload.get("kol_handle")),
                "kol_name": sanitize_public_identity(payload.get("kol_name")),
                "horizon": snapshot.get("horizon", ""),
                "window_name": snapshot.get("window_name", ""),
                "tier": snapshot.get("tier", ""),
                "rank": snapshot.get("rank", ""),
                "batch_count": metrics.get("batch_count", ""),
                "event_count": metrics.get("event_count", ""),
                "unique_symbols": metrics.get("unique_symbols", ""),
                "recommendation_days": metrics.get("recommendation_days", ""),
                "median_excess": metrics.get("median_excess", ""),
                "mean_excess": metrics.get("mean_excess", ""),
                "win_rate": metrics.get("win_rate", ""),
                "median_mae": metrics.get("median_mae", ""),
                "median_mfe": metrics.get("median_mfe", ""),
                "sample_status": metrics.get("sample_status", ""),
                "unmatured_batch_count": metrics.get("unmatured_batch_count", ""),
                "summary": short_excerpt(narrative.get("summary"), 400),
                "limitations": short_excerpt(json.dumps(narrative.get("limitations", []), ensure_ascii=False), 600),
                "algorithm_version": payload.get("version", ""),
            })
        fields = list(rows[0].keys()) if rows else performance_fields
        latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("platform")), str(row.get("kol_key")), str(row.get("horizon")), str(row.get("window_name")))
            if str(row.get("as_of", "")) >= str(latest.get(key, {}).get("as_of", "")):
                latest[key] = row
        latest_count = _write_csv(root / "performance" / "latest.csv", fields, latest.values())
        series_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            series_groups[_year(row.get("as_of"))].append(row)
        series_count = 0
        for year, values in sorted(series_groups.items()):
            series_count += _write_csv(root / "performance" / f"series-{year}.csv", fields, values)
        return {"performance_latest": latest_count, "performance_series": series_count}

    def _export_technical_context(self, root: Path) -> int:
        if not self.market_db.exists():
            return 0
        import duckdb
        con = duckdb.connect(str(self.market_db), read_only=True)
        try:
            available = {
                str(row[0])
                for row in con.execute(
                    "select column_name from information_schema.columns "
                    "where table_name='event_technical_context'"
                ).fetchall()
            }
            release_column = ",foundation_release_id" if "foundation_release_id" in available else ""
            cursor = con.execute(
                "select event_id,symbol,posted_at,as_of_trade_date,adjustment,rsi14,macd_dif,macd_dea,macd_hist,macd_hist_pct,"
                "atr14,atr14_pct,volume_ratio_5,return_20d,distance_60d_high,history_bars,status,warnings_json,feature_version,"
                f"source_hash,computed_at{release_column} from event_technical_context"
            )
            fields = [item[0] for item in cursor.description]
            rows = [dict(zip(fields, row)) for row in cursor.fetchall()]
        finally:
            con.close()
        for row in rows:
            row["warnings_json"] = sanitize_text(row.get("warnings_json"), limit=1000)
        return _write_csv(root / "research" / "technical_context.csv", fields, rows)

    def _export_method_research(self, root: Path) -> int:
        if not self.market_db.exists():
            return 0
        import duckdb
        con = duckdb.connect(str(self.market_db), read_only=True)
        try:
            rows = con.execute(
                "select snapshot_id,event_id,method_version,input_hash,symbol,posted_at,as_of_trade_date,status,payload_json,warnings_json,computed_at "
                "from event_method_research"
            ).fetchall()
            fields = [item[0] for item in con.description]
        finally:
            con.close()
        output = []
        for row in rows:
            item = dict(zip(fields, row))
            payload = _safe_value(_json(item.pop("payload_json"), {}))
            item["warnings"] = _safe_value(_json(item.pop("warnings_json"), []))
            item["research"] = payload
            output.append(item)
        return _write_jsonl(root / "research" / "method_research.jsonl", output, key="snapshot_id")

    def _export_boards(self, root: Path) -> dict[str, int]:
        if not self.market_db.exists():
            return {"board_rps": 0}
        import duckdb
        con = duckdb.connect(str(self.market_db), read_only=True)
        try:
            rows = con.execute(
                "select c.board_code,c.board_name,c.board_type,r.trade_date,r.return_50,r.return_120,r.return_250,"
                "r.rps_50,r.rps_120,r.rps_250,r.breadth,r.turnover_ratio_20,r.status,r.formula_version,r.coverage_ratio,"
                "r.universe_size_50,r.universe_size_120,r.universe_size_250,br.rank_50,br.rank_120,br.rank_250,r.warnings_json "
                "from board_rps r join board_catalog c on c.board_key=r.board_key left join board_rank br "
                "on br.board_key=r.board_key and br.trade_date=r.trade_date"
            ).fetchall()
            fields = [item[0] for item in con.description]
        finally:
            con.close()
        public = []
        for row in rows:
            item = dict(zip(fields, row))
            item["warnings_json"] = sanitize_text(item.get("warnings_json"), limit=1000)
            public.append(item)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in public:
            groups[_year(item.get("trade_date"))].append(item)
        total = 0
        for year, values in sorted(groups.items()):
            total += _write_csv(root / "boards" / f"rps-rank-{year}.csv", fields, values)
        return {"board_rps": total}

    def _write_docs(self, root: Path, counts: dict[str, int]) -> None:
        (root / "GPT_CONTEXT.md").write_text(
            "# KOL Audit Dataset\n\n"
            "This is a privacy-filtered, read-only snapshot for evidence-first analysis of public KOL opinions on A-share instruments.\n\n"
            "Start with `catalog/kols.csv`, `sources/`, `recommendations/drafts.csv`, `events/events.csv`, `returns/`, and `performance/latest.csv`.\n"
            "Returns use the published event baseline rules; KOL performance is batch-weighted and small samples are explicitly marked.\n"
            "Technical and board files are derived research context, not trading signals. Missing values mean unavailable data, not zero.\n\n"
            "This dataset omits full source text, media, raw vendor responses, credentials, local paths, private notes, personal transactions, and purchased raw market files.\n",
            encoding="utf-8",
        )
        (root / "DATA_DICTIONARY.md").write_text(
            "# Data Dictionary\n\n"
            "- `sources/posts-YYYY-MM.jsonl`: one public source item per record; summaries and short evidence are capped and redacted.\n"
            "- `recommendations/drafts.csv`: one AI or rules-generated stock recommendation draft per source item and symbol.\n"
            "- `events/events.csv`: registered audit events and their immutable baseline facts.\n"
            "- `returns/`: daily marks and frozen 1W/1M/3M/6M checkpoints.\n"
            "- `performance/`: batch-weighted KOL snapshots and time series.\n"
            "- `research/technical_context.csv`: event-time indicators such as RSI, MACD, ATR and volume ratio.\n"
            "- `research/method_research.jsonl`: point-in-time multi-method research facts and limitations.\n"
            "- `boards/rps-rank-YYYY.csv`: derived industry/concept relative-strength and ranking records.\n",
            encoding="utf-8",
        )
        (root / "DATA_TERMS.md").write_text(
            "# Terms and Limits\n\n"
            "A recommendation event is an auditable observation, not a trade instruction. A batch is one source item; multiple symbols in one item are aggregated equally for performance analysis.\n\n"
            "Raw prices, adjusted research prices, benchmark excess returns, MAE and MFE retain their local calculation versions. A missing or partial record is not silently filled.\n\n"
            "Public account links are provided for provenance. Availability, edits, deletions, platform limits and survivorship bias can affect historical coverage.\n",
            encoding="utf-8",
        )

    def _build_manifest(self, root: Path, counts: dict[str, int]) -> dict[str, Any]:
        files = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            files.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)})
        return {
            "schema_version": SCHEMA_VERSION,
            "export_version": EXPORT_VERSION,
            "as_of": _latest_date(root),
            "counts": dict(sorted(counts.items())),
            "files": files,
        }


def _latest_date(root: Path) -> str:
    dates: list[str] = []
    for path in root.rglob("*.csv"):
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                for key in ("trade_date", "as_of", "posted_at", "date"):
                    value = str(row.get(key) or "")
                    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
                    if match:
                        dates.append(match.group(1))
                        break
    return max(dates) if dates else ""


def validate_public_dataset(input_path: str | Path) -> dict[str, Any]:
    root = Path(input_path).resolve()
    required = [
        "GPT_CONTEXT.md", "DATA_DICTIONARY.md", "DATA_TERMS.md", "manifest.json",
        "catalog/kols.csv", "catalog/instruments.csv", "events/events.csv",
        "recommendations/drafts.csv", "returns/checkpoints.csv", "performance/latest.csv",
        "research/technical_context.csv", "research/method_research.jsonl",
    ]
    errors: list[str] = []
    for relative in required:
        if not (root / relative).exists():
            errors.append(f"missing:{relative}")
    file_count = 0
    total_bytes = 0
    forbidden_matches: list[str] = []
    for path in root.rglob("*") if root.exists() else []:
        if not path.is_file() or path.name == "manifest.json":
            continue
        file_count += 1
        total_bytes += path.stat().st_size
        if path.stat().st_size > 50 * 1024 * 1024:
            errors.append(f"file_too_large:{path.relative_to(root).as_posix()}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"not_utf8:{path.relative_to(root).as_posix()}")
            continue
        if _FORBIDDEN_EXPORT_RE.search(text):
            forbidden_matches.append(path.relative_to(root).as_posix())
    if forbidden_matches:
        errors.append("forbidden_content:" + ",".join(forbidden_matches))

    def csv_rows(relative: str) -> list[dict[str, str]]:
        path = root / relative
        return _read_csv(path) if path.exists() else []

    events = csv_rows("events/events.csv")
    event_ids = {row.get("event_id", "") for row in events}
    source_ids: set[str] = set()
    for path in (root / "sources").glob("*.jsonl") if (root / "sources").exists() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                source_ids.add(str(json.loads(line).get("post_id", "")))
            except json.JSONDecodeError:
                errors.append(f"invalid_jsonl:{path.name}")
    for row in csv_rows("recommendations/drafts.csv"):
        if row.get("event_id") and row["event_id"] not in event_ids:
            errors.append(f"orphan_draft_event:{row.get('draft_id')}")
        if row.get("post_id") and source_ids and row["post_id"] not in source_ids:
            errors.append(f"orphan_draft_post:{row.get('draft_id')}")
    for path in (root / "returns").glob("daily_marks-*.csv") if (root / "returns").exists() else []:
        for row in _read_csv(path):
            if row.get("event_id") and row["event_id"] not in event_ids:
                errors.append(f"orphan_mark_event:{row.get('event_id')}")
    manifest = {}
    if (root / "manifest.json").exists():
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("invalid_manifest")
    manifest_files = manifest.get("files") if isinstance(manifest, dict) else None
    if isinstance(manifest_files, list):
        expected = {str(item.get("path", "")): item for item in manifest_files if isinstance(item, dict)}
        actual = {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        if set(expected) != set(actual):
            errors.append("manifest_file_set_mismatch")
        for relative, item in expected.items():
            path = actual.get(relative)
            if path is None:
                continue
            if item.get("size") != path.stat().st_size or item.get("sha256") != _sha256(path):
                errors.append(f"manifest_hash_mismatch:{relative}")
    else:
        errors.append("manifest_files_missing")
    manifest_counts = manifest.get("counts") if isinstance(manifest, dict) else None
    if isinstance(manifest_counts, dict):
        count_sources: dict[str, int] = {
            "kols": len(csv_rows("catalog/kols.csv")),
            "instruments": len(csv_rows("catalog/instruments.csv")),
            "drafts": len(csv_rows("recommendations/drafts.csv")),
            "events": len(csv_rows("events/events.csv")),
            "checkpoints": len(csv_rows("returns/checkpoints.csv")),
            "performance_latest": len(csv_rows("performance/latest.csv")),
            "technical_context": len(csv_rows("research/technical_context.csv")),
            "daily_marks": sum(len(_read_csv(path)) for path in (root / "returns").glob("daily_marks-*.csv")) if (root / "returns").exists() else 0,
            "performance_series": sum(len(_read_csv(path)) for path in (root / "performance").glob("series-*.csv")) if (root / "performance").exists() else 0,
            "method_research": sum(len(path.read_text(encoding="utf-8").splitlines()) for path in (root / "research").glob("method_research*.jsonl")) if (root / "research").exists() else 0,
            "board_rps": sum(len(_read_csv(path)) for path in (root / "boards").glob("rps-rank-*.csv")) if (root / "boards").exists() else 0,
        }
        for path in (root / "sources").glob("posts-*.jsonl") if (root / "sources").exists() else []:
            year = path.stem.removeprefix("posts-")
            count_sources[f"sources_{year}"] = len(path.read_text(encoding="utf-8").splitlines())
        for key, expected in manifest_counts.items():
            actual = count_sources.get(str(key), 0)
            if actual != expected:
                errors.append(f"manifest_count_mismatch:{key}:{expected}!={actual}")
    else:
        errors.append("manifest_counts_missing")
    return {
        "ok": not errors,
        "input": str(root),
        "errors": errors,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "event_count": len(event_ids - {""}),
        "source_count": len(source_ids - {""}),
        "manifest_schema": manifest.get("schema_version", ""),
    }


def public_dataset_doctor(runtime_root: str | Path | None = None) -> dict[str, Any]:
    return PublicDatasetExporter(runtime_root).doctor()
