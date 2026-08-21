from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from dataclasses import replace
from datetime import date, datetime, time as datetime_time, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from kol_tracker import (
    AKShareCheckpointProvider,
    EventRecord,
    KolStore,
    WarehousePriceProvider,
    generate_dashboard,
    initialize_seed_events,
    now_iso,
    update_kol_tracking,
    validate_event,
)
from event_context import (
    FEATURE_VERSION,
    compute_event_technical_context,
    event_context_cutoff,
    event_context_input_hash,
    failed_event_technical_context,
)
from market_indicators import INDICATOR_VERSION, compute_daily_indicators
from kol_posts import (
    DeepSeekCredentialStore,
    DeepSeekBatchPostClassifier,
    DeepSeekPostClassifier,
    STRUCTURED_REVIEW_VERSION,
    KeyringCredentialStore,
    KolPostStore,
    ModelWorkerBusyError,
    ModelProviderUnavailableError,
    NitterCredentialStore,
    ReaderCredentialStore,
    RapidOcrBatchClassifier,
    RuleClassifier,
    UnlimitedOcrBatchClassifier,
    ZhihuProfileProvider,
    build_post_classifier,
    build_x_post_provider,
    classify_pending_with_codex,
    initialize_seed_kols,
    load_stock_aliases,
    media_disk_usage,
    process_pending_with_ocr,
    run_post_fetch,
)
from morning_pipeline import MorningPipeline
from morning_orchestrator import MorningOrchestrator
from nitter_runtime import docker_health, materialize_nitter_runtime, timeline_health, xtf_version
from freestockdb_runtime import FreeStockDBRuntime
from kol_intraday import audit_event_intraday, backfill_event_intraday
from pipeline_jobs import run_post_approval_refresh
from event_dossier import EventDossierService
from event_research_ai import build_event_research_interpreter
from event_research_service import EventMethodResearchService
from review_agent import ReviewAgentRepository, ReviewAgentRunner
from market_data import (
    AKShareMarketProvider,
    BaoStockMarketProvider,
    FreeStockDBMarketProvider,
    Instrument,
    MarketStore,
    audit_daily_bars,
    compare_daily_frames,
    default_sync_start,
    normalise_daily_bars,
    sync_daily_bars,
)
from foundation_market_client import FoundationBackedMarketStore, FoundationMarketReader
from purchased_daily import (
    PURCHASED_DAILY_PROVIDER,
    PurchasedDailyProvider,
    audit_purchased_daily_archive,
    import_historical_daily,
)
from stock_leads import extract_stock_leads, reconcile_exact_stock_leads
from recommendation_drafts import RecommendationDraftRepository, review_window_utc
from recommendation_processing import materialize_recommendation_drafts
from kol_performance import DeepSeekPerformanceInterpreter, KolPerformanceService, build_weekly_message
from portfolio import PortfolioStore, load_transactions_json
from public_dataset import (
    PublicDatasetExporter,
    public_dataset_doctor as run_public_dataset_doctor,
    validate_public_dataset,
)


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("TRADING_RUNTIME_ROOT", ROOT / "_runtime" / "trading"))
WATCHLIST = RUNTIME / "watchlist.csv"
RUN_LOG = RUNTIME / "run_log.jsonl"
PRICE_DIR = RUNTIME / "data" / "prices"
REPORT_DIR = RUNTIME / "reports"
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT", Path(__file__).resolve().parents[1] / "_vault"))
OBSIDIAN_PROJECTS = OBSIDIAN_VAULT / "04_Projects"
KOL_ROOT = RUNTIME / "kol"
KOL_DASHBOARD = KOL_ROOT / "reports" / "KOL推荐收益看板.md"
KOL_POST_DB = KOL_ROOT / "posts.db"
KOL_MEDIA = KOL_ROOT / "media"
KOL_CLASSIFIER_SCHEMA = Path(__file__).with_name("kol_classifier_schema.json")
KOL_BATCH_CLASSIFIER_SCHEMA = Path(__file__).with_name("kol_batch_classifier_schema.json")
EVENT_RESEARCH_SCHEMA = Path(__file__).with_name("event_research_schema.json")
KOL_UI_DIST = Path(__file__).with_name("ui") / "dist"
XTF_COMMAND = ROOT / "_runtime" / "venv-x-fetcher" / "Scripts" / "xtf.exe"
NITTER_URL = os.environ.get("KOL_NITTER_URL", "http://127.0.0.1:9377")
NITTER_TEMPLATE = ROOT / "_infra" / "nitter" / "nitter.conf.template"
NITTER_RUNTIME = KOL_ROOT / "nitter"
NOTIFICATION_STATE = KOL_ROOT / "notification_state.json"
FALLBACK_MODE_STATE = KOL_ROOT / "fallback_mode.json"
MARKET_ROOT = RUNTIME / "market"
FOUNDATION_ROOT = Path(os.environ.get("ASHARE_FOUNDATION_ROOT", r"F:\ai-data\ashare"))
FOUNDATION_REPO = Path(
    os.environ.get("ASHARE_FOUNDATION_REPO", r"<AI_HUB_HOME>\ashare-data-foundation")
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
PURCHASED_DAILY_ROOT = Path(
    os.environ.get("PURCHASED_DAILY_ROOT", "<PURCHASED_DATA_HOME>/数据更新时间2026.7.31")
)
EVENT_RESEARCH_LOCK = MARKET_ROOT / "event-method-research.lock"
PORTFOLIO_ROOT = RUNTIME / "portfolio"
UNLIMITED_OCR_ROOT = Path(os.environ.get("UNLIMITED_OCR_ROOT", ROOT.parent / "Unlimited-OCR"))
UNLIMITED_OCR_RUNNER = Path(__file__).with_name("unlimited_ocr_batch.py")
RAPID_OCR_PYTHON = Path(os.environ.get(
    "RAPID_OCR_PYTHON",
    ROOT / "_runtime" / "venv-ocr-fast" / "Scripts" / "python.exe",
))
RAPID_OCR_RUNNER = Path(__file__).with_name("rapid_ocr_batch.py")
ZHIHU_PROFILE_CAPTURE = ROOT / "_automation" / "hermes-capture" / "zhihu_profile_capture.py"
ZHIHU_USER_DATA_DIR = Path(
    os.environ.get(
        "ZHIHU_USER_DATA_DIR",
        Path.home() / "AppData" / "Local" / "hermes" / "browser-profiles" / "zhihu-edge",
    )
)


def _ocr_classifier(timeout_seconds: float = 90) -> RapidOcrBatchClassifier:
    return RapidOcrBatchClassifier(
        RAPID_OCR_PYTHON,
        RAPID_OCR_RUNNER,
        timeout_seconds=timeout_seconds,
    )


DEFAULT_WATCHLIST = [
    {
        "symbol": "600900",
        "name": "长江电力",
        "market": "A股",
        "status": "待研究",
        "reason": "四性合一候选；现金流和股东回报样本",
    },
    {
        "symbol": "600519",
        "name": "贵州茅台",
        "market": "A股",
        "status": "待研究",
        "reason": "高质量消费龙头；估值和增长验证样本",
    },
    {
        "symbol": "000538",
        "name": "云南白药",
        "market": "A股",
        "status": "待研究",
        "reason": "稀缺品牌/医药消费候选",
    },
    {
        "symbol": "600436",
        "name": "片仔癀",
        "market": "A股",
        "status": "待研究",
        "reason": "稀缺品牌/中药资产候选",
    },
    {
        "symbol": "688017",
        "name": "绿的谐波",
        "market": "A股",
        "status": "待补源",
        "reason": "Serenity/白毛二手转述事件候选；需补原始推荐",
    },
]


@dataclass(frozen=True)
class FetchResult:
    symbol: str
    ok: bool
    rows: int = 0
    path: str = ""
    error: str = ""


def ensure_dirs() -> None:
    for path in [RUNTIME, PRICE_DIR, REPORT_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def log_event(event: str, payload: dict) -> None:
    ensure_dirs()
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **payload,
    }
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def init_watchlist(force: bool = False) -> None:
    ensure_dirs()
    if WATCHLIST.exists() and not force:
        print(f"watchlist exists: {WATCHLIST}")
        return
    with WATCHLIST.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["symbol", "name", "market", "status", "reason"],
        )
        writer.writeheader()
        writer.writerows(DEFAULT_WATCHLIST)
    log_event("init_watchlist", {"path": str(WATCHLIST), "count": len(DEFAULT_WATCHLIST)})
    print(f"created watchlist: {WATCHLIST}")


def read_watchlist() -> list[dict]:
    if not WATCHLIST.exists():
        init_watchlist()
    with WATCHLIST.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def selected_symbols(symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip() for s in symbols.split(",") if s.strip()]
    return [row["symbol"] for row in read_watchlist()]


def dependency_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def fetch_akshare(symbol: str, start: str, end: str, adjust: str) -> FetchResult:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        return FetchResult(symbol=symbol, ok=False, error=f"akshare unavailable: {exc}")

    try:
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
        if df.empty:
            return FetchResult(symbol=symbol, ok=False, error="empty dataframe")
        out = PRICE_DIR / f"{symbol}_{start}_{end}_{adjust or 'raw'}_akshare.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        return FetchResult(symbol=symbol, ok=True, rows=len(df), path=str(out))
    except Exception as exc:
        return FetchResult(symbol=symbol, ok=False, error=str(exc))


def verify_baostock(symbol: str, start: str, end: str) -> FetchResult:
    try:
        import baostock as bs  # type: ignore
        import pandas as pd  # type: ignore
    except Exception as exc:
        return FetchResult(symbol=symbol, ok=False, error=f"baostock unavailable: {exc}")

    market = "sh" if symbol.startswith(("6", "9")) else "sz"
    code = f"{market}.{symbol}"
    start_fmt = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    end_fmt = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    lg = bs.login()
    if lg.error_code != "0":
        return FetchResult(symbol=symbol, ok=False, error=f"login failed: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,open,high,low,close,volume,amount,turn",
            start_date=start_fmt,
            end_date=end_fmt,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            return FetchResult(symbol=symbol, ok=False, error=rs.error_msg)
        df = pd.DataFrame(rows, columns=rs.fields)
        if df.empty:
            return FetchResult(symbol=symbol, ok=False, error="empty dataframe")
        out = PRICE_DIR / f"{symbol}_{start}_{end}_baostock.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        return FetchResult(symbol=symbol, ok=True, rows=len(df), path=str(out))
    finally:
        bs.logout()


def fetch_prices(args: argparse.Namespace) -> None:
    ensure_dirs()
    results = [
        fetch_akshare(symbol, args.start, args.end, args.adjust)
        for symbol in selected_symbols(args.symbols)
    ]
    for result in results:
        status = "ok" if result.ok else "failed"
        print(f"{status}: {result.symbol} rows={result.rows} path={result.path} error={result.error}")
    log_event("fetch_prices", {"results": [result.__dict__ for result in results]})


def verify_baostock_cmd(args: argparse.Namespace) -> None:
    ensure_dirs()
    results = [verify_baostock(symbol, args.start, args.end) for symbol in selected_symbols(args.symbols)]
    for result in results:
        status = "ok" if result.ok else "failed"
        print(f"{status}: {result.symbol} rows={result.rows} path={result.path} error={result.error}")
    log_event("verify_baostock", {"results": [result.__dict__ for result in results]})


def latest_price_files() -> list[Path]:
    if not PRICE_DIR.exists():
        return []
    return sorted(PRICE_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)


def generate_report(_: argparse.Namespace) -> None:
    ensure_dirs()
    today = datetime.now().strftime("%Y-%m-%d")
    report = REPORT_DIR / f"{today}__A股交易研究审计日报.md"
    rows = read_watchlist()
    price_files = latest_price_files()
    lines = [
        f"# A股交易研究审计日报 {today}",
        "",
        "## 运行边界",
        "",
        "- 本报告只做研究审计，不构成投资建议。",
        "- AI报告、KOL观点和历史收藏只作为外部信号。",
        "- 买卖、仓位和实盘执行不在 v1 范围内。",
        "",
        "## 股票池",
        "",
        "| 股票 | 名称 | 状态 | 进入理由 |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['symbol']} | {row['name']} | {row['status']} | {row['reason']} |")
    lines.extend([
        "",
        "## 最新数据文件",
        "",
    ])
    if price_files:
        for path in price_files[:10]:
            lines.append(f"- `{path}`")
    else:
        lines.append("- 暂无行情缓存。")
    lines.extend([
        "",
        "## 今日审计问题",
        "",
        "- 哪条证据增强了某只股票的研究价值？",
        "- 哪条证据削弱了原有判断？",
        "- 哪条 KOL 推荐仍缺六要素？",
        "- 哪个结论只是叙事，还没有强证据？",
        "",
        "## 待验证",
        "",
        "- [ ] 拆分 AI 报告中的事实、推断、风险、待验证。",
        "- [ ] 更新 Obsidian 股票研究台。",
        "- [ ] 更新 KOL 推荐事件表。",
    ])
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log_event("generate_report", {"path": str(report)})
    print(f"created report: {report}")


def doctor(_: argparse.Namespace) -> None:
    ensure_dirs()
    print(f"root={ROOT}")
    print(f"runtime={RUNTIME}")
    print(f"watchlist={WATCHLIST} exists={WATCHLIST.exists()}")
    for dep in ["akshare", "baostock", "pandas"]:
        print(f"{dep}={dependency_available(dep)}")
    print(f"obsidian_projects={OBSIDIAN_PROJECTS} exists={OBSIDIAN_PROJECTS.exists()}")


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, flags=re.DOTALL)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("-"):
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _first_url(text: str) -> str:
    match = re.search(r'https?://[^\s)>\]}"]+', text)
    return match.group(0).rstrip(".,，。") if match else ""


def _section_summary(text: str) -> str:
    for heading in ["摘要", "理由", "核心观点", "我的备注", "原始内容"]:
        match = re.search(
            rf"^##+\s*{re.escape(heading)}\s*$\r?\n(.*?)(?=^##+\s|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if not match:
            continue
        for line in match.group(1).splitlines():
            clean = line.strip().lstrip("- ").strip()
            if clean and not clean.startswith(("http://", "https://", "[[")):
                return clean[:500]
    return ""


def _source_note_path(value: str) -> tuple[Path, str]:
    path = Path(value)
    if not path.is_absolute():
        path = OBSIDIAN_VAULT / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"source note not found: {path}")
    try:
        relative = path.relative_to(OBSIDIAN_VAULT.resolve()).as_posix()
    except ValueError:
        relative = str(path)
    return path, relative


def _next_event_id(events: list[EventRecord]) -> str:
    numbers = []
    for event in events:
        match = re.fullmatch(r"KOL-(\d+)", event.event_id)
        if match:
            numbers.append(int(match.group(1)))
    return f"KOL-{max(numbers, default=0) + 1:04d}"


def kol_init(_: argparse.Namespace) -> None:
    store = KolStore(KOL_ROOT)
    created = initialize_seed_events(store)
    generate_dashboard(store, KOL_DASHBOARD)
    print(json.dumps({"ok": True, "created": created, "events": len(store.load_events())}, ensure_ascii=False))


def kol_register(args: argparse.Namespace) -> None:
    store = KolStore(KOL_ROOT)
    source_path, source_note = _source_note_path(args.source_note)
    text = source_path.read_text(encoding="utf-8")
    metadata = _frontmatter(text)
    source_url = args.source_url or metadata.get("source_url", "") or _first_url(text)
    platform = args.platform or metadata.get("source_type", "") or metadata.get("platform", "")
    if not platform:
        platform = "X" if re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/", source_url) else "web"
    thesis = args.thesis or _section_summary(text)
    event = EventRecord(
        event_id=args.event_id or _next_event_id(store.load_events()),
        kol_name=args.kol,
        platform=platform,
        source_url=source_url,
        source_note=source_note,
        posted_at=args.posted_at,
        symbol=args.symbol,
        security_name=args.name or "",
        direction=args.direction,
        thesis=thesis,
        status="active",
        activated_at=now_iso(),
        updated_at=now_iso(),
    )
    errors = validate_event(event)
    if errors:
        raise ValueError(f"cannot activate event; missing or invalid: {', '.join(errors)}")
    created = store.register_event(event)
    if created:
        store.queue_notification(
            {
                "kind": "activation",
                "key": f"activation:{event.event_id}",
                "message": f"KOL事件 {event.event_id} 已激活：{event.kol_name} / {event.symbol} {event.security_name}。",
                "event_id": event.event_id,
            }
        )
    generate_dashboard(store, KOL_DASHBOARD)
    print(json.dumps({"ok": True, "created": created, "event_id": event.event_id}, ensure_ascii=False))


def _send_pending_notifications(store: KolStore) -> list[str]:
    pending = store.pending_notifications()
    if not pending:
        return []

    labels = {
        "activation": "新激活",
        "checkpoint": "检查节点",
        "completed": "跟踪完成",
        "source_failure": "数据源异常",
        "data_conflict": "数据冲突",
        "calculation_failure": "计算异常",
    }
    counts = Counter(payload["kind"] for payload in pending)
    summary = "，".join(f"{labels.get(kind, kind)} {count}" for kind, count in counts.items())
    important = [
        payload["message"]
        for payload in pending
        if payload["kind"] not in {"activation"}
    ][:5]
    suffix = "" if len(important) == len([item for item in pending if item["kind"] != "activation"]) else "\n- 其余详情已写入运行账本"
    detail = "" if not important else "\n" + "\n".join(f"- {message}" for message in important) + suffix
    message = f"[KOL回测库] 合并通知：{summary}{detail}"

    failures: list[str] = []
    activated: set[str] = set()
    if not _send_feishu(message):
        failures.append("notification_digest: Hermes UTF-8 file delivery failed")

    if not failures:
        for payload in pending:
            store.record_notification(payload["key"], payload)
            if payload["kind"] == "activation" and payload.get("event_id"):
                activated.add(payload["event_id"])

    if activated:
        events = [
            replace(event, activation_notified_at=now_iso(), updated_at=now_iso())
            if event.event_id in activated
            else event
            for event in store.load_events()
        ]
        store.save_events(events)
    for failure in failures:
        store.log_run("notification_failed", {"error": failure})
    return failures


def kol_update(args: argparse.Namespace) -> None:
    KOL_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = KOL_ROOT / "kol-update.lock"
    lock = FileLock(str(lock_path), timeout=1)
    try:
        lock.acquire()
    except Timeout:
        print(json.dumps({
            "ok": True,
            "status": "already_running",
            "dry_run": bool(args.dry_run),
            "lock": str(lock_path),
        }, ensure_ascii=False))
        return
    try:
        _kol_update_locked(args)
    finally:
        lock.release()


def _kol_update_locked(args: argparse.Namespace) -> None:
    store = KolStore(KOL_ROOT)
    if not store.events_path.exists():
        raise RuntimeError("KOL event store is not initialized; run kol-init first")
    as_of = date.fromisoformat(args.as_of)
    market_store = _market_store()
    result = update_kol_tracking(
        store,
        market_store,
        AKShareCheckpointProvider(),
        as_of=as_of,
        dashboard_path=KOL_DASHBOARD,
        dry_run=args.dry_run,
        event_ids={args.event_id} if getattr(args, "event_id", "") else None,
    )
    failures: list[str] = []
    performance_summary: dict[str, Any] | None = None
    if not args.dry_run:
        for notification in result.notifications:
            store.queue_notification(notification)
        awaiting = set(result.awaiting_market_data)
        for pending in store.pending_notifications():
            if pending["kind"] == "calculation_failure" and pending.get("event_id") in awaiting:
                store.supersede_notification(
                    pending["key"],
                    reason="event is waiting for its first eligible market bar",
                )
        if args.notify:
            failures = _send_pending_notifications(store)
        performance_summary = KolPerformanceService(
            store,
            post_store=_post_store(),
        ).refresh(as_of=as_of)
    output = {
        "ok": not result.errors,
        "dry_run": args.dry_run,
        "run_id": result.run_id,
        "updated_events": result.updated_events,
        "marks": result.mark_count,
        "new_checkpoints": [row["horizon"] + ":" + row["event_id"] for row in result.new_checkpoints],
        "awaiting_market_data": result.awaiting_market_data,
        "notifications": len(result.notifications),
        "pending_notifications": len(store.pending_notifications()) if not args.dry_run else 0,
        "errors": result.errors,
        "notification_errors": failures,
        "performance": {
            "run_id": performance_summary.get("run_id"),
            "snapshots_inserted": performance_summary.get("snapshots_inserted"),
            "ranked_count": performance_summary.get("coverage", {}).get("ranked_count"),
        } if performance_summary else None,
    }
    print(json.dumps(output, ensure_ascii=False))
    if result.errors:
        raise SystemExit(2)


def _performance_service() -> KolPerformanceService:
    return KolPerformanceService(KolStore(KOL_ROOT), post_store=_post_store())


def kol_performance_doctor(_: argparse.Namespace) -> None:
    service = _performance_service()
    migration = service.migrate_event_identities()
    checks = service.doctor()
    checks["migration"] = migration
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_performance_refresh(args: argparse.Namespace) -> None:
    as_of = date.fromisoformat(args.as_of or date.today().isoformat())
    service = _performance_service()
    migration = service.migrate_event_identities()
    result = service.refresh(as_of=as_of)
    print(json.dumps({
        "ok": True,
        "as_of": as_of.isoformat(),
        "migration": migration,
        "run_id": result["run_id"],
        "snapshots_inserted": result["snapshots_inserted"],
        "coverage": result["coverage"],
    }, ensure_ascii=False))


def kol_performance_backfill(args: argparse.Namespace) -> None:
    service = _performance_service()
    service.migrate_event_identities()
    checkpoints = service.event_store.load_checkpoints()
    dates = sorted({row.get("trade_date", "") for row in checkpoints if row.get("trade_date")})
    start = args.start or (dates[0] if dates else date.today().isoformat())
    end = args.end or date.today().isoformat()
    targets = [date.fromisoformat(value) for value in dates if start <= value <= end]
    if date.fromisoformat(end) not in targets:
        targets.append(date.fromisoformat(end))
    results = [service.refresh(as_of=value) for value in targets]
    print(json.dumps({
        "ok": True,
        "from": start,
        "to": end,
        "as_of_count": len(results),
        "runs": [result["run_id"] for result in results],
        "snapshots_inserted": sum(result["snapshots_inserted"] for result in results),
    }, ensure_ascii=False))


def kol_performance_report(args: argparse.Namespace) -> None:
    as_of = date.fromisoformat(args.as_of or date.today().isoformat())
    service = _performance_service()
    result = service.refresh(as_of=as_of)
    if args.with_ai:
        result = service.explain(result, DeepSeekPerformanceInterpreter())
    message = build_weekly_message(result)
    sent = _send_feishu(message) if args.notify else False
    print(json.dumps({
        "ok": True,
        "as_of": as_of.isoformat(),
        "weekly": bool(args.weekly),
        "message": message,
        "notification_sent": sent,
        "run_id": result["run_id"],
    }, ensure_ascii=False))


def kol_returns_backfill(args: argparse.Namespace) -> None:
    kol_update(
        argparse.Namespace(
            as_of=args.as_of or date.today().isoformat(),
            notify=False,
            dry_run=bool(args.dry_run),
        )
    )


def kol_report(_: argparse.Namespace) -> None:
    store = KolStore(KOL_ROOT)
    generate_dashboard(store, KOL_DASHBOARD)
    print(json.dumps({"ok": True, "dashboard": str(KOL_DASHBOARD)}, ensure_ascii=False))


def kol_doctor(_: argparse.Namespace) -> None:
    store = KolStore(KOL_ROOT)
    events = store.load_events()
    invalid = {event.event_id: validate_event(event) for event in events if validate_event(event)}
    checks = {
        "runtime": str(KOL_ROOT),
        "runtime_exists": KOL_ROOT.exists(),
        "events": len(events),
        "active": sum(event.status == "active" for event in events),
        "marks": len(store.load_marks()),
        "checkpoints": len(store.load_checkpoints()),
        "pending_notifications": len(store.pending_notifications()),
        "invalid_active_events": invalid,
        "dashboard": str(KOL_DASHBOARD),
        "dashboard_exists": KOL_DASHBOARD.exists(),
        "baostock": dependency_available("baostock"),
        "akshare": dependency_available("akshare"),
        "pandas": dependency_available("pandas"),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if invalid or not all(checks[name] for name in ["baostock", "akshare", "pandas"]):
        raise SystemExit(2)


def _post_store() -> KolPostStore:
    store = KolPostStore(KOL_POST_DB, KOL_MEDIA)
    initialize_seed_kols(store)
    return store


def _send_feishu(message: str) -> bool:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="hermes-message-",
            delete=False,
        ) as stream:
            stream.write(message)
            temporary_path = Path(stream.name)
        completed = subprocess.run(
            ["hermes", "send", "--to", "feishu", "--file", str(temporary_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _send_transition_alert(key: str, active: bool, message: str) -> bool:
    NOTIFICATION_STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(NOTIFICATION_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    previous = bool(state.get(key, {}).get("active", False))
    if previous == active:
        return False
    state[key] = {"active": active, "updated_at": now_iso()}
    temporary = NOTIFICATION_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, NOTIFICATION_STATE)
    return _send_feishu(message) if active else False


def _fallback_mode() -> str:
    try:
        value = json.loads(FALLBACK_MODE_STATE.read_text(encoding="utf-8")).get("mode", "shadow")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = "shadow"
    return value if value in {"shadow", "enabled"} else "shadow"


def _post_provider(mode: str):
    # The main X session remains sealed. Automated collection uses a separate
    # low-frequency reader identity stored under ai-hub/twitter-reader.
    if mode not in {"auto", "twitter", "nitter"}:
        raise SystemExit(f"unsupported X provider: {mode}")
    if mode == "nitter":
        return build_x_post_provider(
            "nitter",
            twitter_credentials=ReaderCredentialStore(),
            xtf_command=str(XTF_COMMAND),
            nitter_url=NITTER_URL,
            fallback_mode="disabled",
        )
    fallback_mode = _fallback_mode()
    if fallback_mode in {"shadow", "enabled"}:
        try:
            if not timeline_health(NITTER_URL).get("ready"):
                fallback_mode = "disabled"
        except Exception:
            fallback_mode = "disabled"
    return build_x_post_provider(
        mode,
        twitter_credentials=ReaderCredentialStore(),
        xtf_command=str(XTF_COMMAND),
        nitter_url=NITTER_URL,
        fallback_mode=fallback_mode,
    )


def _zhihu_provider() -> ZhihuProfileProvider:
    return ZhihuProfileProvider(
        ZHIHU_PROFILE_CAPTURE,
        python_command=sys.executable,
        browser_path=os.environ.get("ZHIHU_BROWSER_PATH", ""),
        profile_directory=os.environ.get("ZHIHU_PROFILE_DIRECTORY", "Default"),
        user_data_dir=str(ZHIHU_USER_DATA_DIR),
        port=int(os.environ.get("ZHIHU_CDP_PORT", "9222")),
    )


def kol_post_doctor(args: argparse.Namespace) -> None:
    store = _post_store()
    healthy_fetch_states = {"never", "success", "gap_detected"}
    all_kols = store.list_kols()
    scoped_kols = [
        item for item in all_kols
        if args.platform == "all" or str(item.get("platform") or "X").casefold() == args.platform
    ]
    zhihu_kols = [item for item in all_kols if item.get("platform") == "Zhihu"]
    active_zhihu = [item for item in zhihu_kols if item.get("status") == "active"]
    paused_zhihu = [item for item in zhihu_kols if item.get("status") == "paused"]
    failed_zhihu = [
        item for item in active_zhihu
        if str(item.get("fetch_status") or "") not in healthy_fetch_states
    ]
    failed_scoped = [
        item for item in scoped_kols
        if item.get("status") == "active"
        and str(item.get("fetch_status") or "") not in healthy_fetch_states
    ]
    checks = {
        "ok": True,
        "database": str(store.path),
        "database_exists": store.path.exists(),
        "platform": args.platform,
        "active_kols": len([item for item in scoped_kols if item.get("status") == "active"]),
        "posts": store.count_posts(),
        "pending_reviews": store.count_pending(),
        "media_bytes": media_disk_usage(store.media_root),
        "twitter_cli": shutil.which("twitter") or "",
        "twitter_credentials_configured": KeyringCredentialStore().configured(),
        "twitter_reader_credentials_configured": ReaderCredentialStore().configured(),
        "zhihu_capture_available": ZHIHU_PROFILE_CAPTURE.is_file(),
        "zhihu_active_kols": len(active_zhihu),
        "zhihu_paused_kols": len(paused_zhihu),
        "zhihu_failed_kols": len(failed_zhihu),
        "zhihu_failure_handles": [str(item.get("handle")) for item in failed_zhihu[:20]],
        "failed_kols": len(failed_scoped),
        "failure_handles": [str(item.get("handle")) for item in failed_scoped[:50]],
        "nitter_credentials_configured": NitterCredentialStore().configured(),
        "xtf": str(XTF_COMMAND) if XTF_COMMAND.exists() else "",
        "xtf_version": xtf_version(XTF_COMMAND) if XTF_COMMAND.exists() else "",
        "nitter": timeline_health(NITTER_URL),
        "docker": docker_health(),
        "fallback_mode": _fallback_mode(),
        "shadow_rollout": store.shadow_rollout_status(),
        "codex_cli": shutil.which("codex") or "",
        "classifier_schema": KOL_CLASSIFIER_SCHEMA.exists(),
        "unlimited_ocr_root": str(UNLIMITED_OCR_ROOT),
        "unlimited_ocr_available": UnlimitedOcrBatchClassifier(
            UNLIMITED_OCR_ROOT, UNLIMITED_OCR_RUNNER
        ).available(),
        "rapid_ocr_available": _ocr_classifier().available(),
        "ocr_provider": "rapidocr",
    }
    platform_ok = {
        "all": bool(checks["twitter_cli"] and checks["zhihu_capture_available"]),
        "x": bool(checks["twitter_cli"] and checks["twitter_reader_credentials_configured"]),
        "zhihu": bool(checks["zhihu_capture_available"]),
    }
    dependencies_ok = bool(
        platform_ok[args.platform]
        and checks["codex_cli"]
        and checks["classifier_schema"]
        and checks["rapid_ocr_available"]
    )
    checks["collection_ok"] = not failed_scoped
    checks["ok"] = dependencies_ok and checks["collection_ok"]
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_post_db_backup(_: argparse.Namespace) -> None:
    if not KOL_POST_DB.exists():
        print(json.dumps({"ok": True, "skipped": True, "reason": "database_missing"}))
        return
    backup_root = KOL_ROOT / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / f"posts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with sqlite3.connect(KOL_POST_DB) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    backups = sorted(backup_root.glob("posts-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for expired in backups[14:]:
        expired.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "backup": str(target)}, ensure_ascii=False))


def kol_reader_migrate(_: argparse.Namespace) -> None:
    """Copy the existing Nitter reader session into its isolated X reader slot."""
    source = NitterCredentialStore()
    target = ReaderCredentialStore()
    auth_token, ct0 = source.load_values()
    target.save(auth_token, ct0)
    print(json.dumps({
        "ok": True,
        "source": source.service_name,
        "target": target.service_name,
        "credentials_configured": target.configured(),
    }, ensure_ascii=False))


def kol_collection_doctor(_: argparse.Namespace) -> None:
    store = _post_store()
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=7)).isoformat()
    checks = {
        "ok": ReaderCredentialStore().configured(),
        "database": str(store.path),
        "reader_credentials_configured": ReaderCredentialStore().configured(),
        "main_credentials_configured": KeyringCredentialStore().configured(),
        "nitter_optional": True,
        "nitter": timeline_health(NITTER_URL),
        "x_recent": store.collection_coverage(platform="X", window_start=start, window_end=end),
        "zhihu_recent": store.collection_coverage(platform="Zhihu", window_start=start, window_end=end),
        "open_gaps": store.list_collection_gaps(status="open", limit=100),
        "latest_runs": store.recent_fetch_runs(limit=5),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_gap_audit(args: argparse.Namespace) -> None:
    store = _post_store()
    end = date.today().isoformat() if args.to_date == "auto" else args.to_date
    start = args.from_date
    payload: dict[str, Any] = {
        "ok": True,
        "from": start,
        "to": end,
        "platforms": {},
        "gaps": [],
    }
    for platform in ("X", "Zhihu"):
        coverage = store.collection_coverage(
            platform=platform,
            window_start=start,
            window_end=end,
        )
        payload["platforms"][platform] = coverage
        for item in coverage["items"]:
            if not item.get("last_success_at") or item.get("window_posts", 0) == 0:
                kol = store.get_kol(int(item["id"]))
                store.open_collection_gap(
                    int(item["id"]),
                    platform=platform,
                    window_start=start,
                    window_end=end,
                    last_post_id=str((kol or {}).get("last_post_id") or ""),
                    status="open",
                    error="no posts observed in recovery window",
                )
                payload["gaps"].append({"platform": platform, **item})
    payload["open_gaps"] = store.list_collection_gaps(status="open", limit=1000)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def kol_gap_recover(args: argparse.Namespace) -> None:
    store = _post_store()
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=7)).isoformat() if args.scope == "recent" else "2026-01-01"
    batch_id = f"gap-{args.scope}-{start}-{end}"
    active = [
        item for item in store.list_kols("active")
        if str(item.get("availability_status") or "active") not in {"suspended", "deleted", "paused"}
    ]
    store.create_fetch_batch(
        batch_id,
        batch_kind="recent_recovery" if args.scope == "recent" else "historical_recovery",
        platform="X+Zhihu",
        window_start=start,
        window_end=end,
        strategy_version="kol-collection-v3",
        total_kols=len(active),
    )
    provider = _post_provider("auto")
    aggregate: list[Any] = []
    reset_items = 0
    for platform in ("x", "zhihu"):
        platform_key = platform.casefold()
        fresh_key = f"{batch_id}:{platform_key}:fresh"
        history_key = f"{batch_id}:{platform_key}:history"
        reset_items += store.reset_interrupted_fetch_queue(fresh_key)
        reset_items += store.reset_interrupted_fetch_queue(history_key)
        first = run_post_fetch(
            store,
            provider,
            platform_providers={"zhihu": _zhihu_provider()},
            platforms={platform_key},
            max_count=20 if args.scope == "recent" else 500,
            fresh_first_page=args.scope == "recent",
            reconcile_zhihu=False,
            sleep_seconds=1.0,
            retry_delays=(1.0,),
            batch_key=fresh_key,
            classifier=RuleClassifier(_classification_aliases()),
        )
        aggregate.append(first)
        if args.scope == "recent" and not first.rate_limit_paused and first.queue_pending == 0:
            second = run_post_fetch(
                store,
                provider,
                platform_providers={"zhihu": _zhihu_provider()},
                platforms={platform_key},
                max_count=100,
                reconcile_zhihu=False,
                sleep_seconds=1.0,
                retry_delays=(1.0,),
                batch_key=history_key,
                classifier=RuleClassifier(_classification_aliases()),
            )
            aggregate.append(second)
    successful = sum(item.successful_kols for item in aggregate)
    failed = sum(item.failed_kols for item in aggregate)
    new_posts = sum(item.new_posts for item in aggregate)
    errors = [error for item in aggregate for error in item.errors]
    status = "completed" if not errors or successful else "degraded"
    store.finish_fetch_batch(
        batch_id,
        status=status,
        completed_kols=successful + failed,
        successful_kols=successful,
        failed_kols=failed,
        new_posts=new_posts,
        error="; ".join(errors[:5]),
    )
    print(json.dumps({
        "ok": bool(successful),
        "scope": args.scope,
        "batch_id": batch_id,
        "reset_interrupted_queue_items": reset_items,
        "runs": [item.__dict__ for item in aggregate],
        "coverage": {
            "X": store.collection_coverage(platform="X", window_start=start, window_end=end),
            "Zhihu": store.collection_coverage(platform="Zhihu", window_start=start, window_end=end),
        },
    }, ensure_ascii=False, indent=2))
    if not successful and failed:
        raise SystemExit(2)


def kol_fetch_queue_compact(args: argparse.Namespace) -> None:
    store = _post_store()
    cutoff = datetime.now(SHANGHAI) - timedelta(hours=max(1, args.older_than_hours))
    keys = store.archive_legacy_fetch_batches(cutoff)
    print(json.dumps({"ok": True, "archived_batches": len(keys), "batch_keys": keys}, ensure_ascii=False, indent=2))


def kol_ai_resume(args: argparse.Namespace) -> None:
    store = _post_store()
    completed, failed = classify_pending_with_codex(
        store,
        build_post_classifier(KOL_CLASSIFIER_SCHEMA, ROOT),
        limit=args.limit,
    )
    print(json.dumps({"ok": failed == 0, "completed": completed, "failed": failed}, ensure_ascii=False))


def kol_import(args: argparse.Namespace) -> None:
    store = _post_store()
    if args.platform != "zhihu" or not args.from_linked_profiles:
        raise SystemExit("only --platform zhihu --from-linked-profiles is supported")
    profiles = store.list_digest_author_profiles()
    created = 0
    existing = 0
    imported: list[dict[str, Any]] = []
    for profile in profiles:
        handle = str(profile.get("handle") or "").strip()
        if not handle:
            continue
        kol_id, was_created = store.add_kol(
            str(profile.get("display_name") or handle),
            handle,
            platform="Zhihu",
            profile_url=str(profile.get("profile_url") or ""),
            status="paused",
            tracking_mode="direct_profile",
            domain="Zhihu direct profile",
        )
        store.update_digest_author_profile_status(handle, "paused")
        created += int(was_created)
        existing += int(not was_created)
        kol = store.get_kol(kol_id)
        if kol:
            imported.append(kol)
    print(
        json.dumps(
            {
                "ok": True,
                "platform": "Zhihu",
                "created": created,
                "existing": existing,
                "total": len(imported),
                "items": imported,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def kol_zhihu_onboard(args: argparse.Namespace) -> None:
    store = _post_store()
    zhihu = [
        item for item in store.list_kols()
        if item.get("platform") == "Zhihu" and item.get("tracking_mode") == "direct_profile"
    ]
    paused = [item for item in zhihu if item.get("status") == "paused"]
    active = [item for item in zhihu if item.get("status") == "active"]
    activated: list[dict[str, Any]] = []
    if args.advance:
        for item in paused[: max(1, args.batch_size)]:
            store.update_kol(int(item["id"]), {"status": "active"})
            store.queue_backfill(int(item["id"]), max(1, args.backfill))
            store.update_digest_author_profile_status(str(item["handle"]), "active")
            refreshed = store.get_kol(int(item["id"]))
            if refreshed:
                activated.append(refreshed)
    print(
        json.dumps(
            {
                "ok": True,
                "platform": "Zhihu",
                "active_before": len(active),
                "paused_before": len(paused),
                "activated": activated,
                "active_after": len([
                    item for item in store.list_kols("active")
                    if item.get("platform") == "Zhihu"
                    and item.get("tracking_mode") == "direct_profile"
                ]),
                "remaining_paused": len([
                    item for item in store.list_kols("paused")
                    if item.get("platform") == "Zhihu"
                    and item.get("tracking_mode") == "direct_profile"
                ]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def kol_fallback_mode(args: argparse.Namespace) -> None:
    store = _post_store()
    if not args.set:
        print(json.dumps({"mode": _fallback_mode(), "shadow_rollout": store.shadow_rollout_status()}, ensure_ascii=False, indent=2))
        return
    if args.set == "enabled":
        rollout = store.shadow_rollout_status()
        if not rollout["ready"]:
            print(json.dumps({"ok": False, "mode": _fallback_mode(), "shadow_rollout": rollout}, ensure_ascii=False, indent=2))
            raise SystemExit(2)
    FALLBACK_MODE_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = FALLBACK_MODE_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"mode": args.set, "updated_at": now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, FALLBACK_MODE_STATE)
    print(json.dumps({"ok": True, "mode": args.set}, ensure_ascii=False))


def kol_post_fetch(args: argparse.Namespace) -> None:
    store = _post_store()
    requested = args.backfill or 50
    handles = {
        value.strip().lstrip("@").casefold()
        for value in str(getattr(args, "handles", "") or "").split(",")
        if value.strip()
    }
    result = run_post_fetch(
        store,
        _post_provider(args.provider),
        platform_providers={"zhihu": _zhihu_provider()},
        platforms=None if args.platform == "all" else {args.platform},
        handles=handles or None,
        max_count=requested,
        classifier=RuleClassifier(_classification_aliases()),
        dry_run=args.dry_run,
        batch_key=str(getattr(args, "batch_key", "") or ""),
    )
    codex_completed = codex_failed = 0
    if not args.dry_run and not args.skip_classify:
        codex_completed, codex_failed = classify_pending_with_codex(
            store,
            build_post_classifier(KOL_CLASSIFIER_SCHEMA, ROOT),
            limit=args.classify_limit,
        )
        store.save_fetch_model_result(result.run_id, codex_completed, codex_failed)
    lead_payload: dict[str, Any] = {}
    if not args.dry_run and not getattr(args, "skip_leads", False):
        lead_payload = _extract_leads_to_market(store)
    codex_failure_streak = store.codex_failure_streak()
    payload = {
        **result.__dict__,
        "as_of": args.as_of or date.today().isoformat(),
        "codex_completed": codex_completed,
        "codex_failed": codex_failed,
        "codex_failure_streak": codex_failure_streak,
        "provider": args.provider,
        "dry_run": args.dry_run,
        "fallback_mode": _fallback_mode(),
        "stock_leads": lead_payload,
    }
    if args.notify and not args.alerts_only:
        digest = (
            f"[KOL自动采集] 完成账号 {result.successful_kols}，失败 {result.failed_kols}，"
            f"新增帖子 {result.new_posts}，疑似荐股 {result.candidate_posts}，"
            f"待审核 {result.pending_reviews}，Codex成功 {codex_completed}、失败 {codex_failed}。"
        )
        if result.gap_kols:
            digest += " 数据缺口：" + "、".join("@" + handle for handle in result.gap_kols) + "。"
        if result.errors:
            digest += " 错误：" + "；".join(result.errors[:3])
        payload["notification_sent"] = _send_feishu(digest)
        payload["auth_alert_sent"] = _send_transition_alert(
            "primary_auth_failed",
            result.auth_status == "failed",
            "[KOL自动采集告警] X Cookie 已失效，请在本地工作台系统页更新 auth_token 和 ct0。",
        )
        payload["fallback_alert_sent"] = _send_transition_alert(
            "nitter_fallback_used",
            bool(result.fallback_kols),
            "[KOL自动采集告警] 主采集源不可用，今日已启用本地 Nitter 备用源。",
        )
        payload["all_failed_alert_sent"] = _send_transition_alert(
            "all_providers_failed",
            result.successful_kols == 0 and result.failed_kols > 0,
            "[KOL自动采集告警] 主源与备用源均失败，本次未推进抓取游标。",
        )
        payload["gap_alert_sent"] = _send_transition_alert(
            "gap_detected",
            bool(result.gap_kols),
            "[KOL自动采集告警] 检测到时间线缺口，请在本地工作台检查抓取审计。",
        )
        payload["shadow_alert_sent"] = _send_transition_alert(
            "shadow_fallback_failed",
            bool(result.shadow_failed_kols),
            "[KOL自动采集告警] Nitter shadow 比对失败，请检查备用账号会话和容器状态。",
        )
        payload["codex_alert_sent"] = _send_transition_alert(
            "codex_failure_streak",
            codex_failure_streak >= 3,
            f"[KOL自动采集告警] Codex 已连续 {codex_failure_streak} 次分类运行全部失败，请检查 Codex CLI。",
        )
    if args.alerts_only:
        payload["auth_alert_sent"] = _send_transition_alert(
            "primary_auth_failed",
            result.auth_status == "failed",
            "[KOL采集告警] X会话已失效，请在本地工作台更新凭据。",
        )
        payload["all_failed_alert_sent"] = _send_transition_alert(
            "all_providers_failed",
            result.successful_kols == 0 and result.failed_kols > 0,
            "[KOL采集告警] 本次所有采集源均失败，抓取游标没有推进。",
        )
        payload["gap_alert_sent"] = _send_transition_alert(
            "gap_detected",
            bool(result.gap_kols),
            "[KOL采集告警] 检测到时间线缺口，请在本地工作台检查采集审计。",
        )
        payload["shadow_alert_sent"] = _send_transition_alert(
            "shadow_fallback_failed",
            bool(result.shadow_failed_kols),
            "[KOL采集告警] Nitter影子比对失败，请检查备用会话和容器。",
        )
    print(json.dumps(payload, ensure_ascii=False))
    if result.successful_kols == 0 and result.failed_kols:
        raise SystemExit(2)


def kol_fetch_resume(args: argparse.Namespace) -> None:
    store = _post_store()
    batch_key = args.batch_key or store.latest_pending_fetch_batch(
        "" if args.platform == "all" else args.platform
    )
    if not batch_key:
        print(json.dumps({"ok": True, "status": "nothing_pending"}, ensure_ascii=False))
        return
    result = run_post_fetch(
        store,
        _post_provider(args.provider),
        platform_providers={"zhihu": _zhihu_provider()},
        platforms=None if args.platform == "all" else {args.platform},
        max_count=args.fetch_count,
        classifier=RuleClassifier(_classification_aliases()),
        batch_key=batch_key,
    )
    print(
        json.dumps(
            {"ok": not result.errors, "batch_key": batch_key, **result.__dict__},
            ensure_ascii=False,
            indent=2,
        )
    )
    if result.errors and result.queue_pending == 0:
        raise SystemExit(2)


def kol_nitter_materialize(_: argparse.Namespace) -> None:
    files = materialize_nitter_runtime(NITTER_RUNTIME, NITTER_TEMPLATE, NitterCredentialStore())
    print(
        json.dumps(
            {
                "ok": True,
                "config_path": str(files.config_path),
                "sessions_path": str(files.sessions_path),
            },
            ensure_ascii=False,
        )
    )


def kol_nitter_doctor(_: argparse.Namespace) -> None:
    docker = docker_health()
    nitter = timeline_health(NITTER_URL)
    checks = {
        "ok": False,
        "docker": docker,
        "nitter": nitter,
        "nitter_url": NITTER_URL,
        "nitter_credentials_configured": NitterCredentialStore().configured(),
        "xtf_command": str(XTF_COMMAND) if XTF_COMMAND.exists() else "",
        "xtf_version": xtf_version(XTF_COMMAND) if XTF_COMMAND.exists() else "",
        "config_template": str(NITTER_TEMPLATE),
        "config_template_exists": NITTER_TEMPLATE.exists(),
    }
    checks["ok"] = bool(
        docker["ready"]
        and nitter["ready"]
        and checks["nitter_credentials_configured"]
        and checks["xtf_command"]
        and checks["config_template_exists"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_post_classify(args: argparse.Namespace) -> None:
    store = _post_store()
    aliases = _classification_aliases()
    ocr_completed, ocr_failed = process_pending_with_ocr(
        store,
        _ocr_classifier(),
        RuleClassifier(aliases),
        limit=args.ocr_limit,
    )
    if args.skip_codex:
        store.prepare_model_queue()
        completed = failed = 0
    else:
        try:
            completed, failed = classify_pending_with_codex(
                store,
                build_post_classifier(KOL_CLASSIFIER_SCHEMA, ROOT),
                limit=args.limit,
            )
        except ModelWorkerBusyError:
            print(json.dumps({"ok": True, "skipped": "model_worker_busy"}, ensure_ascii=False))
            return
    leads = _extract_leads_to_market(store)
    print(
        json.dumps(
            {
                "ok": True,
                "degraded": bool(ocr_failed or failed),
                "ocr_completed": ocr_completed,
                "ocr_failed": ocr_failed,
                "codex_completed": completed,
                "codex_failed": failed,
                "stock_leads": leads,
            },
            ensure_ascii=False,
        )
    )


def _recommendation_repair_ids(
    store: KolPostStore,
    *,
    limit: int = 0,
    include_non_candidates: bool = False,
) -> list[str]:
    with store.connect() as db:
        rows = db.execute(
            f"""
            SELECT p.post_id
            FROM posts p
            JOIN classifications c ON c.post_id=p.post_id
            WHERE p.review_status='pending'
              AND ({'1=1' if include_non_candidates else 'c.is_candidate=1'})
              AND (
                  c.draft_generation_status IN ('pending','failed','needs_attention')
                  OR (c.model_status='completed'
                      AND c.content_type='recommendation'
                      AND c.evidence_type='original_pre_event')
              )
            ORDER BY p.posted_at DESC
            """
        ).fetchall()
    values = [str(row[0]) for row in rows]
    return values if limit <= 0 else values[:limit]


def _repair_queue_scope(post: dict[str, Any], review_date: str) -> str:
    start, end = review_window_utc(review_date)
    posted_at = str(post.get("posted_at_utc") or "")
    return "morning" if start <= posted_at < end else "backlog"


def _run_recommendation_rules_repair(
    store: KolPostStore,
    post_ids: list[str],
    *,
    review_date: str,
) -> dict[str, Any]:
    market = _market_store()
    aliases = _classification_aliases()
    classifier = RuleClassifier(aliases)
    repository = RecommendationDraftRepository(store)
    counters = Counter()
    results: list[dict[str, Any]] = []
    for post_id in post_ids:
        post = store.get_post(post_id)
        result = materialize_recommendation_drafts(
            store,
            repository,
            classifier,
            post_id,
            instruments=market.instrument_map(),
            queue_scope=_repair_queue_scope(post, review_date),
            review_date=review_date,
            rules_first=True,
        )
        counters[result["draft_generation_status"]] += 1
        counters["drafts_created"] += int(result["sync"].get("created", 0))
        results.append(
            {
                "post_id": post_id,
                "status": result["draft_generation_status"],
                "drafts": len(result["drafts"]),
                "structured": result["structured"],
            }
        )
    return {"processed": len(post_ids), "counts": dict(counters), "results": results}


def _run_recommendation_ai_repair(
    store: KolPostStore,
    post_ids: list[str],
    *,
    review_date: str,
    max_runtime: float = 240,
) -> dict[str, Any]:
    market = _market_store()
    aliases = _classification_aliases()
    rule_classifier = RuleClassifier(aliases)
    repository = RecommendationDraftRepository(store)
    classifier = DeepSeekPostClassifier(
        KOL_CLASSIFIER_SCHEMA,
        DeepSeekCredentialStore(),
        timeout_seconds=min(45, max(10, max_runtime / 4)),
    )
    counters = Counter()
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    stopped_reason = ""
    started_at = time.monotonic()
    try:
        with store.model_worker():
            recovered = store.recover_interrupted_classification()
            for post_id in post_ids:
                if time.monotonic() - started_at >= max_runtime:
                    stopped_reason = "time_budget_exhausted"
                    break
                post = store.get_post(post_id)
                structured = rule_classifier.classify_structured_text(post)
                stop_after = False
                if structured is not None:
                    store.save_model_classification(
                        post_id,
                        structured,
                        model_name="structured-rules",
                        prompt_version=STRUCTURED_REVIEW_VERSION,
                    )
                else:
                    claimed = store.claim_posts_for_model(1, post_id=post_id, force=True)
                    if not claimed:
                        errors.append(f"{post_id}: not claimable")
                        continue
                    try:
                        payload = classifier.classify(claimed[0])
                        store.save_model_classification(
                            post_id,
                            payload,
                            model_name=classifier.model_name,
                            prompt_version=classifier.prompt_version,
                        )
                    except Exception as exc:
                        error = str(exc)[:2000]
                        store.save_model_classification(
                            post_id,
                            None,
                            model_name=classifier.model_name,
                            prompt_version=classifier.prompt_version,
                            error=error,
                        )
                        errors.append(f"{post_id}: {error}")
                        stop_after = isinstance(exc, ModelProviderUnavailableError)
                result = materialize_recommendation_drafts(
                    store,
                    repository,
                    rule_classifier,
                    post_id,
                    instruments=market.instrument_map(),
                    queue_scope=_repair_queue_scope(post, review_date),
                    review_date=review_date,
                    rules_first=False,
                )
                counters[result["draft_generation_status"]] += 1
                counters["drafts_created"] += int(result["sync"].get("created", 0))
                results.append(
                    {
                        "post_id": post_id,
                        "status": result["draft_generation_status"],
                        "drafts": len(result["drafts"]),
                        "structured": structured is not None,
                    }
                )
                if stop_after:
                    stopped_reason = "provider_unavailable"
                    break
    except ModelWorkerBusyError:
        errors.append("model worker is busy")
    return {
        "processed": len(results),
        "counts": dict(counters),
        "errors": errors,
        "results": results,
        "stopped_reason": stopped_reason,
        "recovered_interrupted": recovered if "recovered" in locals() else {"ocr": 0, "model": 0},
    }


def kol_recommendation_repair(args: argparse.Namespace) -> None:
    store = _post_store()
    review_date = date.today().isoformat()
    if args.doctor:
        with store.connect() as db:
            status_rows = db.execute(
                "SELECT draft_generation_status,COUNT(*) FROM classifications GROUP BY draft_generation_status"
            ).fetchall()
            anomaly_count = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM posts p JOIN classifications c ON c.post_id=p.post_id
                    WHERE p.review_status='pending' AND c.content_type='recommendation'
                      AND c.evidence_type='original_pre_event'
                      AND c.model_status='completed'
                      AND NOT EXISTS(
                          SELECT 1 FROM recommendation_drafts d
                          WHERE d.post_id=p.post_id
                            AND d.status IN ('ready','needs_attention','approved','rejected')
                      )
                    """
                ).fetchone()[0]
            )
        print(json.dumps({
            "ok": True,
            "database": str(store.path),
            "schema": {str(row[0]): int(row[1]) for row in status_rows},
            "recommendation_without_drafts": anomaly_count,
            "repair_candidates": len(_recommendation_repair_ids(store)),
            "full_rules_scan_candidates": len(
                _recommendation_repair_ids(store, include_non_candidates=True)
            ),
        }, ensure_ascii=False, indent=2))
        return

    if args.post_id:
        post_ids = [args.post_id]
    elif args.all or args.pending_ai:
        post_ids = _recommendation_repair_ids(
            store,
            limit=args.limit,
            include_non_candidates=bool(args.all),
        )
    else:
        raise SystemExit("use --doctor, --all, --pending-ai, or --post-id")

    if args.rules_only:
        result = _run_recommendation_rules_repair(store, post_ids, review_date=review_date)
        mode = "rules"
    elif args.pending_ai or args.post_id:
        result = _run_recommendation_ai_repair(
            store,
            post_ids,
            review_date=review_date,
            max_runtime=float(args.max_runtime),
        )
        mode = "ai"
    else:
        result = _run_recommendation_rules_repair(store, post_ids, review_date=review_date)
        mode = "rules"
    result["ok"] = not result.get("errors")
    result["partial"] = bool(result.get("stopped_reason"))
    result["mode"] = mode
    result["review_date"] = review_date
    lead_result = _extract_leads_to_market(store)
    result["stock_leads"] = {
        "processed_posts": lead_result["processed_posts"],
        "created": lead_result["created"],
        "updated": lead_result["updated"],
        "failed": lead_result["failed"],
        "confirmed_count": len(lead_result["confirmed_symbols"]),
        "reconciled_count": len(lead_result["reconciled_symbols"]),
        "queued_count": len(lead_result["queued_symbols"]),
    }
    # A full historical rules scan can touch thousands of posts. Keep the
    # CLI response useful for Hermes and shells without dropping the audit
    # data, which remains in SQLite.
    if len(result.get("results", [])) > 100:
        result["results_sample"] = result["results"][:20]
        result["results_truncated"] = len(result["results"])
        result.pop("results", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


def _review_agent_runner() -> ReviewAgentRunner:
    return ReviewAgentRunner(
        _post_store(),
        KolStore(KOL_ROOT),
        _market_store(),
        classifier=build_post_classifier(KOL_CLASSIFIER_SCHEMA, ROOT),
        ocr_classifier=_ocr_classifier(),
        rule_classifier=RuleClassifier(_classification_aliases()),
    )


def kol_review_agent_doctor(_: argparse.Namespace) -> None:
    store = _post_store()
    repository = ReviewAgentRepository(store)
    summary = repository.summary()
    checks = {
        "ok": True,
        "database": str(store.path),
        "policy_version": summary["settings"]["policy_version"],
        "mode": summary["settings"]["mode"],
        "pending_posts": store.count_pending(),
        "total_decisions": summary["total_decisions"],
        "shadow_days": summary["shadow_days"],
        "agreement": summary["agreement"],
        "activation_ready": summary["activation_ready"],
        "codex_cli": shutil.which("codex") or "",
        "classifier_schema": KOL_CLASSIFIER_SCHEMA.exists(),
        "unlimited_ocr_available": UnlimitedOcrBatchClassifier(
            UNLIMITED_OCR_ROOT, UNLIMITED_OCR_RUNNER
        ).available(),
        "rapid_ocr_available": _ocr_classifier().available(),
        "ocr_provider": "rapidocr",
    }
    checks["ok"] = bool(
        checks["codex_cli"]
        and checks["classifier_schema"]
        and checks["rapid_ocr_available"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_review_agent_run(args: argparse.Namespace) -> None:
    runner = _review_agent_runner()
    result = runner.run(
        mode=args.mode,
        max_runtime_minutes=args.max_runtime,
        max_items=args.max_items,
        post_id=args.post_id or "",
        dry_run=args.dry_run,
    )
    refresh_status = "not_requested"
    if result["mode"] == "enabled" and result["queued_symbols"] and not args.dry_run:
        try:
            refresh = run_post_approval_refresh(
                RUNTIME,
                ROOT,
                result["queued_symbols"],
                as_of=date.today(),
                timeout_seconds=240,
                raise_on_error=True,
            )
            refresh_status = str(refresh["status"])
        except Exception as exc:
            refresh_status = "failed"
            result["errors"].append(f"post-approval refresh: {str(exc)[:1000]}")
    result["refresh_status"] = refresh_status
    result["notification_errors"] = (
        _send_pending_notifications(KolStore(KOL_ROOT)) if not args.dry_run else []
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(2)


def kol_review_agent_report(_: argparse.Namespace) -> None:
    repository = ReviewAgentRepository(_post_store())
    print(json.dumps(repository.summary(), ensure_ascii=False, indent=2))


def kol_morning_pipeline(args: argparse.Namespace) -> None:
    store = _post_store()
    market = _market_store()
    review_date = date.fromisoformat(args.as_of) if args.as_of else date.today()

    def fetcher():
        return run_post_fetch(
            store,
            _post_provider(args.provider),
            platform_providers={"zhihu": _zhihu_provider()},
            platforms=None if args.platform == "all" else {args.platform},
            max_count=args.fetch_count,
            classifier=RuleClassifier(_classification_aliases()),
            batch_key=f"morning:{review_date.isoformat()}:{args.platform}",
            fresh_first_page=True,
        )

    pipeline = MorningPipeline(
        store,
        market,
        rule_classifier=RuleClassifier(_classification_aliases()),
        batch_classifier=DeepSeekBatchPostClassifier(
            KOL_BATCH_CLASSIFIER_SCHEMA,
            DeepSeekCredentialStore(),
        ),
        ocr_classifier=_ocr_classifier(),
        fetcher=fetcher,
        active_kol_count=sum(
            str(item.get("availability_status") or "active") not in {"suspended", "deleted", "protected", "paused"}
            for item in store.list_kols("active", None if args.platform == "all" else args.platform)
        ),
    )
    repository = RecommendationDraftRepository(store)
    repository.interrupt_stale_runs()
    result = pipeline.run(
        as_of=review_date,
        fetch=not args.skip_fetch,
        backlog_limit=args.backlog_limit,
        max_runtime_minutes=args.max_runtime,
        phase=args.phase,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"] or result.get("alert_required"):
        raise SystemExit(2)


def kol_morning_orchestrate(args: argparse.Namespace) -> None:
    store = _post_store()
    market = _market_store()
    review_date = date.today()

    def fetcher():
        return run_post_fetch(
            store,
            _post_provider(args.provider),
            platform_providers={"zhihu": _zhihu_provider()},
            platforms=None if args.platform == "all" else {args.platform},
            max_count=args.fetch_count,
            classifier=RuleClassifier(_classification_aliases()),
            batch_key=f"morning:{review_date.isoformat()}:{args.platform}",
            fresh_first_page=True,
        )

    pipeline = MorningPipeline(
        store,
        market,
        rule_classifier=RuleClassifier(_classification_aliases()),
        batch_classifier=DeepSeekBatchPostClassifier(
            KOL_BATCH_CLASSIFIER_SCHEMA,
            DeepSeekCredentialStore(),
        ),
        ocr_classifier=_ocr_classifier(),
        fetcher=fetcher,
        active_kol_count=sum(
            str(item.get("availability_status") or "active") not in {"suspended", "deleted", "protected", "paused"}
            for item in store.list_kols("active", None if args.platform == "all" else args.platform)
        ),
    )
    repository = RecommendationDraftRepository(store)
    interrupted = repository.interrupt_stale_runs()

    def run_phase(phase: str, runtime: float) -> dict[str, Any]:
        return pipeline.run(
            as_of=review_date,
            fetch=True,
            backlog_limit=0,
            max_runtime_minutes=runtime,
            phase=phase,
        )

    result = MorningOrchestrator(runner=run_phase).run()
    result["interrupted_stale_runs"] = interrupted
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"] or any(item.get("alert_required") for item in result["results"]):
        raise SystemExit(2)


def kol_morning_migrate(_: argparse.Namespace) -> None:
    store = _post_store()
    result = RecommendationDraftRepository(store).migrate_legacy_review_queue()
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


def kol_morning_doctor(_: argparse.Namespace) -> None:
    store = _post_store()
    repository = RecommendationDraftRepository(store)
    checks = {
        "ok": True,
        "database": str(store.path),
        "schema": KOL_BATCH_CLASSIFIER_SCHEMA.exists(),
        "codex_cli": shutil.which("codex") or "",
        "deepseek_configured": DeepSeekCredentialStore().configured(),
        "ocr_available": _ocr_classifier().available(),
        "ocr_provider": "rapidocr",
        "recent_runs": repository.recent_morning_runs(3),
        "today": repository.morning_summary(date.today().isoformat()),
    }
    checks["ok"] = bool(
        checks["schema"]
        and (checks["codex_cli"] or checks["deepseek_configured"])
        and checks["ocr_available"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_ui_doctor(_: argparse.Namespace) -> None:
    checks = {
        "ok": True,
        "frontend_dist": str(KOL_UI_DIST),
        "frontend_exists": (KOL_UI_DIST / "index.html").exists(),
        "fastapi": dependency_available("fastapi"),
        "uvicorn": dependency_available("uvicorn"),
        "database": str(KOL_POST_DB),
        "database_exists": KOL_POST_DB.exists(),
        "url": "http://127.0.0.1:8123",
    }
    checks["ok"] = bool(checks["frontend_exists"] and checks["fastapi"] and checks["uvicorn"])
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def _market_store() -> FoundationBackedMarketStore:
    return FoundationBackedMarketStore(MarketStore(MARKET_ROOT), FOUNDATION_ROOT)


def _resolve_foundation_as_of(value: str | None) -> date:
    """Resolve an operator date without ever treating an open session as complete."""
    if value and value != "auto":
        return date.fromisoformat(value)
    now = datetime.now(SHANGHAI)
    candidate = now.date()
    if now.time() < datetime_time(16, 0):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _foundation_status() -> dict[str, Any]:
    reader = FoundationMarketReader(FOUNDATION_ROOT)
    health = reader.health()
    if not health.get("ok"):
        return {**health, "status": "unavailable", "quality_status": "failed"}
    release = reader.release()
    required = {
        "daily_raw",
        "daily_adjusted",
        "instruments",
        "trading_calendar",
    }
    missing: list[str] = []
    paths: dict[str, str] = {}
    for dataset in sorted(required):
        try:
            path = reader.dataset_path(dataset, release)
            paths[dataset] = str(path)
        except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
            missing.append(f"{dataset}: {error}")
    benchmark_available = False
    benchmark_error = ""
    try:
        benchmark_available = not reader.read_daily("000300", adjustment="raw").empty
    except Exception as error:
        benchmark_error = str(error)
    quality_status = "valid" if not missing else "failed"
    if not benchmark_available and not missing:
        quality_status = "partial"
    coverage = reader.coverage(str(release.get("as_of") or ""))
    if not coverage.get("complete") and not missing:
        quality_status = "partial"
    try:
        disk = shutil.disk_usage(FOUNDATION_ROOT)
        free_bytes = int(disk.free)
    except OSError:
        free_bytes = None
    return {
        **health,
        "status": "ready" if quality_status == "valid" else quality_status,
        "quality_status": quality_status,
        "required_datasets": sorted(required),
        "missing_required_datasets": missing,
        "dataset_paths": paths,
        "benchmark_available": benchmark_available,
        "benchmark_error": benchmark_error,
        "coverage": coverage,
        "coverage_complete": bool(coverage.get("complete")),
        "free_bytes": free_bytes,
    }


def data_foundation_doctor(_: argparse.Namespace) -> None:
    payload = _foundation_status()
    payload["ok"] = bool(payload.get("ok") and not payload.get("missing_required_datasets"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def _run_foundation_refresh(target: date) -> dict[str, Any]:
    """Run the foundation publisher with a hard timeout and no partial promotion."""
    python = FOUNDATION_REPO / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return {"ok": False, "status": "environment_missing", "error": str(python)}
    # A full BaoStock gap repair may cover the active universe sequentially.
    # Keep the operator bounded, but allow the planned 30-minute window.
    timeout = float(os.environ.get("ADF_REFRESH_TIMEOUT_SECONDS", "1800"))
    command = [
        str(python),
        "-m",
        "ashare_data_foundation.cli",
        "--data-root",
        str(FOUNDATION_ROOT),
        "refresh",
        "--as-of",
        target.isoformat(),
        "--primary",
        os.environ.get("ADF_PRIMARY_PROVIDER", "baostock"),
        "--workers",
        os.environ.get("ADF_WORKERS", "8"),
        "--resume",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(FOUNDATION_REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "timed_out",
            "timeout_seconds": timeout,
            "command": command[:6] + ["..."],
        }
    output = (completed.stdout or completed.stderr or "").strip()
    return {
        "ok": completed.returncode == 0,
        "status": "published" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "output_tail": output[-2000:],
    }


def kol_data_refresh(args: argparse.Namespace) -> None:
    """Refresh the shared release, then update every KOL consumer from one pointer."""
    lock_path = KOL_ROOT / "foundation-refresh.lock"
    lock = FileLock(str(lock_path), timeout=1)
    try:
        lock.acquire()
    except Timeout:
        print(json.dumps({"ok": True, "status": "already_running", "lock": str(lock_path)}))
        return
    try:
        target = _resolve_foundation_as_of(args.as_of)
        before = _foundation_status()
        before_as_of = str(before.get("as_of") or "")
        refresh = {"ok": True, "status": "not_needed"}
        refresh_required = bool(
            before_as_of
            and (
                date.fromisoformat(before_as_of) < target
                or before.get("coverage_complete") is False
            )
        )
        if refresh_required:
            refresh = _run_foundation_refresh(target)
        after = _foundation_status()
        effective_text = str(after.get("as_of") or before_as_of)
        steps: list[dict[str, Any]] = [{"step": "foundation_before", **before}, {"step": "foundation_refresh", **refresh}]
        if effective_text:
            effective = min(target, date.fromisoformat(effective_text))
        else:
            effective = target
        update = {
            "ok": False,
            "status": "skipped",
            "reason": "foundation release unavailable",
        }
        if (
            after.get("ok")
            and effective_text
            and (not refresh_required or refresh.get("ok"))
        ):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "kol-update",
                "--as-of",
                effective.isoformat(),
            ]
            if args.notify:
                command.append("--notify")
            if args.dry_run:
                command.append("--dry-run")
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=float(os.environ.get("KOL_UPDATE_TIMEOUT_SECONDS", "1800")),
                    check=False,
                )
                update = {
                    "ok": completed.returncode == 0,
                    "status": "updated" if completed.returncode == 0 else "failed",
                    "effective_as_of": effective.isoformat(),
                    "output_tail": (completed.stdout or completed.stderr or "")[-4000:],
                }
            except subprocess.TimeoutExpired:
                update = {
                    "ok": False,
                    "status": "timed_out",
                    "effective_as_of": effective.isoformat(),
                }
        technical = {
            "ok": False,
            "status": "skipped",
            "reason": "returns update did not complete",
        }
        if update.get("ok") and not args.dry_run:
            context_result = _backfill_event_contexts(
                _market_store(),
                KolStore(KOL_ROOT),
                stale_only=True,
            )
            technical = {
                "ok": bool(context_result.get("ok")),
                "status": "updated" if context_result.get("ok") else "failed",
                "created": len(context_result.get("created", [])),
                "updated": len(context_result.get("updated", [])),
                "skipped": len(context_result.get("skipped", [])),
                "pending": len(context_result.get("pending", [])),
                "errors": context_result.get("errors", []),
            }
        elif args.dry_run:
            technical = {"ok": True, "status": "dry_run"}
        steps.extend([
            {"step": "foundation_after", **after},
            {"step": "kol_update", **update},
            {"step": "technical_context", **technical},
        ])
        payload = {
            "ok": bool(
                after.get("ok")
                and update.get("ok")
                and technical.get("ok")
                and (not refresh_required or refresh.get("ok"))
            ),
            "status": (
                "completed"
                if update.get("ok") and technical.get("ok") and (not refresh_required or refresh.get("ok"))
                else "degraded"
            ),
            "requested_as_of": target.isoformat(),
            "effective_as_of": effective.isoformat(),
            "foundation_release_id": after.get("release_id") or before.get("release_id", ""),
            "steps": steps,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not payload["ok"]:
            raise SystemExit(2)
    finally:
        lock.release()


def _backfill_event_contexts(
    market_store: MarketStore,
    event_store: KolStore,
    *,
    event_ids: set[str] | None = None,
    symbols: set[str] | None = None,
    force: bool = False,
    stale_only: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "feature_version": FEATURE_VERSION,
        "created": [],
        "updated": [],
        "skipped": [],
        "pending": [],
        "errors": [],
    }
    events = [
        event
        for event in event_store.load_events()
        if event.status in {"active", "completed"}
        and (event_ids is None or event.event_id in event_ids)
        and (symbols is None or event.symbol in symbols)
    ]
    foundation_release_id = ""
    foundation = getattr(market_store, "foundation", None)
    if foundation is not None:
        try:
            foundation_release_id = str(foundation.release().get("release_id", ""))
        except Exception:
            foundation_release_id = ""
    for event in events:
        expected_trade_date: date | None = None
        try:
            input_hash = event_context_input_hash(event.symbol, event.posted_at)
            existing = market_store.get_event_technical_context(event.event_id, input_hash=input_hash)
            if (
                stale_only
                and existing is not None
                and str(existing.get("foundation_release_id") or "") == foundation_release_id
                and str(existing.get("status") or "") not in {"pending", "failed"}
            ):
                result["skipped"].append(event.event_id)
                continue
            expected_trade_date = market_store.latest_open_date(event_context_cutoff(event.posted_at))
            frame = market_store.read_daily(event.symbol, adjustment="qfq")
            context = compute_event_technical_context(
                event_id=event.event_id,
                symbol=event.symbol,
                posted_at=event.posted_at,
                qfq_prices=frame,
                expected_trade_date=expected_trade_date,
                foundation_release_id=foundation_release_id,
            )
            changed = market_store.save_event_technical_context(
                context.to_record(),
                force=force,
            )
            bucket = "updated" if existing and changed else "created" if changed else "skipped"
            result[bucket].append(event.event_id)
            if context.status == "pending":
                result["pending"].append(event.event_id)
        except Exception as exc:
            try:
                failed = failed_event_technical_context(
                    event_id=event.event_id,
                    symbol=event.symbol,
                    posted_at=event.posted_at,
                    expected_trade_date=expected_trade_date,
                    error=str(exc),
                    foundation_release_id=foundation_release_id,
                )
                market_store.save_event_technical_context(failed.to_record())
            except Exception:
                pass
            result["errors"].append({"event_id": event.event_id, "error": str(exc)[:1000]})
    result["processed"] = len(events)
    result["ok"] = not result["errors"]
    return result


def kol_context_doctor(_: argparse.Namespace) -> None:
    market = _market_store()
    events = [event for event in KolStore(KOL_ROOT).load_events() if event.status in {"active", "completed"}]
    current = []
    pending = []
    failed = []
    for event in events:
        value = market.get_event_technical_context(
            event.event_id,
            input_hash=event_context_input_hash(event.symbol, event.posted_at),
        )
        if value is None or value.get("status") == "pending":
            pending.append(event.event_id)
        if value is not None:
            current.append(value)
            if value.get("status") == "failed":
                failed.append({"event_id": event.event_id, "error": value.get("error", "")})
    with market.connect() as db:
        schema_version = int(db.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)
    payload = {
        "ok": schema_version >= 4 and not failed,
        "feature_version": FEATURE_VERSION,
        "schema_version": schema_version,
        "formal_events": len(events),
        "contexts": len(current),
        "complete": sum(value.get("status") == "complete" for value in current),
        "partial": sum(value.get("status") == "partial" for value in current),
        "pending_event_ids": pending,
        "failed": failed,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_context_backfill(args: argparse.Namespace) -> None:
    event_ids = {args.event_id} if args.event_id else None
    event_store = KolStore(KOL_ROOT)
    if event_ids and not any(event.event_id in event_ids for event in event_store.load_events()):
        raise SystemExit(f"event not found: {args.event_id}")
    payload = _backfill_event_contexts(
        _market_store(),
        event_store,
        event_ids=event_ids,
        force=args.force,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_event_data_doctor(_: argparse.Namespace) -> None:
    event_store = KolStore(KOL_ROOT)
    market_store = _market_store()
    service = EventDossierService(_post_store(), event_store, market_store)
    events = [event for event in event_store.load_events() if event.status in {"active", "completed"}]
    snapshots = market_store.list_event_dossier_snapshots()
    latest: dict[str, dict[str, Any]] = {}
    for item in snapshots:
        event_id = str(item.get("event_id") or "")
        if event_id and event_id not in latest:
            latest[event_id] = item
    status_counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    for event in events:
        snapshot = latest.get(event.event_id)
        if snapshot:
            snapshot_status = str(snapshot.get("status") or "unknown")
            status_counts[snapshot_status] += 1
            if snapshot_status == "failed":
                errors.append({"event_id": event.event_id, "error": "latest dossier snapshot status=failed"})
            continue
        try:
            dossier_status = str(service.build(event.event_id).get("status") or "unknown")
            status_counts[dossier_status] += 1
            if dossier_status == "failed":
                errors.append({"event_id": event.event_id, "error": "dossier status=failed"})
        except Exception as exc:
            status_counts["failed"] += 1
            errors.append({"event_id": event.event_id, "error": str(exc)[:1000]})
    with market_store.connect() as db:
        schema_version = int(db.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)
    payload = {
        "ok": schema_version >= 7 and not errors,
        "schema_version": schema_version,
        "formal_events": len(events),
        "snapshots": len(snapshots),
        "status_counts": dict(status_counts),
        "missing_snapshot_event_ids": [event.event_id for event in events if event.event_id not in latest],
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_event_data_backfill(args: argparse.Namespace) -> None:
    event_store = KolStore(KOL_ROOT)
    market_store = _market_store()
    service = EventDossierService(_post_store(), event_store, market_store)
    wanted = {args.event_id} if args.event_id else None
    events = [
        event for event in event_store.load_events()
        if event.status in {"active", "completed"}
        and (wanted is None or event.event_id in wanted)
    ]
    if wanted and not events:
        raise SystemExit(f"event not found: {args.event_id}")
    snapshots: dict[str, dict[str, Any]] = {}
    for item in market_store.list_event_dossier_snapshots():
        event_id = str(item.get("event_id") or "")
        if event_id and event_id not in snapshots:
            snapshots[event_id] = item
    result: dict[str, Any] = {"processed": 0, "created": [], "skipped": [], "errors": []}
    for event in events:
        if args.missing_only and event.event_id in snapshots and snapshots[event.event_id].get("status") not in {"failed", "pending"}:
            result["skipped"].append(event.event_id)
            continue
        result["processed"] += 1
        try:
            dossier = service.refresh(event.event_id)
            result["created"].append({"event_id": event.event_id, "status": dossier["status"]})
        except Exception as exc:
            result["errors"].append({"event_id": event.event_id, "error": str(exc)[:1000]})
    result["ok"] = not result["errors"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


def _method_research_service(
    *,
    with_market_providers: bool,
    with_ai: bool,
) -> tuple[EventMethodResearchService, FreeStockDBMarketProvider | None]:
    provider = FreeStockDBMarketProvider() if with_market_providers else None
    interpreter = (
        build_event_research_interpreter(
            EVENT_RESEARCH_SCHEMA,
            ROOT,
            deepseek_credentials=DeepSeekCredentialStore(),
        )
        if with_ai
        else None
    )
    return (
        EventMethodResearchService(
            KolStore(KOL_ROOT),
            _market_store(),
            cross_section_provider=provider,
            minute_provider=provider,
            interpreter=interpreter,
            cache_market_data=True,
        ),
        provider,
    )


def _exclusive_method_research(command):
    @wraps(command)
    def wrapped(args):
        EVENT_RESEARCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(EVENT_RESEARCH_LOCK), timeout=1)
        try:
            with lock:
                return command(args)
        except Timeout as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "busy",
                        "error": "another event method research run is active",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(3) from exc

    return wrapped


def kol_method_research_doctor(_: argparse.Namespace) -> None:
    service, provider = _method_research_service(
        with_market_providers=False,
        with_ai=True,
    )
    try:
        payload = service.doctor()
        interpreter = service.interpreter
        primary_available = DeepSeekCredentialStore().configured()
        payload["ai"] = {
            "primary": str(interpreter.model_name) if interpreter else "",
            "prompt_version": (
                str(interpreter.prompt_version) if interpreter else ""
            ),
            "primary_available": primary_available,
            "backup": "",
            "backup_configured": False,
            "automatic_codex_fallback": False,
        }
        payload["ok"] = bool(payload["ok"] and primary_available)
    finally:
        if provider is not None:
            provider.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if not payload["ok"]:
        raise SystemExit(2)


def _selected_formal_events(
    event_store: KolStore,
    *,
    event_id: str | None,
) -> list[EventRecord]:
    events = [
        event
        for event in event_store.load_events()
        if event.status in {"active", "completed"}
        and (not event_id or event.event_id == event_id)
    ]
    if event_id and not events:
        raise SystemExit(f"event not found: {event_id}")
    return events


def _run_method_ai_batches(
    service: EventMethodResearchService,
    *,
    event_ids: list[str],
    max_events: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "processed": 0,
        "created": [],
        "skipped": [],
        "errors": [],
        "recovered_batches": [],
    }
    eligible = list(dict.fromkeys(event_ids))
    blocked: set[str] = set()
    attempted: set[str] = set()
    limit = max_events if max_events > 0 else len(eligible)

    def event_ids_from(value: dict[str, Any]) -> list[str]:
        ids = [
            str(item.get("event_id") or "")
            for key in ("created", "failed")
            for item in value.get(key) or []
            if isinstance(item, dict)
        ]
        for error in value.get("errors") or []:
            if isinstance(error, dict):
                ids.extend(str(item) for item in error.get("event_ids") or [])
                if error.get("event_id"):
                    ids.append(str(error["event_id"]))
        return list(dict.fromkeys(item for item in ids if item))

    while len(attempted) < limit:
        available = [event_id for event_id in eligible if event_id not in blocked]
        if not available:
            break
        batch = service.interpret_pending(
            event_ids=available,
            max_items=min(5, limit - len(attempted)),
        )
        processed = int(batch.get("processed") or 0)
        result["skipped"].extend(batch.get("skipped") or [])
        if processed == 0:
            break
        batch_ids = event_ids_from(batch)
        if not batch_ids:
            result["errors"].append(
                {"error": "AI batch did not report its processed event ids"}
            )
            break
        batch_created = list(batch.get("created") or [])
        batch_created_ids = {
            str(item.get("event_id") or "")
            for item in batch_created
            if isinstance(item, dict) and item.get("event_id")
        }
        result["created"].extend(batch_created)
        attempted.update(batch_created_ids)
        if batch.get("ok"):
            attempted.update(batch_ids)
            print(
                f"event-research-ai {len(attempted)}/{limit} "
                f"ready={len(result['created'])} failed={len(blocked)}",
                file=sys.stderr,
                flush=True,
            )
            continue

        recovered: list[str] = list(batch_created_ids)
        failed: list[str] = []
        for event_id in (
            value for value in batch_ids if value not in batch_created_ids
        ):
            single = service.interpret_pending(
                event_ids=[event_id],
                max_items=1,
            )
            attempted.add(event_id)
            result["skipped"].extend(single.get("skipped") or [])
            if single.get("ok") and single.get("created"):
                result["created"].extend(single["created"])
                recovered.append(event_id)
                continue
            blocked.add(event_id)
            failed.append(event_id)
            result["errors"].extend(single.get("errors") or [])
        result["recovered_batches"].append(
            {
                "event_ids": batch_ids,
                "recovered": recovered,
                "failed": failed,
                "batch_errors": batch.get("errors") or [],
            }
        )
        print(
            f"event-research-ai {len(attempted)}/{limit} "
            f"ready={len(result['created'])} failed={len(blocked)}",
            file=sys.stderr,
            flush=True,
        )
    result["processed"] = len(attempted)
    result["ok"] = not result["errors"]
    result["skipped"] = list(dict.fromkeys(result["skipped"]))
    return result


@_exclusive_method_research
def kol_method_research_backfill(args: argparse.Namespace) -> None:
    service, provider = _method_research_service(
        with_market_providers=not args.skip_cross_section or args.with_minute,
        with_ai=args.with_ai,
    )
    event_store = service.event_store
    events = _selected_formal_events(event_store, event_id=args.event_id)
    result: dict[str, Any] = {
        "ok": True,
        "formal_events": len(events),
        "processed": 0,
        "created": [],
        "skipped": [],
        "errors": [],
        "ai": None,
    }
    try:
        for event in events:
            existing = service.market_store.get_event_method_research(event.event_id)
            if existing and args.missing_only and not args.force:
                result["skipped"].append(event.event_id)
                continue
            result["processed"] += 1
            print(
                f"event-research {result['processed']}/{len(events)} {event.event_id}",
                file=sys.stderr,
                flush=True,
            )
            try:
                value = service.refresh(
                    event.event_id,
                    fetch_cross_section=not args.skip_cross_section,
                    fetch_minute=args.with_minute,
                )
                result["created"].append(
                    {
                        "event_id": event.event_id,
                        "snapshot_id": value["snapshot_id"],
                        "created": value["created"],
                        "status": value["research"]["status"],
                        "fetch": value["fetch"],
                    }
                )
            except Exception as exc:
                result["errors"].append(
                    {"event_id": event.event_id, "error": str(exc)[:2000]}
                )
        if args.with_ai:
            result["ai"] = _run_method_ai_batches(
                service,
                event_ids=[event.event_id for event in events],
                max_events=args.max_ai,
            )
            result["errors"].extend(result["ai"].get("errors") or [])
    finally:
        if provider is not None:
            provider.close()
    result["ok"] = not result["errors"]
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["ok"]:
        raise SystemExit(2)


@_exclusive_method_research
def kol_method_research_run(args: argparse.Namespace) -> None:
    service, provider = _method_research_service(
        with_market_providers=True,
        with_ai=not args.skip_ai,
    )
    events = _selected_formal_events(service.event_store, event_id=args.event_id)
    pending = [
        event
        for event in events
        if service.market_store.get_event_method_research(event.event_id) is None
        or not service.has_ready_interpretation(
            event.event_id,
            str(
                (
                    service.market_store.get_event_method_research(
                        event.event_id
                    )
                    or {}
                ).get("snapshot_id")
                or ""
            ),
        )
    ][: max(1, args.max_events)]
    result: dict[str, Any] = {
        "ok": True,
        "selected": len(pending),
        "objective": [],
        "ai": None,
        "errors": [],
    }
    try:
        for event in pending:
            print(
                f"event-research {len(result['objective']) + len(result['errors']) + 1}/{len(pending)} {event.event_id}",
                file=sys.stderr,
                flush=True,
            )
            try:
                value = service.refresh(
                    event.event_id,
                    fetch_cross_section=True,
                    fetch_minute=args.with_minute,
                )
                result["objective"].append(
                    {
                        "event_id": event.event_id,
                        "snapshot_id": value["snapshot_id"],
                        "status": value["research"]["status"],
                    }
                )
            except Exception as exc:
                result["errors"].append(
                    {"event_id": event.event_id, "error": str(exc)[:2000]}
                )
        if not args.skip_ai:
            result["ai"] = _run_method_ai_batches(
                service,
                event_ids=[event.event_id for event in pending],
                max_events=len(pending),
            )
            result["errors"].extend(result["ai"].get("errors") or [])
    finally:
        if provider is not None:
            provider.close()
    result["ok"] = not result["errors"]
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["ok"]:
        raise SystemExit(2)


def _classification_aliases() -> dict[str, str]:
    aliases = load_stock_aliases(WATCHLIST, KOL_ROOT / "events.csv")
    try:
        for symbol, instrument in _market_store().instrument_map().items():
            name = str(instrument.get("name") or "").strip()
            if name:
                aliases.setdefault(symbol, name)
    except Exception:
        # Classification remains usable before the market catalog is initialized.
        pass
    return aliases


def _instrument_type(symbol: str) -> str:
    if symbol == "000300":
        return "index"
    if symbol.startswith(("1", "5")):
        return "etf"
    return "stock"


def _exchange(symbol: str, instrument_type: str | None = None) -> str:
    if (instrument_type or _instrument_type(symbol)) == "index" and symbol == "000300":
        return "SH"
    if symbol.startswith(("4", "8", "92")):
        return "BJ"
    return "SH" if symbol.startswith(("5", "6", "9")) else "SZ"


def _seed_market_instruments(store: MarketStore) -> int:
    seeded: dict[str, Instrument] = {}
    for row in read_watchlist():
        symbol = row["symbol"]
        kind = _instrument_type(symbol)
        seeded[symbol] = Instrument(
            symbol,
            row.get("name") or symbol,
            kind,
            _exchange(symbol, kind),
            lifecycle="pinned",
            source="watchlist",
        )
    seeded["000300"] = Instrument(
        "000300", "沪深300", "index", "SH", lifecycle="pinned", source="benchmark"
    )
    event_store = KolStore(KOL_ROOT)
    for event in event_store.load_events():
        if not event.symbol or event.status in {"excluded", "archived"}:
            continue
        kind = _instrument_type(event.symbol)
        posted = event.posted_at[:10] if event.posted_at else ""
        seeded[event.symbol] = Instrument(
            event.symbol,
            event.security_name or event.symbol,
            kind,
            _exchange(event.symbol, kind),
            lifecycle="pinned" if event.status == "active" else "tracking",
            source="kol_event",
            last_mentioned_at=posted,
        )
    try:
        for position in PortfolioStore(PORTFOLIO_ROOT).summary()["positions"]:
            symbol = position["symbol"]
            is_open = position["status"] == "open"
            current = seeded.get(symbol)
            if not is_open and current and current.lifecycle == "pinned":
                continue
            kind = _instrument_type(symbol)
            seeded[symbol] = Instrument(
                symbol,
                position["security_name"] or symbol,
                kind,
                _exchange(symbol, kind),
                lifecycle="pinned" if is_open else "tracking",
                source="portfolio_holding" if is_open else "portfolio_closed",
                first_seen_at=position["first_transaction_at"][:10],
                last_mentioned_at=position["last_transaction_at"][:10],
            )
    except (OSError, ValueError):
        # Market initialization remains usable if a manually edited ledger is invalid.
        pass
    for instrument in seeded.values():
        store.upsert_instrument(instrument)
    return len(seeded)


def _extract_leads_to_market(post_store: KolPostStore | None = None) -> dict[str, Any]:
    posts = post_store or _post_store()
    market = _market_store()
    _seed_market_instruments(market)
    result = extract_stock_leads(
        posts,
        instruments=market.instrument_map(),
        aliases=load_stock_aliases(WATCHLIST, KOL_ROOT / "events.csv"),
    )
    reconciled = reconcile_exact_stock_leads(posts, market.instrument_map())
    confirmed_leads: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = posts.list_stock_leads(status="confirmed", limit=500, offset=offset)
        if not batch:
            break
        confirmed_leads.extend(batch)
        offset += len(batch)
    confirmed_symbols = sorted(
        set(result.confirmed_symbols)
        | set(reconciled)
        | {str(lead["symbol"]) for lead in confirmed_leads}
    )
    queued: list[str] = []
    for symbol in confirmed_symbols:
        leads = posts.list_stock_leads(status="confirmed", symbol=symbol, limit=1)
        if not leads or market.get_instrument(symbol) is None:
            continue
        try:
            mentioned = datetime.fromisoformat(str(leads[0]["posted_at"]).replace("Z", "+00:00")).date()
        except ValueError:
            mentioned = date.today()
        market.touch_mention(symbol, mentioned)
        market.enqueue_sync(symbol, reason=f"kol_lead:{leads[0]['post_id']}")
        queued.append(symbol)
    # Keep CLI output bounded. The full lead rows remain queryable in SQLite;
    # repair commands should report counts and affected symbols only.
    return {
        "processed_posts": result.processed_posts,
        "created": result.created,
        "updated": result.updated,
        "confirmed_symbols": result.confirmed_symbols,
        "failed": result.failed,
        "reconciled_symbols": reconciled,
        "queued_symbols": sorted(set(queued)),
    }


def kol_leads_extract(_: argparse.Namespace) -> None:
    print(json.dumps(_extract_leads_to_market(), ensure_ascii=False, indent=2))


def market_init(args: argparse.Namespace) -> None:
    store = _market_store()
    seeded = _seed_market_instruments(store)
    imported: list[dict[str, Any]] = []
    legacy_already_imported = any(
        run["provider"] == "legacy_import" for run in store.recent_runs(limit=1000)
    )
    if not args.skip_legacy and not legacy_already_imported and PRICE_DIR.exists():
        for path in sorted(PRICE_DIR.glob("*_baostock.csv")):
            symbol = path.name[:6]
            if store.get_instrument(symbol) is None:
                kind = _instrument_type(symbol)
                store.upsert_instrument(
                    Instrument(symbol, symbol, kind, _exchange(symbol, kind), lifecycle="tracking", source="legacy")
                )
            import pandas as pd  # type: ignore

            frame = pd.read_csv(path, encoding="utf-8-sig")

            class LegacyProvider:
                name = "legacy_import"

                def fetch_daily(self, symbol, instrument_type, start, end, adjustment):
                    return frame.copy()

            dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
            if dates.empty:
                continue
            result = sync_daily_bars(
                store,
                LegacyProvider(),
                symbol,
                dates.min().date(),
                dates.max().date(),
                adjustment="qfq",
            )
            imported.append(result.__dict__)
    for instrument in store.list_instruments():
        if instrument["lifecycle"] in {"pinned", "tracking"}:
            store.enqueue_sync(
                instrument["symbol"],
                start=date(2024, 1, 1),
                end=date.today(),
                reason="market_init",
            )
    print(
        json.dumps(
            {"ok": True, "root": str(MARKET_ROOT), "seeded": seeded, "legacy_imports": imported},
            ensure_ascii=False,
            indent=2,
        )
    )


def market_doctor(_: argparse.Namespace) -> None:
    checks: dict[str, Any] = {
        "ok": True,
        "root": str(MARKET_ROOT),
        "duckdb": dependency_available("duckdb"),
        "pyarrow": dependency_available("pyarrow"),
        "pandas": dependency_available("pandas"),
        "baostock": dependency_available("baostock"),
        "akshare": dependency_available("akshare"),
    }
    freestockdb = FreeStockDBMarketProvider()
    try:
        checks["freestockdb"] = freestockdb.health()
    except Exception as exc:
        checks["freestockdb"] = {"ok": False, "error": str(exc)}
    finally:
        freestockdb.close()
    try:
        store = _market_store()
        _seed_market_instruments(store)
        checks["health"] = store.health()
    except Exception as exc:
        checks["ok"] = False
        checks["error"] = str(exc)
    checks["ok"] = bool(
        checks["ok"] and checks["duckdb"] and checks["pyarrow"] and checks["pandas"] and checks["baostock"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def _write_purchased_daily_manifest(report: dict[str, Any]) -> str:
    destination = MARKET_ROOT / "manifests" / f"{PURCHASED_DAILY_PROVIDER}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    return str(destination)


def market_purchased_daily_doctor(args: argparse.Namespace) -> None:
    expected = date.fromisoformat(args.expected_date) if args.expected_date else None
    report = audit_purchased_daily_archive(Path(args.root), expected_date=expected)
    report["manifest_path"] = _write_purchased_daily_manifest(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(2)


def _purchased_event_symbols() -> set[str]:
    symbols: set[str] = set()
    for event in KolStore(KOL_ROOT).load_events():
        symbol = str(event.symbol or "").strip()
        if re.fullmatch(r"\d{6}", symbol) and symbol != "000000":
            symbols.add(symbol)
    return symbols


def _purchased_research_symbols() -> set[str]:
    symbols = _purchased_event_symbols()
    for row in read_watchlist():
        symbol = str(row.get("symbol") or "").strip()
        if re.fullmatch(r"\d{6}", symbol) and symbol != "000000":
            symbols.add(symbol)
    try:
        for position in PortfolioStore(PORTFOLIO_ROOT).summary().get("positions", []):
            symbol = str(position.get("symbol") or "").strip()
            if re.fullmatch(r"\d{6}", symbol) and symbol != "000000":
                symbols.add(symbol)
    except (OSError, ValueError):
        pass
    return symbols


def market_purchased_daily_import(args: argparse.Namespace) -> None:
    root = Path(args.root)
    expected = date.fromisoformat(args.expected_date) if args.expected_date else None
    audit = audit_purchased_daily_archive(root, expected_date=expected)
    audit["manifest_path"] = _write_purchased_daily_manifest(audit)
    if not audit["ok"]:
        print(json.dumps({"ok": False, "audit": audit}, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    if args.from_research_pool:
        symbols = _purchased_research_symbols()
    elif args.from_events:
        symbols = _purchased_event_symbols()
    else:
        symbols = {value.strip() for value in (args.symbols or "").split(",") if value.strip()}
    if not symbols:
        raise SystemExit("provide --symbols or --from-events")
    try:
        normalized_symbols = {str(value).strip().zfill(6) for value in symbols}
    except AttributeError as exc:
        raise SystemExit("symbols must be comma-separated six-digit codes") from exc
    invalid = sorted(value for value in normalized_symbols if not re.fullmatch(r"\d{6}", value))
    if invalid:
        raise SystemExit("invalid symbols: " + ",".join(invalid))

    archive_end = (
        datetime.strptime(str(audit["latest_date"]), "%Y%m%d").date()
        if audit.get("latest_date")
        else None
    )
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else archive_end
    if archive_end is None or end is None:
        raise SystemExit("purchased archive has no usable latest date")
    if end > archive_end:
        end = archive_end
    if end < start:
        raise SystemExit("end date is before start date")
    adjustments = tuple(value.strip() for value in args.adjustments.split(",") if value.strip())
    invalid_adjustments = sorted(set(adjustments) - {"raw", "qfq", "hfq"})
    if not adjustments or invalid_adjustments:
        raise SystemExit("adjustments must be a comma-separated subset of raw,qfq,hfq")

    provider = PurchasedDailyProvider(root)
    source_missing = sorted(
        f"{symbol}.{_exchange(symbol, _instrument_type(symbol))}"
        for symbol in normalized_symbols
        if not provider.has_symbol(symbol)
    )
    payload: dict[str, Any] = {
        "ok": True,
        "provider": PURCHASED_DAILY_PROVIDER,
        "root": str(root),
        "symbols": sorted(normalized_symbols),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustments": adjustments,
        "audit": audit,
        "dry_run": bool(args.dry_run),
        "source_missing_files": source_missing,
        "missing_file_policy": (
            "warning_for_research_pool" if args.from_research_pool else "error"
        ),
    }
    if not args.dry_run:
        store = _market_store()
        _seed_market_instruments(store)
        for symbol in normalized_symbols:
            if not provider.has_symbol(symbol):
                continue
            if store.get_instrument(symbol) is None:
                kind = _instrument_type(symbol)
                store.upsert_instrument(
                    Instrument(
                        symbol,
                        symbol,
                        kind,
                        _exchange(symbol, kind),
                        lifecycle="tracking",
                        source=PURCHASED_DAILY_PROVIDER,
                    )
                )
        result = import_historical_daily(
            store,
            provider,
            normalized_symbols,
            start=start,
            end=end,
            adjustments=adjustments,
        )
        payload.update(result)
        # The purchased snapshot is stock-focused and may not contain every
        # ETF/index in the broader research pool.  Keep those gaps explicit
        # without failing otherwise successful stock-history backfill.
        missing_is_error = not bool(args.from_research_pool)
        payload["ok"] = not result["failed"] and (
            not result["missing_files"] or not missing_is_error
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def market_freestockdb_doctor(args: argparse.Namespace) -> None:
    runtime = FreeStockDBRuntime()
    expected = date.fromisoformat(args.expected_date) if args.expected_date else None
    result = runtime.doctor(expected_trade_date=expected)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(2)


def market_freestockdb_update(args: argparse.Namespace) -> None:
    runtime = FreeStockDBRuntime()
    try:
        result = runtime.update(
            dry_run=bool(args.dry_run),
            timeout=float(args.timeout),
            expected_trade_date=(
                date.fromisoformat(args.expected_date)
                if args.expected_date
                else None
            ),
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok") and not args.dry_run:
        raise SystemExit(2)


def market_freestockdb_repair(args: argparse.Namespace) -> None:
    runtime = FreeStockDBRuntime()
    try:
        result = runtime.repair(
            migrate=bool(args.migrate),
            force_restart=bool(args.force_restart),
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "status": "failed", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(2)


def market_indicators_rebuild(args: argparse.Namespace) -> None:
    store = _market_store()
    requested = {
        value.strip()
        for value in str(args.symbols or "").split(",")
        if value.strip()
    }
    if not requested:
        requested = {
            event.symbol
            for event in KolStore(KOL_ROOT).load_events()
            if event.status in {"active", "completed"}
        }
    output_root = MARKET_ROOT / "warehouse" / "indicators"
    output_root.mkdir(parents=True, exist_ok=True)
    built: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for symbol in sorted(requested):
        try:
            frame = compute_daily_indicators(
                store.read_daily(symbol, adjustment="qfq")
            )
            if frame.empty:
                raise RuntimeError("qfq daily history is empty")
            destination = output_root / f"{symbol}.parquet"
            temporary = destination.with_suffix(".parquet.tmp")
            frame.to_parquet(temporary, index=False, engine="pyarrow")
            os.replace(temporary, destination)
            built.append(
                {
                    "symbol": symbol,
                    "rows": len(frame),
                    "available_from": frame.iloc[0]["trade_date"].isoformat(),
                    "available_to": frame.iloc[-1]["trade_date"].isoformat(),
                    "path": str(destination),
                }
            )
        except Exception as exc:
            failed.append({"symbol": symbol, "error": str(exc)})
    manifest = {
        "formula_version": INDICATOR_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "built": built,
        "failed": failed,
    }
    manifest_path = output_root / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_manifest, manifest_path)
    print(json.dumps({"ok": not failed, **manifest}, ensure_ascii=False))
    if failed:
        raise SystemExit(2)


def _sync_with_fallback(
    store: MarketStore,
    symbol: str,
    start: date,
    end: date,
    adjustment: str,
    *,
    providers: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    provider_chain = list(
        providers
        or [
            BaoStockMarketProvider(),
            AKShareMarketProvider(),
            FreeStockDBMarketProvider(),
        ]
    )
    existing_coverage = next(
        (
            item
            for item in store.get_coverage(symbol)
            if item["dataset"] == "daily"
            and item["adjustment"] == adjustment
            and item["quality_status"] in {"valid", "warning"}
            and any(Path(path).exists() for path in item["paths"])
        ),
        None,
    )
    canonical_end = date.fromisoformat(existing_coverage["end_date"]) if existing_coverage else None
    canonical_provider = existing_coverage.get("provider") if existing_coverage else None
    for provider in provider_chain:
        result = sync_daily_bars(
            store,
            provider,
            symbol,
            start,
            end,
            adjustment=adjustment,
            promote=True,
            preserve_existing_before=canonical_end,
        )
        results.append(result.__dict__)
        if result.quality_status != "quarantined":
            current = next(
                (
                    item for item in store.get_coverage(symbol)
                    if item["dataset"] == "daily" and item["adjustment"] == adjustment
                ),
                None,
            )
            canonical_end = date.fromisoformat(current["end_date"]) if current and current.get("end_date") else canonical_end
            if canonical_provider is None:
                canonical_provider = current.get("provider") if current else result.provider
            elif current and current.get("provider") != canonical_provider:
                store.preserve_coverage_provider(symbol, adjustment, canonical_provider)
            # A valid but stale primary result is not considered complete.  The
            # local FreeStockDB source may fill only dates after the canonical
            # end; it never overwrites the earlier primary rows.
            if canonical_end is None or canonical_end >= end or provider.name == "freestockdb":
                break
    return results


def _drain_market_queue(store: MarketStore, *, as_of: date, symbols: set[str] | None = None) -> dict[str, Any]:
    completed = failed = skipped = 0
    results: list[dict[str, Any]] = []
    providers = [
        BaoStockMarketProvider(),
        AKShareMarketProvider(),
        FreeStockDBMarketProvider(),
    ]
    try:
        for item in store.pending_sync():
            if symbols is not None and item["symbol"] not in symbols:
                continue
            instrument = store.get_instrument(item["symbol"])
            if instrument is None:
                store.mark_sync(item["queue_key"], "failed", "instrument missing")
                failed += 1
                continue
            if instrument["lifecycle"] not in {"pinned", "tracking"}:
                store.mark_sync(item["queue_key"], "completed", "inactive_lifecycle_skipped")
                skipped += 1
                continue
            fallback = date.fromisoformat(item["requested_start"]) if item["requested_start"] else date(2024, 1, 1)
            end = date.fromisoformat(item["requested_end"]) if item["requested_end"] else as_of
            adjustments = ("raw",) if instrument["instrument_type"] == "index" else ("raw", "qfq")
            item_ok = True
            for adjustment in adjustments:
                start = (
                    fallback
                    if item.get("reason") == "manual_backfill"
                    else default_sync_start(store, item["symbol"], adjustment, fallback=fallback)
                )
                attempts = _sync_with_fallback(
                    store,
                    item["symbol"],
                    start,
                    end,
                    adjustment,
                    providers=providers,
                )
                results.extend(attempts)
                item_ok = item_ok and any(
                    attempt["quality_status"] in {"valid", "warning"}
                    and bool(attempt["normalized_paths"])
                    for attempt in attempts
                )
            store.mark_sync(
                item["queue_key"],
                "completed" if item_ok else "failed",
                "" if item_ok else "all providers failed",
            )
            completed += int(item_ok)
            failed += int(not item_ok)
    finally:
        for provider in providers:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
    return {"completed": completed, "failed": failed, "skipped": skipped, "results": results}


def _completed_market_sync_date(store: MarketStore, requested: date) -> date:
    latest_value = str(store.health().get("latest_open_date") or "")
    if not latest_value:
        return requested
    latest_completed = date.fromisoformat(latest_value)
    return min(requested, latest_completed)


def market_backfill(args: argparse.Namespace) -> None:
    store = _market_store()
    _seed_market_instruments(store)
    start = date.fromisoformat(args.start)
    requested_end = date.fromisoformat(args.end or date.today().isoformat())
    end = _completed_market_sync_date(store, requested_end)
    symbols = {value.strip() for value in args.symbols.split(",") if value.strip()}
    invalid = sorted(symbol for symbol in symbols if not re.fullmatch(r"\d{6}", symbol))
    if invalid:
        raise SystemExit(
            "invalid symbols: " + ",".join(invalid) + "; quote comma-separated codes in PowerShell"
        )
    for symbol in symbols:
        if store.get_instrument(symbol) is None:
            kind = _instrument_type(symbol)
            store.upsert_instrument(
                Instrument(symbol, symbol, kind, _exchange(symbol, kind), lifecycle="tracking", source="manual_backfill")
            )
        store.enqueue_sync(symbol, start=start, end=end, priority=10, reason="manual_backfill")
    payload = _drain_market_queue(store, as_of=end, symbols=symbols)
    payload["technical_context"] = _backfill_event_contexts(
        store,
        KolStore(KOL_ROOT),
        symbols=symbols,
    )
    print(json.dumps({"ok": payload["failed"] == 0, **payload}, ensure_ascii=False, indent=2))
    if payload["failed"]:
        raise SystemExit(2)


def market_minute_fetch(args: argparse.Namespace) -> None:
    store = _market_store()
    _seed_market_instruments(store)
    symbol = args.symbol.strip()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end or args.start)
    if end < start:
        raise SystemExit("end date must be on or after start date")
    instrument = store.get_instrument(symbol)
    if instrument is None:
        kind = _instrument_type(symbol)
        store.upsert_instrument(
            Instrument(symbol, symbol, kind, _exchange(symbol, kind), lifecycle="tracking", source="manual_minute")
        )
        instrument = store.get_instrument(symbol)
    provider = FreeStockDBMarketProvider()
    try:
        frame = provider.fetch_minute(
            symbol,
            instrument["instrument_type"],
            start,
            end,
            frequency=args.frequency,
            adjustment=args.adjustment,
        )
        frame = frame[
            frame["trade_datetime"].dt.date.between(start, end, inclusive="both")
        ].copy()
        result = store.save_minute_snapshot(
            provider=provider.name,
            symbol=symbol,
            as_of=end,
            frequency=args.frequency,
            adjustment=args.adjustment,
            frame=frame,
            parameters={
                "source": "local_http",
                "base_url": provider.base_url,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "provider": provider.name, "symbol": symbol, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
    finally:
        provider.close()
    print(json.dumps({"ok": result.quality_status == "valid", **result.__dict__}, ensure_ascii=False, indent=2))
    if result.quality_status != "valid":
        raise SystemExit(2)


def kol_intraday_backfill(args: argparse.Namespace) -> None:
    event_store = KolStore(KOL_ROOT)
    event_ids = None if args.all else {args.event_id}
    if event_ids and not any(event.event_id in event_ids for event in event_store.load_events()):
        raise SystemExit(f"event not found: {args.event_id}")
    payload = backfill_event_intraday(
        _market_store(),
        event_store,
        event_ids=event_ids,
        force=args.force,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_intraday_audit(_: argparse.Namespace) -> None:
    payload = audit_event_intraday(_market_store(), KolStore(KOL_ROOT))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def market_sync(args: argparse.Namespace) -> None:
    store = _market_store()
    _seed_market_instruments(store)
    requested_as_of = date.fromisoformat(args.as_of or date.today().isoformat())
    if isinstance(store, FoundationBackedMarketStore):
        # The consumer task must never call BaoStock/AKShare or write a second
        # daily warehouse.  The foundation publisher owns refresh and atomic
        # release selection; this task only rebuilds derived KOL context.
        as_of = store.latest_open_date(requested_as_of)
        if as_of is None:
            raise SystemExit("shared foundation has no completed trading date")
        payload: dict[str, Any] = {
            "read_only_consumer": True,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "results": [],
            "foundation_release_id": str(store.foundation.release().get("release_id", "")),
            "foundation_as_of": str(store.foundation.release().get("as_of", "")),
            "foundation_status": _foundation_status(),
        }
        requested = {value.strip() for value in (args.symbols or "").split(",") if value.strip()}
        payload["technical_context"] = _backfill_event_contexts(
            store,
            KolStore(KOL_ROOT),
            symbols=requested or None,
        )
        payload["intraday_context"] = backfill_event_intraday(
            store,
            KolStore(KOL_ROOT),
            event_ids=None,
        )
        payload["as_of"] = as_of.isoformat()
        payload["requested_as_of"] = requested_as_of.isoformat()
        payload["calendar_error"] = ""
        print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
        return
    calendar_error = ""
    try:
        open_dates = BaoStockMarketProvider().fetch_calendar(
            requested_as_of - timedelta(days=730), requested_as_of
        )
        store.replace_calendar(open_dates, provider="baostock")
        store.refresh_lifecycles(as_of=requested_as_of)
    except Exception as exc:
        calendar_error = str(exc)
    as_of = _completed_market_sync_date(store, requested_as_of)
    requested = {value.strip() for value in (args.symbols or "").split(",") if value.strip()}
    active = [item for item in store.list_instruments() if item["lifecycle"] in {"pinned", "tracking"}]
    for instrument in active:
        if requested and instrument["symbol"] not in requested:
            continue
        store.enqueue_sync(instrument["symbol"], end=as_of, reason="daily_sync")
    payload = _drain_market_queue(store, as_of=as_of, symbols=requested or None)
    payload["technical_context"] = _backfill_event_contexts(
        store,
        KolStore(KOL_ROOT),
        symbols=requested or None,
    )
    payload["intraday_context"] = backfill_event_intraday(
        store,
        KolStore(KOL_ROOT),
        event_ids=None,
    )
    payload["calendar_error"] = calendar_error
    payload["as_of"] = as_of.isoformat()
    payload["requested_as_of"] = requested_as_of.isoformat()
    if args.alerts_only:
        payload["alert_sent"] = _send_transition_alert(
            "market_sync_failed",
            payload["failed"] > 0,
            f"[行情采集告警] {payload['failed']} 个标的同步失败，请打开数据中心检查。",
        )
    print(json.dumps({"ok": payload["failed"] == 0, **payload}, ensure_ascii=False))
    if payload["failed"]:
        raise SystemExit(2)


def market_audit(args: argparse.Namespace) -> None:
    store = _market_store()
    symbols = {value.strip() for value in (args.symbols or "").split(",") if value.strip()}
    issues: list[dict[str, Any]] = []
    for coverage in store.get_coverage():
        if coverage["dataset"] != "daily" or (symbols and coverage["symbol"] not in symbols):
            continue
        frame = store.read_daily(coverage["symbol"], adjustment=coverage["adjustment"])
        audit = audit_daily_bars(frame)
        if audit.issues:
            run_id = f"local-audit-{uuid.uuid4().hex}"
            store.record_quality_issues(run_id, coverage["symbol"], audit.issues)
            issues.extend({"symbol": coverage["symbol"], **item.__dict__} for item in audit.issues)
    if args.cross_check:
        targets = symbols or {"600900", "600519", "159139", "000300"}
        end = date.fromisoformat(args.as_of or date.today().isoformat())
        for symbol in targets:
            instrument = store.get_instrument(symbol)
            primary = store.read_daily(symbol, adjustment="raw")
            if instrument is None or primary.empty:
                continue
            for provider in (AKShareMarketProvider(), FreeStockDBMarketProvider()):
                try:
                    raw = provider.fetch_daily(
                        symbol, instrument["instrument_type"], end - timedelta(days=45), end, "raw"
                    )
                    secondary = normalise_daily_bars(
                        raw,
                        symbol=symbol,
                        instrument_type=instrument["instrument_type"],
                        provider=provider.name,
                        adjustment="raw",
                    )
                    conflicts = compare_daily_frames(primary, secondary)
                except Exception as exc:
                    from market_data import QualityIssue

                    conflicts = [QualityIssue("cross_source_unavailable", "warning", f"{provider.name}: {str(exc)[:1900]}")]
                if conflicts:
                    run_id = f"cross-audit-{provider.name}-{uuid.uuid4().hex}"
                    store.record_quality_issues(run_id, symbol, conflicts)
                    issues.extend({"symbol": symbol, "provider": provider.name, **item.__dict__} for item in conflicts)
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
    blocking = sum(item["severity"] == "error" for item in issues)
    print(json.dumps({"ok": blocking == 0, "blocking": blocking, "issues": issues}, ensure_ascii=False, indent=2))
    if blocking:
        raise SystemExit(2)


def market_weekly(args: argparse.Namespace) -> None:
    store = _market_store()
    _seed_market_instruments(store)
    as_of = date.fromisoformat(args.as_of or date.today().isoformat())
    imported = 0
    errors: list[str] = []
    master_errors: list[str] = []
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "market-baostock-master",
                "--as-of",
                as_of.isoformat(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        if completed.returncode == 0:
            imported += int(json.loads(completed.stdout.strip()).get("imported", 0))
        else:
            master_errors.append(f"baostock_master: {(completed.stderr or completed.stdout)[-1000:]}")
    except subprocess.TimeoutExpired:
        master_errors.append("baostock_master: timed out after 45 seconds")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "market-etf-master",
                "--as-of",
                as_of.isoformat(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        if completed.returncode == 0:
            imported += int(json.loads(completed.stdout.strip()).get("imported", 0))
        else:
            errors.append(f"akshare_etf_master: {(completed.stderr or completed.stdout)[-1000:]}")
    except subprocess.TimeoutExpired:
        errors.append("akshare_etf_master: timed out after 45 seconds")
    lead_reconciliation = _extract_leads_to_market()
    snapshots = 0
    auxiliary_circuit_open = False
    auxiliary_failure_reported = False
    auxiliary_skipped = 0
    if not args.master_only:
        for instrument in store.list_instruments():
            if instrument["lifecycle"] not in {"pinned", "tracking"} or instrument["instrument_type"] != "stock":
                continue
            for dataset in ("valuation", "financial_summary", "announcements", "fund_flow"):
                if auxiliary_circuit_open:
                    auxiliary_skipped += 1
                    continue
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "market-aux-fetch",
                    "--symbol",
                    instrument["symbol"],
                    "--dataset",
                    dataset,
                    "--as-of",
                    as_of.isoformat(),
                ]
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=45,
                        check=False,
                    )
                    if completed.returncode == 0:
                        snapshots += int(json.loads(completed.stdout.strip()).get("saved", False))
                    else:
                        if not auxiliary_failure_reported:
                            errors.append(
                                "akshare auxiliary provider circuit opened after "
                                f"{instrument['symbol']}:{dataset}: "
                                f"{(completed.stderr or completed.stdout)[-1000:]}"
                            )
                            auxiliary_failure_reported = True
                        auxiliary_circuit_open = True
                except subprocess.TimeoutExpired:
                    if not auxiliary_failure_reported:
                        errors.append(
                            f"akshare auxiliary provider circuit opened after {instrument['symbol']}:{dataset}: timed out after 45 seconds"
                        )
                        auxiliary_failure_reported = True
                    auxiliary_circuit_open = True
    errors = [*master_errors, *errors]
    alert_sent = _send_transition_alert(
        "market_master_failed",
        bool(master_errors),
        "[证券主数据告警] 本周全市场主数据刷新失败，请打开数据中心检查。",
    )
    print(
        json.dumps(
            {
                "ok": not master_errors,
                "as_of": as_of.isoformat(),
                "master_rows": imported,
                "stock_leads": lead_reconciliation,
                "snapshots": snapshots,
                "auxiliary_circuit_open": auxiliary_circuit_open,
                "auxiliary_skipped": auxiliary_skipped,
                "errors": errors,
                "alert_sent": alert_sent,
            },
            ensure_ascii=False,
        )
    )
    if master_errors:
        raise SystemExit(2)


def market_etf_master(args: argparse.Namespace) -> None:
    store = _market_store()
    instruments = AKShareMarketProvider().fetch_etf_instruments()
    snapshot_date = date.fromisoformat(args.as_of or date.today().isoformat())
    snapshot = store.save_instrument_snapshot("akshare", instruments, snapshot_date)
    if snapshot.quality_status == "quarantined":
        raise SystemExit("empty AKShare ETF master")
    store.upsert_instrument_catalog(instruments, provider="akshare", snapshot_date=snapshot_date)
    imported = store.upsert_instruments(instruments)
    print(json.dumps({"ok": True, "imported": imported}, ensure_ascii=False))


def market_baostock_master(args: argparse.Namespace) -> None:
    store = _market_store()
    instruments = BaoStockMarketProvider().fetch_instruments()
    snapshot_date = date.fromisoformat(args.as_of or date.today().isoformat())
    snapshot = store.save_instrument_snapshot("baostock", instruments, snapshot_date)
    if snapshot.quality_status == "quarantined":
        raise SystemExit(snapshot.error or "empty BaoStock instrument master")
    store.upsert_instrument_catalog(instruments, provider="baostock", snapshot_date=snapshot_date)
    imported = store.upsert_instruments(instruments)
    print(json.dumps({"ok": True, "imported": imported}, ensure_ascii=False))


def market_aux_fetch(args: argparse.Namespace) -> None:
    store = _market_store()
    instrument = store.get_instrument(args.symbol)
    if instrument is None:
        print(json.dumps({"ok": False, "error": "instrument not found"}))
        raise SystemExit(2)
    as_of = date.fromisoformat(args.as_of)
    try:
        frame = AKShareMarketProvider().fetch_auxiliary(
            args.symbol, instrument["exchange"], args.dataset, as_of
        )
        result = store.save_auxiliary_snapshot(
            provider="akshare",
            dataset=args.dataset,
            symbol=args.symbol,
            as_of=as_of,
            frame=frame,
            parameters={"experimental": args.dataset == "fund_flow"},
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:2000]}, ensure_ascii=False))
        raise SystemExit(2)
    print(json.dumps({"ok": True, "saved": result.quality_status == "valid"}, ensure_ascii=False))


def data_digest(args: argparse.Namespace) -> None:
    post_store = _post_store()
    post_summary = post_store.summary()
    review_summary = ReviewAgentRepository(post_store).summary()
    market_health = _market_store().health()
    message = (
        f"[研究数据日报] 帖子 {post_summary['total_posts']}，审核待办 {post_summary['actionable_posts']}，"
        f"股票线索 {post_summary['stock_leads']}（待确认 {post_summary['pending_stock_leads']}），"
        f"活跃数据标的 {market_health['active_instruments']}，覆盖记录 {market_health['coverage_count']}，"
        f"数据警告 {market_health['warning_count']}。"
        f"审核智能体 {review_summary['settings']['mode']} 模式，累计决定 {review_summary['total_decisions']}，"
        f"转人工 {review_summary['decision_counts'].get('needs_human', 0)}，"
        f"影子一致率 {review_summary['agreement'] * 100:.1f}%。"
    )
    notification_errors = _send_pending_notifications(KolStore(KOL_ROOT)) if args.notify else []
    sent = _send_feishu(message) if args.notify else False
    print(
        json.dumps(
            {
                "ok": True,
                "message": message,
                "notification_sent": sent,
                "notification_errors": notification_errors,
            },
            ensure_ascii=False,
        )
    )


def portfolio_import(args: argparse.Namespace) -> None:
    store = PortfolioStore(PORTFOLIO_ROOT)
    transactions = load_transactions_json(Path(args.file))
    result = store.record_many(transactions)
    market = _market_store()
    summary = store.summary()
    queued: list[str] = []
    catalog = market.instrument_map()
    for position in summary["positions"]:
        symbol = position["symbol"]
        existing = catalog.get(symbol, {})
        kind = str(existing.get("instrument_type") or _instrument_type(symbol))
        is_open = position["status"] == "open"
        may_update_closed = not existing or str(existing.get("source") or "") in {
            "holding", "portfolio_holding", "portfolio_closed"
        }
        if is_open or may_update_closed:
            market.upsert_instrument(
                Instrument(
                    symbol,
                    position["security_name"] or str(existing.get("name") or symbol),
                    kind,
                    str(existing.get("exchange") or _exchange(symbol, kind)),
                    lifecycle="pinned" if is_open else "tracking",
                    source="portfolio_holding" if is_open else "portfolio_closed",
                    first_seen_at=position["first_transaction_at"][:10],
                    last_mentioned_at=position["last_transaction_at"][:10],
                )
            )
        market.enqueue_sync(
            symbol,
            priority=5 if is_open else 20,
            reason="portfolio_holding" if is_open else "portfolio_closed",
        )
        queued.append(symbol)
    print(json.dumps({**result, "market_sync_queued": sorted(queued), "summary": summary}, ensure_ascii=False, indent=2))


def portfolio_report(_: argparse.Namespace) -> None:
    print(json.dumps(PortfolioStore(PORTFOLIO_ROOT).summary(), ensure_ascii=False, indent=2))


def public_dataset_doctor_cmd(_: argparse.Namespace) -> None:
    print(json.dumps(run_public_dataset_doctor(), ensure_ascii=False, indent=2))


def public_dataset_export_cmd(args: argparse.Namespace) -> None:
    result = PublicDatasetExporter().export(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def public_dataset_validate_cmd(args: argparse.Namespace) -> None:
    result = validate_public_dataset(args.input)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A-share trading research audit helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=lambda args: init_watchlist(args.force))

    p_fetch = sub.add_parser("fetch-prices")
    p_fetch.add_argument("--symbols")
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--adjust", default="qfq")
    p_fetch.set_defaults(func=fetch_prices)

    p_bao = sub.add_parser("verify-baostock")
    p_bao.add_argument("--symbols", default="600900,600519")
    p_bao.add_argument("--start", required=True)
    p_bao.add_argument("--end", required=True)
    p_bao.set_defaults(func=verify_baostock_cmd)

    p_report = sub.add_parser("generate-report")
    p_report.set_defaults(func=generate_report)

    p_doctor = sub.add_parser("doctor")
    p_doctor.set_defaults(func=doctor)

    p_kol_init = sub.add_parser("kol-init", help="initialize the local KOL event store")
    p_kol_init.set_defaults(func=kol_init)

    p_kol_doctor = sub.add_parser("kol-doctor", help="check the KOL tracking runtime")
    p_kol_doctor.set_defaults(func=kol_doctor)

    p_kol_register = sub.add_parser("kol-register", help="activate one audited KOL event")
    p_kol_register.add_argument("--source-note", required=True)
    p_kol_register.add_argument("--kol", required=True)
    p_kol_register.add_argument("--symbol", required=True)
    p_kol_register.add_argument("--direction", choices=["long", "short"], required=True)
    p_kol_register.add_argument("--posted-at", required=True)
    p_kol_register.add_argument("--name")
    p_kol_register.add_argument("--thesis")
    p_kol_register.add_argument("--platform")
    p_kol_register.add_argument("--source-url")
    p_kol_register.add_argument("--event-id")
    p_kol_register.set_defaults(func=kol_register)

    p_kol_update = sub.add_parser("kol-update", help="update KOL event returns")
    p_kol_update.add_argument("--as-of", required=True)
    p_kol_update.add_argument("--notify", action="store_true")
    p_kol_update.add_argument("--dry-run", action="store_true")
    p_kol_update.add_argument("--event-id", help="update one event only")
    p_kol_update.set_defaults(func=kol_update)

    p_kol_returns_backfill = sub.add_parser(
        "kol-returns-backfill",
        help="rebuild all available event marks through the latest requested trading day",
    )
    p_kol_returns_backfill.add_argument("--as-of")
    p_kol_returns_backfill.add_argument("--dry-run", action="store_true")
    p_kol_returns_backfill.set_defaults(func=kol_returns_backfill)

    p_kol_report = sub.add_parser("kol-report", help="regenerate the local KOL return report")
    p_kol_report.set_defaults(func=kol_report)

    p_performance_doctor = sub.add_parser(
        "kol-performance-doctor",
        help="audit stable KOL identities and performance snapshot storage",
    )
    p_performance_doctor.set_defaults(func=kol_performance_doctor)

    p_performance_refresh = sub.add_parser(
        "kol-performance-refresh",
        help="refresh deterministic batch-weighted KOL performance snapshots",
    )
    p_performance_refresh.add_argument("--as-of")
    p_performance_refresh.set_defaults(func=kol_performance_refresh)

    p_performance_backfill = sub.add_parser(
        "kol-performance-backfill",
        help="replay performance snapshots at historical mature checkpoint dates",
    )
    p_performance_backfill.add_argument("--start")
    p_performance_backfill.add_argument("--end")
    p_performance_backfill.set_defaults(func=kol_performance_backfill)

    p_performance_report = sub.add_parser(
        "kol-performance-report",
        help="generate the KOL performance report and optional weekly notification",
    )
    p_performance_report.add_argument("--weekly", action="store_true")
    p_performance_report.add_argument("--notify", action="store_true")
    p_performance_report.add_argument(
        "--with-ai",
        action="store_true",
        help=(
            "ask DeepSeek V4 Flash through OpenCode Go to explain aggregate "
            "facts; falls back to deterministic text"
        ),
    )
    p_performance_report.add_argument("--as-of")
    p_performance_report.set_defaults(func=kol_performance_report)

    p_context_doctor = sub.add_parser("kol-context-doctor", help="check event-time technical context coverage")
    p_context_doctor.set_defaults(func=kol_context_doctor)

    p_context_backfill = sub.add_parser("kol-context-backfill", help="compute event-time technical context")
    context_target = p_context_backfill.add_mutually_exclusive_group(required=True)
    context_target.add_argument("--all", action="store_true")
    context_target.add_argument("--event-id")
    p_context_backfill.add_argument("--force", action="store_true")
    p_context_backfill.set_defaults(func=kol_context_backfill)

    p_event_data_doctor = sub.add_parser("kol-event-data-doctor", help="audit formal event dossier coverage")
    p_event_data_doctor.set_defaults(func=kol_event_data_doctor)

    p_event_data_backfill = sub.add_parser("kol-event-data-backfill", help="build formal event audit dossiers")
    event_data_scope = p_event_data_backfill.add_mutually_exclusive_group(required=True)
    event_data_scope.add_argument("--all", action="store_true")
    event_data_scope.add_argument("--event-id")
    p_event_data_backfill.add_argument("--missing-only", action="store_true")
    p_event_data_backfill.set_defaults(func=kol_event_data_backfill)

    p_method_doctor = sub.add_parser(
        "kol-method-research-doctor",
        help="audit point-in-time multi-method research coverage",
    )
    p_method_doctor.set_defaults(func=kol_method_research_doctor)

    p_method_backfill = sub.add_parser(
        "kol-method-research-backfill",
        help="backfill point-in-time research for formal KOL events",
    )
    method_scope = p_method_backfill.add_mutually_exclusive_group(required=True)
    method_scope.add_argument("--all", action="store_true")
    method_scope.add_argument("--event-id")
    p_method_backfill.add_argument("--with-minute", action="store_true")
    p_method_backfill.add_argument("--with-ai", action="store_true")
    p_method_backfill.add_argument("--skip-cross-section", action="store_true")
    p_method_backfill.add_argument("--missing-only", action="store_true")
    p_method_backfill.add_argument("--force", action="store_true")
    p_method_backfill.add_argument(
        "--max-ai",
        type=int,
        default=0,
        help="maximum AI interpretations; zero processes all selected events",
    )
    p_method_backfill.set_defaults(func=kol_method_research_backfill)

    p_method_run = sub.add_parser(
        "kol-method-research-run",
        help="process the persistent pending event research queue",
    )
    p_method_run.add_argument("--pending", action="store_true")
    p_method_run.add_argument("--event-id")
    p_method_run.add_argument("--max-events", type=int, default=25)
    p_method_run.add_argument("--with-minute", action="store_true")
    p_method_run.add_argument("--skip-ai", action="store_true")
    p_method_run.set_defaults(func=kol_method_research_run)

    p_import = sub.add_parser("kol-import", help="import KOL accounts from linked source metadata")
    p_import.add_argument("--platform", choices=["zhihu"], required=True)
    p_import.add_argument("--from-linked-profiles", action="store_true", required=True)
    p_import.set_defaults(func=kol_import)

    p_zhihu_onboard = sub.add_parser("kol-zhihu-onboard", help="activate the next batch of Zhihu direct-profile KOLs")
    p_zhihu_onboard.add_argument("--advance", action="store_true")
    p_zhihu_onboard.add_argument("--batch-size", type=int, default=8)
    p_zhihu_onboard.add_argument("--backfill", type=int, default=20)
    p_zhihu_onboard.set_defaults(func=kol_zhihu_onboard)

    p_post_doctor = sub.add_parser("kol-post-doctor", help="check KOL post collection dependencies")
    p_post_doctor.add_argument("--platform", choices=["all", "x", "zhihu"], default="all")
    p_post_doctor.set_defaults(func=kol_post_doctor)

    p_post_backup = sub.add_parser("kol-post-db-backup", help="create a consistent SQLite backup")
    p_post_backup.set_defaults(func=kol_post_db_backup)

    p_reader_migrate = sub.add_parser(
        "kol-reader-migrate",
        help="copy the isolated Nitter reader session into ai-hub/twitter-reader",
    )
    p_reader_migrate.set_defaults(func=kol_reader_migrate)

    p_collection_doctor = sub.add_parser(
        "kol-collection-doctor",
        help="inspect reader credentials and platform collection coverage",
    )
    p_collection_doctor.set_defaults(func=kol_collection_doctor)

    p_gap_audit = sub.add_parser("kol-gap-audit", help="audit recent KOL collection gaps")
    p_gap_audit.add_argument("--from", dest="from_date", required=True)
    p_gap_audit.add_argument("--to", dest="to_date", default="auto")
    p_gap_audit.set_defaults(func=kol_gap_audit)

    p_gap_recover = sub.add_parser("kol-gap-recover", help="resume recent or historical KOL collection recovery")
    p_gap_recover.add_argument("--scope", choices=["recent", "historical"], required=True)
    p_gap_recover.add_argument("--resume", action="store_true")
    p_gap_recover.set_defaults(func=kol_gap_recover)

    p_queue_compact = sub.add_parser("kol-fetch-queue-compact", help="archive superseded legacy fetch batches")
    p_queue_compact.add_argument("--older-than-hours", type=int, default=48)
    p_queue_compact.set_defaults(func=kol_fetch_queue_compact)

    p_ai_resume = sub.add_parser("kol-ai-resume", help="resume the durable AI review queue")
    p_ai_resume.add_argument("--limit", type=int, default=0)
    p_ai_resume.set_defaults(func=kol_ai_resume)

    p_fallback_mode = sub.add_parser("kol-fallback-mode", help="inspect or enable Nitter fallback")
    p_fallback_mode.add_argument("--set", choices=["shadow", "enabled"])
    p_fallback_mode.set_defaults(func=kol_fallback_mode)

    p_post_fetch = sub.add_parser("kol-post-fetch", help="fetch and classify watched KOL accounts")
    p_post_fetch.add_argument("--backfill", type=int, metavar="COUNT")
    p_post_fetch.add_argument("--as-of")
    p_post_fetch.add_argument("--notify", action="store_true")
    p_post_fetch.add_argument("--alerts-only", action="store_true")
    p_post_fetch.add_argument("--skip-classify", action="store_true")
    p_post_fetch.add_argument(
        "--skip-leads",
        action="store_true",
        help="skip the durable stock-lead extraction pass for a targeted collection retry",
    )
    p_post_fetch.add_argument("--classify-limit", type=int, default=0)
    p_post_fetch.add_argument("--provider", choices=["auto", "twitter", "nitter"], default="auto")
    p_post_fetch.add_argument("--platform", choices=["all", "x", "zhihu"], default="all")
    p_post_fetch.add_argument(
        "--handles",
        help="comma-separated handles for a targeted retry; omit to fetch all active accounts",
    )
    p_post_fetch.add_argument(
        "--batch-key",
        help="persistent queue key used to resume an interrupted account batch",
    )
    p_post_fetch.add_argument("--dry-run", action="store_true")
    p_post_fetch.set_defaults(func=kol_post_fetch)

    p_fetch_resume = sub.add_parser(
        "kol-fetch-resume",
        help="resume the latest incomplete persistent KOL fetch queue",
    )
    p_fetch_resume.add_argument("--batch-key")
    p_fetch_resume.add_argument(
        "--provider",
        choices=["auto", "twitter", "nitter"],
        default="auto",
    )
    p_fetch_resume.add_argument(
        "--platform",
        choices=["all", "x", "zhihu"],
        default="all",
    )
    p_fetch_resume.add_argument("--fetch-count", type=int, default=50)
    p_fetch_resume.set_defaults(func=kol_fetch_resume)

    p_nitter_doctor = sub.add_parser("kol-nitter-doctor", help="check local Nitter failover")
    p_nitter_doctor.set_defaults(func=kol_nitter_doctor)

    p_nitter_materialize = sub.add_parser("kol-nitter-materialize", help=argparse.SUPPRESS)
    p_nitter_materialize.set_defaults(func=kol_nitter_materialize)

    p_post_classify = sub.add_parser("kol-post-classify", help="classify pending candidate posts")
    p_post_classify.add_argument("--pending", action="store_true")
    p_post_classify.add_argument("--ocr-limit", type=int, default=50)
    p_post_classify.add_argument("--skip-codex", action="store_true")
    p_post_classify.add_argument("--limit", type=int, default=0, help="0 processes the durable queue until empty")
    p_post_classify.set_defaults(func=kol_post_classify)

    p_recommendation_repair = sub.add_parser(
        "kol-recommendation-repair",
        help="repair missed recommendation drafts without auto-approving events",
    )
    p_recommendation_repair.add_argument("--doctor", action="store_true")
    p_recommendation_repair.add_argument("--all", action="store_true")
    p_recommendation_repair.add_argument("--rules-only", action="store_true")
    p_recommendation_repair.add_argument("--pending-ai", action="store_true")
    p_recommendation_repair.add_argument("--post-id")
    p_recommendation_repair.add_argument("--limit", type=int, default=0)
    p_recommendation_repair.add_argument("--max-runtime", type=float, default=240)
    p_recommendation_repair.set_defaults(func=kol_recommendation_repair)

    p_review_agent_doctor = sub.add_parser(
        "kol-review-agent-doctor", help="check the policy-controlled KOL review agent"
    )
    p_review_agent_doctor.set_defaults(func=kol_review_agent_doctor)

    p_review_agent_run = sub.add_parser(
        "kol-review-agent-run", help="classify and audit pending posts through the review policy"
    )
    p_review_agent_run.add_argument("--mode", choices=["shadow", "enabled"])
    p_review_agent_run.add_argument("--max-runtime", type=float, default=25)
    p_review_agent_run.add_argument("--max-items", type=int, default=20)
    p_review_agent_run.add_argument("--post-id")
    p_review_agent_run.add_argument("--dry-run", action="store_true")
    p_review_agent_run.set_defaults(func=kol_review_agent_run)

    p_review_agent_report = sub.add_parser(
        "kol-review-agent-report", help="show shadow validation and decision metrics"
    )
    p_review_agent_report.set_defaults(func=kol_review_agent_report)

    p_morning = sub.add_parser(
        "kol-morning-run", help="fetch and prepare the evidence-first morning recommendation queue"
    )
    p_morning.add_argument("--as-of")
    p_morning.add_argument("--provider", choices=["auto", "twitter", "nitter"], default="auto")
    p_morning.add_argument("--platform", choices=["all", "x", "zhihu"], default="all")
    p_morning.add_argument("--fetch-count", type=int, default=50)
    p_morning.add_argument("--backlog-limit", type=int, default=20)
    p_morning.add_argument("--phase", choices=["initial", "refresh", "final", "preview"], default="initial")
    p_morning.add_argument("--max-runtime", type=float, default=85)
    p_morning.add_argument("--skip-fetch", action="store_true")
    p_morning.set_defaults(func=kol_morning_pipeline)

    p_morning_orchestrate = sub.add_parser(
        "kol-morning-orchestrate", help="run the due morning phase and chain missed phases safely"
    )
    p_morning_orchestrate.add_argument("--provider", choices=["auto", "twitter", "nitter"], default="auto")
    p_morning_orchestrate.add_argument("--platform", choices=["all", "x", "zhihu"], default="all")
    p_morning_orchestrate.add_argument("--fetch-count", type=int, default=20)
    p_morning_orchestrate.set_defaults(func=kol_morning_orchestrate)

    p_morning_migrate = sub.add_parser(
        "kol-morning-migrate", help="archive legacy non-candidate review items without deleting sources"
    )
    p_morning_migrate.set_defaults(func=kol_morning_migrate)

    p_morning_doctor = sub.add_parser(
        "kol-morning-doctor", help="check the morning recommendation pipeline"
    )
    p_morning_doctor.set_defaults(func=kol_morning_doctor)

    p_leads = sub.add_parser("kol-leads-extract", help="extract auditable stock leads from saved KOL posts")
    p_leads.add_argument("--pending", action="store_true")
    p_leads.set_defaults(func=kol_leads_extract)

    p_market_init = sub.add_parser("market-init", help="initialize the local Parquet and DuckDB market store")
    p_market_init.add_argument("--skip-legacy", action="store_true")
    p_market_init.set_defaults(func=market_init)

    p_market_doctor = sub.add_parser("market-doctor", help="check market data dependencies and coverage")
    p_market_doctor.set_defaults(func=market_doctor)

    p_foundation_doctor = sub.add_parser(
        "data-foundation-doctor",
        help="check the pinned shared A-share foundation release without network calls",
    )
    p_foundation_doctor.set_defaults(func=data_foundation_doctor)

    p_foundation_refresh = sub.add_parser(
        "kol-data-refresh",
        help="refresh the shared A-share release and update KOL consumers from it",
    )
    p_foundation_refresh.add_argument(
        "--as-of",
        default="auto",
        help="target completed trading date, or auto (never uses an open session)",
    )
    p_foundation_refresh.add_argument("--notify", action="store_true")
    p_foundation_refresh.add_argument("--dry-run", action="store_true")
    p_foundation_refresh.set_defaults(func=kol_data_refresh)

    p_purchased_daily_doctor = sub.add_parser(
        "market-purchased-daily-doctor",
        help="audit the read-only purchased daily CSV snapshot",
    )
    p_purchased_daily_doctor.add_argument("--root", default=str(PURCHASED_DAILY_ROOT))
    p_purchased_daily_doctor.add_argument("--expected-date")
    p_purchased_daily_doctor.set_defaults(func=market_purchased_daily_doctor)

    p_purchased_daily_import = sub.add_parser(
        "market-purchased-daily-import",
        help="merge historical rows from the purchased daily snapshot",
    )
    p_purchased_daily_import.add_argument("--root", default=str(PURCHASED_DAILY_ROOT))
    scope = p_purchased_daily_import.add_mutually_exclusive_group()
    scope.add_argument("--symbols")
    scope.add_argument("--from-events", action="store_true")
    scope.add_argument(
        "--from-research-pool",
        action="store_true",
        help="use formal events, watchlist, and portfolio symbols",
    )
    p_purchased_daily_import.add_argument("--start", default="1990-01-01")
    p_purchased_daily_import.add_argument("--end")
    p_purchased_daily_import.add_argument("--expected-date")
    p_purchased_daily_import.add_argument("--adjustments", default="raw")
    p_purchased_daily_import.add_argument("--dry-run", action="store_true")
    p_purchased_daily_import.set_defaults(func=market_purchased_daily_import)

    p_market_freestockdb_doctor = sub.add_parser(
        "market-freestockdb-doctor", help="check the optional local FreeStockDB service"
    )
    p_market_freestockdb_doctor.add_argument(
        "--expected-date",
        help="expected latest completed A-share trading date (YYYY-MM-DD)",
    )
    p_market_freestockdb_doctor.set_defaults(func=market_freestockdb_doctor)

    p_market_freestockdb_update = sub.add_parser(
        "market-freestockdb-update", help="verify and update the isolated local FreeStockDB dataset"
    )
    p_market_freestockdb_update.add_argument("--dry-run", action="store_true")
    p_market_freestockdb_update.add_argument(
        "--timeout",
        type=float,
        default=3300,
        help="maximum vendor updater runtime in seconds",
    )
    p_market_freestockdb_update.add_argument(
        "--expected-date",
        help="required latest A-share trading date after the update (YYYY-MM-DD)",
    )
    p_market_freestockdb_update.set_defaults(func=market_freestockdb_update)

    p_market_freestockdb_repair = sub.add_parser(
        "market-freestockdb-repair",
        help="migrate, verify, and repair the isolated local FreeStockDB runtime",
    )
    p_market_freestockdb_repair.add_argument("--migrate", action="store_true")
    p_market_freestockdb_repair.add_argument("--force-restart", action="store_true")
    p_market_freestockdb_repair.set_defaults(func=market_freestockdb_repair)

    p_market_indicators = sub.add_parser(
        "market-indicators-rebuild",
        help="rebuild deterministic qfq indicator caches for tracked event symbols",
    )
    p_market_indicators.add_argument("--symbols")
    p_market_indicators.set_defaults(func=market_indicators_rebuild)

    p_market_backfill = sub.add_parser("market-backfill", help="backfill selected stocks, ETFs, or indexes")
    p_market_backfill.add_argument("--symbols", required=True)
    p_market_backfill.add_argument("--start", default="2024-01-01")
    p_market_backfill.add_argument("--end")
    p_market_backfill.set_defaults(func=market_backfill)

    p_market_minute = sub.add_parser(
        "market-minute-fetch", help="read and persist optional FreeStockDB minute bars"
    )
    p_market_minute.add_argument("--symbol", required=True)
    p_market_minute.add_argument("--start", required=True)
    p_market_minute.add_argument("--end")
    p_market_minute.add_argument("--frequency", choices=["1m", "5m", "15m", "30m", "60m"], default="1m")
    p_market_minute.add_argument("--adjustment", choices=["raw", "qfq", "hfq"], default="raw")
    p_market_minute.set_defaults(func=market_minute_fetch)

    p_intraday = sub.add_parser(
        "kol-intraday-backfill", help="compute short intraday windows around formal KOL events"
    )
    scope = p_intraday.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--event-id")
    p_intraday.add_argument("--force", action="store_true")
    p_intraday.set_defaults(func=kol_intraday_backfill)

    p_intraday_audit = sub.add_parser(
        "kol-intraday-audit", help="audit intraday event context coverage"
    )
    p_intraday_audit.set_defaults(func=kol_intraday_audit)

    p_market_sync = sub.add_parser("market-sync", help="incrementally update active market instruments")
    p_market_sync.add_argument("--as-of")
    p_market_sync.add_argument("--symbols")
    p_market_sync.add_argument("--alerts-only", action="store_true")
    p_market_sync.set_defaults(func=market_sync)

    p_market_audit = sub.add_parser("market-audit", help="audit normalized data and optionally compare providers")
    p_market_audit.add_argument("--symbols")
    p_market_audit.add_argument("--as-of")
    p_market_audit.add_argument("--cross-check", action="store_true")
    p_market_audit.set_defaults(func=market_audit)

    p_market_weekly = sub.add_parser("market-weekly", help="refresh master data and weekly research snapshots")
    p_market_weekly.add_argument("--as-of")
    p_market_weekly.add_argument("--master-only", action="store_true")
    p_market_weekly.set_defaults(func=market_weekly)

    p_market_etf_master = sub.add_parser("market-etf-master", help=argparse.SUPPRESS)
    p_market_etf_master.add_argument("--as-of")
    p_market_etf_master.set_defaults(func=market_etf_master)

    p_market_baostock_master = sub.add_parser("market-baostock-master", help=argparse.SUPPRESS)
    p_market_baostock_master.add_argument("--as-of")
    p_market_baostock_master.set_defaults(func=market_baostock_master)

    p_market_aux = sub.add_parser("market-aux-fetch", help=argparse.SUPPRESS)
    p_market_aux.add_argument("--symbol", required=True)
    p_market_aux.add_argument(
        "--dataset",
        choices=["valuation", "financial_summary", "announcements", "fund_flow"],
        required=True,
    )
    p_market_aux.add_argument("--as-of", required=True)
    p_market_aux.set_defaults(func=market_aux_fetch)

    p_digest = sub.add_parser("data-digest", help="emit one combined KOL and market data summary")
    p_digest.add_argument("--notify", action="store_true")
    p_digest.set_defaults(func=data_digest)

    p_portfolio_import = sub.add_parser("portfolio-import", help="import an auditable personal transaction JSON file")
    p_portfolio_import.add_argument("--file", required=True)
    p_portfolio_import.set_defaults(func=portfolio_import)

    p_portfolio_report = sub.add_parser("portfolio-report", help="summarize recorded positions and realized cash results")
    p_portfolio_report.set_defaults(func=portfolio_report)

    p_ui_doctor = sub.add_parser("kol-ui-doctor", help="check the local KOL web console")
    p_ui_doctor.set_defaults(func=kol_ui_doctor)

    p_public_doctor = sub.add_parser(
        "public-dataset-doctor",
        help="check the private inputs required for a sanitized public dataset export",
    )
    p_public_doctor.set_defaults(func=public_dataset_doctor_cmd)

    p_public_export = sub.add_parser(
        "public-dataset-export",
        help="export a deterministic privacy-filtered public KOL audit dataset",
    )
    p_public_export.add_argument("--output", required=True)
    p_public_export.set_defaults(func=public_dataset_export_cmd)

    p_public_validate = sub.add_parser(
        "public-dataset-validate",
        help="validate a previously exported public KOL audit dataset",
    )
    p_public_validate.add_argument("--input", required=True)
    p_public_validate.set_defaults(func=public_dataset_validate_cmd)
    return parser


def _configure_console_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def main(argv: Iterable[str] | None = None) -> None:
    _configure_console_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
