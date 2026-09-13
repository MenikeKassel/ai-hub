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

SOURCE_FILE = Path(__file__).resolve().parents[1] / "kol_api.py"

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

from kol_tracker import MARK_FIELDS, SHANGHAI, KolStore, event_amendment_changes, now_iso, validate_event

from event_context import FEATURE_VERSION, event_context_input_hash

from nitter_runtime import docker_container_health, docker_health, http_health, xtf_version

from market_data import FreeStockDBMarketProvider, Instrument, MarketStore

from market_indicators import INDICATOR_VERSION, compute_daily_indicators

from freestockdb_runtime import FreeStockDBRuntime

from kol_intraday import backfill_event_intraday

from event_dossier import EventDossierService

from event_research_ai import build_event_research_interpreter

from event_research_service import EventMethodResearchService

from pipeline_jobs import reconcile_refresh_state, read_refresh_state, run_post_approval_refresh

from review_agent import ReviewAgentRepository, rollback_decision

from review_queue import ReviewQueueService

from runtime_jobs import read_workers

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

ROOT = SOURCE_FILE.resolve().parents[2]

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
            SOURCE_FILE.resolve().parents[3] / "freestock" / "stockdb",
        )
    )
    freestockdb_url: str = "http://127.0.0.1:7899"

    @classmethod
    def default(cls) -> "ApiSettings":
        runtime = Path(os.environ.get("TRADING_RUNTIME_ROOT", ROOT / "_runtime" / "trading"))
        return cls(
            runtime_root=runtime,
            frontend_dist=ROOT / "_automation" / "trading_research" / "ui" / "dist",
            codex_schema=SOURCE_FILE.with_name("kol_classifier_schema.json"),
            xtf_command=ROOT / "_runtime" / "venv-x-fetcher" / "Scripts" / "xtf.exe",
            nitter_url=os.environ.get("KOL_NITTER_URL", "http://127.0.0.1:9377"),
            freestockdb_root=Path(
                os.environ.get(
                    "FREESTOCKDB_ROOT",
                    SOURCE_FILE.resolve().parents[3] / "freestock" / "stockdb",
                )
            ),
            freestockdb_url=os.environ.get("FREESTOCKDB_URL", "http://127.0.0.1:7899"),
        )

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
    if os.environ.get("KOL_OFFLINE_TESTS") == "1":
        raise RuntimeError("Real scheduled tasks are forbidden in offline tests")
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


from .models import (
    KolCreate,
    KolPatch,
    KolBackfillRequest,
    DiscoveryRunRequest,
    DiscoveryDecisionRequest,
    DigestAuthorProfileInput,
    DigestAuthorProfileBatch,
    FetchRequest,
    TwitterCredentialRequest,
    XSessionCredentialRequest,
    XSessionStatusRequest,
    XCollectionPolicyRequest,
    PublicBackupPolicyRequest,
    DeepSeekCredentialRequest,
    PerformanceRefreshRequest,
    FoundationRefreshRequest,
    Draft,
    ReviewRequest,
    RecommendationDraftPatch,
    RecommendationDraftAction,
    RecommendationDraftBulkPreviewRequest,
    RecommendationDraftBulkApproveRequest,
    ManualRecommendationDraftRequest,
    StockLeadReviewRequest,
    ReviewAgentSettingsRequest,
    EventUpdateRequest,
    EventAmendmentRequest,
    InstrumentCreate,
    InstrumentPatch,
    MarketSyncRequest,
    FreeStockDBUpdateRequest,
)
