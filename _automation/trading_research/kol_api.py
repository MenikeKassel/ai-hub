from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import uuid
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
    ReaderCredentialStore,
    XSessionManager,
    XSessionUnavailableError,
    PublicBackupGate,
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
    _resolve_twitter_command,
)
from kol_review import approve_post, approve_recommendation_draft, post_review_lock
from kol_leaderboard import build_kol_leaderboard
from kol_performance import KolPerformanceService
from kol_tracker import MARK_FIELDS, SHANGHAI, KolStore, now_iso, validate_event
from event_context import FEATURE_VERSION, event_context_input_hash
from nitter_runtime import docker_container_health, docker_health, http_health, xtf_version
from market_data import FreeStockDBMarketProvider, Instrument, MarketStore
from market_indicators import INDICATOR_VERSION, compute_daily_indicators
from freestockdb_runtime import FreeStockDBRuntime
from kol_intraday import backfill_event_intraday
from event_dossier import EventDossierService
from event_research_ai import build_event_research_interpreter
from event_research_service import EventMethodResearchService
from pipeline_jobs import read_refresh_state, run_post_approval_refresh
from review_agent import ReviewAgentRepository, rollback_decision
from recommendation_drafts import RecommendationDraftRepository, review_window_utc
from recommendation_processing import materialize_recommendation_drafts
from stock_leads import extract_stock_leads, reconcile_exact_stock_leads
from market_admissions import MarketAdmissionRepository, read_published_manifest
from market_policy import (
    MarketWriteBlockedError,
    assert_market_runtime_writes_allowed,
    assert_research_writes_allowed,
    assert_returns_writes_allowed,
    load_market_recovery_mode,
    market_runtime_writes_enabled,
)
from model_budget import ModelDailyBudget, OcrDailyBudget
from foundation_market_client import FoundationBackedMarketStore, FoundationMarketReader


ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_ROOT = Path(
    os.environ.get(
        "ASHARE_FOUNDATION_ROOT",
        ROOT / "_runtime" / "trading" / "market" / "foundation",
    )
)
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
    freestockdb_root: Path = Path(
        os.environ.get(
            "FREESTOCKDB_ROOT",
            Path(__file__).resolve().parents[3] / "freestock" / "stockdb",
        )
    )
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
            freestockdb_root=Path(
                os.environ.get(
                    "FREESTOCKDB_ROOT",
                    Path(__file__).resolve().parents[3] / "freestock" / "stockdb",
                )
            ),
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
    availability_status: str | None = Field(default=None, max_length=30)
    availability_reason: str | None = Field(default=None, max_length=2000)


class KolBackfillRequest(BaseModel):
    count: int = Field(default=200, ge=1)


class DiscoveryRunRequest(BaseModel):
    platform: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=200)


class DiscoveryDecisionRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


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


class XSessionCredentialRequest(TwitterCredentialRequest):
    label: str = Field(default="", max_length=100)


class XSessionStatusRequest(BaseModel):
    status: str = Field(pattern="^(ready|disabled)$")
    reason: str = Field(default="", max_length=2000)


class XCollectionPolicyRequest(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    limit_mode: str | None = Field(default=None, pattern="^(bounded|unlimited)$")
    min_interval_seconds: int | None = Field(default=None, ge=0, le=86400)
    reason: str = Field(default="", max_length=2000)


class PublicBackupPolicyRequest(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    limit_mode: str | None = Field(default=None, pattern="^(bounded|unlimited)$")
    min_interval_seconds: int | None = Field(default=None, ge=0, le=86400)
    reason: str = Field(default="", max_length=2000)


class DeepSeekCredentialRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=1024)


class PerformanceRefreshRequest(BaseModel):
    as_of: str | None = None


class FoundationRefreshRequest(BaseModel):
    as_of: str = Field(default="auto", pattern=r"^(auto|\d{4}-\d{2}-\d{2})$")
    notify: bool = True


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


class RecommendationDraftBulkPreviewRequest(BaseModel):
    review_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    queue_scope: str = Field(default="morning", pattern="^(morning|backlog)$")
    status: str = Field(default="ready", pattern="^ready$")
    limit: int = Field(default=200, ge=1, le=200)


class RecommendationDraftBulkApproveRequest(BaseModel):
    snapshot_token: str = Field(min_length=20, max_length=200)
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
    local_market_store = MarketStore(config.runtime_root / "market")
    default_runtime = Path(
        os.environ.get("TRADING_RUNTIME_ROOT", ROOT / "_runtime" / "trading")
    ).resolve()
    market_store = (
        FoundationBackedMarketStore(local_market_store, FOUNDATION_ROOT)
        if (FOUNDATION_ROOT / "current.json").is_file()
        and config.runtime_root.resolve() == default_runtime
        else local_market_store
    )
    freestockdb_runtime = FreeStockDBRuntime(
        config.freestockdb_root,
        config.freestockdb_url,
        runtime_root=config.runtime_root,
    )
    review_agent = ReviewAgentRepository(post_store)
    recommendation_drafts = RecommendationDraftRepository(post_store)
    market_admissions = MarketAdmissionRepository(post_store)
    credentials = credential_store or KeyringCredentialStore()
    reader_credentials = ReaderCredentialStore()
    x_sessions = XSessionManager(post_store)
    public_backup = PublicBackupGate(post_store)
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
        port=int(os.environ.get("ZHIHU_CDP_PORT", "9223")),
    )
    ocr_root = Path(os.environ.get("UNLIMITED_OCR_ROOT", ROOT.parent / "Unlimited-OCR"))
    ocr_runner = Path(__file__).with_name("unlimited_ocr_batch.py")
    rapid_ocr_python = Path(os.environ.get(
        "RAPID_OCR_PYTHON",
        ROOT / "_runtime" / "venv-ocr-fast" / "Scripts" / "python.exe",
    ))
    rapid_ocr_runner = Path(__file__).with_name("rapid_ocr_batch.py")
    initialize_seed_kols(post_store)

    # The discovery core is mounted into this application.  It shares the
    # existing posts.db and KOL identity tables, so discovery cannot create a
    # second universe of accounts or require a second console port.
    discovery_store = None
    discovery_service = None
    discovery_registry = None
    discovery_scorer = None
    discovery_error = ""
    discovery_scorer_error = ""
    try:
        from kol_audit.discovery.scoring import CandidateScorer
        from kol_audit.discovery.service import DiscoveryService
        from kol_audit.discovery.store import DiscoveryStore
        from kol_discovery_runtime import build_provider_registry

        capture_root = Path(
            os.environ.get(
                "KOL_DISCOVERY_CAPTURE_ROOT",
                str(config.runtime_root / "kol-discovery" / "captures"),
            )
        )
        discovery_store = DiscoveryStore(kol_root / "posts.db", post_store)
        discovery_registry = build_provider_registry(
            capture_root,
            x_provider=build_x_post_provider(
                "auto",
                xtf_command=xtf_command,
                fallback_mode="disabled",
                session_manager=x_sessions,
                batch_key="discovery:shared",
            ),
            zhihu_provider=zhihu_provider,
        )
        discovery_service = DiscoveryService(discovery_store, discovery_registry)
    except Exception as exc:
        discovery_error = str(exc)
        logger.warning("Full-platform discovery is unavailable: %s", exc)
    if discovery_store is not None:
        try:
            from kol_audit.discovery.scoring import CandidateScorer
            from kol_discovery_runtime import OpenCodeGoCandidateScoreProvider

            discovery_scorer = CandidateScorer(
                discovery_store,
                OpenCodeGoCandidateScoreProvider(),
            )
        except Exception as exc:
            discovery_scorer_error = str(exc)
            logger.warning("Discovery AI scoring is unavailable: %s", exc)

    app = FastAPI(title="KOL Research Console", version="3.0.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.settings = config
    app.state.post_store = post_store
    app.state.event_store = event_store
    app.state.kol_performance = performance
    app.state.market_store = market_store
    app.state.freestockdb_runtime = freestockdb_runtime
    app.state.event_dossier = event_dossier
    app.state.event_research = event_research
    app.state.review_agent = review_agent
    app.state.recommendation_drafts = recommendation_drafts
    app.state.market_admissions = market_admissions
    app.state.deepseek_credentials = deepseek_credentials
    app.state.twitter_reader_credentials = reader_credentials
    app.state.x_sessions = x_sessions
    app.state.public_backup = public_backup
    collection_runs: dict[str, dict[str, Any]] = {}
    app.state.discovery_store = discovery_store
    app.state.discovery_service = discovery_service
    app.state.discovery_registry = discovery_registry
    app.state.discovery_scorer = discovery_scorer
    app.state.discovery_error = discovery_error
    app.state.discovery_scorer_error = discovery_scorer_error

    recovery_mode_path = config.runtime_root / "market" / "recovery-mode.json"

    def market_recovery_mode() -> dict[str, Any]:
        return load_market_recovery_mode(recovery_mode_path).as_dict()

    def assert_market_writes_allowed() -> None:
        try:
            assert_market_runtime_writes_allowed(recovery_mode_path)
        except MarketWriteBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def market_writes_enabled() -> bool:
        return market_runtime_writes_enabled(recovery_mode_path)

    def returns_writes_enabled() -> bool:
        return load_market_recovery_mode(recovery_mode_path).returns_writes_enabled

    def research_writes_enabled() -> bool:
        return load_market_recovery_mode(recovery_mode_path).research_writes_enabled

    def assert_returns_updates_allowed() -> None:
        try:
            assert_returns_writes_allowed(recovery_mode_path)
        except MarketWriteBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def assert_research_updates_allowed() -> None:
        try:
            assert_research_writes_allowed(recovery_mode_path)
        except MarketWriteBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.state.market_recovery_mode = market_recovery_mode

    def compute_market_health() -> dict[str, Any]:
        mode = market_recovery_mode()
        # Health must stay responsive while a sync/update process owns the
        # DuckDB lock.  Do not queue a UI request behind a long market job.
        try:
            with market_store.lock(timeout=0.05):
                pass
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if any(token in lowered for token in ("lock", "timeout", "already open", "cannot open file")):
                return {
                    "ok": False,
                    "status": "degraded",
                    "market_status": "market_locked",
                    "daily_data_status": "market_locked",
                    "market_session_status": "unknown",
                    "database": str(market_store.db_path),
                    "lagging_symbols": [],
                    "lagging_symbol_count": 0,
                    "error_type": "database_lock",
                    "error": "market database is busy; retry after the current task completes",
                    "recovery_mode": mode.get("mode", "live"),
                    "as_of": mode.get("as_of", ""),
                    "write_enabled": bool(mode.get("write_enabled", True)),
                    "market_update_enabled": bool(mode.get("market_update_enabled", mode.get("write_enabled", True))),
                    "returns_update_enabled": bool(mode.get("returns_update_enabled", mode.get("write_enabled", True))),
                    "research_update_enabled": bool(mode.get("research_update_enabled", mode.get("write_enabled", True))),
                    "publication_mode": str(mode.get("publication_mode") or "atomic_daily"),
                }
        try:
            value = market_store.health()
            # Expose the restored tracked-union coverage explicitly so the
            # console can distinguish a complete historical snapshot from
            # the larger instrument catalog.
            try:
                active_symbols = {
                    str(item.get("symbol"))
                    for item in market_store.list_instruments()
                    if str(item.get("lifecycle") or "") in {"tracking", "pinned"}
                }
                coverage_rows = market_store.get_coverage()
                daily_rows = [
                    row for row in coverage_rows
                    if str(row.get("dataset")) == "daily"
                    and str(row.get("symbol")) in active_symbols
                ]
                value["target_sequence_count"] = len(active_symbols)
                value["raw_sequence_count"] = sum(str(row.get("adjustment")) == "raw" for row in daily_rows)
                value["qfq_sequence_count"] = sum(str(row.get("adjustment")) == "qfq" for row in daily_rows)
                value["raw_current_sequence_count"] = sum(
                    str(row.get("adjustment")) == "raw" and str(row.get("end_date")) == str(mode.get("as_of") or "")
                    for row in daily_rows
                )
                value["qfq_current_sequence_count"] = sum(
                    str(row.get("adjustment")) == "qfq" and str(row.get("end_date")) == str(mode.get("as_of") or "")
                    for row in daily_rows
                )
            except Exception as coverage_error:
                value["coverage_error"] = str(coverage_error)[:500]
            foundation_reader = FoundationMarketReader(FOUNDATION_ROOT)
            foundation = foundation_reader.health()
            if foundation.get("ok"):
                foundation["coverage"] = foundation_reader.coverage(str(foundation.get("as_of") or ""))
                foundation["coverage_complete"] = bool(foundation["coverage"].get("complete"))
            value["foundation"] = foundation
            # The instrument table also contains newly discovered leads that
            # are intentionally waiting for the next publication.  Health's
            # formal freshness contract is the published manifest, so those
            # queued candidates do not make an otherwise complete release look
            # stale.
            published_manifest = read_published_manifest(config.runtime_root / "market")
            published_symbols = {
                str(symbol) for symbol in published_manifest.get("published_symbols", [])
                if str(symbol).isdigit()
            }
            cutoff = str(mode.get("as_of") or "")
            formal_raw = {
                str(row.get("symbol")): str(row.get("end_date") or "")
                for row in daily_rows
                if str(row.get("adjustment")) == "raw" and str(row.get("symbol")) in published_symbols
            }
            formal_qfq = {
                str(row.get("symbol")): str(row.get("end_date") or "")
                for row in daily_rows
                if str(row.get("adjustment")) == "qfq" and str(row.get("symbol")) in published_symbols
            }
            formal_lagging = sorted(
                symbol for symbol in published_symbols
                if cutoff and (formal_raw.get(symbol, "") < cutoff or formal_qfq.get(symbol, "") < cutoff)
            )
            value["published_target_sequence_count"] = len(published_symbols)
            value["published_raw_current_sequence_count"] = sum(formal_raw.get(symbol) == cutoff for symbol in published_symbols)
            value["published_qfq_current_sequence_count"] = sum(formal_qfq.get(symbol) == cutoff for symbol in published_symbols)
            value["published_lagging_symbols"] = formal_lagging
            value["published_lagging_symbol_count"] = len(formal_lagging)
            if cutoff and not formal_lagging:
                value["latest_open_date"] = cutoff
                value["daily_data_status"] = "current"
                value["market_status"] = "current"
                value["lagging_symbols"] = []
                value["lagging_symbol_count"] = 0
            value["recovery_mode"] = mode.get("mode", "live")
            value["as_of"] = mode.get("as_of", "")
            value["write_enabled"] = bool(mode.get("write_enabled", True))
            value["market_update_enabled"] = bool(mode.get("market_update_enabled", value["write_enabled"]))
            value["returns_update_enabled"] = bool(mode.get("returns_update_enabled", value["write_enabled"]))
            value["research_update_enabled"] = bool(mode.get("research_update_enabled", value["write_enabled"]))
            value["publication_mode"] = str(mode.get("publication_mode") or "atomic_daily")
            return value
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
                "recovery_mode": mode.get("mode", "live"),
                "as_of": mode.get("as_of", ""),
                "write_enabled": bool(mode.get("write_enabled", True)),
                "market_update_enabled": bool(mode.get("market_update_enabled", mode.get("write_enabled", True))),
                "returns_update_enabled": bool(mode.get("returns_update_enabled", mode.get("write_enabled", True))),
                "research_update_enabled": bool(mode.get("research_update_enabled", mode.get("write_enabled", True))),
                "publication_mode": str(mode.get("publication_mode") or "atomic_daily"),
            }

    market_health_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
    market_health_lock = threading.Lock()

    def safe_market_health(*, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        cached = market_health_cache.get("value")
        if not force and cached is not None and now < market_health_cache["expires_at"]:
            return cached
        with market_health_lock:
            now = time.monotonic()
            cached = market_health_cache.get("value")
            if not force and cached is not None and now < market_health_cache["expires_at"]:
                return cached
            value = compute_market_health()
            manifest = read_published_manifest(config.runtime_root / "market")
            value["published_manifest"] = {
                key: manifest.get(key)
                for key in (
                    "as_of", "baseline_count", "extension_count", "published_count",
                    "symbols_sha256", "source", "updated_at",
                )
                if key in manifest
            }
            value["admissions"] = market_admissions.summary()
            market_health_cache.update(
                {"expires_at": time.monotonic() + 30.0, "value": value}
            )
            return value

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
            mode = market_recovery_mode()
            historical = (
                str(mode.get("mode")) == "historical"
                and not bool(mode.get("write_enabled", True))
            )
            value["root"] = str(config.freestockdb_root)
            value["data_path"] = str(config.freestockdb_root / "data")
            value["base_url"] = config.freestockdb_url
            value["url"] = config.freestockdb_url
            value["read_only"] = historical
            value["recovery_mode"] = mode.get("mode", "live")
            value["as_of"] = mode.get("as_of", "")
            value["write_enabled"] = bool(mode.get("write_enabled", True))
            if historical:
                provider_ok = bool(value.get("provider", {}).get("ok"))
                service_ok = bool(value.get("service_ok"))
                loopback_ok = bool(value.get("listener", {}).get("loopback_only"))
                value["ok"] = service_ok and provider_ok and loopback_ok
                value["status"] = "historical_read_only" if value["ok"] else "historical_unavailable"
                value["service_status"] = value.get("status")
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

    def route_confirmed_leads(symbols: list[str]) -> dict[str, Any]:
        if not market_writes_enabled():
            mode = market_recovery_mode()
            admitted = market_admissions.queue_confirmed_symbols(
                symbols,
                as_of=str(mode.get("as_of") or ""),
            )
            return {
                "queued_symbols": [],
                "queued_symbol_count": 0,
                "admission_symbols": admitted[:20],
                "admission_symbol_count": len(set(admitted)),
            }
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
        return {
            "queued_symbols": sorted(set(queued))[:20],
            "queued_symbol_count": len(set(queued)),
            "admission_symbols": [],
            "admission_symbol_count": 0,
        }

    def extract_leads() -> dict[str, Any]:
        result = extract_stock_leads(
            post_store,
            instruments=market_store.instrument_map(),
            aliases=load_stock_aliases(config.runtime_root / "watchlist.csv", event_store.events_path),
        )
        reconciled = reconcile_exact_stock_leads(post_store, market_store.instrument_map())
        symbols = sorted(set(result.confirmed_symbols) | set(reconciled))
        routed = route_confirmed_leads(symbols)
        return {
            **asdict(result),
            "reconciled_symbols": reconciled,
            **routed,
            "market_write_mode": (
                "live" if market_writes_enabled() else "historical_admission_only"
            ),
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
        routed = route_confirmed_leads(symbols)
        return {
            **asdict(result),
            "reconciled_symbols": reconciled,
            **routed,
            "market_write_mode": (
                "live" if market_writes_enabled() else "historical_admission_only"
            ),
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
                classifier = build_post_classifier(
                    config.codex_schema,
                    ROOT,
                    deepseek_credentials=deepseek_credentials,
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

            if market_writes_enabled():
                try:
                    mentioned = datetime.fromisoformat(str(post["posted_at"]).replace("Z", "+00:00")).date()
                except ValueError:
                    mentioned = date.today()
                market_store.touch_mention(draft.symbol, mentioned)
                market_store.enqueue_sync(draft.symbol, reason=f"kol_review:{post_id}")
            else:
                market_admissions.queue_confirmed_symbols(
                    [draft.symbol],
                    as_of=str(market_recovery_mode().get("as_of") or ""),
                )
        return symbols

    def approve_draft_record(draft_id: int, note: str = "") -> tuple[dict[str, Any], Any]:
        """Apply the same guarded approval path used by the single-item endpoint."""
        current = recommendation_drafts.get_draft(draft_id)
        if current["status"] == "approved":
            return current, None
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
        canonical_name = str(instrument.get("name") or current.get("security_name") or "")
        leads = post_store.list_stock_leads(
            post_id=current["post_id"],
            symbol=current["symbol"],
            limit=10,
        )
        if not leads:
            post_store.upsert_stock_lead(
                {
                    "post_id": current["post_id"],
                    "kol_id": post_store.get_post(current["post_id"])["kol_id"],
                    "symbol": current["symbol"],
                    "security_name": canonical_name,
                    "instrument_type": instrument.get("instrument_type") or "stock",
                    "direction": current["direction"],
                    "mention_kind": "recommendation",
                    "evidence_text": current["thesis"],
                    "extraction_method": "recommendation_draft_approval",
                    "confidence": current.get("confidence") or 0,
                    "status": "pending",
                }
            )
            leads = post_store.list_stock_leads(
                post_id=current["post_id"],
                symbol=current["symbol"],
                limit=10,
            )
        if leads and leads[0]["status"] != "confirmed":
            post_store.review_stock_lead(
                int(leads[0]["id"]),
                "confirmed",
                note or "Confirmed during recommendation draft approval.",
                symbol=current["symbol"],
                security_name=canonical_name,
            )
        event_draft = {
            "symbol": current["symbol"],
            "security_name": canonical_name,
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
            note=note,
            audit_detail={"recommendation_draft_id": draft_id},
        )
        approved = recommendation_drafts.mark_approved(
            draft_id,
            result.event_ids[0],
            note,
        )
        review_agent.mark_overridden_for_post(
            current["post_id"],
            "approved",
            [event_draft],
        )
        if market_writes_enabled():
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
        return approved, result

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

    def require_discovery(*, require_scorer: bool = False) -> tuple[Any, Any, Any, Any]:
        if not all(
            value is not None
            for value in (
                discovery_store,
                discovery_service,
                discovery_registry,
            )
        ):
            raise HTTPException(
                status_code=503,
                detail=f"full-platform discovery unavailable: {discovery_error or 'not configured'}",
            )
        if require_scorer and discovery_scorer is None:
            raise HTTPException(
                status_code=503,
                detail=f"discovery AI scoring unavailable: {discovery_scorer_error or 'not configured'}",
            )
        return discovery_store, discovery_service, discovery_registry, discovery_scorer

    @app.get("/api/discovery/platforms")
    def discovery_platforms() -> list[dict[str, Any]]:
        _store, _service, registry, _scorer = require_discovery()
        return registry.platforms()

    @app.post("/api/discovery/runs", status_code=201)
    def start_discovery(body: DiscoveryRunRequest) -> dict[str, Any]:
        _store, service, registry, _scorer = require_discovery()
        try:
            platform_info = next(
                item for item in registry.platforms() if item["platform"] == body.platform
            )
            health = platform_info.get("health") or {}
            if not platform_info.get("available"):
                status = str(health.get("status") or "untested")
                reason = str(health.get("reason") or health.get("mode") or "provider is not ready")
                raise HTTPException(
                    status_code=409,
                    detail=f"{body.platform} discovery is {status}: {reason}",
                )
            return service.run(body.platform, body.query, limit=body.limit)
        except KeyError as exc:
            raise HTTPException(503, str(exc)) from exc
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/discovery/runs/{run_id}")
    def discovery_run(run_id: str) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/discovery/candidates")
    def discovery_candidates(
        state: str | None = None,
        platform: str | None = None,
        query: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        store, _service, _registry, _scorer = require_discovery()
        return store.list_candidates(
            state=state,
            platform=platform,
            query=query,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/discovery/candidates/{candidate_id}")
    def discovery_candidate(candidate_id: str) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.get_candidate(candidate_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/discovery/candidates/{candidate_id}/score")
    def score_discovery_candidate(candidate_id: str) -> dict[str, Any]:
        _store, _service, _registry, scorer = require_discovery(require_scorer=True)
        try:
            return scorer.score(candidate_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/discovery/candidates/{candidate_id}/accept")
    def accept_discovery_candidate(
        candidate_id: str,
        body: DiscoveryDecisionRequest,
    ) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.accept(candidate_id, note=body.note)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/discovery/candidates/{candidate_id}/reject")
    def reject_discovery_candidate(
        candidate_id: str,
        body: DiscoveryDecisionRequest,
    ) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.reject(candidate_id, body.note)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/discovery/candidates/{candidate_id}/retry")
    def retry_discovery_candidate(candidate_id: str) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.retry(candidate_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/discovery/kols/{kol_id}/profile")
    def discovery_kol_profile(kol_id: int) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        kol = post_store.get_kol(kol_id)
        if kol is None:
            raise HTTPException(404, "KOL not found")
        return {"kol": kol, "history": store.profile_history(kol_id)}

    @app.post("/api/discovery/kols/{kol_id}/fetch", status_code=202)
    def discovery_kol_fetch(kol_id: int, body: KolBackfillRequest) -> dict[str, Any]:
        require_discovery()
        try:
            return post_store.queue_backfill(kol_id, body.count)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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
                "long_event_count": item["horizons"]["1W"]["long_event_count"],
                "short_event_count": item["horizons"]["1W"]["short_event_count"],
                "executable_long_event_count": item["horizons"]["1W"]["executable_long_event_count"],
                "audit_event_count": item["horizons"]["1W"]["audit_event_count"],
                "executable_event_count": item["horizons"]["1W"]["event_count"],
                "horizons": horizons,
            })
        return {
            "policy": {
                "primary_metric": "platform-separated median long-only batch excess return",
                "batch_weighting": "same-post stocks are equal-weighted into one batch",
                "score": None,
                "direction_policy": "short events are retained for audit but excluded from A-share return statistics",
                "sample_policy": "small long-only samples are shown but never formally ranked",
                "counting_policy": "long_event_count counts the long events in the return sample; short_event_count counts excluded short events (audit only, independent of window/horizon); executable_long_event_count counts long events free of primary execution warnings; audit_event_count = long_event_count + short_event_count",
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
        assert_returns_updates_allowed()
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
            values = body.model_dump(exclude_none=True)
            availability = values.pop("availability_status", None)
            reason = values.pop("availability_reason", "")
            result = post_store.update_kol(kol_id, values) if values else post_store.get_kol(kol_id)
            if result is None:
                raise KeyError(f"KOL not found: {kol_id}")
            if availability is not None:
                result = post_store.set_account_availability(
                    kol_id,
                    availability,
                    reason=reason,
                    source="manual_ui",
                )
            return result
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

    @app.post("/api/recommendation-drafts/bulk-preview")
    def bulk_preview_recommendation_drafts(
        body: RecommendationDraftBulkPreviewRequest,
    ) -> dict[str, Any]:
        try:
            return {
                **recommendation_drafts.create_bulk_snapshot(
                    review_date=body.review_date,
                    queue_scope=body.queue_scope,
                    status_filter=body.status,
                    limit=body.limit,
                ),
                "ok": True,
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/recommendation-drafts/bulk-approve")
    def bulk_approve_recommendation_drafts(
        body: RecommendationDraftBulkApproveRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            snapshot = recommendation_drafts.consume_bulk_snapshot(body.snapshot_token)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        approved: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        symbols: set[str] = set()
        event_ids: list[str] = []
        for raw_id in snapshot["draft_ids"]:
            draft_id = int(raw_id)
            try:
                current = recommendation_drafts.get_draft(draft_id)
                if current["status"] != "ready" or current.get("attention_reasons"):
                    skipped.append({
                        "draft_id": draft_id,
                        "reason": "changed_since_preview",
                        "status": current["status"],
                        "attention_reasons": current.get("attention_reasons", []),
                    })
                    continue
                result, approval = approve_draft_record(draft_id, body.note)
                symbols.add(str(result["symbol"]))
                if approval is not None:
                    event_ids.extend(approval.event_ids)
                approved.append({
                    "draft_id": draft_id,
                    "event_id": result.get("event_id") or (approval.event_ids[0] if approval else ""),
                    "status": result["status"],
                    "symbol": result["symbol"],
                })
            except KeyError as exc:
                failed.append({"draft_id": draft_id, "error": str(exc)})
            except Exception as exc:
                failed.append({"draft_id": draft_id, "error": str(exc)[:1000]})
        if symbols and returns_writes_enabled():
            background_tasks.add_task(
                run_post_approval_refresh,
                config.runtime_root,
                ROOT,
                sorted(symbols),
                as_of=date.today(),
            )
        if event_ids and research_writes_enabled():
            background_tasks.add_task(
                lambda ids=sorted(set(event_ids)): [event_dossier.refresh(event_id) for event_id in ids]
            )
        return {
            "ok": not failed,
            "snapshot_consumed": True,
            "processed": len(approved) + len(skipped) + len(failed),
            "approved": approved,
            "skipped": skipped,
            "failed": failed,
            "queued_symbols": sorted(symbols),
            "refresh_status": "queued" if symbols else "not_needed",
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
            approved, result = approve_draft_record(draft_id, body.note)
            if result is None:
                return sanitize_recommendation_draft(approved)
            refresh_status = "not_requested"
            if returns_writes_enabled():
                background_tasks.add_task(
                    run_post_approval_refresh,
                    config.runtime_root,
                    ROOT,
                    [approved["symbol"]],
                    as_of=date.today(),
                )
                refresh_status = "queued"
            if research_writes_enabled():
                background_tasks.add_task(event_dossier.refresh, result.event_ids[0])
            return {
                **sanitize_recommendation_draft(approved),
                "created_event": bool(result.created_events),
                "refresh_status": refresh_status,
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
                if market_writes_enabled():
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
                if body.refresh_returns and queued_symbols and returns_writes_enabled():
                    background_tasks.add_task(
                        run_post_approval_refresh,
                        config.runtime_root,
                        ROOT,
                        queued_symbols,
                        as_of=date.today(),
                    )
                    refresh_status = "queued"
                for event_id in result.event_ids:
                    if research_writes_enabled():
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
        if body.provider == "nitter":
            raise HTTPException(409, "Nitter is shadow-only and disabled for automatic collection")
        fallback_mode = "disabled"
        provider = build_x_post_provider(
            body.provider,
            xtf_command=str(xtf_command),
            nitter_url=config.nitter_url,
            fallback_mode=fallback_mode,
            session_manager=x_sessions,
            batch_key=f"api:fetch:{uuid.uuid4().hex}",
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

    @app.get("/api/collection/coverage")
    def collection_coverage(
        platform: str = Query(default="", pattern="^(|X|Zhihu)$"),
        window_start: str = "",
        window_end: str = "",
    ) -> dict[str, Any]:
        if platform:
            return post_store.collection_coverage(
                platform=platform,
                window_start=window_start,
                window_end=window_end,
            )
        return {
            "platform": "all",
            "items": [
                post_store.collection_coverage(
                    platform=value,
                    window_start=window_start,
                    window_end=window_end,
                )
                for value in ("X", "Zhihu")
            ],
        }

    @app.post("/api/collection/recovery/preview")
    def collection_recovery_preview() -> dict[str, Any]:
        start = (date.today() - timedelta(days=7)).isoformat()
        end = date.today().isoformat()
        return {
            "ok": True,
            "scope": "recent",
            "window_start": start,
            "window_end": end,
            "coverage": [
                post_store.collection_coverage(platform=value, window_start=start, window_end=end)
                for value in ("X", "Zhihu")
            ],
            "reader_configured": any(item.get("status") == "ready" for item in x_sessions.policy_status().get("slots", [])),
            "x_session_ready": any(item.get("status") == "ready" for item in x_sessions.policy_status().get("slots", [])),
            "ai_is_optional": True,
        }

    @app.post("/api/collection/recovery/start", status_code=202)
    def collection_recovery_start() -> dict[str, Any]:
        if not any(item.get("status") == "ready" for item in x_sessions.policy_status().get("slots", [])):
            raise HTTPException(409, "no verified X session is ready")
        for existing in collection_runs.values():
            if existing.get("status") != "running":
                continue
            try:
                process = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {existing['pid']}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if str(existing["pid"]) in (process.stdout or ""):
                    raise HTTPException(409, "a collection recovery run is already active")
            except HTTPException:
                raise
            except Exception:
                existing["status"] = "completed"
                existing["completed_at"] = now_iso()
        run_id = uuid.uuid4().hex
        cli = ROOT / "_automation" / "trading_research" / "trading_cli.py"
        log_dir = config.runtime_root / "kol" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"collection-recovery-{run_id}.log"
        handle = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [sys.executable, str(cli), "kol-gap-recover", "--scope", "recent", "--resume"],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
        finally:
            handle.close()
        collection_runs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "pid": process.pid,
            "log": str(log_path),
            "started_at": now_iso(),
        }
        return collection_runs[run_id]

    @app.get("/api/collection/recovery/{run_id}")
    def collection_recovery_status(run_id: str) -> dict[str, Any]:
        item = collection_runs.get(run_id)
        if not item:
            raise HTTPException(404, "recovery run not found in this server session")
        try:
            process = subprocess.run(
                ["tasklist", "/FI", f"PID eq {item['pid']}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            alive = str(item["pid"]) in (process.stdout or "")
        except Exception:
            alive = False
        if not alive and item["status"] == "running":
            item["status"] = "completed"
            item["completed_at"] = now_iso()
        item["coverage"] = {
            "X": post_store.collection_coverage(platform="X"),
            "Zhihu": post_store.collection_coverage(platform="Zhihu"),
        }
        return item

    @app.post("/api/collection/recovery/{run_id}/cancel")
    def collection_recovery_cancel(run_id: str) -> dict[str, Any]:
        item = collection_runs.get(run_id)
        if not item:
            raise HTTPException(404, "recovery run not found in this server session")
        subprocess.run(["taskkill", "/PID", str(item["pid"]), "/T", "/F"], capture_output=True, check=False)
        item["status"] = "cancelled"
        item["completed_at"] = now_iso()
        return item

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

    @app.get("/api/market/admissions")
    def list_market_admissions(
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            return {
                "items": market_admissions.list(status=status, limit=limit, offset=offset),
                "summary": market_admissions.summary(),
                "published_manifest": read_published_manifest(config.runtime_root / "market"),
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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
            writes_enabled = market_writes_enabled()
            if body.action == "confirmed":
                if current["status"] == "confirmed":
                    if body.symbol is not None and body.symbol != current["symbol"]:
                        raise ValueError("已确认线索不能直接改代码，请先退回待确认")
                    if not writes_enabled:
                        market_admissions.queue_confirmed_symbols(
                            [symbol],
                            as_of=str(market_recovery_mode().get("as_of") or ""),
                        )
                        return {**current, "market_admission_status": "queued"}
                    return current
                if current["status"] != "pending":
                    raise ValueError("只有待确认线索可以晋升为确认状态")
                if writes_enabled:
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
                if not writes_enabled:
                    market_admissions.queue_confirmed_symbols(
                        [symbol],
                        as_of=str(market_recovery_mode().get("as_of") or ""),
                    )
                    return {**reviewed, "market_admission_status": "queued"}
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
                    if instrument_before is not None:
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
        assert_market_writes_allowed()
        return market_store.upsert_instrument(Instrument(**body.model_dump()))

    @app.patch("/api/instruments/{symbol}")
    def patch_instrument(symbol: str, body: InstrumentPatch) -> dict[str, Any]:
        assert_market_writes_allowed()
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
        assert_market_writes_allowed()
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

    def run_foundation_refresh_job(as_of: str, notify: bool, state_path: Path) -> None:
        command = [
            sys.executable,
            str(ROOT / "_automation" / "trading_research" / "trading_cli.py"),
            "kol-data-refresh",
            "--as-of",
            as_of,
        ]
        if notify:
            command.append("--notify")
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                check=False,
            )
            output = (completed.stdout or completed.stderr or "").strip()
            state = {
                "status": "completed" if completed.returncode == 0 else "degraded",
                "returncode": completed.returncode,
                "output_tail": output[-4000:],
                "finished_at": now_iso(),
            }
        except subprocess.TimeoutExpired:
            state = {
                "status": "degraded",
                "returncode": -1,
                "output_tail": "shared foundation refresh timed out after 1800 seconds",
                "finished_at": now_iso(),
            }
        temporary = state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, state_path)

    @app.post("/api/market/foundation-refresh", status_code=202)
    def refresh_foundation(
        body: FoundationRefreshRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        assert_market_writes_allowed()
        state_path = config.runtime_root / "market" / "foundation-refresh.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            current = {}
        if current.get("status") == "running":
            return {"ok": True, "status": "already_running", "state": current}
        state_path.write_text(
            json.dumps(
                {"status": "running", "as_of": body.as_of, "started_at": now_iso()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        background_tasks.add_task(run_foundation_refresh_job, body.as_of, body.notify, state_path)
        return {"ok": True, "status": "triggered", "as_of": body.as_of}

    @app.get("/api/market/foundation-refresh")
    def foundation_refresh_status() -> dict[str, Any]:
        state_path = config.runtime_root / "market" / "foundation-refresh.json"
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"status": "idle"}

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
        assert_market_writes_allowed()
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
        assert_research_updates_allowed()
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
        assert_research_updates_allowed()
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
            if market_writes_enabled():
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
            if revision.get("recalculation_required") and returns_writes_enabled():
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
                if research_writes_enabled():
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
        assert_research_updates_allowed()
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

    def build_system_diagnostics(*, force: bool = False) -> dict[str, Any]:
        recent_runs = post_store.recent_fetch_runs(limit=1)
        attempts = post_store.recent_fetch_attempts(limit=100)
        twitter_attempts = [item for item in attempts if item["provider"] == "twitter-cli"]
        fallback_attempts = [item for item in attempts if item["provider"] == "nitter"]
        docker = docker_health()
        # Health polling must not issue an authenticated timeline request to
        # X.  Nitter remains shadow-only; an inexpensive loopback probe is
        # sufficient for the console status card.
        nitter = http_health(config.nitter_url)
        nitter = {
            "ready": bool(nitter.get("ready")),
            "status": "http_ok" if nitter.get("ready") else "unavailable",
            "http_status": int(nitter.get("status") or 0),
            "error": str(nitter.get("error") or ""),
        }
        redis = docker_container_health("nitter-redis")
        zhihu_kols = [item for item in post_store.list_kols() if item.get("platform") == "Zhihu"]
        active_zhihu = [item for item in zhihu_kols if item.get("status") == "active"]
        paused_zhihu = [item for item in zhihu_kols if item.get("status") == "paused"]
        health_cutoff = datetime.now(SHANGHAI) - timedelta(hours=24)

        def checked_recently(item: dict[str, Any]) -> bool:
            raw = str(item.get("availability_checked_at") or item.get("updated_at") or "")
            try:
                checked = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=SHANGHAI)
                return checked.astimezone(SHANGHAI) >= health_cutoff
            except ValueError:
                return False

        zhihu_failed = [
            item for item in active_zhihu
            if checked_recently(item)
            and str(item.get("fetch_status") or "") not in {"never", "success"}
        ]
        zhihu_stale = [item for item in active_zhihu if not checked_recently(item)]
        zhihu_status = (
            "disabled"
            if not active_zhihu
            else "degraded" if zhihu_failed
            else "success" if any(item.get("fetch_status") == "success" for item in active_zhihu)
            else "stale"
        )
        try:
            with socket.create_connection(("127.0.0.1", int(zhihu_provider.port)), timeout=0.4):
                zhihu_browser_status = "ready"
        except OSError:
            zhihu_browser_status = "browser_unavailable"
        if zhihu_failed:
            recent_errors = " ".join(str(item.get("last_error") or "") for item in zhihu_failed)
            lowered_errors = recent_errors.casefold()
            if "rate" in lowered_errors or "429" in lowered_errors:
                zhihu_browser_status = "rate_limited"
            elif "auth" in lowered_errors or "登录" in recent_errors:
                zhihu_browser_status = "auth_required"
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
        market_component = safe_market_health(force=force)
        freestock_component = safe_freestockdb_health(force=force)
        post_recovery = post_store.post_recovery_summary()
        queue_status = {
            "x": post_store.fetch_queue_overview("X"),
            "zhihu": post_store.fetch_queue_overview("Zhihu"),
            "ocr_model": post_store.model_queue_summary(daily_limit=250, ocr_daily_limit=150),
            "post_recovery": post_recovery,
            "market_admissions": market_admissions.summary(),
        }
        x_session_health = x_sessions.policy_status()
        public_backup_health = public_backup.status()
        slot_statuses = [str(item.get("status") or "") for item in x_session_health.get("slots", [])]
        x_pool_status = (
            "cooldown" if x_session_health.get("paused_until") or "cooldown" in slot_statuses
            else "ready" if "ready" in slot_statuses
            else "auth_required" if "auth_required" in slot_statuses
            else "disabled" if slot_statuses and all(value == "disabled" for value in slot_statuses)
            else "pending_verification"
        )
        discovery_platforms_value: list[dict[str, Any]] = []
        discovery_runtime_error = ""
        if discovery_registry is not None:
            try:
                discovery_platforms_value = discovery_registry.platforms()
            except Exception as exc:
                discovery_runtime_error = str(exc)
                logger.warning("Discovery health degraded: %s", exc)
        discovery_available = discovery_registry is not None
        discovery_scoring_available = discovery_scorer is not None
        refresh_state = read_refresh_state(config.runtime_root)
        if (
            str(market_component.get("recovery_mode")) == "historical"
            and refresh_state.get("status") == "failed"
        ):
            refresh_state = {
                **refresh_state,
                "status": "disabled",
                "reason": "historical_read_only",
                "legacy_state": refresh_state.get("status"),
            }
        return {
            "ok": True,
            "date": date.today().isoformat(),
            "recovery_mode": market_component.get("recovery_mode", "live"),
            "as_of": market_component.get("as_of", ""),
            "write_enabled": bool(market_component.get("write_enabled", True)),
            "market_update_enabled": bool(market_component.get("market_update_enabled", market_component.get("write_enabled", True))),
            "returns_update_enabled": bool(market_component.get("returns_update_enabled", market_component.get("write_enabled", True))),
            "research_update_enabled": bool(market_component.get("research_update_enabled", market_component.get("write_enabled", True))),
            "twitter_cli": _resolve_twitter_command("twitter"),
            "twitter_credentials_configured": credentials.configured(),
            "twitter_reader_credentials_configured": reader_credentials.configured(),
            "x_sessions": x_session_health,
            "x_collection_status": x_pool_status,
            "public_backup": public_backup_health,
            "twitter_auth_status": recent_runs[0].get("auth_status", "unknown") if recent_runs else "never",
            "zhihu_capture_available": zhihu_provider.script_path.is_file(),
            "zhihu_active_kols": len(active_zhihu),
            "zhihu_paused_kols": len(paused_zhihu),
            "zhihu_failed_kols": len(zhihu_failed),
            "zhihu_failure_handles": [str(item.get("handle")) for item in zhihu_failed[:20]],
            "zhihu_stale_kols": len(zhihu_stale),
            "zhihu_fetch_status": zhihu_status,
            "zhihu_status": zhihu_browser_status,
            "zhihu_cdp_port": int(zhihu_provider.port),
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
            "post_recovery": post_recovery,
            "queue_status": queue_status,
            "post_fetch_task": _task_status("KOL_Post_Fetch_Daily"),
            "return_task": _task_status("KOL_Return_Tracker_Daily"),
            "market_sync_task": _task_status("Market_Data_Sync_Daily"),
            "classification_task": _task_status("KOL_Post_Classify_Daily"),
            "review_agent_task": _task_status("KOL_Review_Agent"),
            "morning_pipeline_task": _task_status("KOL_Morning_Pipeline"),
            "morning_runs": recommendation_drafts.recent_morning_runs(5),
            "digest_task": _task_status("Research_Data_Digest_Daily"),
            "review_agent": review_summary,
            "discovery": {
                "available": discovery_available,
                "scoring_available": discovery_scoring_available,
                "platforms": discovery_platforms_value,
                "error": discovery_error,
                "runtime_error": discovery_runtime_error,
                "scoring_error": discovery_scorer_error,
            },
            "market": market_component,
            "freestockdb": freestock_component,
            "component_status": {
                "hermes_gateway": {
                    "status": "running" if gateway_running else "stopped",
                    "pid": gateway_pid,
                },
                "operator": {"status": "available"},
                "x": {
                    "status": x_pool_status,
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
                "discovery": {
                    "status": "ready" if discovery_available else "unavailable",
                    "platform_count": len(discovery_platforms_value),
                    "scoring_status": "ready" if discovery_scoring_available else "degraded",
                },
            },
            "pipeline_refresh": refresh_state,
            "nitter_start_task": _task_status("KOL_Nitter_Shadow_Logon"),
        }

    diagnostics_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
    diagnostics_lock = threading.Lock()

    @app.get("/api/system/diagnostics")
    def system_diagnostics(force: bool = Query(default=False)) -> dict[str, Any]:
        current = time.monotonic()
        cached = diagnostics_cache.get("value")
        if not force and cached is not None and current < diagnostics_cache["expires_at"]:
            return {**cached, "diagnostics_cached": True}
        with diagnostics_lock:
            current = time.monotonic()
            cached = diagnostics_cache.get("value")
            if not force and cached is not None and current < diagnostics_cache["expires_at"]:
                return {**cached, "diagnostics_cached": True}
            value = build_system_diagnostics(force=force)
            diagnostics_cache.update(
                {"expires_at": time.monotonic() + 60.0, "value": value}
            )
            return {**value, "diagnostics_cached": False}

    @app.get("/api/system/health")
    def system_health() -> dict[str, Any]:
        """Return the console-critical state without invoking deep OS/provider probes."""
        mode = market_recovery_mode()
        recent_runs = post_store.recent_fetch_runs(limit=1)
        attempts = post_store.recent_fetch_attempts(limit=20)
        twitter_attempts = [item for item in attempts if item["provider"] == "twitter-cli"]
        latest_twitter_attempt = twitter_attempts[0] if twitter_attempts else {}
        latest_twitter_error = str(latest_twitter_attempt.get("error_code") or "")
        x_session_health = x_sessions.policy_status()
        slot_statuses = [str(item.get("status") or "") for item in x_session_health.get("slots", [])]
        x_pool_status = (
            "cooldown" if x_session_health.get("paused_until") or "cooldown" in slot_statuses
            else "ready" if "ready" in slot_statuses
            else "auth_required" if "auth_required" in slot_statuses
            else "disabled" if slot_statuses and all(value == "disabled" for value in slot_statuses)
            else "pending_verification"
        )
        zhihu_kols = [item for item in post_store.list_kols() if item.get("platform") == "Zhihu"]
        active_zhihu = [item for item in zhihu_kols if item.get("status") == "active"]
        paused_zhihu = [item for item in zhihu_kols if item.get("status") == "paused"]
        failed_zhihu = [
            item for item in active_zhihu
            if str(item.get("fetch_status") or "") not in {"never", "success"}
        ]
        try:
            with socket.create_connection(("127.0.0.1", int(zhihu_provider.port)), timeout=0.15):
                zhihu_browser_status = "ready"
        except OSError:
            zhihu_browser_status = "browser_unavailable"

        cached_diagnostics = dict(diagnostics_cache.get("value") or {})
        cached_market = market_health_cache.get("value") or {
            "ok": False,
            "status": "checking",
            "market_status": "checking",
            "active_instruments": 0,
            "coverage_count": 0,
            "lagging_symbol_count": 0,
            "recovery_mode": mode.get("mode", "live"),
            "as_of": mode.get("as_of", ""),
            "write_enabled": bool(mode.get("write_enabled", True)),
            "market_update_enabled": bool(mode.get("market_update_enabled", mode.get("write_enabled", True))),
            "returns_update_enabled": bool(mode.get("returns_update_enabled", mode.get("write_enabled", True))),
            "research_update_enabled": bool(mode.get("research_update_enabled", mode.get("write_enabled", True))),
            "publication_mode": str(mode.get("publication_mode") or "atomic_daily"),
            "admissions": market_admissions.summary(),
        }
        cached_freestock = freestockdb_health_cache.get("value") or {
            "ok": False,
            "status": "checking",
            "service_status": "checking",
            "root": str(config.freestockdb_root),
            "data_path": str(config.freestockdb_root / "data"),
            "url": config.freestockdb_url,
            "read_only": not market_writes_enabled(),
        }
        component_status = dict(cached_diagnostics.get("component_status") or {})
        component_status.update(
            {
                "operator": {"status": "available"},
                "x": {"status": x_pool_status, "error_code": latest_twitter_error},
                "market": {
                    "status": cached_market.get("market_status", cached_market.get("status", "checking")),
                    "ok": bool(cached_market.get("ok")),
                },
                "freestockdb": {
                    "status": cached_freestock.get("service_status", "checking"),
                    "ok": bool(cached_freestock.get("ok")),
                },
            }
        )
        component_status.setdefault("hermes_gateway", {"status": "checking", "pid": ""})
        component_status.setdefault("nitter", {"status": "checking", "ready": False})
        component_status.setdefault("ai", {"status": "ready"})
        model_budget = ModelDailyBudget(post_store, daily_limit=250).status()
        ocr_budget = OcrDailyBudget(post_store, daily_limit=150).status()
        queue_status = {
            "x": post_store.fetch_queue_overview("X"),
            "zhihu": post_store.fetch_queue_overview("Zhihu"),
            "ocr_model": post_store.model_queue_summary(daily_limit=250, ocr_daily_limit=150),
            "post_recovery": post_store.post_recovery_summary(),
            "market_admissions": market_admissions.summary(),
        }
        reconciliation = system_reconciliation()
        return {
            **cached_diagnostics,
            "ok": True,
            "date": date.today().isoformat(),
            "lightweight": True,
            "diagnostics_cached": bool(cached_diagnostics),
            "recovery_mode": mode.get("mode", "live"),
            "as_of": mode.get("as_of", ""),
            "write_enabled": bool(mode.get("write_enabled", True)),
            "market_update_enabled": bool(mode.get("market_update_enabled", mode.get("write_enabled", True))),
            "returns_update_enabled": bool(mode.get("returns_update_enabled", mode.get("write_enabled", True))),
            "research_update_enabled": bool(mode.get("research_update_enabled", mode.get("write_enabled", True))),
            "publication_mode": str(mode.get("publication_mode") or "atomic_daily"),
            "twitter_cli": _resolve_twitter_command("twitter"),
            "twitter_credentials_configured": credentials.configured(),
            "twitter_reader_credentials_configured": reader_credentials.configured(),
            "x_sessions": x_session_health,
            "x_collection_status": x_pool_status,
            "public_backup": public_backup.status(),
            "twitter_auth_status": recent_runs[0].get("auth_status", "unknown") if recent_runs else "never",
            "zhihu_capture_available": zhihu_provider.script_path.is_file(),
            "zhihu_active_kols": len(active_zhihu),
            "zhihu_paused_kols": len(paused_zhihu),
            "zhihu_failed_kols": len(failed_zhihu),
            "zhihu_failure_handles": [str(item.get("handle") or "") for item in failed_zhihu[:20]],
            "zhihu_fetch_status": "degraded" if failed_zhihu else "success" if active_zhihu else "disabled",
            "zhihu_status": zhihu_browser_status,
            "zhihu_cdp_port": int(zhihu_provider.port),
            "codex_cli": shutil.which("codex") or "",
            "deepseek_credentials_configured": deepseek_credentials.configured(),
            "deepseek_provider": DeepSeekPostClassifier.provider_name,
            "deepseek_model": DeepSeekPostClassifier.model_name,
            "unlimited_ocr_root": str(ocr_root),
            "unlimited_ocr_available": UnlimitedOcrBatchClassifier(ocr_root, ocr_runner).available(),
            "rapid_ocr_runtime": str(rapid_ocr_python),
            "rapid_ocr_available": RapidOcrBatchClassifier(rapid_ocr_python, rapid_ocr_runner).available(),
            "ocr_provider": "rapidocr",
            "database": str(post_store.path),
            "media_root": str(post_store.media_root),
            "media_bytes": int(cached_diagnostics.get("media_bytes") or 0),
            "post_recovery": post_store.post_recovery_summary(),
            "queue_status": queue_status,
            "reconciliation": reconciliation,
            "model_daily_budget": model_budget,
            "ocr_daily_budget": ocr_budget,
            "model_queue": post_store.model_queue_summary(daily_limit=250),
            "market_admissions": market_admissions.summary(),
            "post_fetch_task": cached_diagnostics.get("post_fetch_task", "checking"),
            "return_task": cached_diagnostics.get("return_task", "disabled" if not market_writes_enabled() else "checking"),
            "market_sync_task": cached_diagnostics.get("market_sync_task", "disabled" if not market_writes_enabled() else "checking"),
            "classification_task": cached_diagnostics.get("classification_task", "checking"),
            "review_agent_task": cached_diagnostics.get("review_agent_task", "checking"),
            "morning_pipeline_task": cached_diagnostics.get("morning_pipeline_task", "checking"),
            "morning_runs": recommendation_drafts.recent_morning_runs(5),
            "digest_task": cached_diagnostics.get("digest_task", "checking"),
            "nitter_credentials_configured": nitter_credentials.configured(),
            "nitter_url": config.nitter_url,
            "nitter_ready": bool(cached_diagnostics.get("nitter_ready")),
            "nitter_status": cached_diagnostics.get("nitter_status", "checking"),
            "nitter_http_status": int(cached_diagnostics.get("nitter_http_status") or 0),
            "redis_ready": bool(cached_diagnostics.get("redis_ready")),
            "docker_installed": bool(cached_diagnostics.get("docker_installed")),
            "docker_ready": bool(cached_diagnostics.get("docker_ready")),
            "docker_version": str(cached_diagnostics.get("docker_version") or ""),
            "xtf_command": str(xtf_command) if xtf_command.exists() else "",
            "xtf_version": cached_diagnostics.get("xtf_version", ""),
            "nitter_start_task": cached_diagnostics.get("nitter_start_task", "checking"),
            "twitter_success_rate": (
                sum(item["status"] == "success" for item in twitter_attempts) / len(twitter_attempts)
                if twitter_attempts else None
            ),
            "nitter_attempt_count": int(cached_diagnostics.get("nitter_attempt_count") or 0),
            "fallback_mode": _fallback_mode(config.runtime_root),
            "market": cached_market,
            "freestockdb": cached_freestock,
            "pipeline_refresh": read_refresh_state(config.runtime_root),
            "review_agent": cached_diagnostics.get("review_agent", {}),
            "shadow_rollout": cached_diagnostics.get("shadow_rollout", post_store.shadow_rollout_status()),
            "component_status": component_status,
        }

    @app.get("/api/system/queues")
    def system_queues() -> dict[str, Any]:
        """Return all durable backlogs; this endpoint is read-only and cheap."""
        reconciliation = system_reconciliation()
        return {
            "ok": True,
            "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "x": post_store.fetch_queue_overview("X"),
            "zhihu": post_store.fetch_queue_overview("Zhihu"),
            "ocr_model": post_store.model_queue_summary(daily_limit=250, ocr_daily_limit=150),
            "post_recovery": post_store.post_recovery_summary(),
            "market_admissions": market_admissions.summary(),
            "reconciliation": reconciliation,
        }

    @app.get("/api/system/reconciliation")
    def system_reconciliation() -> dict[str, Any]:
        """Expose the last offline reconciliation without probing providers."""
        report_dir = config.runtime_root / "restore-reports"
        reports = sorted(report_dir.glob("kol-operational-reconcile*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        path = reports[0] if reports else report_dir / "kol-operational-reconcile.json"
        report: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                report = loaded
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        with post_store.connect() as db:
            pending_recommendation = int(db.execute(
                "SELECT COUNT(*) FROM stock_leads WHERE status='pending' AND mention_kind='recommendation'"
            ).fetchone()[0])
            archive_only = int(db.execute(
                "SELECT COUNT(*) FROM stock_leads WHERE status='pending' AND mention_kind<>'recommendation'"
            ).fetchone()[0])
            active_drafts = int(db.execute(
                "SELECT COUNT(*) FROM recommendation_drafts WHERE status IN ('ready','needs_attention')"
            ).fetchone()[0])
        return {
            "run_id": str(report.get("run_id") or ""),
            "status": str(report.get("status") or ("completed" if report.get("ok") and report.get("published") else "idle" if not report else "blocked")),
            "report_path": str(path),
            "counts": report.get("counts") or {},
            "errors": report.get("errors") or [],
            "pending_recommendation_leads": pending_recommendation,
            "archive_only_leads": archive_only,
            "active_drafts": active_drafts,
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

    @app.get("/api/system/x-sessions")
    def x_session_status() -> dict[str, Any]:
        return x_sessions.policy_status()

    @app.put("/api/system/x-sessions/{slot_id}/credentials")
    def save_x_session_credentials(slot_id: int, body: XSessionCredentialRequest) -> dict[str, Any]:
        try:
            return x_sessions.save_credentials(slot_id, body.auth_token, body.ct0, label=body.label)
        except (CredentialValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CredentialStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/system/x-sessions/{slot_id}/verify")
    def verify_x_session(slot_id: int) -> dict[str, Any]:
        try:
            return x_sessions.verify_slot(slot_id, _resolve_twitter_command("twitter"))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except XSessionUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/system/x-sessions/{slot_id}")
    def patch_x_session(slot_id: int, body: XSessionStatusRequest) -> dict[str, Any]:
        try:
            return x_sessions.set_status(slot_id, body.status, reason=body.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/system/x-policy")
    def patch_x_policy(body: XCollectionPolicyRequest) -> dict[str, Any]:
        if body.enabled is None and body.paused is None and body.limit_mode is None and body.min_interval_seconds is None:
            raise HTTPException(status_code=422, detail="policy change is required")
        try:
            return x_sessions.set_policy(
                enabled=body.enabled,
                paused=body.paused,
                reason=body.reason,
                limit_mode=body.limit_mode,
                min_interval_seconds=body.min_interval_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/system/public-backup")
    def public_backup_status() -> dict[str, Any]:
        return public_backup.status()

    @app.patch("/api/system/public-backup")
    def patch_public_backup(body: PublicBackupPolicyRequest) -> dict[str, Any]:
        if body.enabled is None and body.paused is None and body.limit_mode is None and body.min_interval_seconds is None:
            raise HTTPException(status_code=422, detail="policy change is required")
        try:
            return public_backup.set_policy(
                enabled=body.enabled,
                paused=body.paused,
                reason=body.reason,
                limit_mode=body.limit_mode,
                min_interval_seconds=body.min_interval_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/system/twitter-credentials")
    def save_twitter_credentials(body: TwitterCredentialRequest) -> dict[str, bool]:
        try:
            validate_twitter_credentials(body.auth_token, body.ct0)
        except CredentialValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise HTTPException(status_code=409, detail="请选择 X 会话槽位后保存，旧单槽位接口不会覆盖现有会话")

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

    @app.post("/api/system/twitter-reader/promote-nitter")
    def promote_nitter_reader() -> dict[str, bool]:
        try:
            auth_token, ct0 = nitter_credentials.load_values()
            reader_credentials.save(auth_token, ct0)
        except CredentialStorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Nitter reader credentials are not configured") from exc
        return {"ok": True, "configured": reader_credentials.configured()}

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
