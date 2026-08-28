from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest

from public_dataset import (
    PublicDatasetExporter,
    canonical_url,
    sanitize_public_identity,
    sanitize_text,
    validate_public_dataset,
)


def _hash_tree(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _create_fixture_runtime(root: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    (root / "kol").mkdir(parents=True)
    posts = root / "kol" / "posts.db"
    con = sqlite3.connect(posts)
    con.executescript(
        """
        create table kols (id integer, display_name text, platform text, handle text, profile_url text, status text, tracking_mode text);
        create table posts (post_id text, kol_id integer, platform text, handle text, author_name text, url text, posted_at text, post_type text, review_status text);
        create table classifications (post_id text, content_type text, evidence_type text, is_candidate integer, model_summary text);
        create table recommendation_drafts (id integer, post_id text, symbol text, security_name text, direction text, thesis text,
          evidence_type text, evidence_spans_json text, conditions_json text, mention_kind text, confidence real, status text,
          queue_scope text, reviewed_at text, event_id text, extraction_version text, action text, horizon text);
        """
    )
    con.execute("insert into kols values (1,'公开账号','X','WwQQ129146','https://x.com/WwQQ129146','active','direct')")
    con.execute("insert into posts values (?,?,?,?,?,?,?,?,?)", ("p1", 1, "X", "WwQQ129146", "公开账号", "https://x.com/WwQQ129146/status/1?utm_source=private", "2026-08-01T08:00:00+08:00", "original", "pending"))
    con.execute("insert into classifications values (?,?,?,?,?)", ("p1", "recommendation", "original_pre_event", 1, "短摘要 13800138000"))
    con.execute("insert into recommendation_drafts values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (1, "p1", "600000", "测试股份", "long", "公开理由", "original_pre_event", '["证据片段"]', '["条件"]', "recommendation", 0.95, "ready", "morning", "", "", "v1", "buy", "swing"))
    con.commit()
    con.close()

    events_fields = ["event_id", "kol_name", "kol_id", "kol_handle", "platform", "source_url", "source_post_id", "posted_at", "symbol", "security_name", "direction", "thesis", "status", "baseline_rule", "baseline_date", "baseline_price_raw", "benchmark_symbol", "benchmark_baseline_price", "execution_warning"]
    _write_csv(root / "kol" / "events.csv", events_fields, [{"event_id": "E1", "kol_name": "公开账号", "kol_id": "1", "kol_handle": "WwQQ129146", "platform": "X", "source_url": "https://x.com/WwQQ129146/status/1?x=1", "source_post_id": "p1", "posted_at": "2026-08-01T08:00:00+08:00", "symbol": "600000", "security_name": "测试股份", "direction": "long", "thesis": "公开理由", "status": "active", "baseline_rule": "next_open", "baseline_date": "2026-08-03", "baseline_price_raw": "10", "benchmark_symbol": "000300", "benchmark_baseline_price": "4000", "execution_warning": ""}])
    _write_csv(root / "kol" / "daily_marks.csv", ["event_id", "trade_date", "raw_return"], [{"event_id": "E1", "trade_date": "2026-08-03", "raw_return": "0.01"}])
    _write_csv(root / "kol" / "checkpoints.csv", ["event_id", "horizon"], [{"event_id": "E1", "horizon": "1W"}])

    market = root / "market"
    market.mkdir()
    con = duckdb.connect(str(market / "market.duckdb"))
    con.execute("create table instrument_catalog(symbol varchar,name varchar,instrument_type varchar,exchange varchar,status varchar,list_date varchar,provider varchar,snapshot_date varchar)")
    con.execute("insert into instrument_catalog values ('600000','测试股份','stock','SSE','active','2000-01-01','fixture','2026-08-01')")
    con.execute("create table event_technical_context(event_id varchar,symbol varchar,posted_at varchar,as_of_trade_date varchar,adjustment varchar,rsi14 double,macd_dif double,macd_dea double,macd_hist double,macd_hist_pct double,atr14 double,atr14_pct double,volume_ratio_5 double,return_20d double,distance_60d_high double,history_bars integer,status varchar,warnings_json varchar,feature_version varchar,source_hash varchar,computed_at varchar)")
    con.execute("create table event_method_research(snapshot_id varchar,event_id varchar,method_version varchar,input_hash varchar,symbol varchar,posted_at varchar,as_of_trade_date varchar,status varchar,payload_json varchar,warnings_json varchar,computed_at varchar)")
    con.execute("create table board_catalog(board_key varchar,board_code varchar,board_name varchar,board_type varchar)")
    con.execute("create table board_rps(board_key varchar,trade_date date,return_50 double,return_120 double,return_250 double,rps_50 double,rps_120 double,rps_250 double,breadth double,turnover_ratio_20 double,status varchar,formula_version varchar,coverage_ratio double,universe_size_50 integer,universe_size_120 integer,universe_size_250 integer,warnings_json varchar)")
    con.execute("create table board_rank(board_key varchar,trade_date date,rank_50 integer,rank_120 integer,rank_250 integer)")
    con.close()


def test_public_redaction_and_public_identity() -> None:
    phone = "138" + "00138000"
    email = "a" + "@example.com"
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz"
    local_path = "E:" + "\\private\\x.db"
    text = sanitize_text(f"phone {phone}, email {email}, secret {secret}, path {local_path}, QQ 123456")
    assert phone not in text
    assert email not in text
    assert secret not in text
    assert "E:\\private" not in text
    assert "123456" not in text
    assert sanitize_public_identity("WwQQ129146") == "WwQQ129146"
    assert canonical_url("https://x.com/a/status/1?utm_source=secret") == "https://x.com/a/status/1"


def test_export_is_deterministic_and_read_only(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _create_fixture_runtime(runtime)
    source = runtime / "kol" / "posts.db"
    before = source.stat().st_mtime_ns
    exporter = PublicDatasetExporter(runtime)
    first = tmp_path / "first"
    second = tmp_path / "second"
    exporter.export(first)
    exporter.export(second)
    assert _hash_tree(first) == _hash_tree(second)
    assert source.stat().st_mtime_ns == before
    report = validate_public_dataset(first)
    assert report["ok"], report
    assert "WwQQ129146" in (first / "catalog" / "kols.csv").read_text(encoding="utf-8")
    assert "13800138000" not in "\n".join(path.read_text(encoding="utf-8") for path in first.rglob("*.csv"))


def test_invalid_staging_does_not_replace_previous_snapshot(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _create_fixture_runtime(runtime)
    exporter = PublicDatasetExporter(runtime)
    destination = tmp_path / "latest"
    exporter.export(destination)
    before = _hash_tree(destination)

    def invalid_export(root: Path) -> dict[str, int]:
        root.mkdir(parents=True, exist_ok=True)
        (root / "partial.txt").write_text("incomplete", encoding="utf-8")
        return {}

    exporter._export_to = invalid_export  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="staging validation failed"):
        exporter.export(destination)
    assert _hash_tree(destination) == before
