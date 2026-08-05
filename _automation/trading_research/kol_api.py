from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from kol_posts import (
    CredentialStorageError,
    CredentialValidationError,
    DeepSeekPostClassifier,
    DeepSeekCredentialStore,
    KeyringCredentialStore,
    KolPostStore,
    ModelWorkerBusyError,
    NitterCredentialStore,
    RapidOcrBatchClassifier,
    RuleClassifier,
    STRUCTURED_REVIEW_VERSION,
    UnlimitedOcrBatchClassifier,
    ZhihuProfileProvider,
    build_post_classifier,
    build_x_post_provider,
    classify_pending_with_codex,
    initialize_seed_kols,
    load_stock_aliases,
    media_disk_usage,
    run_post_fetch,
    validate_twitter_credentials,
)
from kol_review import approve_post, approve_recommendation_draft, post_review_lock
from kol_leaderboard import build_kol_leaderboard
from kol_performance import KolPerformanceService
from kol_tracker import MARK_FIELDS, SHANGHAI, KolStore, now_iso, validate_event
from event_context import FEATURE_VERSION, event_context_input_hash
from nitter_runtime import docker_container_health, docker_health, timeline_health, xtf_version
from market_data import FreeStockDBMarketProvider, Instrument, MarketStore
from market_indicators import INDICATOR_VERSION, compute_daily_indicators
from freestockdb_runtime import FreeStockDBRuntime
from kol_intraday import backfill_event_intraday
from board_mainline import BoardMainlineStore, FallbackBoardProvider, sync_board_snapshot
from event_dossier import EventDossierService
from event_research_ai import build_event_research_interpreter
from event_research_service import EventMethodResearchService
from pipeline_jobs import read_refresh_state, run_post_approval_refresh
from review_agent import ReviewAgentRepository, rollback_decision
from recommendation_drafts import RecommendationDraftRepository, review_window_utc
from recommendation_processing import materialize_recommendation_drafts
from stock_leads import extract_stock_leads, reconcile_exact_stock_leads


ROOT = Path(__file__).resolve().parents[2]
OPERATOR_TASKS = {
    "morning": "KOL_Morning_Pipeline",
    "fetch": "KOL_Post_Fetch_Daily",
    "zhihu": "KOL_Zhihu_Fetch_Morning",
}


@dataclass(frozen=True)
class ApiSettings:
    runtime_root: Path
    frontend_dist: Path
    codex_schema: Path
    xtf_command: Path | None = None
    nitter_url: str = "http://127.0.0.1:9377"
    freestockdb_root: Path = Path("<AI_HUB_HOME>/stockdb")
    freestockdb_url: str = "http://127.0.0.1:7899"

    @classmethod
    def default(cls) -> "ApiSettings":
        runtime = Path(os.environ.get("TRADING_RUNTIME_ROOT", ROOT / "_runtime" / "trading"))
        return cls(
            runtime_root=runtime,
            frontend_dist=ROOT / "_automation" / "trading_research" / "ui" / "dist",
            codex_schema=Path(__file__).with_name("kol_classifier_schema.json"),
            xtf_command=ROOT / "_runtime" / "venv-x-fetcher" / "Scripts" / "xtf.exe",
            nitter_url=os.environ.get("KOL_NITTER_URL", "http://127.0.0.1:9377"),
            freestockdb_root=Path(os.environ.get("FREESTOCKDB_ROOT", "<AI_HUB_HOME>/stockdb")),
            freestockdb_url=os.environ.get("FREESTOCKDB_URL", "http://127.0.0.1:7899"),
        )


class KolCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    handle: str = Field(min_length=1, max_length=100)
    platform: str = Field(default="X", pattern="^(X|Zhihu)$")
    profile_url: str = Field(default="", max_length=500)
    domain: str = Field(default="", max_length=300)
    tracking_mode: str = Field(default="all", max_length=30)


class KolPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    domain: str | None = Field(default=None, max_length=300)
    status: str | None = None
    tracking_mode: str | None = Field(default=None, max_length=30)


class KolBackfillRequest(BaseModel):
    count: int = Field(default=200, ge=1)


class DigestAuthorProfileInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    profile_url: str = Field(min_length=20, max_length=500)


class DigestAuthorProfileBatch(BaseModel):
    profiles: list[DigestAuthorProfileInput] = Field(min_length=1, max_length=200)


class FetchRequest(BaseModel):
    max_count: int = Field(default=50, ge=1, le=100)
    classify: bool = True
    provider: str = Field(default="auto", pattern="^(auto|twitter|nitter)$")


class TwitterCredentialRequest(BaseModel):
    auth_token: str = Field(min_length=1, max_length=8192)
    ct0: str = Field(min_length=1, max_length=8192)


class DeepSeekCredentialRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=1024)


class PerformanceRefreshRequest(BaseModel):
    as_of: str | None = None


class Draft(BaseModel):
    symbol: str
    security_name: str = ""
    direction: str
    action: str = Field(default="watch", pattern="^(buy|add|hold|watch|reduce|sell|avoid)$")
    horizon: str = Field(default="unspecified", pattern="^(intraday|short|swing|medium_long|unspecified)$")
    strength: str = Field(default="unspecified", pattern="^(explicit|moderate|weak|unspecified)$")
    thesis: str
    evidence_type: str
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_spans: list[str] = Field(default_factory=list)
    evidence_source: str = "text"
    conditions: list[str] = Field(default_factory=list)
    depends_on_ocr: bool = False
    mention_kind: str = "recommendation"


class ReviewRequest(BaseModel):
    action: str
    note: str = Field(default="", max_length=2000)
    drafts: list[Draft] = Field(default_factory=list)
    confirm_leads: bool = False
    refresh_returns: bool = False


class RecommendationDraftPatch(BaseModel):
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, min_length=1, max_length=100)
    direction: str | None = Field(default=None, pattern="^(long|short)$")
    action: str | None = Field(default=None, pattern="^(buy|add|hold|watch|reduce|sell|avoid)$")
    horizon: str | None = Field(default=None, pattern="^(intraday|short|swing|medium_long|unspecified)$")
    strength: str | None = Field(default=None, pattern="^(explicit|moderate|weak|unspecified)$")
    thesis: str | None = Field(default=None, min_length=1, max_length=4000)
    evidence_type: str | None = Field(default=None, max_length=40)
    evidence_spans: list[str] | None = None
    conditions: list[str] | None = None
    mention_kind: str | None = Field(default=None, max_length=40)
    correction_type: str | None = Field(
        default=None,
        pattern="^(missed_stock|wrong_mapping|wrong_direction|wrong_thesis|wrong_evidence|wrong_content_type)$",
    )
    note: str = Field(default="", max_length=2000)


class RecommendationDraftAction(BaseModel):
    note: str = Field(default="", max_length=2000)


class ManualRecommendationDraftRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    security_name: str = Field(min_length=1, max_length=100)
    direction: str = Field(default="long", pattern="^(long|short)$")
    action: str = Field(default="watch", pattern="^(buy|add|hold|watch|reduce|sell|avoid)$")
    horizon: str = Field(default="unspecified", pattern="^(intraday|short|swing|medium_long|unspecified)$")
    strength: str = Field(default="explicit", pattern="^(explicit|moderate|weak|unspecified)$")
    thesis: str = Field(min_length=1, max_length=4000)
    evidence_spans: list[str] = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    evidence_source: str = Field(default="text", pattern="^(text|ocr|image)$")
    depends_on_ocr: bool = False
    review_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    correction_type: str = Field(default="missed_stock", pattern="^missed_stock$")
    note: str = Field(min_length=1, max_length=2000)


class StockLeadReviewRequest(BaseModel):
    action: str = Field(pattern="^(confirmed|ignored|pending)$")
    note: str = Field(default="", max_length=2000)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, max_length=100)


class ReviewAgentSettingsRequest(BaseModel):
    mode: str = Field(pattern="^(shadow|enabled)$")


class EventUpdateRequest(BaseModel):
    action: str = Field(pattern="^(activate|exclude|restore|archive)$")
    kol_name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: str | None = Field(default=None, min_length=1, max_length=30)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_note: str | None = Field(default=None, min_length=1, max_length=2000)
    posted_at: str | None = Field(default=None, min_length=1, max_length=80)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, max_length=100)
    direction: str | None = Field(default=None, pattern="^(long|short)$")
    thesis: str | None = Field(default=None, min_length=1, max_length=4000)
    exclusion_reason: str | None = Field(default=None, max_length=2000)


class EventAmendmentRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    kol_name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: str | None = Field(default=None, min_length=1, max_length=30)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_note: str | None = Field(default=None, max_length=2000)
    posted_at: str | None = Field(default=None, min_length=1, max_length=80)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, min_length=1, max_length=100)
    direction: str | None = Field(default=None, pattern="^(long|short)$")
    thesis: str | None = Field(default=None, min_length=1, max_length=4000)


class InstrumentCreate(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=100)
    instrument_type: str = Field(pattern="^(stock|etf|index)$")
    exchange: str = Field(pattern="^(SH|SZ|BJ)$")
    lifecycle: str = Field(default="tracking", pattern="^(pinned|tracking|archived)$")
    source: str = Field(default="manual", max_length=100)


class InstrumentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    lifecycle: str | None = Field(default=None, pattern="^(pinned|tracking|archived)$")
    status: str | None = Field(default=None, max_length=30)


class MarketSyncRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None


class BoardSyncRequest(BaseModel):
    as_of: date | None = None


class FreeStockDBUpdateRequest(BaseModel):
    dry_run: bool = False


def _sanitize_post(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("raw_json", None)
    result["local_media"] = [
        {
            **item,
            "api_url": f"/media/{result['post_id']}/{Path(str(item.get('path') or '')).name}",
        }
        for item in result.get("local_media", [])
        if item.get("path")
    ]
    return result


def _task_status(task_name: str) -> str:
    if os.name != "nt":
        return "unsupported"
    completed = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", task_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return "installed" if completed.returncode == 0 else "missing"


def _read_process_pid(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    if isinstance(payload, dict):
        payload = payload.get("pid")
    value = str(payload or "").strip()
    return value if value.isdigit() else ""


def _start_scheduled_task(task_name: str) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("scheduled task control is only available on Windows")
    completed = subprocess.run(
        ["schtasks.exe", "/Run", "/TN", task_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unable to start scheduled task").strip()
        raise RuntimeError(detail[-1000:])
    return {"ok": True, "task": task_name, "started_at": now_iso()}


def _fallback_mode(runtime_root: Path) -> str:
    try:
        value = json.loads((runtime_root / "kol" / "fallback_mode.json").read_text(encoding="utf-8")).get("mode")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = "shadow"
    return value if value in {"shadow", "enabled"} else "shadow"


def create_app(
    settings: ApiSettings | None = None,
    credential_store: KeyringCredentialStore | None = None,
    nitter_credential_store: NitterCredentialStore | None = None,
    deepseek_credential_store: DeepSeekCredentialStore | None = None,
) -> FastAPI:
    config = settings or ApiSettings.default()
    kol_root = config.runtime_root / "kol"
    post_store = KolPostStore(kol_root / "posts.db", kol_root / "media")
    post_store.interrupt_stale_fetch_runs()
    event_store = KolStore(kol_root)
    performance = KolPerformanceService(event_store, post_store=post_store)
    market_store = MarketStore(config.runtime_root / "market")
    freestockdb_runtime = FreeStockDBRuntime(
        config.freestockdb_root,
        config.freestockdb_url,
        runtime_root=config.runtime_root,
    )
    board_store = BoardMainlineStore(market_store)
    review_agent = ReviewAgentRepository(post_store)
    recommendation_drafts = RecommendationDraftRepository(post_store)
    credentials = credential_store or KeyringCredentialStore()
    nitter_credentials = nitter_credential_store or NitterCredentialStore()
    deepseek_credentials = deepseek_credential_store or DeepSeekCredentialStore()
    event_research = EventMethodResearchService(
        event_store,
        market_store,
        interpreter=build_event_research_interpreter(
            Path(__file__).with_name("event_research_schema.json"),
            ROOT,
            deepseek_credentials=deepseek_credentials,
        ),
    )
    event_dossier = EventDossierService(
        post_store,
        event_store,
        market_store,
        method_research_service=event_research,
    )
    xtf_command = config.xtf_command or ROOT / "_runtime" / "venv-x-fetcher" / "Scripts" / "xtf.exe"
    zhihu_provider = ZhihuProfileProvider(
        ROOT / "_automation" / "hermes-capture" / "zhihu_profile_capture.py",
        python_command=sys.executable,
        browser_path=os.environ.get("ZHIHU_BROWSER_PATH", ""),
        profile_directory=os.environ.get("ZHIHU_PROFILE_DIRECTORY", "Default"),
        user_data_dir=os.environ.get(
            "ZHIHU_USER_DATA_DIR",
            str(Path.home() / "AppData" / "Local" / "hermes" / "browser-profiles" / "zhihu-edge"),
        ),
        port=int(os.environ.get("ZHIHU_CDP_PORT", "9222")),
    )
    ocr_root = Path(os.environ.get("UNLIMITED_OCR_ROOT", ROOT.parent / "Unlimited-OCR"))
    ocr_runner = Path(__file__).with_name("unlimited_ocr_batch.py")
    rapid_ocr_python = Path(os.environ.get(
        "RAPID_OCR_PYTHON",
        ROOT / "_runtime" / "venv-ocr-fast" / "Scripts" / "python.exe",
    ))
    rapid_ocr_runner = Path(__file__).with_name("rapid_ocr_batch.py")
    initialize_seed_kols(post_store)

    app = FastAPI(title="KOL Research Console", version="3.0.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.settings = config
    app.state.post_store = post_store
    app.state.event_store = event_store
    app.state.kol_performance = performance
    app.state.market_store = market_store
    app.state.freestockdb_runtime = freestockdb_runtime
    app.state.board_store = board_store
    app.state.event_dossier = event_dossier
    app.state.event_research = event_research
    app.state.review_agent = review_agent
    app.state.recommendation_drafts = recommendation_drafts
    app.state.deepseek_credentials = deepseek_credentials

    def safe_market_health() -> dict[str, Any]:
        try:
            return market_store.health()
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            locked = any(token in lowered for token in ("lock", "already open", "cannot open file"))
            return {
                "ok": False,
                "status": "degraded",
                "market_status": "market_locked" if locked else "market_unavailable",
                "daily_data_status": "market_locked" if locked else "market_unavailable",
                "market_session_status": "unknown",
                "database": str(market_store.db_path),
                "lagging_symbols": [],
                "lagging_symbol_count": 0,
                "error_type": "database_lock" if locked else type(exc).__name__,
                "error": message[-1000:],
            }

    freestockdb_health_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
    freestockdb_health_lock = threading.Lock()

    def safe_freestockdb_health(*, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        cached = freestockdb_health_cache.get("value")
        if not force and cached is not None and now < freestockdb_health_cache["expires_at"]:
            return cached
        with freestockdb_health_lock:
            now = time.monotonic()
            cached = freestockdb_health_cache.get("value")
            if not force and cached is not None and now < freestockdb_health_cache["expires_at"]:
                return cached
            try:
                value = freestockdb_runtime.doctor(include_samples=True)
            except Exception as exc:
                value = {
                    "ok": False,
                    "status": "unavailable",
                    "root": str(config.freestockdb_root),
                    "base_url": config.freestockdb_url,
                    "error": str(exc)[:1000],
                }
            freestockdb_health_cache.update(
                {"expires_at": time.monotonic() + 60.0, "value": value}
            )
            return value

    def classification_aliases() -> dict[str, str]:
        aliases = load_stock_aliases(config.runtime_root / "watchlist.csv", event_store.events_path)
        for symbol, instrument in market_store.instrument_map().items():
            name = str(instrument.get("name") or "").strip()
            if name:
                aliases.setdefault(symbol, name)
        return aliases

    def queue_confirmed_leads(symbols: list[str]) -> list[str]:
        queued: list[str] = []
        for symbol in symbols:
            if market_store.get_instrument(symbol) is None:
                continue
            leads = post_store.list_stock_leads(status="confirmed", symbol=symbol, limit=1)
            if not leads:
                continue
            try:
                mentioned = datetime.fromisoformat(str(leads[0]["posted_at"]).replace("Z", "+00:00")).date()
            except ValueError:
                mentioned = date.today()
            market_store.touch_mention(symbol, mentioned)
            market_store.enqueue_sync(symbol, reason=f"kol_lead:{leads[0]['post_id']}")
            queued.append(symbol)
        return sorted(set(queued))

    def extract_leads() -> dict[str, Any]:
        result = extract_stock_leads(
            post_store,
            instruments=market_store.instrument_map(),
            aliases=load_stock_aliases(config.runtime_root / "watchlist.csv", event_store.events_path),
        )
        reconciled = reconcile_exact_stock_leads(post_store, market_store.instrument_map())
        symbols = sorted(set(result.confirmed_symbols) | set(reconciled))
        return {
            **asdict(result),
            "reconciled_symbols": reconciled,
            "queued_symbols": queue_confirmed_leads(symbols),
        }

    def extract_leads_for_post(post_id: str) -> dict[str, Any]:
        result = extract_stock_leads(
            post_store,
            instruments=market_store.instrument_map(),
            aliases=load_stock_aliases(config.runtime_root / "watchlist.csv", event_store.events_path),
            post_ids=[post_id],
        )
        reconciled = reconcile_exact_stock_leads(
            post_store,
            market_store.instrument_map(),
            post_id=post_id,
        )
        symbols = sorted(set(result.confirmed_symbols) | set(reconciled))
        return {
            **asdict(result),
            "reconciled_symbols": reconciled,
            "queued_symbols": queue_confirmed_leads(symbols),
        }

    def recommendation_queue_scope(post: dict[str, Any]) -> str:
        review_date = date.today().isoformat()
        window_start, window_end = review_window_utc(review_date)
        posted_at = str(post.get("posted_at_utc") or "")
        return "morning" if window_start <= posted_at < window_end else "backlog"

    def reprocess_recommendation_post(post_id: str) -> dict[str, Any]:
        with post_store.model_worker():
            post = post_store.get_post(post_id)
            queue_scope = recommendation_queue_scope(post)
            review_date = date.today().isoformat()
            rule_classifier = RuleClassifier(classification_aliases())
            structured = rule_classifier.classify_structured_text(post)
            if structured is not None:
                post_store.save_model_classification(
                    post_id,
                    structured,
                    model_name="structured-rules",
                    prompt_version=STRUCTURED_REVIEW_VERSION,
                )
            else:
                claimed = post_store.claim_posts_for_model(1, post_id=post_id, force=True)
                if not claimed:
                    raise ModelWorkerBusyError("post classification is already running")
                current = claimed[0]
                classifier = DeepSeekPostClassifier(
                    config.codex_schema,
                    deepseek_credentials,
                )
                try:
                    payload = classifier.classify(current)
                    post_store.save_model_classification(
                        post_id,
                        payload,
                        model_name=classifier.model_name,
                        prompt_version=classifier.prompt_version,
                    )
                except Exception as exc:
                    post_store.save_model_classification(
                        post_id,
                        None,
                        model_name=classifier.model_name,
                        prompt_version=classifier.prompt_version,
                        error=str(exc),
                    )
                    raise
            result = materialize_recommendation_drafts(
                post_store,
                recommendation_drafts,
                rule_classifier,
                post_id,
                instruments=market_store.instrument_map(),
                queue_scope=queue_scope,
                review_date=review_date,
                rules_first=False,
            )
        extract_leads_for_post(post_id)
        return result

    def board_with_related_events(board_code: str, board_type: str | None) -> dict[str, Any]:
        board = board_store.get_board(board_code, board_type)
        if board is None:
            raise HTTPException(404, "board not found")
        symbols = {str(item["symbol"]) for item in board.get("members", [])}
        board["related_events"] = [
            {
                "event_id": event.event_id,
                "kol_name": event.kol_name,
                "platform": event.platform,
                "posted_at": event.posted_at,
                "symbol": event.symbol,
                "security_name": event.security_name,
                "direction": event.direction,
                "status": event.status,
                "source_url": event.source_url,
            }
            for event in event_store.load_events()
            if event.status in {"active", "completed"} and event.symbol in symbols
        ]
        return board

    def technical_context_for(event: Any) -> dict[str, Any] | None:
        return market_store.get_event_technical_context(
            event.event_id,
            input_hash=event_context_input_hash(event.symbol, event.posted_at),
        )

    def sanitize_recommendation_draft(value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        result["local_media"] = [
            {
                **item,
                "api_url": f"/media/{result['post_id']}/{Path(str(item.get('path') or '')).name}",
            }
            for item in result.get("local_media", [])
            if item.get("path")
        ]
        return result

    def confirm_approval_leads(post_id: str, drafts: list[Draft], note: str) -> list[str]:
        post = post_store.get_post(post_id)
        symbols = sorted({draft.symbol for draft in drafts})
        instruments: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            instrument = market_store.get_instrument(symbol)
            if instrument is None:
                raise ValueError(
                    f"{symbol} is not in the instrument master; refresh the master or correct the symbol"
                )
            instruments[symbol] = instrument

        for draft in drafts:
            matches = post_store.list_stock_leads(post_id=post_id, symbol=draft.symbol, limit=10)
            if not matches:
                post_store.upsert_stock_lead(
                    {
                        "post_id": post_id,
                        "kol_id": post["kol_id"],
                        "symbol": draft.symbol,
                        "security_name": draft.security_name or instruments[draft.symbol]["name"],
                        "instrument_type": instruments[draft.symbol]["instrument_type"],
                        "direction": draft.direction,
                        "mention_kind": "recommendation",
                        "evidence_text": draft.thesis,
                        "extraction_method": "unified_review",
                        "confidence": draft.confidence,
                        "status": "pending",
                    }
                )
                matches = post_store.list_stock_leads(post_id=post_id, symbol=draft.symbol, limit=10)
            lead = matches[0]
            if lead["status"] == "ignored":
                post_store.review_stock_lead(
                    lead["id"],
                    "pending",
                    "Restored by unified post review.",
                )
            if lead["status"] != "confirmed":
                post_store.review_stock_lead(
                    lead["id"],
                    "confirmed",
                    note or "Confirmed during unified post review.",
                    symbol=draft.symbol,
                    security_name=draft.security_name or instruments[draft.symbol]["name"],
                )

            try:
                mentioned = datetime.fromisoformat(str(post["posted_at"]).replace("Z", "+00:00")).date()
            except ValueError:
                mentioned = date.today()
            market_store.touch_mention(draft.symbol, mentioned)
            market_store.enqueue_sync(draft.symbol, reason=f"kol_review:{post_id}")
        return symbols

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        value = post_store.summary()
        events = event_store.load_events()
        marks = event_store.load_marks()
        latest: dict[str, dict[str, str]] = {}
        for mark in marks:
            if mark["event_id"] not in latest or mark["trade_date"] > latest[mark["event_id"]]["trade_date"]:
                latest[mark["event_id"]] = mark
        value.update(
            {
                "active_events": sum(event.status == "active" for event in events),
                "completed_events": sum(event.status == "completed" for event in events),
                "checkpoint_count": len(event_store.load_checkpoints()),
                "latest_event_marks": list(latest.values()),
                "market": safe_market_health(),
            }
        )
        return value

    @app.get("/api/kols")
    def list_kols(status: str | None = None) -> list[dict[str, Any]]:
        return post_store.list_kols(status)

    @app.get("/api/digest-authors")
    def digest_authors(limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
        return post_store.list_digest_authors(limit)

    @app.put("/api/digest-authors/profiles")
    def bind_digest_author_profiles(body: DigestAuthorProfileBatch) -> dict[str, Any]:
        try:
            items = post_store.upsert_digest_author_profiles(
                [item.model_dump() for item in body.profiles]
            )
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        all_authors = post_store.list_digest_authors(500)
        return {
            "updated": len(items),
            "resolved": sum(bool(item.get("profile_url")) for item in all_authors),
            "unresolved": sum(not item.get("profile_url") for item in all_authors),
            "items": items,
        }

    @app.get("/api/kol-leaderboard")
    def kol_leaderboard() -> dict[str, Any]:
        result = performance.compute(as_of=date.today(), window="all", horizon="1W", primary_only=True)
        rows: list[dict[str, Any]] = []
        for item in result["rows"]:
            horizons: dict[str, Any] = {}
            for horizon, metrics in item["horizons"].items():
                value = dict(metrics)
                value.setdefault("samples", value.get("batch_count", 0))
                value.setdefault("median_adverse", value.get("median_mae"))
                horizons[horizon] = value
            rows.append({
                "kol_name": item["kol_name"],
                "kol_key": item["kol_key"],
                "kol_id": item["kol_id"],
                "kol_handle": item["kol_handle"],
                "platform": item["platform"],
                "tier": item["tier"],
                "rank": item["rank"],
                "rank_horizon": item["rank_horizon"],
                "score": None,
                "event_count": item["horizons"]["1W"]["event_count"],
                "executable_event_count": item["horizons"]["1W"]["event_count"],
                "horizons": horizons,
            })
        return {
            "policy": {
                "primary_metric": "platform-separated median batch excess return",
                "batch_weighting": "same-post stocks are equal-weighted into one batch",
                "score": None,
                "sample_policy": "small samples are shown but never formally ranked",
            },
            "as_of": result["as_of"],
            "rows": rows,
        }

    @app.get("/api/kol-performance")
    def kol_performance(
        platform: str | None = None,
        window: str = Query(default="all", pattern="^(7|30|90|all)$"),
        horizon: str = Query(default="1W", pattern="^(1W|1M|3M|6M)$"),
        as_of: str | None = None,
    ) -> dict[str, Any]:
        try:
            effective_date = date.fromisoformat(as_of) if as_of else date.today()
            return performance.compute(
                as_of=effective_date,
                platform=platform,
                window=window,
                horizon=horizon,
                primary_only=True,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/kol-performance/{kol_key}")
    def kol_performance_detail(kol_key: str, as_of: str | None = None) -> dict[str, Any]:
        try:
            effective_date = date.fromisoformat(as_of) if as_of else date.today()
            result = performance.compute(as_of=effective_date, platform="all", window="all", horizon="1W", primary_only=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        row = next((item for item in result["rows"] if item["kol_key"] == kol_key), None)
        if row is None:
            raise HTTPException(404, "KOL performance identity not found")
        return {
            "version": result["version"],
            "as_of": result["as_of"],
            "row": row,
            "series": {
                horizon: performance.store.series(kol_key, horizon=horizon, window_name="all")
                for horizon in ("1W", "1M", "3M", "6M")
            },
        }

    @app.get("/api/kol-performance/{kol_key}/series")
    def kol_performance_series(
        kol_key: str,
        horizon: str = Query(default="1W", pattern="^(1W|1M|3M|6M)$"),
        range: str = Query(default="all", pattern="^(7|30|90|all)$"),
    ) -> dict[str, Any]:
        points = performance.store.series(kol_key, horizon=horizon, window_name="all")
        return {
            "kol_key": kol_key,
            "horizon": horizon,
            "range": range,
            "points": points,
            "point_count": len(points),
        }

    @app.post("/api/kol-performance/refresh")
    def refresh_kol_performance(body: PerformanceRefreshRequest | None = None) -> dict[str, Any]:
        try:
            effective_date = date.fromisoformat(body.as_of) if body and body.as_of else date.today()
            performance.migrate_event_identities()
            result = performance.refresh(as_of=effective_date)
            return {
                "ok": True,
                "as_of": result["as_of"],
                "run_id": result["run_id"],
                "snapshots_inserted": result["snapshots_inserted"],
                "coverage": result["coverage"],
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/kols", status_code=201)
    def create_kol(body: KolCreate) -> dict[str, Any]:
        try:
            tracking_mode = body.tracking_mode
            if body.platform == "Zhihu" and tracking_mode == "all":
                tracking_mode = "direct_profile"
            kol_id, created = post_store.add_kol(
                body.display_name,
                body.handle,
                body.domain,
                tracking_mode=tracking_mode,
                platform=body.platform,
                profile_url=body.profile_url,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not created:
            raise HTTPException(409, "KOL handle already exists")
        return post_store.get_kol(kol_id) or {}

    @app.patch("/api/kols/{kol_id}")
    def patch_kol(kol_id: int, body: KolPatch) -> dict[str, Any]:
        try:
            return post_store.update_kol(kol_id, body.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/kols/{kol_id}/backfill")
    def queue_kol_backfill(kol_id: int, body: KolBackfillRequest) -> dict[str, Any]:
        try:
            return post_store.queue_backfill(kol_id, body.count)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/posts")
    def list_posts(
        review_status: str | None = None,
        kol_id: int | None = None,
        candidate_only: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [
            _sanitize_post(value)
            for value in post_store.list_posts(
                review_status=review_status,
                kol_id=kol_id,
                candidate_only=candidate_only,
                limit=limit,
                offset=offset,
            )
        ]

    @app.get("/api/posts/{post_id}")
    def get_post(post_id: str) -> dict[str, Any]:
        try:
            return _sanitize_post(post_store.get_post(post_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/posts/{post_id}/classify")
    def classify_post(post_id: str) -> dict[str, Any]:
        try:
            post_store.get_post(post_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            result = reprocess_recommendation_post(post_id)
        except ModelWorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"recommendation processing failed: {exc}") from exc
        response = _sanitize_post(post_store.get_post(post_id))
        response["recommendation_drafts"] = [
            sanitize_recommendation_draft(item) for item in result["drafts"]
        ]
        response["draft_generation_status"] = result["draft_generation_status"]
        return response

    @app.post("/api/posts/{post_id}/recommendation-reprocess")
    def reprocess_recommendation(post_id: str) -> dict[str, Any]:
        try:
            result = reprocess_recommendation_post(post_id)
            return {
                "ok": True,
                "post": _sanitize_post(post_store.get_post(post_id)),
                "draft_generation_status": result["draft_generation_status"],
                "draft_generation_error": result["draft_generation_error"],
                "drafts": [sanitize_recommendation_draft(item) for item in result["drafts"]],
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ModelWorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"recommendation reprocess failed: {exc}") from exc

    @app.get("/api/morning-review")
    def morning_review(
        review_date: date = Query(default_factory=date.today),
        status: str | None = Query(default=None, pattern="^(ready|needs_attention|approved|rejected)$"),
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        include_history: bool = Query(default=False),
        history_page: int = Query(default=1, ge=1),
        history_page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        day = review_date.isoformat()
        drafts = recommendation_drafts.list_drafts(
            review_date=day,
            status=status,
            queue_scope="morning",
            limit=limit,
            offset=offset,
        )
        window_start, window_end = review_window_utc(day)
        posts = post_store.list_posts(
            posted_from_utc=window_start,
            posted_to_utc=window_end,
            limit=500,
        )
        approved_drafts = recommendation_drafts.list_drafts(
            reviewed_date=day,
            status="approved",
            limit=1000,
        )
        history_drafts: list[dict[str, Any]] = []
        if include_history:
            history_drafts = recommendation_drafts.list_drafts(
                status=status,
                queue_scope="backlog",
                limit=history_page_size,
                offset=(history_page - 1) * history_page_size,
            )
        return {
            "review_date": day,
            "summary": recommendation_drafts.morning_summary(day),
            "delivery": recommendation_drafts.morning_delivery(day),
            "posts": [_sanitize_post(item) for item in posts],
            "drafts": [sanitize_recommendation_draft(item) for item in drafts],
            "history_drafts": [sanitize_recommendation_draft(item) for item in history_drafts],
            "history_page": history_page,
            "history_page_size": history_page_size,
            "history_has_more": len(history_drafts) == history_page_size,
            "approved_drafts": [sanitize_recommendation_draft(item) for item in approved_drafts],
        }

    @app.get("/api/recommendation-drafts/{draft_id}")
    def get_recommendation_draft(draft_id: int) -> dict[str, Any]:
        try:
            return sanitize_recommendation_draft(recommendation_drafts.get_draft(draft_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/recommendation-drafts/{draft_id}/revisions")
    def recommendation_draft_revisions(draft_id: int) -> list[dict[str, Any]]:
        try:
            recommendation_drafts.get_draft(draft_id)
            return recommendation_drafts.list_revisions(draft_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/posts/{post_id}/recommendation-drafts", status_code=201)
    def create_manual_recommendation_draft(
        post_id: str,
        body: ManualRecommendationDraftRequest,
    ) -> dict[str, Any]:
        values = body.model_dump(exclude={"review_date", "correction_type", "note"})
        try:
            created = recommendation_drafts.create_manual_draft(
                post_id,
                values,
                market_store.instrument_map(),
                review_date=body.review_date,
                correction_type=body.correction_type,
                note=body.note,
            )
            return sanitize_recommendation_draft(created)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/recommendation-drafts/{draft_id}")
    def patch_recommendation_draft(
        draft_id: int,
        body: RecommendationDraftPatch,
    ) -> dict[str, Any]:
        values = body.model_dump(exclude_none=True)
        note = str(values.pop("note", ""))
        correction_type = values.pop("correction_type", None)
        try:
            updated = recommendation_drafts.update_draft(
                draft_id,
                values,
                market_store.instrument_map(),
                note=note,
                correction_type=correction_type,
            )
            return sanitize_recommendation_draft(updated)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/recommendation-drafts/{draft_id}/approve")
    def approve_draft(
        draft_id: int,
        body: RecommendationDraftAction,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            current = recommendation_drafts.get_draft(draft_id)
            if current["status"] == "approved":
                return sanitize_recommendation_draft(current)
            if current["status"] not in {"ready", "needs_attention"}:
                raise ValueError("recommendation draft is not pending review")
            hard_blockers = set(current["attention_reasons"]) - {"image_dependency"}
            if hard_blockers:
                raise ValueError(
                    "recommendation draft still needs attention: " + ", ".join(sorted(hard_blockers))
                )
            instrument = market_store.get_instrument(current["symbol"])
            if instrument is None:
                raise ValueError("instrument is not available in the market master")
            event_draft = {
                "symbol": current["symbol"],
                "security_name": current["security_name"] or instrument["name"],
                "direction": current["direction"],
                "thesis": current["thesis"],
                "evidence_type": current["evidence_type"],
                "conditions": current["conditions"],
            }
            result = approve_recommendation_draft(
                post_store,
                event_store,
                current["post_id"],
                event_draft,
                note=body.note,
                audit_detail={"recommendation_draft_id": draft_id},
            )
            approved = recommendation_drafts.mark_approved(
                draft_id,
                result.event_ids[0],
                body.note,
            )
            review_agent.mark_overridden_for_post(
                current["post_id"],
                "approved",
                [event_draft],
            )
            try:
                mentioned_at = datetime.fromisoformat(
                    str(current["posted_at"]).replace("Z", "+00:00")
                ).date()
            except ValueError:
                mentioned_at = date.today()
            market_store.touch_mention(current["symbol"], mentioned_at)
            market_store.enqueue_sync(
                current["symbol"],
                reason=f"recommendation_draft:{draft_id}",
            )
            background_tasks.add_task(
                run_post_approval_refresh,
                config.runtime_root,
                ROOT,
                [current["symbol"]],
                as_of=date.today(),
            )
            background_tasks.add_task(event_dossier.refresh, result.event_ids[0])
            return {
                **sanitize_recommendation_draft(approved),
                "created_event": bool(result.created_events),
                "refresh_status": "queued",
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/recommendation-drafts/{draft_id}/reject")
    def reject_draft(draft_id: int, body: RecommendationDraftAction) -> dict[str, Any]:
        try:
            return sanitize_recommendation_draft(recommendation_drafts.reject(draft_id, body.note))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/recommendation-drafts/{draft_id}/retry")
    def retry_draft(draft_id: int) -> dict[str, Any]:
        try:
            current = recommendation_drafts.get_draft(draft_id)
            with post_store.model_worker():
                claimed = post_store.claim_posts_for_model(
                    1,
                    post_id=current["post_id"],
                    force=True,
                )
                if not claimed:
                    raise ModelWorkerBusyError("post classification is already running")
                post = claimed[0]
                classifier = RuleClassifier(classification_aliases())
                payload = classifier.classify_structured_text(post)
                model_name = "structured-rules"
                prompt_version = STRUCTURED_REVIEW_VERSION
                if payload is None:
                    model = DeepSeekPostClassifier(
                        config.codex_schema,
                        deepseek_credentials,
                    )
                    payload = model.classify(post)
                    model_name = model.model_name
                    prompt_version = model.prompt_version
                post_store.save_model_classification(
                    current["post_id"],
                    payload,
                    model_name=model_name,
                    prompt_version=prompt_version,
                )
                result = materialize_recommendation_drafts(
                    post_store,
                    recommendation_drafts,
                    classifier,
                    current["post_id"],
                    instruments=market_store.instrument_map(),
                    queue_scope=current["queue_scope"],
                    review_date=current["review_date"],
                    rules_first=False,
                )
            return {
                "ok": True,
                "drafts": [
                    sanitize_recommendation_draft(item)
                    for item in result["drafts"]
                ],
                "draft_generation_status": result["draft_generation_status"],
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ModelWorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"draft retry failed: {exc}") from exc

    @app.post("/api/posts/{post_id}/review")
    def review_post(
        post_id: str,
        body: ReviewRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        if body.action == "approve":
            try:
                queued_symbols: list[str] = []
                if body.confirm_leads:
                    queued_symbols = confirm_approval_leads(post_id, body.drafts, body.note)
                confirmed_symbols = {
                    str(lead["symbol"])
                    for lead in post_store.list_stock_leads(
                        post_id=post_id,
                        status="confirmed",
                        limit=1000,
                    )
                }
                draft_symbols = {draft.symbol for draft in body.drafts}
                missing = sorted(draft_symbols - confirmed_symbols)
                if missing:
                    raise ValueError(
                        "请先在股票线索中确认这些标的，再批准推荐事件："
                        + ", ".join(missing)
                    )
                result = approve_post(
                    post_store,
                    event_store,
                    post_id,
                    [draft.model_dump() for draft in body.drafts],
                    note=body.note,
                )
                review_agent.mark_overridden_for_post(
                    post_id,
                    "approved",
                    [draft.model_dump() for draft in body.drafts],
                )
                for draft in body.drafts:
                    instrument = market_store.get_instrument(draft.symbol)
                    if instrument is None:
                        continue
                    market_store.upsert_instrument(
                        Instrument(
                            symbol=draft.symbol,
                            name=draft.security_name or instrument["name"],
                            instrument_type=instrument["instrument_type"],
                            exchange=instrument["exchange"],
                            status=instrument["status"],
                            list_date=instrument["list_date"],
                            lifecycle="pinned",
                            source="kol_event",
                            first_seen_at=instrument["first_seen_at"],
                            last_mentioned_at=str(post_store.get_post(post_id)["posted_at"])[:10],
                        )
                    )
                refresh_status = "not_requested"
                if body.refresh_returns and queued_symbols:
                    background_tasks.add_task(
                        run_post_approval_refresh,
                        config.runtime_root,
                        ROOT,
                        queued_symbols,
                        as_of=date.today(),
                    )
                    refresh_status = "queued"
                for event_id in result.event_ids:
                    background_tasks.add_task(event_dossier.refresh, event_id)
                return {
                    **asdict(result),
                    "queued_symbols": queued_symbols,
                    "refresh_status": refresh_status,
                }
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, str(exc)) from exc
        action_map = {"exclude": "excluded", "ignore": "ignored", "pending": "pending"}
        if body.action not in action_map:
            raise HTTPException(422, "invalid review action")
        try:
            with post_review_lock(post_store, post_id):
                post = post_store.get_post(post_id)
                if post["review_status"] == "approved":
                    raise HTTPException(409, "approved posts require explicit event reconciliation")
                target_status = action_map[body.action]
                if post["review_status"] == target_status:
                    return _sanitize_post(post)
                post_store.set_review(post_id, target_status, body.note)
                review_agent.mark_overridden_for_post(post_id, target_status)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _sanitize_post(post_store.get_post(post_id))

    @app.post("/api/fetch")
    def fetch_posts(body: FetchRequest) -> dict[str, Any]:
        fallback_mode = _fallback_mode(config.runtime_root)
        if body.provider == "nitter" and fallback_mode != "enabled":
            raise HTTPException(409, "Nitter is shadow-only until the rollout gate passes")
        provider = build_x_post_provider(
            body.provider,
            twitter_credentials=credentials,
            xtf_command=str(xtf_command),
            nitter_url=config.nitter_url,
            fallback_mode=fallback_mode,
        )
        result = run_post_fetch(
            post_store,
            provider,
            platform_providers={"zhihu": zhihu_provider},
            max_count=body.max_count,
            classifier=RuleClassifier(
                classification_aliases()
            ),
        )
        classified = failed = 0
        if body.classify and result.candidate_posts:
            classified, failed = classify_pending_with_codex(
                post_store,
                build_post_classifier(
                    config.codex_schema,
                    ROOT,
                    deepseek_credentials=deepseek_credentials,
                ),
            )
        post_store.save_fetch_model_result(result.run_id, classified, failed)
        lead_result = extract_leads()
        if result.successful_kols == 0 and result.failed_kols:
            raise HTTPException(502, "; ".join(result.errors[:3]) or "all X account fetches failed")
        return {
            **asdict(result),
            "codex_classified": classified,
            "codex_failed": failed,
            "stock_leads": lead_result,
        }

    @app.post("/api/stock-leads/extract")
    def extract_stock_lead_queue() -> dict[str, Any]:
        return extract_leads()

    @app.get("/api/stock-leads")
    def list_stock_leads(
        status: str | None = None,
        symbol: str | None = None,
        kol_id: int | None = None,
        post_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return post_store.list_stock_leads(
            status=status,
            symbol=symbol,
            kol_id=kol_id,
            post_id=post_id,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/stock-mentions")
    def list_stock_mentions(
        q: str = Query(default="", max_length=200),
        kind: str = Query(default="", max_length=40),
        kol_id: int | None = None,
        symbol: str = Query(default="", pattern=r"^$|^\d{6}$"),
        date_from: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
        date_to: str = Query(default="", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        result = post_store.query_stock_mentions(
            query=q,
            kind=kind,
            kol_id=kol_id,
            symbol=symbol,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )
        related: dict[tuple[str, str], list[str]] = {}
        for event in event_store.load_events():
            post_id = event.source_note.removeprefix("post:").strip() if event.source_note.startswith("post:") else ""
            related.setdefault((post_id, event.symbol), []).append(event.event_id)
        for item in result["items"]:
            item["related_event_ids"] = related.get((str(item["post_id"]), str(item["symbol"])), [])
        return result

    @app.post("/api/stock-leads/{lead_id}/review")
    def review_stock_lead(lead_id: int, body: StockLeadReviewRequest) -> dict[str, Any]:
        try:
            current = post_store.get_stock_lead(lead_id)
            symbol = body.symbol or current["symbol"]
            instrument_before = None
            if body.action == "confirmed":
                if current["status"] == "confirmed":
                    if body.symbol is not None and body.symbol != current["symbol"]:
                        raise ValueError("已确认线索不能直接改代码，请先退回待确认")
                    return current
                if current["status"] != "pending":
                    raise ValueError("只有待确认线索可以晋升为确认状态")
                instrument_before = market_store.get_instrument(symbol)
                if instrument_before is None:
                    raise ValueError(f"{symbol} 不在证券主数据中，请先刷新主数据或修正代码")
            reviewed = post_store.review_stock_lead(
                lead_id,
                body.action,
                body.note,
                symbol=body.symbol,
                security_name=body.security_name,
            )
            if body.action == "confirmed":
                try:
                    mentioned = datetime.fromisoformat(str(current["posted_at"]).replace("Z", "+00:00")).date()
                except ValueError:
                    mentioned = date.today()
                try:
                    market_store.touch_mention(symbol, mentioned)
                    market_store.enqueue_sync(symbol, reason=f"kol_lead:{current['post_id']}")
                except Exception:
                    post_store.review_stock_lead(
                        lead_id,
                        "pending",
                        "市场数据排队失败，确认操作已回滚",
                        symbol=current["symbol"],
                        security_name=current["security_name"],
                    )
                    assert instrument_before is not None
                    market_store.restore_research_state(
                        symbol,
                        lifecycle=instrument_before["lifecycle"],
                        last_mentioned_at=instrument_before["last_mentioned_at"],
                    )
                    raise
            return reviewed
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"market queue failed: {exc}") from exc

    @app.get("/api/instruments")
    def list_instruments(lifecycle: str | None = None) -> list[dict[str, Any]]:
        return market_store.list_instruments(lifecycle)

    @app.post("/api/instruments", status_code=201)
    def create_instrument(body: InstrumentCreate) -> dict[str, Any]:
        return market_store.upsert_instrument(Instrument(**body.model_dump()))

    @app.patch("/api/instruments/{symbol}")
    def patch_instrument(symbol: str, body: InstrumentPatch) -> dict[str, Any]:
        current = market_store.get_instrument(symbol)
        if current is None:
            raise HTTPException(404, "instrument not found")
        changes = body.model_dump(exclude_none=True)
        return market_store.upsert_instrument(
            Instrument(
                symbol=symbol,
                name=changes.get("name", current["name"]),
                instrument_type=current["instrument_type"],
                exchange=current["exchange"],
                status=changes.get("status", current["status"]),
                list_date=current["list_date"],
                lifecycle=changes.get("lifecycle", current["lifecycle"]),
                source=current["source"],
                first_seen_at=current["first_seen_at"],
                last_mentioned_at=current["last_mentioned_at"],
            )
        )

    @app.post("/api/market/sync", status_code=202)
    def enqueue_market_sync(body: MarketSyncRequest) -> dict[str, Any]:
        symbols = body.symbols or [
            item["symbol"]
            for item in market_store.list_instruments()
            if item["lifecycle"] in {"pinned", "tracking"}
        ]
        queued: list[str] = []
        for symbol in symbols:
            try:
                market_store.enqueue_sync(
                    symbol,
                    start=body.start,
                    end=body.end,
                    priority=20,
                    reason="ui_manual",
                )
                queued.append(symbol)
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "queued_symbols": queued}

    @app.get("/api/market/health")
    def market_health() -> dict[str, Any]:
        return {
            **safe_market_health(),
            "freestockdb": safe_freestockdb_health(),
            "coverage": market_store.get_coverage(),
            "issues": market_store.quality_issues(),
            "runs": market_store.recent_runs(),
            "queue": market_store.pending_sync(),
        }

    @app.get("/api/market/providers/freestockdb")
    def freestockdb_health() -> dict[str, Any]:
        return safe_freestockdb_health(force=True)

    def run_freestockdb_update_job(dry_run: bool) -> None:
        try:
            freestockdb_runtime.update(dry_run=dry_run, timeout=3300)
        except Exception as exc:
            logger.error("FreeStockDB background update failed: %s", exc)
        finally:
            freestockdb_health_cache["expires_at"] = 0.0

    @app.post("/api/market/providers/freestockdb/update", status_code=202)
    def update_freestockdb(
        body: FreeStockDBUpdateRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        current = safe_freestockdb_health(force=True)
        if current.get("port_conflict"):
            raise HTTPException(409, "FreeStockDB port conflict")
        if not current.get("update_ready"):
            failed = [
                name
                for name, passed in current.get("checks", {}).items()
                if passed is False
                and name not in {"service", "provider", "freshness"}
            ]
            raise HTTPException(
                409,
                "FreeStockDB update preflight failed: " + ",".join(failed),
            )
        background_tasks.add_task(run_freestockdb_update_job, body.dry_run)
        return {"ok": True, "status": "queued", "dry_run": body.dry_run}

    @app.get("/api/market/instruments/{symbol}/coverage")
    def instrument_coverage(symbol: str) -> list[dict[str, Any]]:
        if market_store.get_instrument(symbol) is None:
            raise HTTPException(404, "instrument not found")
        return market_store.get_coverage(symbol)

    @app.get("/api/market/instruments/{symbol}/daily")
    def instrument_daily(
        symbol: str,
        adjustment: str = "raw",
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        if adjustment not in {"raw", "qfq", "hfq"}:
            raise HTTPException(422, "invalid adjustment")
        frame = market_store.read_daily(symbol, adjustment=adjustment)
        if frame.empty:
            return []
        if start is not None:
            frame = frame[frame["trade_date"] >= start]
        if end is not None:
            frame = frame[frame["trade_date"] <= end]
        frame = frame.where(frame.notna(), None)
        return frame.to_dict(orient="records")

    @app.get("/api/market/daily/{symbol}/indicators")
    def market_daily_indicators(
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        frame = compute_daily_indicators(
            market_store.read_daily(symbol, adjustment="qfq")
        )
        available_from = (
            frame.iloc[0]["trade_date"].isoformat() if not frame.empty else ""
        )
        available_to = (
            frame.iloc[-1]["trade_date"].isoformat() if not frame.empty else ""
        )
        if start is not None and not frame.empty:
            frame = frame[frame["trade_date"] >= start]
        if end is not None and not frame.empty:
            frame = frame[frame["trade_date"] <= end]
        rows: list[dict[str, Any]] = []
        for raw_row in frame.to_dict(orient="records"):
            row: dict[str, Any] = {}
            for key, raw_value in raw_row.items():
                if pd.isna(raw_value):
                    row[key] = None
                elif isinstance(raw_value, date):
                    row[key] = raw_value.isoformat()
                elif hasattr(raw_value, "item"):
                    row[key] = raw_value.item()
                else:
                    row[key] = raw_value
            rows.append(row)
        return {
            "symbol": symbol,
            "adjustment": "qfq",
            "formula_version": INDICATOR_VERSION,
            "available_from": available_from,
            "available_to": available_to,
            "row_count": len(rows),
            "rows": rows,
        }

    @app.get("/api/board-mainline/health")
    def board_mainline_health() -> dict[str, Any]:
        return board_store.health()

    @app.get("/api/board-mainline")
    def list_board_mainline(
        board_type: str = Query(default="industry", pattern="^(industry|concept)$"),
        status: str = "",
        q: str = "",
        as_of: date | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=200),
        sort_by: str = Query(
            default="rps_50",
            pattern="^(rps_50|rps_120|rps_250|breadth|turnover_ratio_20|board_name)$",
        ),
        descending: bool = True,
    ) -> dict[str, Any]:
        return board_store.list_mainline(
            board_type=board_type,
            status=status,
            query=q,
            as_of=as_of,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            descending=descending,
        )

    @app.get("/api/board-mainline/{board_code}/series")
    def board_mainline_series(
        board_code: str,
        board_type: str | None = Query(default=None, pattern="^(industry|concept)$"),
        limit: int = Query(default=320, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        if board_store.get_board(board_code, board_type) is None:
            raise HTTPException(404, "board not found")
        return board_store.series(board_code, board_type=board_type, limit=limit)

    @app.get("/api/board-mainline/{board_code}/rank-series")
    def board_rank_series(
        board_code: str,
        board_type: str | None = Query(default=None, pattern="^(industry|concept)$"),
        window: int = Query(default=50, ge=1, le=250),
        range_name: str = Query(default="120", alias="range", pattern="^(all|120|250)$"),
    ) -> dict[str, Any]:
        try:
            return board_store.rank_series(
                board_code,
                board_type=board_type,
                window=window,
                range_name=range_name,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/board-mainline/{board_code}")
    def board_mainline_detail(
        board_code: str,
        board_type: str | None = Query(default=None, pattern="^(industry|concept)$"),
    ) -> dict[str, Any]:
        return board_with_related_events(board_code, board_type)

    def run_board_sync_job(as_of: date) -> None:
        store = BoardMainlineStore(MarketStore(config.runtime_root / "market"))
        result = sync_board_snapshot(store, FallbackBoardProvider(), as_of=as_of)
        if result.succeeded:
            store.compute_rps(as_of=as_of, formula_version="board-rps-v2")

    @app.post("/api/board-mainline/sync", status_code=202)
    def sync_board_mainline(
        body: BoardSyncRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        if board_store.health()["latest_run"] and board_store.health()["latest_run"]["status"] == "running":
            return {"ok": True, "status": "already_running"}
        as_of = body.as_of or date.today()
        background_tasks.add_task(run_board_sync_job, as_of)
        return {"ok": True, "status": "queued", "as_of": as_of.isoformat()}

    @app.get("/api/events")
    def list_events() -> list[dict[str, Any]]:
        latest: dict[str, dict[str, str]] = {}
        for mark in event_store.load_marks():
            if mark["event_id"] not in latest or mark["trade_date"] > latest[mark["event_id"]]["trade_date"]:
                latest[mark["event_id"]] = mark
        values = []
        for event in event_store.load_events():
            source_post_id = ""
            if event.source_note.startswith("post:"):
                source_post_id = event.source_note.removeprefix("post:").strip()
            if not re.fullmatch(r"\d{5,25}", source_post_id):
                match = re.search(r"/status/(\d{5,25})", event.source_url)
                source_post_id = match.group(1) if match else ""
            values.append(
                {
                    **event.to_row(),
                    "latest_mark": latest.get(event.event_id),
                    "technical_context": technical_context_for(event),
                    "intraday_context": market_store.get_event_intraday_context(event.event_id),
                    "followups": post_store.list_retrospective_followups(source_post_id),
                }
            )
        return values

    @app.get("/api/events/{event_id}/dossier")
    def event_dossier_endpoint(event_id: str) -> dict[str, Any]:
        try:
            dossier = event_dossier.build(event_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        snapshot = event_dossier.latest_snapshot(event_id)
        if snapshot:
            dossier["snapshot"] = {
                "snapshot_id": snapshot.get("snapshot_id", ""),
                "input_hash": snapshot.get("input_hash", ""),
                "status": snapshot.get("status", ""),
                "created_at": snapshot.get("created_at", ""),
            }
        return dossier

    @app.get("/api/events/{event_id}/research")
    def event_method_research(event_id: str) -> dict[str, Any]:
        try:
            return event_research.get_section(event_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            return {
                "status": "failed",
                "data": None,
                "interpretation": None,
                "warnings": [
                    "method_research_failed",
                    f"{type(exc).__name__}:{str(exc)[:500]}",
                ],
                "snapshot_id": "",
            }

    @app.get("/api/events/{event_id}/research/history")
    def event_method_research_history(event_id: str) -> list[dict[str, Any]]:
        try:
            event_research._event(event_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return event_research.history(event_id)

    @app.post("/api/events/{event_id}/research/refresh", status_code=202)
    def refresh_event_method_research(
        event_id: str,
        fetch_cross_section: bool = False,
        fetch_minute: bool = False,
        with_ai: bool = False,
    ) -> dict[str, Any]:
        provider: FreeStockDBMarketProvider | None = None
        try:
            if fetch_cross_section or fetch_minute:
                provider = FreeStockDBMarketProvider(config.freestockdb_url)
                service = EventMethodResearchService(
                    event_store,
                    market_store,
                    cross_section_provider=provider,
                    minute_provider=provider,
                    interpreter=event_research.interpreter,
                )
            else:
                service = event_research
            result = service.refresh(
                event_id,
                fetch_cross_section=fetch_cross_section,
                fetch_minute=fetch_minute,
            )
            ai = (
                service.interpret_pending(event_ids=[event_id], max_items=1)
                if with_ai
                else None
            )
            return {
                "ok": not ai or bool(ai.get("ok")),
                "event_id": event_id,
                "snapshot_id": result["snapshot_id"],
                "created": result["created"],
                "status": result["research"]["status"],
                "fetch": result["fetch"],
                "ai": ai,
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)[:2000]) from exc
        finally:
            if provider is not None:
                provider.close()

    @app.get("/api/events/{event_id}/market-series")
    def event_market_series(
        event_id: str,
        adjustment: str = Query(default="qfq", pattern="^(raw|qfq|hfq)$"),
        frequency: str = Query(default="d", pattern="^(d|1m|5m|15m|30m|60m)$"),
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return event_dossier.market_series(
                event_id,
                adjustment=adjustment,
                frequency=frequency,
                start=start,
                end=end,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/events/{event_id}/minute-series")
    def event_minute_series(
        event_id: str,
        frequency: str = Query(default="1m", pattern="^(1m|5m|15m|30m|60m)$"),
        adjustment: str = Query(default="raw", pattern="^(raw|qfq|hfq)$"),
        start: date | None = None,
        end: date | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=500, ge=1, le=2000),
    ) -> dict[str, Any]:
        try:
            return event_dossier.minute_series(
                event_id,
                frequency=frequency,
                adjustment=adjustment,
                start=start,
                end=end,
                page=page,
                page_size=page_size,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/events/{event_id}/dossier/export")
    def event_dossier_export(event_id: str) -> dict[str, Any]:
        try:
            dossier = event_dossier.build(event_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        dossier["snapshots"] = event_dossier.market_store.list_event_dossier_snapshots(event_id)
        return dossier

    @app.post("/api/events/{event_id}/data-refresh", status_code=202)
    def refresh_event_dossier(event_id: str) -> dict[str, Any]:
        try:
            dossier = event_dossier.refresh(event_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)[:2000]) from exc
        return {
            "ok": True,
            "status": dossier["status"],
            "event_id": event_id,
            "snapshot": dossier.get("snapshot"),
            "section_status": dossier["section_status"],
        }

    @app.patch("/api/events/{event_id}")
    def update_event(event_id: str, body: EventUpdateRequest) -> dict[str, Any]:
        events = event_store.load_events()
        current = next((event for event in events if event.event_id == event_id), None)
        if current is None:
            raise HTTPException(404, f"event not found: {event_id}")
        transitions = {
            "activate": {"candidate"},
            "exclude": {"candidate"},
            "restore": {"excluded", "archived"},
            "archive": {"excluded"},
        }
        if current.status not in transitions[body.action]:
            raise HTTPException(
                409,
                f"cannot {body.action} event in {current.status} status",
            )
        values = body.model_dump(
            exclude_none=True,
            exclude={"action", "exclusion_reason"},
        )
        candidate = replace(current, **values, updated_at=now_iso())
        if body.action == "activate":
            candidate = replace(
                candidate,
                status="active",
                exclusion_reason="",
                activated_at=now_iso(),
            )
            errors = validate_event(candidate)
            if errors:
                raise HTTPException(422, "激活信息不完整：" + ", ".join(errors))
            instrument = market_store.get_instrument(candidate.symbol)
            market_store.upsert_instrument(
                Instrument(
                    symbol=candidate.symbol,
                    name=candidate.security_name or (instrument or {}).get("name", candidate.symbol),
                    instrument_type=(instrument or {}).get("instrument_type", "stock"),
                    exchange=(instrument or {}).get(
                        "exchange",
                        "BJ" if candidate.symbol.startswith(("4", "8", "92")) else "SH" if candidate.symbol.startswith("6") else "SZ",
                    ),
                    lifecycle="pinned",
                    source="kol_event",
                    first_seen_at=(instrument or {}).get("first_seen_at", ""),
                    last_mentioned_at=candidate.posted_at[:10],
                )
            )
            market_store.enqueue_sync(candidate.symbol, reason=f"kol_event:{candidate.event_id}")
        elif body.action == "exclude":
            reason = (body.exclusion_reason or "").strip()
            if not reason:
                raise HTTPException(422, "排除原因不能为空")
            candidate = replace(candidate, status="excluded", exclusion_reason=reason)
        elif body.action == "archive":
            candidate = replace(candidate, status="archived")
        else:
            target = "candidate" if current.status == "excluded" else "excluded"
            candidate = replace(
                candidate,
                status=target,
                exclusion_reason="" if target == "candidate" else current.exclusion_reason,
            )
        try:
            updated = event_store.update_event(candidate, action=body.action)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return updated.to_row()

    @app.post("/api/events/{event_id}/amendments")
    def amend_event(
        event_id: str,
        body: EventAmendmentRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True, exclude={"reason"})
        try:
            updated, revision = event_store.amend_event(
                event_id,
                changes,
                reason=body.reason,
            )
            refresh_status = "not_required"
            if revision.get("recalculation_required"):
                instrument = market_store.get_instrument(updated.symbol)
                market_store.upsert_instrument(
                    Instrument(
                        symbol=updated.symbol,
                        name=updated.security_name or (instrument or {}).get("name", updated.symbol),
                        instrument_type=(instrument or {}).get("instrument_type", "stock"),
                        exchange=(instrument or {}).get(
                            "exchange",
                            "BJ" if updated.symbol.startswith(("4", "8", "92")) else "SH" if updated.symbol.startswith("6") else "SZ",
                        ),
                        lifecycle="pinned",
                        source="event_amendment",
                        first_seen_at=(instrument or {}).get("first_seen_at", ""),
                        last_mentioned_at=updated.posted_at[:10],
                    )
                )
                market_store.enqueue_sync(updated.symbol, reason=f"event_amendment:{event_id}")
                background_tasks.add_task(
                    run_post_approval_refresh,
                    config.runtime_root,
                    ROOT,
                    [updated.symbol],
                    as_of=date.today(),
                )
                background_tasks.add_task(event_dossier.refresh, updated.event_id)
                refresh_status = "queued"
            return {
                "event": updated.to_row(),
                "revision": revision,
                "refresh_status": refresh_status,
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/events/{event_id}/revisions")
    def event_revisions(event_id: str) -> list[dict[str, Any]]:
        if not any(event.event_id == event_id for event in event_store.load_events()):
            raise HTTPException(404, f"event not found: {event_id}")
        return event_store.list_event_revisions(event_id)

    @app.get("/api/events/{event_id}/series")
    def event_series(
        event_id: str,
        range_name: str = Query(default="all", alias="range", pattern="^(all|120|250)$"),
    ) -> list[dict[str, str]]:
        event = next(
            (item for item in event_store.load_events() if item.event_id == event_id),
            None,
        )
        if event is None:
            raise HTTPException(404, "event not found")
        marks = sorted(
            (
                row
                for row in event_store.load_marks()
                if row["event_id"] == event_id
            ),
            key=lambda row: row["trade_date"],
        )
        if not marks:
            return []
        by_date = {row["trade_date"]: row for row in marks}
        last_saved_date = date.fromisoformat(marks[-1]["trade_date"])
        latest_open_date = market_store.latest_open_date(date.today()) or last_saved_date
        open_dates = market_store.open_dates_between(
            date.fromisoformat(marks[0]["trade_date"]),
            max(last_saved_date, latest_open_date),
        )
        if open_dates:
            rows: list[dict[str, str]] = []
            for index, trade_date in enumerate(open_dates):
                key = trade_date.isoformat()
                saved = by_date.get(key)
                if saved is not None:
                    rows.append(saved)
                    continue
                placeholder = {field: "" for field in MARK_FIELDS}
                placeholder.update(
                    {
                        "event_id": event.event_id,
                        "trade_date": key,
                        "tracking_days": str(index),
                        "data_status": "suspended_or_missing",
                    }
                )
                rows.append(placeholder)
        else:
            rows = marks
        if range_name != "all":
            rows = rows[-int(range_name) :]
        return rows

    @app.get("/api/events/{event_id}/features")
    def event_features(event_id: str) -> dict[str, Any]:
        event = next((item for item in event_store.load_events() if item.event_id == event_id), None)
        if event is None:
            raise HTTPException(404, "event not found")
        saved = technical_context_for(event)
        if saved is not None:
            return saved
        return {
            "snapshot_id": "",
            "event_id": event.event_id,
            "feature_version": FEATURE_VERSION,
            "input_hash": event_context_input_hash(event.symbol, event.posted_at),
            "symbol": event.symbol,
            "posted_at": event.posted_at,
            "expected_trade_date": "",
            "as_of_trade_date": "",
            "adjustment": "qfq",
            "rsi14": None,
            "macd_dif": None,
            "macd_dea": None,
            "macd_hist": None,
            "macd_hist_pct": None,
            "atr14": None,
            "atr14_pct": None,
            "volume_ratio_5": None,
            "return_20d": None,
            "distance_60d_high": None,
            "history_bars": 0,
            "status": "pending",
            "warnings": ["not_computed"],
            "source_hash": "",
            "error": "",
            "computed_at": "",
        }

    @app.get("/api/events/{event_id}/intraday-context")
    def event_intraday_context(event_id: str) -> dict[str, Any]:
        if not any(event.event_id == event_id for event in event_store.load_events()):
            raise HTTPException(404, "event not found")
        return market_store.get_event_intraday_context(event_id) or {
            "event_id": event_id,
            "status": "pending",
            "warnings": ["not_computed"],
        }

    @app.post("/api/events/{event_id}/intraday-backfill", status_code=202)
    def event_intraday_backfill(event_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        if not any(event.event_id == event_id for event in event_store.load_events()):
            raise HTTPException(404, "event not found")

        def run() -> None:
            backfill_event_intraday(market_store, event_store, event_ids={event_id}, force=True)

        background_tasks.add_task(run)
        return {"ok": True, "status": "queued", "event_id": event_id}

    @app.get("/api/checkpoints")
    def checkpoints() -> list[dict[str, str]]:
        return event_store.load_checkpoints()

    @app.get("/api/fetch-runs")
    def fetch_runs() -> list[dict[str, Any]]:
        return post_store.recent_fetch_runs()

    @app.get("/api/tasks")
    def operator_tasks() -> dict[str, Any]:
        fetch = post_store.recent_fetch_runs(1)
        morning = recommendation_drafts.recent_morning_runs(1)
        return {
            "tasks": [
                {
                    "id": task_id,
                    "task": task_name,
                    "status": _task_status(task_name),
                }
                for task_id, task_name in OPERATOR_TASKS.items()
            ],
            "fetch": fetch[0] if fetch else None,
            "morning": morning[0] if morning else None,
        }

    @app.post("/api/tasks/{task_id}/start", status_code=202)
    def start_operator_task(task_id: str) -> dict[str, Any]:
        task_name = OPERATOR_TASKS.get(task_id)
        if task_name is None:
            raise HTTPException(404, "unknown operator task")
        try:
            return _start_scheduled_task(task_name)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/review-agent/summary")
    def review_agent_summary() -> dict[str, Any]:
        return review_agent.summary()

    @app.get("/api/review-agent/decisions")
    def review_agent_decisions(
        decision: str | None = None,
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return review_agent.list_decisions(
            decision=decision,
            status=status,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/review-agent/decisions/{decision_id}")
    def review_agent_decision(decision_id: int) -> dict[str, Any]:
        try:
            return review_agent.get_decision(decision_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.patch("/api/review-agent/settings")
    def patch_review_agent_settings(body: ReviewAgentSettingsRequest) -> dict[str, Any]:
        try:
            return review_agent.set_mode(body.mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/review-agent/decisions/{decision_id}/rollback")
    def rollback_review_agent_decision(decision_id: int) -> dict[str, Any]:
        try:
            return rollback_decision(review_agent, post_store, event_store, decision_id, market_store)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/fetch-attempts")
    def fetch_attempts(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return post_store.recent_fetch_attempts(limit)

    @app.get("/api/system/health")
    def system_health() -> dict[str, Any]:
        recent_runs = post_store.recent_fetch_runs(limit=1)
        attempts = post_store.recent_fetch_attempts(limit=100)
        twitter_attempts = [item for item in attempts if item["provider"] == "twitter-cli"]
        fallback_attempts = [item for item in attempts if item["provider"] == "nitter"]
        docker = docker_health()
        nitter = timeline_health(config.nitter_url)
        redis = docker_container_health("nitter-redis")
        zhihu_kols = [item for item in post_store.list_kols() if item.get("platform") == "Zhihu"]
        active_zhihu = [item for item in zhihu_kols if item.get("status") == "active"]
        paused_zhihu = [item for item in zhihu_kols if item.get("status") == "paused"]
        zhihu_failed = [
            item for item in active_zhihu
            if str(item.get("fetch_status") or "") not in {"never", "success"}
        ]
        zhihu_status = (
            "disabled"
            if not active_zhihu
            else "degraded" if zhihu_failed
            else "success" if any(item.get("fetch_status") == "success" for item in active_zhihu)
            else "never"
        )
        latest_twitter_attempt = twitter_attempts[0] if twitter_attempts else {}
        latest_twitter_error = str(
            latest_twitter_attempt.get("error_code") or ""
        )
        x_status = (
            "rate_limited"
            if latest_twitter_error == "rate_limited"
            else "authentication_failed"
            if latest_twitter_error == "authentication_failed"
            else "ok"
            if latest_twitter_attempt.get("status") == "success"
            else "unknown"
        )
        gateway_pid_path = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "hermes"
            / "gateway.pid"
        )
        gateway_pid = ""
        gateway_running = False
        try:
            gateway_pid = _read_process_pid(gateway_pid_path)
            process = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"PID eq {gateway_pid}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            gateway_running = bool(
                gateway_pid
                and gateway_pid in (process.stdout or "")
                and "No tasks" not in (process.stdout or "")
            )
        except (OSError, subprocess.SubprocessError):
            gateway_running = False
        review_summary = review_agent.summary()
        market_component = safe_market_health()
        freestock_component = safe_freestockdb_health()
        return {
            "ok": True,
            "date": date.today().isoformat(),
            "twitter_cli": shutil.which("twitter") or "",
            "twitter_credentials_configured": credentials.configured(),
            "twitter_auth_status": recent_runs[0].get("auth_status", "unknown") if recent_runs else "never",
            "zhihu_capture_available": zhihu_provider.script_path.is_file(),
            "zhihu_active_kols": len(active_zhihu),
            "zhihu_paused_kols": len(paused_zhihu),
            "zhihu_failed_kols": len(zhihu_failed),
            "zhihu_failure_handles": [str(item.get("handle")) for item in zhihu_failed[:20]],
            "zhihu_fetch_status": zhihu_status,
            "nitter_credentials_configured": nitter_credentials.configured(),
            "nitter_url": config.nitter_url,
            "nitter_ready": nitter["ready"],
            "nitter_status": nitter["status"],
            "nitter_http_status": nitter["http_status"],
            "redis_ready": redis["ready"],
            "docker_installed": docker["installed"],
            "docker_ready": docker["ready"],
            "docker_version": docker["version"],
            "xtf_command": str(xtf_command) if xtf_command.exists() else "",
            "xtf_version": xtf_version(xtf_command) if xtf_command.exists() else "",
            "twitter_success_rate": (
                sum(item["status"] == "success" for item in twitter_attempts) / len(twitter_attempts)
                if twitter_attempts else None
            ),
            "nitter_attempt_count": len(fallback_attempts),
            "fallback_mode": _fallback_mode(config.runtime_root),
            "shadow_rollout": post_store.shadow_rollout_status(),
            "codex_cli": shutil.which("codex") or "",
            "deepseek_credentials_configured": deepseek_credentials.configured(),
            "deepseek_provider": DeepSeekPostClassifier.provider_name,
            "deepseek_model": DeepSeekPostClassifier.model_name,
            "unlimited_ocr_root": str(ocr_root),
            "unlimited_ocr_available": UnlimitedOcrBatchClassifier(ocr_root, ocr_runner).available(),
            "rapid_ocr_runtime": str(rapid_ocr_python),
            "rapid_ocr_available": RapidOcrBatchClassifier(
                rapid_ocr_python, rapid_ocr_runner
            ).available(),
            "ocr_provider": "rapidocr",
            "database": str(post_store.path),
            "media_root": str(post_store.media_root),
            "media_bytes": media_disk_usage(post_store.media_root),
            "post_fetch_task": _task_status("KOL_Post_Fetch_Daily"),
            "return_task": _task_status("KOL_Return_Tracker_Daily"),
            "market_sync_task": _task_status("Market_Data_Sync_Daily"),
            "classification_task": _task_status("KOL_Post_Classify_Daily"),
            "review_agent_task": _task_status("KOL_Review_Agent"),
            "morning_pipeline_task": _task_status("KOL_Morning_Pipeline"),
            "morning_runs": recommendation_drafts.recent_morning_runs(5),
            "digest_task": _task_status("Research_Data_Digest_Daily"),
            "review_agent": review_summary,
            "market": market_component,
            "freestockdb": freestock_component,
            "component_status": {
                "hermes_gateway": {
                    "status": "running" if gateway_running else "stopped",
                    "pid": gateway_pid,
                },
                "operator": {"status": "available"},
                "x": {
                    "status": x_status,
                    "error_code": latest_twitter_error,
                },
                "nitter": {
                    "status": nitter["status"],
                    "ready": nitter["ready"],
                },
                "ai": {
                    "status": (
                        "degraded"
                        if review_summary.get("model_failures", 0)
                        else "ready"
                    )
                },
                "market": {
                    "status": market_component.get(
                        "market_status",
                        market_component.get("status", "unknown"),
                    ),
                    "ok": bool(market_component.get("ok")),
                },
                "freestockdb": {
                    "status": freestock_component.get(
                        "service_status",
                        "unknown",
                    ),
                    "ok": bool(freestock_component.get("ok")),
                },
            },
            "pipeline_refresh": read_refresh_state(config.runtime_root),
            "nitter_start_task": _task_status("KOL_Nitter_Start"),
        }

    @app.get("/api/pipeline/status")
    def pipeline_status() -> dict[str, Any]:
        recent_fetches = post_store.recent_fetch_runs(limit=1)
        today = date.today()
        current = datetime.now(SHANGHAI)
        operational_date = today + timedelta(days=1) if current.hour >= 9 else today
        pending_start, pending_end = review_window_utc(operational_date.isoformat())
        with post_store.connect() as db:
            latest_ai = db.execute(
                "SELECT MAX(updated_at) FROM classifications WHERE model_status='completed'"
            ).fetchone()[0]
            pending_ai = int(db.execute(
                """
                SELECT COUNT(*) FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                  AND c.model_status IN ('not_requested','running')
                  AND p.posted_at_utc>=? AND p.posted_at_utc<?
                """,
                (pending_start, pending_end),
            ).fetchone()[0])
            failed_ai = int(db.execute(
                """
                SELECT COUNT(*) FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1 AND c.model_status='failed'
                  AND p.posted_at_utc>=? AND p.posted_at_utc<?
                """,
                (pending_start, pending_end),
            ).fetchone()[0])
        queue_summary = recommendation_drafts.morning_summary(operational_date.isoformat())
        tomorrow = today + timedelta(days=1)
        market = safe_market_health()
        latest_open = str(market.get("latest_open_date") or "")
        session_status = str(market.get("market_session_status") or "")
        if session_status in {"pre_open", "trading"}:
            market_status = session_status
        elif latest_open and latest_open < today.isoformat():
            market_status = "closed"
        else:
            market_status = market.get("daily_data_status", "unknown")
        return {
            "as_of": now_iso(),
            "latest_fetch_at": recent_fetches[0].get("completed_at", "") if recent_fetches else "",
            "latest_fetch_status": recent_fetches[0].get("status", "never") if recent_fetches else "never",
            "latest_ai_at": str(latest_ai or ""),
            "pending_ai": pending_ai,
            "failed_ai": failed_ai,
            "pending_human_review": int(queue_summary["waiting_review"]),
            "queue_review_date": operational_date.isoformat(),
            "queue_kind": "next_preview" if operational_date > today else "today",
            "morning_delivery": recommendation_drafts.morning_delivery(today.isoformat()),
            "next_preview": recommendation_drafts.morning_summary(tomorrow.isoformat()),
            "latest_trade_date": market.get("latest_daily_date", ""),
            "expected_trade_date": latest_open,
            "market_status": market_status,
            "lagging_symbols": market.get("lagging_symbols", []),
            "refresh": read_refresh_state(config.runtime_root),
        }

    @app.post("/api/system/twitter-credentials")
    def save_twitter_credentials(body: TwitterCredentialRequest) -> dict[str, bool]:
        try:
            auth_token, ct0 = validate_twitter_credentials(body.auth_token, body.ct0)
            credentials.save(auth_token, ct0)
        except CredentialValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CredentialStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "configured": True}

    @app.post("/api/system/nitter-credentials")
    def save_nitter_credentials(body: TwitterCredentialRequest) -> dict[str, bool]:
        try:
            auth_token, ct0 = validate_twitter_credentials(body.auth_token, body.ct0)
            nitter_credentials.save(auth_token, ct0)
        except CredentialValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CredentialStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "configured": True}

    @app.post("/api/system/opencode-go-credentials")
    @app.post("/api/system/deepseek-credentials")
    def save_deepseek_credentials(body: DeepSeekCredentialRequest) -> dict[str, bool]:
        try:
            deepseek_credentials.save(body.api_key)
        except CredentialValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CredentialStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "configured": True}

    media_mount = kol_root / "media"
    app.mount("/media", StaticFiles(directory=media_mount), name="kol-media")

    if config.frontend_dist.exists():
        assets = config.frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str) -> FileResponse:
            requested = (config.frontend_dist / path).resolve()
            if path and requested.is_relative_to(config.frontend_dist.resolve()) and requested.is_file():
                return FileResponse(requested)
            return FileResponse(config.frontend_dist / "index.html")

    return app


app = create_app()
