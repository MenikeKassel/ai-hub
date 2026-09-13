from __future__ import annotations
import csv
import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from filelock import FileLock, Timeout as FileLockTimeout
from kol_tracker import SHANGHAI, now_iso
from runtime_jobs import initialize_schema, owned_worker, worker_active
from .core import FetchSummary, ModelWorkerBusyError, PROVIDER_PRIORITY, PostRecord, ProviderAttempt, RuleResult, SEED_KOLS, _json, _loads, _normalise_attributed_name, _zhihu_profile_handle


class KolPostStore:
    def __init__(self, path: Path, media_root: Path):
        self.path = Path(path)
        self.media_root = Path(media_root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        initialize_schema(self.path, self._migrate)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'X',
                    handle TEXT NOT NULL COLLATE NOCASE,
                    profile_url TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
                    tracking_mode TEXT NOT NULL DEFAULT 'all',
                    last_post_id TEXT NOT NULL DEFAULT '',
                    last_fetched_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_gap_at TEXT NOT NULL DEFAULT '',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    backfill_requested INTEGER NOT NULL DEFAULT 0,
                    backfill_status TEXT NOT NULL DEFAULT 'idle',
                    backfill_completed_depth INTEGER NOT NULL DEFAULT 0,
                    backfill_result_count INTEGER NOT NULL DEFAULT 0,
                    backfill_warning TEXT NOT NULL DEFAULT '',
                    fetch_status TEXT NOT NULL DEFAULT 'never',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform,handle)
                );
                CREATE TABLE IF NOT EXISTS kol_account_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kol_id INTEGER NOT NULL REFERENCES kols(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'system',
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kol_account_status_history
                    ON kol_account_status_history(kol_id, observed_at DESC);
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL DEFAULT '',
                    article_title TEXT NOT NULL DEFAULT '',
                    article_text TEXT NOT NULL DEFAULT '',
                    quoted_id TEXT NOT NULL DEFAULT '',
                    quoted_text TEXT NOT NULL DEFAULT '',
                    quoted_author TEXT NOT NULL DEFAULT '',
                    reply_to_id TEXT NOT NULL DEFAULT '',
                    reply_to_author TEXT NOT NULL DEFAULT '',
                    posted_at TEXT NOT NULL,
                    posted_at_utc TEXT NOT NULL,
                    post_type TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    media_json TEXT NOT NULL DEFAULT '[]',
                    local_media_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    raw_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    canonical_provider TEXT NOT NULL DEFAULT 'twitter-cli-legacy',
                    metrics_provider TEXT NOT NULL DEFAULT 'twitter-cli-legacy',
                    provider_warning TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(review_status IN ('pending','approved','excluded','ignored','capture_failed')),
                    review_note TEXT NOT NULL DEFAULT '',
                    source_note TEXT NOT NULL DEFAULT '',
                    notion_url TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_posts_kol_date ON posts(kol_id, posted_at DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_review ON posts(review_status, posted_at DESC);
                CREATE TABLE IF NOT EXISTS digest_attributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    attributed_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    section_text TEXT NOT NULL DEFAULT '',
                    symbols_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'secondhand_aggregation',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(source_post_id,normalized_name)
                );
                CREATE INDEX IF NOT EXISTS idx_digest_attributions_name
                    ON digest_attributions(normalized_name,last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS digest_author_profiles (
                    normalized_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    handle TEXT NOT NULL UNIQUE,
                    profile_url TEXT NOT NULL UNIQUE,
                    tracking_status TEXT NOT NULL DEFAULT 'linked_only'
                        CHECK(tracking_status IN ('linked_only','paused','active')),
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS classifications (
                    post_id TEXT PRIMARY KEY REFERENCES posts(post_id),
                    rule_score INTEGER NOT NULL DEFAULT 0,
                    rule_reasons_json TEXT NOT NULL DEFAULT '[]',
                    rule_symbols_json TEXT NOT NULL DEFAULT '[]',
                    rule_direction TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT 'other',
                    evidence_type TEXT NOT NULL DEFAULT 'ambiguous',
                    is_candidate INTEGER NOT NULL DEFAULT 0,
                    model_status TEXT NOT NULL DEFAULT 'not_requested',
                    model_attempts INTEGER NOT NULL DEFAULT 0,
                    model_next_retry_at TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    model_summary TEXT NOT NULL DEFAULT '',
                    drafts_json TEXT NOT NULL DEFAULT '[]',
                    draft_generation_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(draft_generation_status IN ('pending','generated','not_applicable','needs_attention','failed')),
                    draft_generation_version TEXT NOT NULL DEFAULT '',
                    draft_generation_error TEXT NOT NULL DEFAULT '',
                    draft_generated_at TEXT NOT NULL DEFAULT '',
                    model_error TEXT NOT NULL DEFAULT '',
                    ocr_status TEXT NOT NULL DEFAULT 'not_needed',
                    ocr_attempts INTEGER NOT NULL DEFAULT 0,
                    ocr_next_retry_at TEXT NOT NULL DEFAULT '',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    ocr_provider TEXT NOT NULL DEFAULT '',
                    ocr_confidence REAL NOT NULL DEFAULT 0,
                    ocr_details_json TEXT NOT NULL DEFAULT '[]',
                    ocr_error TEXT NOT NULL DEFAULT '',
                    ocr_updated_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id),
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fetch_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    requested_count INTEGER NOT NULL,
                    total_kols INTEGER NOT NULL DEFAULT 0,
                    processed_kols INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    successful_kols INTEGER NOT NULL DEFAULT 0,
                    failed_kols INTEGER NOT NULL DEFAULT 0,
                    new_posts INTEGER NOT NULL DEFAULT 0,
                    candidate_posts INTEGER NOT NULL DEFAULT 0,
                    auth_status TEXT NOT NULL DEFAULT 'unknown',
                    codex_completed INTEGER NOT NULL DEFAULT 0,
                    codex_failed INTEGER NOT NULL DEFAULT 0,
                    gap_kols_json TEXT NOT NULL DEFAULT '[]',
                    fallback_kols_json TEXT NOT NULL DEFAULT '[]',
                    shadow_failed_kols_json TEXT NOT NULL DEFAULT '[]',
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    platform_breakdown_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS fetch_queue (
                    batch_key TEXT NOT NULL,
                    kol_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    requested_count INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued'
                        CHECK(state IN ('queued','running','cooldown','completed')),
                    not_before TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_key,kol_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_queue_pending
                    ON fetch_queue(batch_key,state,not_before,position);
                CREATE TABLE IF NOT EXISTS post_recovery_queue (
                    post_id TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    url TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued'
                        CHECK(state IN ('queued','running','cooldown','hydrated','terminal')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    queued_at TEXT NOT NULL,
                    hydrated_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_post_recovery_pending
                    ON post_recovery_queue(platform,state,next_attempt_at,updated_at);
                CREATE TABLE IF NOT EXISTS post_recovery_runs (
                    run_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queued INTEGER NOT NULL DEFAULT 0,
                    hydrated INTEGER NOT NULL DEFAULT 0,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    pending INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS x_session_slots (
                    slot_id INTEGER PRIMARY KEY CHECK(slot_id BETWEEN 1 AND 3),
                    label TEXT NOT NULL DEFAULT '',
                    credential_service TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL DEFAULT '',
                    screen_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending_verification'
                        CHECK(status IN ('pending_verification','ready','cooldown','auth_required','disabled')),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_verified_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    cooldown_until TEXT NOT NULL DEFAULT '',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    consecutive_rate_limits INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_collection_policy (
                    policy_id INTEGER PRIMARY KEY CHECK(policy_id=1),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    global_limit_24h INTEGER NOT NULL DEFAULT 180,
                    session_limit_24h INTEGER NOT NULL DEFAULT 90,
                    min_interval_seconds INTEGER NOT NULL DEFAULT 60,
                    public_enabled INTEGER NOT NULL DEFAULT 1,
                    public_limit_24h INTEGER NOT NULL DEFAULT 30,
                    next_slot_id INTEGER NOT NULL DEFAULT 1 CHECK(next_slot_id BETWEEN 1 AND 3),
                    paused_until TEXT NOT NULL DEFAULT '',
                    pause_reason TEXT NOT NULL DEFAULT '',
                    public_paused_until TEXT NOT NULL DEFAULT '',
                    public_pause_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_request_budget (
                    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id INTEGER REFERENCES x_session_slots(slot_id),
                    source TEXT NOT NULL DEFAULT 'primary'
                        CHECK(source IN ('primary','public')),
                    batch_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    estimated_requests INTEGER NOT NULL DEFAULT 1,
                    reserved_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'reserved'
                        CHECK(status IN ('reserved','completed','failed','cancelled')),
                    error_code TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_x_request_budget_time
                    ON x_request_budget(reserved_at,slot_id,status);
                CREATE TABLE IF NOT EXISTS x_batch_leases (
                    batch_key TEXT PRIMARY KEY,
                    slot_id INTEGER NOT NULL REFERENCES x_session_slots(slot_id),
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','completed','paused','failed')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    pause_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_page_checkpoints (
                    kol_id INTEGER PRIMARY KEY REFERENCES kols(id) ON DELETE CASCADE,
                    slot_id INTEGER NOT NULL REFERENCES x_session_slots(slot_id),
                    user_id TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL DEFAULT 'freshness'
                        CHECK(phase IN ('freshness','history','completed','paused')),
                    latest_seen_post_id TEXT NOT NULL DEFAULT '',
                    contiguous_post_id TEXT NOT NULL DEFAULT '',
                    cursor TEXT NOT NULL DEFAULT '',
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    last_page_new_ids INTEGER NOT NULL DEFAULT 0,
                    stop_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fetch_batches (
                    batch_id TEXT PRIMARY KEY,
                    batch_kind TEXT NOT NULL CHECK(batch_kind IN ('freshness','recent_recovery','historical_recovery')),
                    platform TEXT NOT NULL,
                    window_start TEXT NOT NULL DEFAULT '',
                    window_end TEXT NOT NULL DEFAULT '',
                    strategy_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    total_kols INTEGER NOT NULL DEFAULT 0,
                    completed_kols INTEGER NOT NULL DEFAULT 0,
                    successful_kols INTEGER NOT NULL DEFAULT 0,
                    failed_kols INTEGER NOT NULL DEFAULT 0,
                    new_posts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_batches_status
                    ON fetch_batches(platform,status,updated_at DESC);
                CREATE TABLE IF NOT EXISTS fetch_batch_archive (
                    batch_key TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    archived_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kol_id INTEGER NOT NULL REFERENCES kols(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    last_post_id TEXT NOT NULL DEFAULT '',
                    recovery_depth INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    error TEXT NOT NULL DEFAULT '',
                    last_verified_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kol_id,window_start,window_end)
                );
                CREATE INDEX IF NOT EXISTS idx_collection_gaps_open
                    ON collection_gaps(platform,status,updated_at);
                CREATE TABLE IF NOT EXISTS post_sources (
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(post_id, provider)
                );
                CREATE TABLE IF NOT EXISTS post_content_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_post_content_revisions_post
                    ON post_content_revisions(post_id,id DESC);
                CREATE TABLE IF NOT EXISTS fetch_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES fetch_runs(run_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    handle TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    post_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_comparisons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES fetch_runs(run_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    handle TEXT NOT NULL,
                    primary_count INTEGER NOT NULL,
                    fallback_count INTEGER NOT NULL,
                    matching_count INTEGER NOT NULL,
                    coverage REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id,kol_id)
                );
                CREATE TABLE IF NOT EXISTS stock_leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    kol_id INTEGER NOT NULL REFERENCES kols(id),
                    symbol TEXT NOT NULL,
                    security_name TEXT NOT NULL DEFAULT '',
                    instrument_type TEXT NOT NULL DEFAULT 'stock',
                    direction TEXT NOT NULL DEFAULT '',
                    mention_kind TEXT NOT NULL DEFAULT 'analysis',
                    evidence_text TEXT NOT NULL DEFAULT '',
                    extraction_method TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','confirmed','ignored')),
                    auto_confirmed INTEGER NOT NULL DEFAULT 0,
                    review_note TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(post_id,symbol)
                );
                CREATE TABLE IF NOT EXISTS stock_lead_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL REFERENCES stock_leads(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lead_extractions (
                    post_id TEXT PRIMARY KEY REFERENCES posts(post_id) ON DELETE CASCADE,
                    source_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    extracted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_agent_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    mode TEXT NOT NULL DEFAULT 'shadow' CHECK(mode IN ('shadow','enabled')),
                    policy_version TEXT NOT NULL DEFAULT 'review-agent-v1',
                    shadow_started_at TEXT NOT NULL DEFAULT '',
                    enabled_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_agent_runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    auto_approved INTEGER NOT NULL DEFAULT 0,
                    auto_excluded INTEGER NOT NULL DEFAULT 0,
                    auto_ignored INTEGER NOT NULL DEFAULT 0,
                    needs_human INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS review_agent_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    input_signature TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN (
                        'auto_approve','auto_exclude','auto_ignore','needs_human','failed'
                    )),
                    confidence REAL NOT NULL DEFAULT 0,
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    drafts_json TEXT NOT NULL DEFAULT '[]',
                    validator_errors_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN (
                        'proposed','applied','overridden','rolled_back','failed'
                    )),
                    event_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_event_ids_json TEXT NOT NULL DEFAULT '[]',
                    side_effects_json TEXT NOT NULL DEFAULT '{}',
                    model_name TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT '',
                    overridden_at TEXT NOT NULL DEFAULT '',
                    rolled_back_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(post_id,input_signature,policy_version,mode)
                );
                CREATE TABLE IF NOT EXISTS recommendation_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    security_name TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT 'watch',
                    horizon TEXT NOT NULL DEFAULT 'unspecified',
                    strength TEXT NOT NULL DEFAULT 'unspecified',
                    thesis TEXT NOT NULL DEFAULT '',
                    evidence_type TEXT NOT NULL DEFAULT 'ambiguous',
                    evidence_spans_json TEXT NOT NULL DEFAULT '[]',
                    evidence_source TEXT NOT NULL DEFAULT 'text',
                    depends_on_ocr INTEGER NOT NULL DEFAULT 0,
                    conditions_json TEXT NOT NULL DEFAULT '[]',
                    mention_kind TEXT NOT NULL DEFAULT 'recommendation',
                    confidence REAL NOT NULL DEFAULT 0,
                    model_name TEXT NOT NULL DEFAULT '',
                    extraction_version TEXT NOT NULL,
                    source_signature TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN (
                        'ready','needs_attention','approved','rejected','superseded'
                    )),
                    attention_reasons_json TEXT NOT NULL DEFAULT '[]',
                    queue_scope TEXT NOT NULL DEFAULT 'morning' CHECK(queue_scope IN ('morning','backlog')),
                    review_date TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(post_id,symbol,extraction_version)
                );
                CREATE TABLE IF NOT EXISTS recommendation_draft_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES recommendation_drafts(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id INTEGER NOT NULL REFERENCES recommendation_drafts(id) ON DELETE CASCADE,
                    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
                    correction_type TEXT NOT NULL,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT 'human',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendation_bulk_snapshots (
                    token TEXT PRIMARY KEY,
                    review_date TEXT NOT NULL,
                    queue_scope TEXT NOT NULL,
                    status_filter TEXT NOT NULL,
                    draft_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS morning_runs (
                    run_id TEXT PRIMARY KEY,
                    review_date TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    fetched_posts INTEGER NOT NULL DEFAULT 0,
                    reviewed_posts INTEGER NOT NULL DEFAULT 0,
                    ready_drafts INTEGER NOT NULL DEFAULT 0,
                    attention_drafts INTEGER NOT NULL DEFAULT 0,
                    failed_posts INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'legacy',
                    active_kols INTEGER NOT NULL DEFAULT 0,
                    successful_kols INTEGER NOT NULL DEFAULT 0,
                    failed_kols INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    platform_breakdown_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_attempts_run ON fetch_attempts(run_id,id);
                CREATE INDEX IF NOT EXISTS idx_provider_comparisons_run ON provider_comparisons(run_id,id);
                CREATE INDEX IF NOT EXISTS idx_stock_leads_status ON stock_leads(status,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_stock_leads_symbol ON stock_leads(symbol,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_review_agent_decisions_post
                    ON review_agent_decisions(post_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_review_agent_decisions_queue
                    ON review_agent_decisions(decision,status,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_recommendation_drafts_queue
                    ON recommendation_drafts(review_date,queue_scope,status,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_recommendation_drafts_post
                    ON recommendation_drafts(post_id,status,id);
                CREATE INDEX IF NOT EXISTS idx_draft_revisions_draft
                    ON draft_revisions(draft_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_posts_review_posted
                    ON posts(review_status,posted_at_utc DESC,post_id);
                CREATE INDEX IF NOT EXISTS idx_classifications_model_queue
                    ON classifications(is_candidate,model_status,model_next_retry_at,updated_at);
                CREATE INDEX IF NOT EXISTS idx_fetch_queue_batch_state
                    ON fetch_queue(batch_key,state,position,kol_id);
                CREATE INDEX IF NOT EXISTS idx_kols_platform_runtime
                    ON kols(platform,status,updated_at DESC);
                """
            )
            table_sql = str(
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='kols'"
                ).fetchone()["sql"]
            )
            if "handle TEXT NOT NULL COLLATE NOCASE UNIQUE" in table_sql:
                db.commit()
                db.execute("PRAGMA foreign_keys=OFF")
                db.executescript(
                    """
                    ALTER TABLE kols RENAME TO kols_legacy_unique_handle;
                    CREATE TABLE kols (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        display_name TEXT NOT NULL,
                        platform TEXT NOT NULL DEFAULT 'X',
                        handle TEXT NOT NULL COLLATE NOCASE,
                        profile_url TEXT NOT NULL,
                        domain TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
                        tracking_mode TEXT NOT NULL DEFAULT 'all',
                        last_post_id TEXT NOT NULL DEFAULT '',
                        last_fetched_at TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        last_gap_at TEXT NOT NULL DEFAULT '',
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        backfill_requested INTEGER NOT NULL DEFAULT 0,
                        backfill_status TEXT NOT NULL DEFAULT 'idle',
                        backfill_completed_depth INTEGER NOT NULL DEFAULT 0,
                        backfill_result_count INTEGER NOT NULL DEFAULT 0,
                        backfill_warning TEXT NOT NULL DEFAULT '',
                        fetch_status TEXT NOT NULL DEFAULT 'never',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform,handle)
                    );
                    INSERT INTO kols(
                        id,display_name,platform,handle,profile_url,domain,status,tracking_mode,
                        last_post_id,last_fetched_at,last_success_at,last_gap_at,consecutive_failures,
                        backfill_requested,backfill_status,backfill_completed_depth,backfill_result_count,
                        backfill_warning,fetch_status,created_at,updated_at
                    )
                    SELECT
                        id,display_name,platform,handle,profile_url,domain,status,tracking_mode,
                        last_post_id,last_fetched_at,last_success_at,last_gap_at,consecutive_failures,
                        backfill_requested,backfill_status,backfill_completed_depth,backfill_result_count,
                        backfill_warning,fetch_status,created_at,updated_at
                    FROM kols_legacy_unique_handle;
                    DROP TABLE kols_legacy_unique_handle;
                    """
                )
                db.execute("PRAGMA foreign_keys=ON")
            legacy_fk_needed = False
            for table in ("posts", "fetch_attempts", "provider_comparisons", "stock_leads"):
                for row in db.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                    if str(row["table"]) == "kols_legacy_unique_handle":
                        legacy_fk_needed = True
            if legacy_fk_needed:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS kols_legacy_unique_handle (
                        id INTEGER PRIMARY KEY
                    );
                    INSERT OR IGNORE INTO kols_legacy_unique_handle(id)
                    SELECT id FROM kols;
                    CREATE TRIGGER IF NOT EXISTS trg_kols_legacy_insert
                    AFTER INSERT ON kols
                    BEGIN
                        INSERT OR IGNORE INTO kols_legacy_unique_handle(id) VALUES (NEW.id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_kols_legacy_delete
                    AFTER DELETE ON kols
                    BEGIN
                        DELETE FROM kols_legacy_unique_handle WHERE id=OLD.id;
                    END;
                    """
                )
            migrations = {
                "posts": {
                    "reply_to_id": "TEXT NOT NULL DEFAULT ''",
                    "reply_to_author": "TEXT NOT NULL DEFAULT ''",
                    "canonical_provider": "TEXT NOT NULL DEFAULT 'twitter-cli-legacy'",
                    "metrics_provider": "TEXT NOT NULL DEFAULT 'twitter-cli-legacy'",
                    "provider_warning": "TEXT NOT NULL DEFAULT ''",
                },
                "fetch_runs": {
                    "auth_status": "TEXT NOT NULL DEFAULT 'unknown'",
                    "codex_completed": "INTEGER NOT NULL DEFAULT 0",
                    "codex_failed": "INTEGER NOT NULL DEFAULT 0",
                    "fallback_kols_json": "TEXT NOT NULL DEFAULT '[]'",
                    "shadow_failed_kols_json": "TEXT NOT NULL DEFAULT '[]'",
                    "total_kols": "INTEGER NOT NULL DEFAULT 0",
                    "processed_kols": "INTEGER NOT NULL DEFAULT 0",
                    "stage": "TEXT NOT NULL DEFAULT 'queued'",
                    "platform_breakdown_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "kols": {
                    "last_success_at": "TEXT NOT NULL DEFAULT ''",
                    "last_gap_at": "TEXT NOT NULL DEFAULT ''",
                    "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_requested": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_status": "TEXT NOT NULL DEFAULT 'idle'",
                    "backfill_completed_depth": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_result_count": "INTEGER NOT NULL DEFAULT 0",
                    "backfill_warning": "TEXT NOT NULL DEFAULT ''",
                    "external_account_id": "TEXT NOT NULL DEFAULT ''",
                    "availability_status": "TEXT NOT NULL DEFAULT 'active'",
                    "availability_reason": "TEXT NOT NULL DEFAULT ''",
                    "availability_checked_at": "TEXT NOT NULL DEFAULT ''",
                },
                "classifications": {
                    "model_summary": "TEXT NOT NULL DEFAULT ''",
                    "model_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "model_next_retry_at": "TEXT NOT NULL DEFAULT ''",
                    "draft_generation_status": "TEXT NOT NULL DEFAULT 'pending'",
                    "draft_generation_version": "TEXT NOT NULL DEFAULT ''",
                    "draft_generation_error": "TEXT NOT NULL DEFAULT ''",
                    "draft_generated_at": "TEXT NOT NULL DEFAULT ''",
                    "ocr_status": "TEXT NOT NULL DEFAULT 'not_needed'",
                    "ocr_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "ocr_next_retry_at": "TEXT NOT NULL DEFAULT ''",
                    "ocr_text": "TEXT NOT NULL DEFAULT ''",
                    "ocr_provider": "TEXT NOT NULL DEFAULT ''",
                    "ocr_confidence": "REAL NOT NULL DEFAULT 0",
                    "ocr_details_json": "TEXT NOT NULL DEFAULT '[]'",
                    "ocr_error": "TEXT NOT NULL DEFAULT ''",
                    "ocr_updated_at": "TEXT NOT NULL DEFAULT ''",
                },
                "review_agent_decisions": {
                    "created_event_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                    "side_effects_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "recommendation_drafts": {
                    "evidence_source": "TEXT NOT NULL DEFAULT 'text'",
                    "depends_on_ocr": "INTEGER NOT NULL DEFAULT 0",
                    "action": "TEXT NOT NULL DEFAULT 'watch'",
                    "horizon": "TEXT NOT NULL DEFAULT 'unspecified'",
                    "strength": "TEXT NOT NULL DEFAULT 'unspecified'",
                },
                "morning_runs": {
                    "phase": "TEXT NOT NULL DEFAULT 'legacy'",
                    "active_kols": "INTEGER NOT NULL DEFAULT 0",
                    "successful_kols": "INTEGER NOT NULL DEFAULT 0",
                    "failed_kols": "INTEGER NOT NULL DEFAULT 0",
                    "stage": "TEXT NOT NULL DEFAULT 'queued'",
                    "progress_current": "INTEGER NOT NULL DEFAULT 0",
                    "progress_total": "INTEGER NOT NULL DEFAULT 0",
                    "platform_breakdown_json": "TEXT NOT NULL DEFAULT '{}'",
                },
                "x_page_checkpoints": {
                    "user_id": "TEXT NOT NULL DEFAULT ''",
                },
                "x_collection_policy": {
                    "public_enabled": "INTEGER NOT NULL DEFAULT 1",
                    "public_limit_24h": "INTEGER NOT NULL DEFAULT 30",
                    "public_paused_until": "TEXT NOT NULL DEFAULT ''",
                    "public_pause_reason": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table, columns in migrations.items():
                existing_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                for column, definition in columns.items():
                    if column not in existing_columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            budget_columns = db.execute("PRAGMA table_info(x_request_budget)").fetchall()
            budget_names = {row[1] for row in budget_columns}
            budget_slot_required = any(row[1] == "slot_id" and int(row[3]) == 1 for row in budget_columns)
            if "source" not in budget_names or budget_slot_required:
                # Early builds made slot_id mandatory. Rebuild this small
                # append-only audit table so public (cookie-free) requests can
                # use a NULL slot without inventing a personal account.
                db.execute("PRAGMA foreign_keys=OFF")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS x_request_budget_v2 (
                        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        slot_id INTEGER REFERENCES x_session_slots(slot_id),
                        source TEXT NOT NULL DEFAULT 'primary' CHECK(source IN ('primary','public')),
                        batch_key TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        estimated_requests INTEGER NOT NULL DEFAULT 1,
                        reserved_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'reserved' CHECK(status IN ('reserved','completed','failed','cancelled')),
                        error_code TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO x_request_budget_v2(request_id,slot_id,source,batch_key,operation,estimated_requests,reserved_at,completed_at,status,error_code,error)
                    SELECT request_id,slot_id,'primary',batch_key,operation,estimated_requests,reserved_at,completed_at,status,error_code,error
                    FROM x_request_budget;
                    DROP TABLE x_request_budget;
                    ALTER TABLE x_request_budget_v2 RENAME TO x_request_budget;
                    CREATE INDEX IF NOT EXISTS idx_x_request_budget_time
                        ON x_request_budget(reserved_at,slot_id,source,status);
                    """
                )
                db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                """
                INSERT OR IGNORE INTO post_sources(
                    post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json
                )
                SELECT post_id,'twitter-cli-legacy',fetched_at,content_hash,raw_json,metrics_json,'[]'
                FROM posts
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO review_agent_settings(
                    id,mode,policy_version,shadow_started_at,updated_at
                ) VALUES(1,'shadow','review-agent-v1',?,?)
                """,
                (now_iso(), now_iso()),
            )
            if db.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
                db.execute("INSERT INTO schema_meta(version) VALUES (16)")
            else:
                db.execute("UPDATE schema_meta SET version=18")
            db.execute(
                """
                UPDATE classifications
                SET draft_generation_status=CASE
                    WHEN EXISTS(
                        SELECT 1 FROM recommendation_drafts d
                        WHERE d.post_id=classifications.post_id
                          AND d.status IN ('ready','needs_attention','approved','rejected')
                    ) THEN 'generated'
                    WHEN model_status='failed' THEN 'failed'
                    WHEN model_status='completed' AND content_type<>'recommendation' THEN 'not_applicable'
                    ELSE 'pending'
                END,
                draft_generation_error=CASE WHEN model_status='failed' THEN model_error ELSE '' END,
                draft_generation_version=CASE WHEN prompt_version<>'' THEN prompt_version ELSE 'pending-migration' END
                WHERE draft_generation_version=''
                """
            )
            db.execute(
                """
                UPDATE classifications SET ocr_status=CASE
                    WHEN EXISTS(
                        SELECT 1 FROM posts p WHERE p.post_id=classifications.post_id
                        AND p.local_media_json NOT IN ('','[]')
                        AND p.review_status='pending'
                    ) THEN CASE WHEN ocr_status='completed' THEN ocr_status ELSE 'not_requested' END
                    ELSE 'not_needed' END
                WHERE ocr_status IN ('','not_needed','not_requested')
                """
            )
            run_level_warnings = {
                "shadow_coverage_below_threshold",
                "shadow_fallback_failed",
                "shadow_compared",
                "fallback_used",
            }
            for row in db.execute(
                "SELECT post_id,canonical_provider,provider_warning FROM posts WHERE provider_warning<>''"
            ).fetchall():
                warnings = [
                    warning
                    for warning in str(row["provider_warning"] or "").split(";")
                    if warning and warning not in run_level_warnings
                ]
                if str(row["canonical_provider"] or "") != "nitter":
                    warnings = [warning for warning in warnings if warning != "timestamp_recovered_from_snowflake"]
                cleaned = ";".join(dict.fromkeys(warnings))
                if cleaned != row["provider_warning"]:
                    db.execute("UPDATE posts SET provider_warning=? WHERE post_id=?", (cleaned, row["post_id"]))
            db.execute("PRAGMA user_version=21")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for source, target, default in [
            ("media_json", "media", []),
            ("local_media_json", "local_media", []),
            ("metrics_json", "metrics", {}),
            ("rule_reasons_json", "rule_reasons", []),
            ("rule_symbols_json", "rule_symbols", []),
            ("drafts_json", "drafts", []),
            ("ocr_details_json", "ocr_details", []),
            ("gap_kols_json", "gap_kols", []),
            ("fallback_kols_json", "fallback_kols", []),
            ("shadow_failed_kols_json", "shadow_failed_kols", []),
            ("platform_breakdown_json", "platform_breakdown", {}),
            ("errors_json", "errors", []),
            ("warnings_json", "warnings", []),
        ]:
            if source in value:
                value[target] = _loads(value.pop(source), default)
        return value

    def add_kol(
        self,
        display_name: str,
        handle: str,
        domain: str = "",
        status: str = "active",
        tracking_mode: str = "all",
        platform: str = "X",
        profile_url: str = "",
    ) -> tuple[int, bool]:
        clean_handle = handle.strip().lstrip("@")
        clean_platform = platform.strip().title()
        if clean_platform == "X":
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", clean_handle):
                raise ValueError("invalid X handle")
            canonical_profile = profile_url.strip() or f"https://x.com/{clean_handle}"
        elif clean_platform == "Zhihu":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", clean_handle):
                raise ValueError("invalid Zhihu url_token")
            canonical_profile = profile_url.strip() or f"https://www.zhihu.com/people/{clean_handle}"
        else:
            raise ValueError("unsupported KOL platform")
        if status not in {"active", "paused"}:
            raise ValueError("invalid KOL status")
        timestamp = now_iso()
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM kols WHERE platform=? AND handle=? COLLATE NOCASE",
                (clean_platform, clean_handle),
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = db.execute(
                """
                INSERT INTO kols(
                    display_name,platform,handle,profile_url,domain,status,tracking_mode,
                    external_account_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    display_name.strip() or clean_handle,
                    clean_platform,
                    clean_handle,
                    canonical_profile,
                    domain.strip(),
                    status,
                    tracking_mode,
                    clean_handle,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid), True

    def update_kol(self, kol_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"display_name", "domain", "status", "tracking_mode"}
        updates = {key: str(value).strip() for key, value in changes.items() if key in allowed}
        if not updates:
            raise ValueError("no editable KOL fields supplied")
        if "status" in updates and updates["status"] not in {"active", "paused"}:
            raise ValueError("invalid KOL status")
        updates["updated_at"] = now_iso()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE kols SET {assignments} WHERE id=?",  # noqa: S608 - field names are allowlisted.
                (*updates.values(), kol_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"KOL not found: {kol_id}")
        result = self.get_kol(kol_id)
        assert result is not None
        return result

    def set_account_availability(
        self,
        kol_id: int,
        status: str,
        *,
        reason: str = "",
        source: str = "system",
    ) -> dict[str, Any]:
        allowed = {
            "active", "rate_limited", "provider_failed", "protected",
            "suspected_unavailable", "suspended", "deleted", "renamed", "paused",
        }
        if status not in allowed:
            raise ValueError(f"invalid account availability status: {status}")
        timestamp = now_iso()
        with self.connect() as db:
            previous = db.execute(
                "SELECT availability_status,availability_reason FROM kols WHERE id=?",
                (kol_id,),
            ).fetchone()
            cursor = db.execute(
                """
                UPDATE kols SET availability_status=?,availability_reason=?,
                    availability_checked_at=?,updated_at=? WHERE id=?
                """,
                (status, reason[:2000], timestamp, timestamp, kol_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"KOL not found: {kol_id}")
            if previous is None or str(previous["availability_status"] or "active") != status or str(previous["availability_reason"] or "") != reason[:2000]:
                db.execute(
                    """
                    INSERT INTO kol_account_status_history(kol_id,status,reason,source,observed_at)
                    VALUES(?,?,?,?,?)
                    """,
                    (kol_id, status, reason[:2000], source[:100], timestamp),
                )
        result = self.get_kol(kol_id)
        assert result is not None
        return result

    def account_availability_history(self, kol_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM kol_account_status_history WHERE kol_id=? ORDER BY observed_at DESC LIMIT ?",
                (kol_id, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_kols(self, status: str | None = None, platform: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM kols"
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if platform:
            conditions.append("lower(platform)=?")
            params.append(platform.casefold())
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY display_name COLLATE NOCASE"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, tuple(params)).fetchall()]

    def get_kol(self, kol_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM kols WHERE id=?", (kol_id,)).fetchone()
        return self._row(row)

    def get_kol_by_handle(self, handle: str, platform: str | None = None) -> dict[str, Any] | None:
        with self.connect() as db:
            clean_handle = handle.strip().lstrip("@")
            if platform:
                row = db.execute(
                    "SELECT * FROM kols WHERE platform=? AND handle=? COLLATE NOCASE",
                    (platform.strip().title(), clean_handle),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM kols WHERE handle=? COLLATE NOCASE ORDER BY platform='X' DESC LIMIT 1",
                    (clean_handle,),
                ).fetchone()
        return self._row(row)

    @contextmanager
    def model_worker(self, timeout: float = 0):
        lock = FileLock(str(self.path.with_suffix(".model-worker.lock")))
        try:
            lock.acquire(timeout=timeout)
        except FileLockTimeout as exc:
            raise ModelWorkerBusyError("another Codex classifier is already running") from exc
        try:
            yield
        finally:
            lock.release()

    def queue_backfill(self, kol_id: int, count: int) -> dict[str, Any]:
        if count < 1:
            raise ValueError("backfill count must be positive")
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE kols
                SET backfill_requested=?,backfill_status='queued',backfill_completed_depth=0,
                    backfill_result_count=0,backfill_warning='',updated_at=?
                WHERE id=?
                """,
                (int(count), timestamp, kol_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"KOL not found: {kol_id}")
        result = self.get_kol(kol_id)
        assert result is not None
        return result

    def update_fetch_state(
        self,
        kol_id: int,
        post_id: str,
        status: str,
        *,
        fetched_count: int = 0,
        requested_count: int = 0,
    ) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT last_post_id,last_success_at,consecutive_failures,
                    backfill_requested,backfill_status,backfill_completed_depth,
                    backfill_result_count,backfill_warning
                FROM kols WHERE id=?
                """,
                (kol_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"KOL not found: {kol_id}")
            backfill_requested = int(row["backfill_requested"] or 0)
            backfill_status = str(row["backfill_status"] or "idle")
            backfill_completed_depth = int(row["backfill_completed_depth"] or 0)
            backfill_result_count = int(row["backfill_result_count"] or 0)
            backfill_warning = str(row["backfill_warning"] or "")
            if backfill_status == "queued" and backfill_requested > 0:
                backfill_result_count = max(0, fetched_count)
                if status == "success" and fetched_count >= max(1, requested_count):
                    backfill_completed_depth = max(
                        backfill_completed_depth,
                        min(requested_count, backfill_requested),
                    )
                    if backfill_completed_depth >= backfill_requested:
                        backfill_requested = 0
                        backfill_status = "completed"
                elif status == "success":
                    backfill_status = "needs_review"
                    backfill_warning = (
                        f"provider returned {fetched_count} of {requested_count}; "
                        "timeline may be exhausted or provider-limited"
                    )
            clean_success = status == "success"
            non_failure = status in {"success", "gap_detected"}
            stored_post_id = post_id if clean_success else str(row["last_post_id"] or "")
            last_success_at = timestamp if clean_success else str(row["last_success_at"] or "")
            consecutive_failures = (
                0
                if non_failure
                else int(row["consecutive_failures"] or 0) + 1
            )
            db.execute(
                """
                UPDATE kols
                SET last_post_id=?,last_fetched_at=?,last_success_at=?,fetch_status=?,
                    availability_status=CASE
                        WHEN ?='success' AND availability_status IN ('rate_limited','provider_failed')
                        THEN 'active' ELSE availability_status END,
                    availability_reason=CASE
                        WHEN ?='success' AND availability_status IN ('rate_limited','provider_failed')
                        THEN '' ELSE availability_reason END,
                    last_gap_at=CASE WHEN ?='gap_detected' THEN ? ELSE last_gap_at END,
                    consecutive_failures=?,backfill_requested=?,backfill_status=?,
                    backfill_completed_depth=?,backfill_result_count=?,backfill_warning=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    stored_post_id,
                    timestamp,
                    last_success_at,
                    status,
                    status,
                    status,
                    status,
                    timestamp,
                    consecutive_failures,
                    backfill_requested,
                    backfill_status,
                    backfill_completed_depth,
                    backfill_result_count,
                    backfill_warning,
                    timestamp,
                    kol_id,
                ),
            )

    def mark_fetch_failed(self, kol_id: int, status: str = "failed") -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE kols
                SET fetch_status=?,consecutive_failures=consecutive_failures+1,updated_at=?
                WHERE id=?
                """,
                (status, now_iso(), kol_id),
            )

    def upsert_post(self, post: PostRecord, *, run_id: str = "") -> bool:
        timestamp = now_iso()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM posts WHERE post_id=?", (post.post_id,)).fetchone()
            existing_has_content = bool(
                existing is not None
                and (str(existing["text"] or "").strip() or str(existing["article_text"] or "").strip())
            )
            warning = post.provider_warning
            if existing is not None:
                if not existing_has_content and str(existing["content_hash"] or "") != post.content_hash:
                    db.execute(
                        """
                        INSERT INTO post_content_revisions(
                            post_id,provider,fetched_at,previous_hash,content_hash,raw_json,created_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            post.post_id,
                            post.canonical_provider,
                            post.fetched_at,
                            str(existing["content_hash"] or ""),
                            post.content_hash,
                            _json(post.raw_payload),
                            timestamp,
                        ),
                    )
                try:
                    existing_time = datetime.fromisoformat(str(existing["posted_at_utc"]).replace("Z", "+00:00"))
                    incoming_time = datetime.fromisoformat(post.posted_at_utc.replace("Z", "+00:00"))
                    if abs((incoming_time - existing_time).total_seconds()) > 60:
                        warning = "source_conflict"
                except ValueError:
                    warning = "source_conflict"
                if "source_conflict" in str(existing["provider_warning"] or "").split(";"):
                    warning = "source_conflict"

            db.execute(
                """
                INSERT INTO post_sources(
                    post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(post_id,provider) DO UPDATE SET
                    fetched_at=excluded.fetched_at,content_hash=excluded.content_hash,
                    raw_json=excluded.raw_json,metrics_json=excluded.metrics_json,
                    warnings_json=excluded.warnings_json
                """,
                (
                    post.post_id,
                    post.canonical_provider,
                    post.fetched_at,
                    post.content_hash,
                    _json(post.raw_payload),
                    _json(post.metrics),
                    _json([warning] if warning else []),
                ),
            ) if existing is not None else None

            # A complete private/archive row is authoritative.  Bulk account
            # fetches may provide fresher metrics or an alternate raw source,
            # but must never replace its正文、媒体、原始响应、摘要或内容哈希.
            if existing is not None and existing_has_content:
                db.execute(
                    """
                    UPDATE posts SET metrics_json=?,metrics_provider=?,provider_warning=?,fetched_at=?
                    WHERE post_id=?
                    """,
                    (
                        _json(post.metrics),
                        post.metrics_provider,
                        warning or str(existing["provider_warning"] or ""),
                        post.fetched_at,
                        post.post_id,
                    ),
                )
                return False

            if existing is None:
                db.execute(
                    """
                    INSERT INTO posts(
                        post_id,kol_id,platform,handle,author_name,url,text,article_title,article_text,
                        quoted_id,quoted_text,quoted_author,reply_to_id,reply_to_author,
                        posted_at,posted_at_utc,post_type,language,media_json,metrics_json,raw_json,
                        content_hash,fetched_at,canonical_provider,metrics_provider,provider_warning,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        post.post_id, post.kol_id, post.platform, post.handle, post.author_name,
                        post.url, post.text, post.article_title, post.article_text, post.quoted_id,
                        post.quoted_text, post.quoted_author, post.reply_to_id, post.reply_to_author,
                        post.posted_at, post.posted_at_utc, post.post_type, post.language,
                        _json(post.media), _json(post.metrics), _json(post.raw_payload),
                        post.content_hash, post.fetched_at, post.canonical_provider,
                        post.metrics_provider, warning, timestamp,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO post_sources(
                        post_id,provider,fetched_at,content_hash,raw_json,metrics_json,warnings_json
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        post.post_id, post.canonical_provider, post.fetched_at, post.content_hash,
                        _json(post.raw_payload), _json(post.metrics), _json([warning] if warning else []),
                    ),
                )
                return True

            existing_provider = str(existing["canonical_provider"] or "twitter-cli-legacy")
            incoming_priority = PROVIDER_PRIORITY.get(post.canonical_provider, 0)
            existing_priority = PROVIDER_PRIORITY.get(existing_provider, 0)
            if incoming_priority >= existing_priority:
                db.execute(
                    """
                    UPDATE posts SET author_name=?,url=?,text=?,article_title=?,article_text=?,
                        quoted_id=?,quoted_text=?,quoted_author=?,reply_to_id=?,reply_to_author=?,
                        posted_at=?,posted_at_utc=?,post_type=?,language=?,media_json=?,raw_json=?,
                        content_hash=?,canonical_provider=?,metrics_json=?,metrics_provider=?,
                        provider_warning=?,fetched_at=?,updated_at=? WHERE post_id=?
                    """,
                    (
                        post.author_name, post.url, post.text, post.article_title, post.article_text,
                        post.quoted_id, post.quoted_text, post.quoted_author, post.reply_to_id,
                        post.reply_to_author, post.posted_at, post.posted_at_utc, post.post_type,
                        post.language, _json(post.media), _json(post.raw_payload), post.content_hash,
                        post.canonical_provider, _json(post.metrics), post.metrics_provider, warning,
                        post.fetched_at, timestamp, post.post_id,
                    ),
                )
            else:
                db.execute(
                    """
                    UPDATE posts SET metrics_json=?,metrics_provider=?,provider_warning=?,
                        fetched_at=?,updated_at=? WHERE post_id=?
                    """,
                    (
                        _json(post.metrics), post.metrics_provider,
                        warning or str(existing["provider_warning"] or ""),
                        post.fetched_at, timestamp, post.post_id,
                    ),
                )
            return False

    def list_post_sources(self, post_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM post_sources WHERE post_id=? ORDER BY fetched_at DESC",
                (post_id,),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def replace_digest_attributions(
        self,
        post_id: str,
        attributions: list[dict[str, Any]],
    ) -> None:
        timestamp = now_iso()
        normalized_values: set[str] = set()
        with self.connect() as db:
            for attribution in attributions:
                name = re.sub(r"\s+", " ", str(attribution.get("author_name") or "")).strip()
                normalized = _normalise_attributed_name(name)
                if not normalized or normalized in normalized_values:
                    continue
                normalized_values.add(normalized)
                db.execute(
                    """
                    INSERT INTO digest_attributions(
                        source_post_id,attributed_name,normalized_name,section_text,
                        symbols_json,status,first_seen_at,last_seen_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_post_id,normalized_name) DO UPDATE SET
                        attributed_name=excluded.attributed_name,
                        section_text=excluded.section_text,
                        symbols_json=excluded.symbols_json,
                        status=excluded.status,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        post_id,
                        name,
                        normalized,
                        str(attribution.get("section_text") or "")[:20000],
                        _json(list(attribution.get("symbols") or [])),
                        "secondhand_aggregation",
                        timestamp,
                        timestamp,
                    ),
                )
            if normalized_values:
                placeholders = ",".join("?" for _ in normalized_values)
                db.execute(
                    f"DELETE FROM digest_attributions WHERE source_post_id=? "  # noqa: S608 - placeholders only.
                    f"AND normalized_name NOT IN ({placeholders})",
                    (post_id, *sorted(normalized_values)),
                )
            else:
                db.execute("DELETE FROM digest_attributions WHERE source_post_id=?", (post_id,))

    def list_digest_authors(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT d.*,p.url AS source_url,p.posted_at,p.article_title
                FROM digest_attributions d
                JOIN posts p ON p.post_id=d.source_post_id
                ORDER BY p.posted_at DESC,d.id DESC
                """
            ).fetchall()
            profile_rows = db.execute("SELECT * FROM digest_author_profiles").fetchall()
        profiles_by_name = {str(row["normalized_name"]): row for row in profile_rows}
        profiles_by_handle = {
            _normalise_attributed_name(str(row["handle"])): row for row in profile_rows
        }
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw_normalized = str(row["normalized_name"])
            aliases = [raw_normalized]
            for suffix in ("的一周操作总结", "的一周总结"):
                if raw_normalized.endswith(suffix):
                    aliases.append(raw_normalized[: -len(suffix)])
            profile = next(
                (
                    profiles_by_name.get(alias) or profiles_by_handle.get(alias)
                    for alias in aliases
                    if profiles_by_name.get(alias) or profiles_by_handle.get(alias)
                ),
                None,
            )
            normalized = str(profile["normalized_name"]) if profile is not None else raw_normalized
            value = grouped.get(normalized)
            if value is None:
                value = {
                    "author_name": (
                        str(profile["display_name"])
                        if profile is not None
                        else str(row["attributed_name"])
                    ),
                    "normalized_name": normalized,
                    "status": "attributed_only",
                    "source_kind": "secondhand_aggregation",
                    "summary_count": 0,
                    "latest_posted_at": str(row["posted_at"]),
                    "latest_source_url": str(row["source_url"]),
                    "latest_title": str(row["article_title"]),
                    "latest_summary": str(row["section_text"]),
                    "symbols": [],
                    "profile_handle": str(profile["handle"] if profile is not None else ""),
                    "profile_url": str(profile["profile_url"] if profile is not None else ""),
                    "tracking_status": str(
                        profile["tracking_status"] if profile is not None else "unresolved"
                    ),
                    "profile_updated_at": str(
                        profile["updated_at"] if profile is not None else ""
                    ),
                }
                grouped[normalized] = value
            value["summary_count"] += 1
            value["symbols"] = sorted(
                set(value["symbols"]) | set(_loads(str(row["symbols_json"]), []))
            )
        return list(grouped.values())[: max(1, min(int(limit), 500))]

    def upsert_digest_author_profiles(
        self,
        profiles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not profiles:
            raise ValueError("at least one Zhihu author profile is required")
        prepared: list[tuple[str, str, str, str]] = []
        names: set[str] = set()
        handles: set[str] = set()
        for profile in profiles:
            display_name = re.sub(r"\s+", " ", str(profile.get("display_name") or "")).strip()
            if not display_name or len(display_name) > 100:
                raise ValueError("invalid attributed author name")
            normalized = _normalise_attributed_name(display_name)
            profile_url = str(profile.get("profile_url") or "").strip()
            handle = _zhihu_profile_handle(profile_url)
            if normalized in names or handle.casefold() in handles:
                raise ValueError("duplicate attributed author profile in request")
            names.add(normalized)
            handles.add(handle.casefold())
            prepared.append((normalized, display_name, handle, profile_url.rstrip("/")))
        timestamp = now_iso()
        with self.connect() as db:
            for normalized, display_name, handle, profile_url in prepared:
                db.execute(
                    """
                    INSERT INTO digest_author_profiles(
                        normalized_name,display_name,handle,profile_url,
                        tracking_status,source,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(normalized_name) DO UPDATE SET
                        display_name=excluded.display_name,
                        handle=excluded.handle,
                        profile_url=excluded.profile_url,
                        updated_at=excluded.updated_at
                    """,
                    (
                        normalized,
                        display_name,
                        handle,
                        profile_url,
                        "linked_only",
                        "manual",
                        timestamp,
                        timestamp,
                    ),
                )
        by_name = {item["normalized_name"]: item for item in self.list_digest_authors(500)}
        return [by_name[normalized] for normalized, _, _, _ in prepared if normalized in by_name]

    def list_digest_author_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM digest_author_profiles ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_digest_author_profile_status(self, handle: str, status: str) -> None:
        if status not in {"linked_only", "paused", "active"}:
            raise ValueError("invalid digest author profile status")
        with self.connect() as db:
            db.execute(
                """
                UPDATE digest_author_profiles
                SET tracking_status=?,updated_at=?
                WHERE handle=? COLLATE NOCASE
                """,
                (status, now_iso(), handle.strip()),
            )

    def record_fetch_attempts(
        self,
        run_id: str,
        kol_id: int,
        handle: str,
        attempts: list[ProviderAttempt],
    ) -> None:
        if not attempts:
            return
        with self.connect() as db:
            db.executemany(
                """
                INSERT INTO fetch_attempts(
                    run_id,kol_id,handle,provider,status,post_count,duration_ms,
                    error_code,error,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id, kol_id, handle, attempt.provider, attempt.status,
                        attempt.post_count, attempt.duration_ms, attempt.error_code,
                        attempt.error[:1000], now_iso(),
                    )
                    for attempt in attempts
                ],
            )

    def recent_fetch_attempts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM fetch_attempts ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_provider_comparisons(
        self,
        run_id: str,
        kol_id: int,
        comparisons: list[dict[str, Any]],
    ) -> None:
        if not comparisons:
            return
        with self.connect() as db:
            for comparison in comparisons:
                db.execute(
                    """
                    INSERT INTO provider_comparisons(
                        run_id,kol_id,handle,primary_count,fallback_count,
                        matching_count,coverage,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,kol_id) DO UPDATE SET
                        primary_count=excluded.primary_count,
                        fallback_count=excluded.fallback_count,
                        matching_count=excluded.matching_count,
                        coverage=excluded.coverage,
                        created_at=excluded.created_at
                    """,
                    (
                        run_id,
                        kol_id,
                        str(comparison.get("handle") or ""),
                        int(comparison.get("primary_count") or 0),
                        int(comparison.get("fallback_count") or 0),
                        int(comparison.get("matching_count") or 0),
                        float(comparison.get("coverage") or 0),
                        now_iso(),
                    ),
                )

    def shadow_rollout_status(self, required_runs: int = 3, threshold: float = 0.95) -> dict[str, Any]:
        active_ids = [int(item["id"]) for item in self.list_kols("active")]
        active_count = len(active_ids)
        with self.connect() as db:
            candidates = db.execute(
                "SELECT run_id,started_at FROM fetch_runs WHERE status='success' "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (max(required_runs * 10, 30),),
            ).fetchall()
            runs = []
            seen_dates: set[str] = set()
            for run in candidates:
                run_date = str(run["started_at"])[:10]
                if run_date in seen_dates:
                    continue
                seen_dates.add(run_date)
                runs.append(run)
                if len(runs) == required_runs:
                    break
            details = []
            for run in runs:
                placeholders = ",".join("?" for _ in active_ids)
                row = db.execute(
                    "SELECT COUNT(*) count,MIN(coverage) min_coverage,AVG(coverage) avg_coverage "
                    f"FROM provider_comparisons WHERE run_id=? "
                    + (f"AND kol_id IN ({placeholders})" if active_ids else "AND 1=0"),
                    (run["run_id"], *active_ids),
                ).fetchone()
                details.append(
                    {
                        "run_id": run["run_id"],
                        "started_at": run["started_at"],
                        "comparison_count": int(row["count"] or 0),
                        "min_coverage": float(row["min_coverage"] or 0),
                        "avg_coverage": float(row["avg_coverage"] or 0),
                    }
                )
        ready = active_count > 0 and len(details) == required_runs and all(
            item["comparison_count"] >= active_count and item["min_coverage"] >= threshold
            for item in details
        )
        return {
            "ready": ready,
            "required_runs": required_runs,
            "required_distinct_dates": required_runs,
            "threshold": threshold,
            "active_kols": active_count,
            "runs": details,
        }

    def save_local_media(self, post_id: str, local_media: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            row = db.execute("SELECT local_media_json FROM posts WHERE post_id=?", (post_id,)).fetchone()
            if row is None:
                raise KeyError(f"post not found: {post_id}")
            existing = _loads(row["local_media_json"], [])
            merged: dict[str, dict[str, Any]] = {}
            for item in [*existing, *local_media]:
                key = str(item.get("source_url") or item.get("sha256") or item.get("path") or "")
                if key:
                    merged[key] = item
            db.execute(
                "UPDATE posts SET local_media_json=?,updated_at=? WHERE post_id=?",
                (_json(list(merged.values())), now_iso(), post_id),
            )

    def save_rule_classification(self, post_id: str, result: RuleResult) -> None:
        with self.connect() as db:
            media_row = db.execute(
                "SELECT local_media_json FROM posts WHERE post_id=?",
                (post_id,),
            ).fetchone()
            has_local_media = bool(media_row and _loads(media_row["local_media_json"], []))
            ocr_status = "not_requested" if has_local_media and result.is_candidate else "not_needed"
            db.execute(
                """
                INSERT INTO classifications(
                    post_id,rule_score,rule_reasons_json,rule_symbols_json,rule_direction,
                    content_type,evidence_type,is_candidate,ocr_status,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET
                    rule_score=excluded.rule_score,rule_reasons_json=excluded.rule_reasons_json,
                    rule_symbols_json=excluded.rule_symbols_json,rule_direction=excluded.rule_direction,
                    content_type=CASE WHEN classifications.model_status='completed'
                        THEN classifications.content_type ELSE excluded.content_type END,
                    evidence_type=CASE WHEN classifications.model_status='completed'
                        THEN classifications.evidence_type ELSE excluded.evidence_type END,
                    is_candidate=excluded.is_candidate,
                    ocr_status=CASE
                        WHEN classifications.ocr_status='completed' THEN classifications.ocr_status
                        ELSE excluded.ocr_status END,
                    updated_at=excluded.updated_at
                """,
                (
                    post_id,
                    result.score,
                    _json(result.reasons),
                    _json(result.symbols),
                    result.direction,
                    result.content_type,
                    result.evidence_type,
                    int(result.is_candidate),
                    ocr_status,
                    now_iso(),
                ),
            )

    def upsert_stock_lead(self, value: dict[str, Any]) -> bool:
        timestamp = now_iso()
        symbol = str(value.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("invalid stock lead symbol")
        status = str(value.get("status") or "pending")
        if status not in {"pending", "confirmed", "ignored"}:
            raise ValueError("invalid stock lead status")
        with self.connect() as db:
            existing = db.execute(
                "SELECT id,status FROM stock_leads WHERE post_id=? AND symbol=?",
                (value["post_id"], symbol),
            ).fetchone()
            db.execute(
                """
                INSERT INTO stock_leads(
                    post_id,kol_id,symbol,security_name,instrument_type,direction,mention_kind,
                    evidence_text,extraction_method,confidence,status,auto_confirmed,
                    first_seen_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(post_id,symbol) DO UPDATE SET
                    security_name=CASE WHEN excluded.security_name<>'' THEN excluded.security_name ELSE stock_leads.security_name END,
                    instrument_type=excluded.instrument_type,
                    direction=CASE WHEN excluded.direction<>'' THEN excluded.direction ELSE stock_leads.direction END,
                    mention_kind=excluded.mention_kind,
                    evidence_text=CASE WHEN excluded.evidence_text<>'' THEN excluded.evidence_text ELSE stock_leads.evidence_text END,
                    extraction_method=CASE
                        WHEN stock_leads.extraction_method='exact_code' THEN stock_leads.extraction_method
                        WHEN excluded.extraction_method='exact_code' THEN excluded.extraction_method
                        WHEN stock_leads.extraction_method='name_match' THEN stock_leads.extraction_method
                        ELSE excluded.extraction_method END,
                    confidence=MAX(stock_leads.confidence,excluded.confidence),
                    status=CASE
                        WHEN stock_leads.status IN ('confirmed','ignored') THEN stock_leads.status
                        ELSE excluded.status END,
                    auto_confirmed=MAX(stock_leads.auto_confirmed,excluded.auto_confirmed),
                    updated_at=excluded.updated_at
                """,
                (
                    value["post_id"],
                    int(value["kol_id"]),
                    symbol,
                    str(value.get("security_name") or "").strip(),
                    str(value.get("instrument_type") or "stock"),
                    str(value.get("direction") or ""),
                    str(value.get("mention_kind") or "analysis"),
                    str(value.get("evidence_text") or "")[:2000],
                    str(value.get("extraction_method") or "rule"),
                    float(value.get("confidence") or 0),
                    status,
                    int(bool(value.get("auto_confirmed"))),
                    timestamp,
                    timestamp,
                ),
            )
        return existing is None

    def get_stock_lead(self, lead_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT l.*,p.url,p.text,p.posted_at,p.review_status,k.display_name,k.handle
                FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                WHERE l.id=?
                """,
                (lead_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"stock lead not found: {lead_id}")
        value = dict(row)
        value["auto_confirmed"] = bool(value["auto_confirmed"])
        return value

    def list_stock_leads(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
        kol_id: int | None = None,
        post_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("l.status=?")
            params.append(status)
        if symbol:
            clauses.append("l.symbol=?")
            params.append(symbol)
        if kol_id:
            clauses.append("l.kol_id=?")
            params.append(kol_id)
        if post_id:
            clauses.append("l.post_id=?")
            params.append(post_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT l.*,p.url,p.text,p.posted_at,p.review_status,k.display_name,k.handle
                FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                {where}
                ORDER BY p.posted_at DESC,l.id DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["auto_confirmed"] = bool(value["auto_confirmed"])
            values.append(value)
        return values

    def query_stock_mentions(
        self,
        *,
        query: str = "",
        kind: str = "",
        kol_id: int | None = None,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.strip():
            term = f"%{query.strip().lower()}%"
            clauses.append(
                "(LOWER(l.symbol) LIKE ? OR LOWER(l.security_name) LIKE ? "
                "OR LOWER(k.display_name) LIKE ? OR LOWER(k.handle) LIKE ? "
                "OR LOWER(p.text) LIKE ? OR LOWER(l.evidence_text) LIKE ?)"
            )
            params.extend([term] * 6)
        if kind:
            clauses.append("l.mention_kind=?")
            params.append(kind)
        if kol_id is not None:
            clauses.append("l.kol_id=?")
            params.append(kol_id)
        if symbol:
            clauses.append("l.symbol=?")
            params.append(symbol)
        if date_from:
            clauses.append("substr(p.posted_at,1,10)>=?")
            params.append(date_from)
        if date_to:
            clauses.append("substr(p.posted_at,1,10)<=?")
            params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_page = max(1, page)
        safe_page_size = max(1, min(page_size, 100))
        offset = (safe_page - 1) * safe_page_size
        with self.connect() as db:
            total = int(db.execute(
                f"""
                SELECT COUNT(*) FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                {where}
                """,
                params,
            ).fetchone()[0])
            rows = db.execute(
                f"""
                SELECT l.*,p.url,p.text,p.article_title,p.article_text,p.posted_at,
                    p.review_status,c.ocr_text,k.display_name,k.handle
                FROM stock_leads l
                JOIN posts p ON p.post_id=l.post_id
                LEFT JOIN classifications c ON c.post_id=l.post_id
                JOIN kols k ON k.id=l.kol_id
                {where}
                ORDER BY p.posted_at DESC,l.id DESC LIMIT ? OFFSET ?
                """,
                [*params, safe_page_size, offset],
            ).fetchall()
        items = []
        for row in rows:
            value = dict(row)
            value["auto_confirmed"] = bool(value["auto_confirmed"])
            items.append(value)
        return {
            "items": items,
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "total_pages": (total + safe_page_size - 1) // safe_page_size,
        }

    def review_stock_lead(
        self,
        lead_id: int,
        action: str,
        note: str = "",
        *,
        symbol: str | None = None,
        security_name: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"confirmed", "ignored", "pending"}:
            raise ValueError("invalid stock lead review action")
        updates = ["status=?", "review_note=?", "reviewed_at=?", "updated_at=?"]
        timestamp = now_iso()
        params: list[Any] = [action, note.strip(), timestamp if action != "pending" else "", timestamp]
        if symbol is not None:
            clean_symbol = symbol.strip()
            if not re.fullmatch(r"\d{6}", clean_symbol):
                raise ValueError("invalid stock lead symbol")
            updates.append("symbol=?")
            params.append(clean_symbol)
        if security_name is not None:
            updates.append("security_name=?")
            params.append(security_name.strip())
        params.append(lead_id)
        try:
            with self.connect() as db:
                cursor = db.execute(
                    f"UPDATE stock_leads SET {','.join(updates)} WHERE id=?",
                    params,
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"stock lead not found: {lead_id}")
                db.execute(
                    "INSERT INTO stock_lead_reviews(lead_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                    (lead_id, action, _json({"note": note, "symbol": symbol, "security_name": security_name}), timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("a stock lead for this post and symbol already exists") from exc
        return self.get_stock_lead(lead_id)

    def auto_confirm_stock_lead(self, lead_id: int, note: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE stock_leads SET status='confirmed',auto_confirmed=1,
                    review_note=?,reviewed_at=?,updated_at=?
                WHERE id=? AND status='pending' AND extraction_method='exact_code'
                """,
                (note, timestamp, timestamp, lead_id),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "INSERT INTO stock_lead_reviews(lead_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                    (lead_id, "auto_confirmed", _json({"note": note}), timestamp),
                )
        return self.get_stock_lead(lead_id)

    def lead_extraction_signature(self, post_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT source_signature FROM lead_extractions WHERE post_id=? AND status='completed'",
                (post_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def mark_lead_extraction(self, post_id: str, signature: str, *, error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO lead_extractions(post_id,source_signature,status,error,extracted_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET source_signature=excluded.source_signature,
                    status=excluded.status,error=excluded.error,extracted_at=excluded.extracted_at
                """,
                (post_id, signature, "failed" if error else "completed", error[:2000], now_iso()),
            )

    def save_model_classification(
        self,
        post_id: str,
        payload: dict[str, Any] | None,
        *,
        model_name: str,
        prompt_version: str,
        error: str = "",
    ) -> None:
        status = "failed" if error else "completed"
        value = payload or {}
        with self.connect() as db:
            attempt_row = db.execute(
                "SELECT model_attempts FROM classifications WHERE post_id=?",
                (post_id,),
            ).fetchone()
        attempts_before = int(attempt_row[0] or 0) if attempt_row else 0
        retry_delays = (timedelta(minutes=30), timedelta(hours=6), timedelta(hours=24))
        retry_at = (
            (datetime.now(SHANGHAI) + retry_delays[min(attempts_before, 2)]).isoformat(timespec="seconds")
            if error
            else ""
        )
        with self.connect() as db:
            db.execute(
                """
                UPDATE classifications SET model_status=?,model_name=?,prompt_version=?,
                    model_attempts=model_attempts+1,model_next_retry_at=?,
                    content_type=COALESCE(NULLIF(?,''),content_type),
                    evidence_type=COALESCE(NULLIF(?,''),evidence_type),confidence=?,
                    model_summary=?,drafts_json=?,model_error=?,
                    draft_generation_status=CASE
                        WHEN EXISTS(
                            SELECT 1 FROM recommendation_drafts d
                            WHERE d.post_id=classifications.post_id
                              AND d.status IN ('ready','needs_attention','approved','rejected')
                        ) THEN 'generated'
                        WHEN ?<>'' THEN 'failed'
                        ELSE 'pending'
                    END,
                    draft_generation_error=?,draft_generation_version=?,updated_at=? WHERE post_id=?
                """,
                (
                    status,
                    model_name,
                    prompt_version,
                    retry_at,
                    str(value.get("content_type") or ""),
                    str(value.get("evidence_type") or ""),
                    float(value.get("confidence") or 0),
                    str(value.get("summary") or "")[:2000],
                    _json(value.get("drafts") or []),
                    error[:2000],
                    error,
                    error[:2000],
                    prompt_version,
                    now_iso(),
                    post_id,
                ),
            )

    def set_draft_generation_status(
        self,
        post_id: str,
        status: str,
        *,
        version: str = "",
        error: str = "",
    ) -> None:
        if status not in {"pending", "generated", "not_applicable", "needs_attention", "failed"}:
            raise ValueError("invalid draft generation status")
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE classifications
                SET draft_generation_status=?,draft_generation_version=?,draft_generation_error=?,
                    draft_generated_at=CASE WHEN ? IN ('generated','not_applicable') THEN ? ELSE draft_generated_at END,
                    updated_at=?
                WHERE post_id=?
                """,
                (status, version[:120], error[:2000], status, timestamp, timestamp, post_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"classification not found: {post_id}")

    def enqueue_post_recovery(self, *, platform: str = "") -> int:
        """Queue public link-only rows for content hydration, idempotently."""
        timestamp = now_iso()
        clauses = [
            "p.canonical_provider='public-dataset-recovery'",
            "trim(coalesce(p.text,''))=''",
            "trim(coalesce(p.article_text,''))=''",
        ]
        params: list[Any] = []
        if platform:
            clauses.append("p.platform=?")
            params.append(platform)
        where = " AND ".join(clauses)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT p.post_id,p.kol_id,p.platform,p.handle,p.url FROM posts p WHERE {where}",
                params,
            ).fetchall()
            db.executemany(
                """
                INSERT INTO post_recovery_queue(
                    post_id,kol_id,platform,handle,url,state,attempts,next_attempt_at,
                    last_error_code,last_error,provider,queued_at,hydrated_at,updated_at
                ) VALUES(?,?,?,?,?,'queued',0,'','','','',?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET
                    kol_id=excluded.kol_id,platform=excluded.platform,handle=excluded.handle,
                    url=excluded.url,updated_at=excluded.updated_at
                """,
                [
                    (row["post_id"], row["kol_id"], row["platform"], row["handle"], row["url"], timestamp, "", timestamp)
                    for row in rows
                ],
            )
        return len(rows)

    def list_post_recovery_queue(
        self, *, platform: str = "", limit: int = 100, resume: bool = True
    ) -> list[dict[str, Any]]:
        clauses = ["state IN ('queued','cooldown')"] if resume else ["state='queued'"]
        params: list[Any] = []
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        clauses.append("(next_attempt_at='' OR next_attempt_at<=?)")
        params.append(now_iso())
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM post_recovery_queue WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at,post_id LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_post_recovery_running(self, post_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE post_recovery_queue SET state='running',attempts=attempts+1,updated_at=? "
                "WHERE post_id=? AND state IN ('queued','cooldown') AND (next_attempt_at='' OR next_attempt_at<=?)",
                (now_iso(), post_id, now_iso()),
            )
            return cursor.rowcount > 0

    def reconcile_post_recovery_queue(self) -> int:
        """Mark link-only queue rows hydrated when a bulk account fetch filled them."""
        timestamp = now_iso()
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT q.post_id
                FROM post_recovery_queue q
                JOIN posts p ON p.post_id=q.post_id
                WHERE q.state IN ('queued','cooldown','running')
                  AND (trim(coalesce(p.text,''))<>'' OR trim(coalesce(p.article_text,''))<>'')
                """
            ).fetchall()
            if not rows:
                return 0
            db.execute(
                """
                UPDATE post_recovery_queue
                SET state='hydrated',next_attempt_at='',last_error_code='',last_error='',
                    provider=COALESCE(NULLIF((SELECT p.canonical_provider FROM posts p WHERE p.post_id=post_recovery_queue.post_id),''),'bulk-fetch'),
                    hydrated_at=COALESCE(NULLIF(hydrated_at,''),?),updated_at=?
                WHERE state IN ('queued','cooldown','running')
                  AND EXISTS (
                    SELECT 1 FROM posts p WHERE p.post_id=post_recovery_queue.post_id
                      AND (trim(coalesce(p.text,''))<>'' OR trim(coalesce(p.article_text,''))<>'')
                  )
                """,
                (timestamp, timestamp),
            )
        return len(rows)

    def finish_post_recovery(
        self,
        post_id: str,
        *,
        state: str,
        provider: str = "",
        error_code: str = "",
        error: str = "",
        retry_after_seconds: int = 0,
    ) -> None:
        if state not in {"queued", "cooldown", "hydrated", "terminal"}:
            raise ValueError(f"invalid post recovery state: {state}")
        timestamp = now_iso()
        next_attempt = ""
        if retry_after_seconds:
            next_attempt = (datetime.now(SHANGHAI) + timedelta(seconds=retry_after_seconds)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                """
                UPDATE post_recovery_queue SET state=?,next_attempt_at=?,last_error_code=?,last_error=?,
                    provider=COALESCE(NULLIF(?,''),provider),hydrated_at=CASE WHEN ?='hydrated' THEN ? ELSE hydrated_at END,
                    updated_at=? WHERE post_id=?
                """,
                (state, next_attempt, error_code[:80], error[:2000], provider, state, timestamp, timestamp, post_id),
            )

    def post_recovery_summary(self, *, platform: str = "") -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        with self.connect() as db:
            rows = db.execute(
                "SELECT state,count(*) AS count FROM post_recovery_queue WHERE "
                + " AND ".join(clauses) + " GROUP BY state",
                params,
            ).fetchall()
        values = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "total": sum(values.values()),
            "queued": values.get("queued", 0),
            "running": values.get("running", 0),
            "cooldown": values.get("cooldown", 0),
            "hydrated": values.get("hydrated", 0),
            "terminal": values.get("terminal", 0),
        }

    def get_post(self, post_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                    c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                    c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                    c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                    c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                    c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                    c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
                FROM posts p JOIN kols k ON k.id=p.kol_id
                LEFT JOIN classifications c ON c.post_id=p.post_id
                WHERE p.post_id=?
                """,
                (post_id,),
            ).fetchone()
        value = self._row(row)
        if value is None:
            raise KeyError(f"post not found: {post_id}")
        value["is_candidate"] = bool(value.get("is_candidate"))
        return value

    def list_posts(
        self,
        *,
        review_status: str | None = None,
        kol_id: int | None = None,
        candidate_only: bool = False,
        posted_date: str | None = None,
        posted_from_utc: str | None = None,
        posted_to_utc: str | None = None,
        model_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if review_status:
            clauses.append("p.review_status=?")
            params.append(review_status)
        if kol_id:
            clauses.append("p.kol_id=?")
            params.append(kol_id)
        if candidate_only:
            clauses.append("c.is_candidate=1")
        if posted_date:
            clauses.append("substr(p.posted_at,1,10)=?")
            params.append(posted_date)
        if posted_from_utc:
            clauses.append("p.posted_at_utc>=?")
            params.append(posted_from_utc)
        if posted_to_utc:
            clauses.append("p.posted_at_utc<?")
            params.append(posted_to_utc)
        if model_status:
            clauses.append("c.model_status=?")
            params.append(model_status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"""
            SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
            FROM posts p JOIN kols k ON k.id=p.kol_id
            LEFT JOIN classifications c ON c.post_id=p.post_id
            {where} ORDER BY p.posted_at DESC LIMIT ? OFFSET ?
        """
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self.connect() as db:
            values = [self._row(row) for row in db.execute(query, params).fetchall()]
        return [{**value, "is_candidate": bool(value.get("is_candidate"))} for value in values if value]

    def reconcile_zhihu_historical_backfill(self) -> int:
        """Archive pre-onboarding Zhihu answers left pending by interrupted backfills."""
        timestamp = now_iso()
        note = "Zhihu historical backfill is archive-only."
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT p.post_id
                FROM posts p
                JOIN kols k ON k.id=p.kol_id
                JOIN classifications c ON c.post_id=p.post_id
                WHERE p.platform='Zhihu'
                    AND p.post_type='answer'
                    AND k.tracking_mode='direct_profile'
                    AND datetime(p.posted_at_utc) < datetime(k.created_at)
                    AND p.review_status='pending'
                    AND c.model_status='not_requested'
                    AND NOT EXISTS (
                        SELECT 1 FROM recommendation_drafts d
                        WHERE d.post_id=p.post_id
                            AND d.status IN ('ready','needs_attention','approved')
                    )
                """
            ).fetchall()
            post_ids = [str(row["post_id"]) for row in rows]
            if not post_ids:
                return 0
            placeholders = ",".join("?" for _ in post_ids)
            db.execute(
                f"UPDATE posts SET review_status='ignored',review_note=?,updated_at=? "
                f"WHERE post_id IN ({placeholders})",
                (note, timestamp, *post_ids),
            )
            detail = _json({"policy": "zhihu_direct_profile_backfill_archive_only"})
            db.executemany(
                "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,'ignored',?,?)",
                [(post_id, detail, timestamp) for post_id in post_ids],
            )
        return len(post_ids)

    def list_retrospective_followups(self, source_post_id: str) -> list[dict[str, Any]]:
        if not re.fullmatch(r"\d{5,25}", source_post_id):
            return []
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.post_id,p.url,p.text,p.posted_at,p.review_status,
                    k.display_name,c.evidence_type
                FROM posts p
                JOIN kols k ON k.id=p.kol_id
                JOIN classifications c ON c.post_id=p.post_id
                WHERE p.quoted_id=? AND c.evidence_type='retrospective'
                    AND p.review_status IN ('excluded','ignored')
                ORDER BY p.posted_at,p.post_id
                """,
                (source_post_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_retrospective_followups_by_source(
        self,
        source_post_ids: list[str] | set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        valid = sorted({value for value in source_post_ids if re.fullmatch(r"\d{5,25}", value)})
        if not valid:
            return {}
        placeholders = ",".join("?" for _ in valid)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT p.quoted_id AS source_post_id,p.post_id,p.url,p.text,p.posted_at,
                    p.review_status,k.display_name,c.evidence_type
                FROM posts p
                JOIN kols k ON k.id=p.kol_id
                JOIN classifications c ON c.post_id=p.post_id
                WHERE p.quoted_id IN ({placeholders}) AND c.evidence_type='retrospective'
                    AND p.review_status IN ('excluded','ignored')
                ORDER BY p.quoted_id,p.posted_at,p.post_id
                """,
                valid,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            value = dict(row)
            source_post_id = str(value.pop("source_post_id") or "")
            grouped.setdefault(source_post_id, []).append(value)
        return grouped

    def list_posts_for_model(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
            SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
            FROM posts p JOIN kols k ON k.id=p.kol_id
            JOIN classifications c ON c.post_id=p.post_id
            WHERE p.review_status='pending' AND c.is_candidate=1
                AND c.model_status='not_requested'
                AND c.ocr_status IN ('not_needed','completed','failed')
                AND (c.rule_symbols_json='[]' OR c.rule_direction='' OR c.evidence_type='ambiguous')
            ORDER BY p.posted_at DESC LIMIT ?
        """
        with self.connect() as db:
            values = [self._row(row) for row in db.execute(query, (max(1, min(limit, 500)),)).fetchall()]
        return [{**value, "is_candidate": True} for value in values if value]

    def claim_posts_for_ocr(self, limit: int = 8) -> list[dict[str, Any]]:
        timestamp = now_iso()
        stale_before = (datetime.now(SHANGHAI) - timedelta(minutes=30)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE classifications SET ocr_status='not_requested',ocr_updated_at=? "
                "WHERE ocr_status='running' AND ocr_updated_at<?",
                (timestamp, stale_before),
            )
            db.execute(
                "UPDATE classifications SET ocr_status='not_needed',ocr_updated_at=? "
                "WHERE ocr_status='not_requested' AND is_candidate=0",
                (timestamp,),
            )
            rows = db.execute(
                """
                SELECT p.post_id FROM posts p JOIN classifications c ON c.post_id=p.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                    AND c.model_status IN ('not_requested','failed')
                    AND p.local_media_json NOT IN ('','[]')
                    AND (
                        c.ocr_status='not_requested' OR (
                            c.ocr_status='failed' AND c.ocr_attempts<3
                            AND (c.ocr_next_retry_at='' OR c.ocr_next_retry_at<=?)
                        )
                    )
                ORDER BY p.posted_at DESC LIMIT ?
                """,
                (timestamp, max(1, min(limit, 50))),
            ).fetchall()
            ids = [str(row["post_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE classifications SET ocr_status='running',ocr_updated_at=? "
                    f"WHERE post_id IN ({placeholders})",
                    (timestamp, *ids),
                )
        return [self.get_post(post_id) for post_id in ids]

    def recover_interrupted_classification(self) -> dict[str, int]:
        timestamp = now_iso()
        with self.connect() as db:
            ocr = db.execute(
                "UPDATE classifications SET ocr_status='not_requested',ocr_updated_at=?,updated_at=? "
                "WHERE ocr_status='running'",
                (timestamp, timestamp),
            ).rowcount
            model = db.execute(
                "UPDATE classifications SET model_status='not_requested',model_error='',updated_at=? "
                "WHERE model_status='running'",
                (timestamp,),
            ).rowcount
        return {"ocr": max(0, ocr), "model": max(0, model)}

    def recover_stale_classification(
        self,
        *,
        ocr_minutes: int = 30,
        model_minutes: int = 10,
    ) -> dict[str, int]:
        timestamp = now_iso()
        ocr_before = (datetime.now(SHANGHAI) - timedelta(minutes=max(1, int(ocr_minutes)))).isoformat(timespec="seconds")
        model_before = (datetime.now(SHANGHAI) - timedelta(minutes=max(1, int(model_minutes)))).isoformat(timespec="seconds")
        with self.connect() as db:
            ocr = db.execute(
                "UPDATE classifications SET ocr_status='not_requested',ocr_updated_at=?,updated_at=? "
                "WHERE ocr_status='running' AND ocr_updated_at<?",
                (timestamp, timestamp, ocr_before),
            ).rowcount
            model = db.execute(
                "UPDATE classifications SET model_status='not_requested',model_error='',updated_at=? "
                "WHERE model_status='running' AND updated_at<?",
                (timestamp, model_before),
            ).rowcount
        return {"ocr": max(0, int(ocr)), "model": max(0, int(model))}

    def save_ocr_result(
        self,
        post_id: str,
        text: str = "",
        *,
        error: str = "",
        provider: str = "",
        confidence: float = 0,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        status = "failed" if error else "completed"
        retry_at = (
            (datetime.now(SHANGHAI) + timedelta(hours=1)).isoformat(timespec="seconds")
            if error
            else ""
        )
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE classifications SET ocr_status=?,ocr_attempts=ocr_attempts+1,
                    ocr_next_retry_at=?,ocr_text=?,ocr_provider=?,ocr_confidence=?,
                    ocr_details_json=?,ocr_error=?,ocr_updated_at=?,updated_at=?
                WHERE post_id=?
                """,
                (
                    status, retry_at, text[:20000], provider[:80], max(0, min(float(confidence), 1)),
                    _json(details or []), error[:2000], timestamp, timestamp, post_id,
                ),
            )

    def prepare_model_queue(self) -> int:
        with self.connect() as db:
            non_candidates = db.execute(
                """
                UPDATE classifications SET model_status='not_needed',model_name='rules',
                    model_error='',updated_at=?
                WHERE is_candidate=0 AND model_status='not_requested'
                """,
                (now_iso(),),
            )
            resolved_candidates = db.execute(
                """
                UPDATE classifications SET model_status='not_needed',model_name='rules+ocr',
                    model_error='',updated_at=?
                WHERE post_id IN (SELECT post_id FROM posts WHERE review_status='pending')
                    AND is_candidate=1 AND model_status='not_requested'
                    AND rule_symbols_json<>'[]' AND rule_direction<>''
                    AND ocr_status IN ('not_needed','completed','failed')
                """,
                (now_iso(),),
            )
        return int(non_candidates.rowcount) + int(resolved_candidates.rowcount)

    def release_model_claim(self, post_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE classifications SET model_status='not_requested',updated_at=? "
                "WHERE post_id=? AND model_status='running'",
                (now_iso(), post_id),
            )

    def release_ocr_claim(self, post_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE classifications SET ocr_status='not_requested',ocr_updated_at=?,updated_at=? "
                "WHERE post_id=? AND ocr_status='running'",
                (now_iso(), now_iso(), post_id),
            )

    def claim_posts_for_model(
        self,
        limit: int = 1,
        *,
        post_id: str = "",
        force: bool = False,
    ) -> list[dict[str, Any]]:
        timestamp = now_iso()
        stale_before = (datetime.now(SHANGHAI) - timedelta(minutes=10)).isoformat(timespec="seconds")
        row_limit = max(1, min(limit, 100))
        select = """
            SELECT p.*,k.display_name,k.domain,c.rule_score,c.rule_reasons_json,
                c.rule_symbols_json,c.rule_direction,c.content_type,c.evidence_type,
                c.is_candidate,c.model_status,c.model_name,c.prompt_version,c.confidence,c.model_summary,
                c.drafts_json,c.draft_generation_status,c.draft_generation_version,
                c.draft_generation_error,c.draft_generated_at,c.model_error,c.ocr_status,c.ocr_attempts,c.ocr_text,
                c.ocr_provider,c.ocr_confidence,c.ocr_details_json,
                c.ocr_error,c.ocr_updated_at,c.updated_at AS classification_updated_at
            FROM posts p JOIN kols k ON k.id=p.kol_id
            JOIN classifications c ON c.post_id=p.post_id
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE classifications SET model_status='not_requested',updated_at=?
                WHERE model_status='running' AND updated_at<?
                """,
                (timestamp, stale_before),
            )
            if post_id:
                status_clause = "c.model_status<>'running'" if force else "c.model_status='not_requested'"
                rows = db.execute(
                    select
                    + f" WHERE p.post_id=? AND p.review_status='pending' AND {status_clause} LIMIT 1",
                    (post_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    select
                    + """
                    WHERE p.review_status='pending' AND c.is_candidate=1 AND (
                        c.model_status='not_requested' OR (
                            c.model_status='failed' AND c.model_attempts<3
                            AND (c.model_next_retry_at='' OR c.model_next_retry_at<=?)
                        )
                    )
                    AND c.ocr_status IN ('not_needed','completed','failed')
                    AND (c.rule_symbols_json='[]' OR c.rule_direction='' OR c.evidence_type='ambiguous')
                    ORDER BY CASE c.model_status WHEN 'not_requested' THEN 0 ELSE 1 END,
                             p.posted_at DESC LIMIT ?
                    """,
                    (timestamp, row_limit),
                ).fetchall()
            ids = [str(row["post_id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE classifications SET model_status='running',updated_at=? "
                    f"WHERE post_id IN ({placeholders})",
                    (timestamp, *ids),
                )
        values = [self._row(row) for row in rows]
        return [
            {**value, "is_candidate": bool(value.get("is_candidate")), "model_status": "running"}
            for value in values
            if value
        ]

    def set_review(self, post_id: str, action: str, note: str = "", detail: dict[str, Any] | None = None) -> None:
        if action not in {"approved", "excluded", "ignored", "capture_failed", "pending"}:
            raise ValueError("invalid review action")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE posts SET review_status=?,review_note=?,updated_at=? WHERE post_id=?",
                (action, note.strip(), now_iso(), post_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"post not found: {post_id}")
            db.execute(
                "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                (post_id, action, _json(detail or {"note": note}), now_iso()),
            )

    def record_review_action(self, post_id: str, action: str, detail: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            if db.execute("SELECT 1 FROM posts WHERE post_id=?", (post_id,)).fetchone() is None:
                raise KeyError(f"post not found: {post_id}")
            db.execute(
                "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                (post_id, action, _json(detail or {}), now_iso()),
            )

    def latest_review(self, post_id: str, action: str | None = None) -> dict[str, Any] | None:
        where = "post_id=?"
        params: tuple[Any, ...] = (post_id,)
        if action:
            where += " AND action=?"
            params = (post_id, action)
        with self.connect() as db:
            row = db.execute(
                f"SELECT * FROM reviews WHERE {where} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["detail"] = _loads(value.pop("detail_json"), {})
        return value

    def active_approval_attempt(self, post_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            started = db.execute(
                "SELECT * FROM reviews WHERE post_id=? AND action='approval_started' ORDER BY id DESC LIMIT 1",
                (post_id,),
            ).fetchone()
            terminal = db.execute(
                "SELECT MAX(id) FROM reviews WHERE post_id=? "
                "AND action IN ('approved','capture_failed','excluded','ignored')",
                (post_id,),
            ).fetchone()[0]
        if started is None or (terminal is not None and int(terminal) > int(started["id"])):
            return None
        value = dict(started)
        value["detail"] = _loads(value.pop("detail_json"), {})
        return value

    def set_capture_links(self, post_id: str, source_note: str, notion_url: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE posts SET source_note=?,notion_url=?,updated_at=? WHERE post_id=?",
                (source_note, notion_url, now_iso(), post_id),
            )

    def set_local_source_ref(self, post_id: str, source_ref: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE posts SET source_note=?,notion_url='',updated_at=? WHERE post_id=?",
                (source_ref, now_iso(), post_id),
            )

    def count_posts(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM posts").fetchone()[0])

    def has_post(self, post_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM posts WHERE post_id=?", (post_id,)).fetchone() is not None

    def count_pending(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM posts WHERE review_status='pending'").fetchone()[0])

    def summary(self) -> dict[str, Any]:
        with self.connect() as db:
            counts = {
                row["review_status"]: int(row["count"])
                for row in db.execute("SELECT review_status,COUNT(*) count FROM posts GROUP BY review_status")
            }
            candidates = int(
                db.execute(
                    "SELECT COUNT(*) FROM classifications c JOIN posts p ON p.post_id=c.post_id "
                    "WHERE c.is_candidate=1 AND p.review_status='pending'"
                ).fetchone()[0]
            )
            classification_pending = int(
                db.execute(
                    "SELECT COUNT(*) FROM classifications c JOIN posts p ON p.post_id=c.post_id "
                    "WHERE c.is_candidate=1 AND p.review_status='pending' "
                    "AND c.model_status IN ('not_requested','running','failed')"
                ).fetchone()[0]
            )
            last_run = self._row(db.execute("SELECT * FROM fetch_runs ORDER BY started_at DESC LIMIT 1").fetchone())
            stock_leads = int(db.execute("SELECT COUNT(*) FROM stock_leads").fetchone()[0])
            pending_stock_leads = int(
                db.execute("SELECT COUNT(*) FROM stock_leads WHERE status='pending'").fetchone()[0]
            )
            confirmed_stock_leads = int(
                db.execute("SELECT COUNT(*) FROM stock_leads WHERE status='confirmed'").fetchone()[0]
            )
        return {
            "total_posts": self.count_posts(),
            "pending_posts": counts.get("pending", 0),
            "actionable_posts": candidates,
            "screened_pending_posts": max(0, counts.get("pending", 0) - candidates),
            "classification_pending": classification_pending,
            "candidate_posts": candidates,
            "approved_posts": counts.get("approved", 0),
            "active_kols": len(self.list_kols("active")),
            "stock_leads": stock_leads,
            "pending_stock_leads": pending_stock_leads,
            "confirmed_stock_leads": confirmed_stock_leads,
            "last_fetch_run": last_run,
        }

    def model_queue_summary(
        self,
        *,
        daily_limit: int = 250,
        ocr_daily_limit: int = 150,
    ) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT c.model_status,COUNT(*) count
                FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                GROUP BY c.model_status
                """
            ).fetchall()
            processable = int(db.execute(
                """
                SELECT COUNT(*)
                FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                  AND (
                    c.model_status IN ('not_requested','running')
                    OR (c.model_status='failed' AND c.model_attempts<3)
                  )
                  AND c.ocr_status IN ('not_needed','completed','failed')
                  AND (c.rule_symbols_json='[]' OR c.rule_direction='' OR c.evidence_type='ambiguous')
                """
            ).fetchone()[0])
            manual_attention = int(db.execute(
                """
                SELECT COUNT(*)
                FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                  AND c.model_status='failed' AND c.model_attempts>=3
                """
            ).fetchone()[0])
            awaiting_ocr = int(db.execute(
                """
                SELECT COUNT(*)
                FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                  AND c.model_status IN ('not_requested','failed')
                  AND p.local_media_json NOT IN ('','[]')
                  AND (
                    c.ocr_status IN ('not_requested','running')
                    OR (c.ocr_status='failed' AND c.ocr_attempts<3)
                  )
                """
            ).fetchone()[0])
            ocr_manual_attention = int(db.execute(
                """
                SELECT COUNT(*)
                FROM classifications c JOIN posts p ON p.post_id=c.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                  AND c.model_status IN ('not_requested','failed')
                  AND c.ocr_status='failed' AND c.ocr_attempts>=3
                """
            ).fetchone()[0])
            non_candidate_pending = int(db.execute(
                "SELECT COUNT(*) FROM classifications WHERE is_candidate=0 AND model_status='not_requested'"
            ).fetchone()[0])
        counts = {str(row["model_status"]): int(row["count"]) for row in rows}
        limit = max(1, int(daily_limit))
        ocr_limit = max(1, int(ocr_daily_limit))
        estimated_model_days = (processable + limit - 1) // limit
        estimated_ocr_days = (awaiting_ocr + ocr_limit - 1) // ocr_limit
        return {
            "candidate_total": sum(counts.values()),
            "completed": counts.get("completed", 0),
            "not_requested": counts.get("not_requested", 0),
            "running": counts.get("running", 0),
            "failed": counts.get("failed", 0),
            "not_needed": counts.get("not_needed", 0),
            "processable_remaining": processable,
            "awaiting_ocr": awaiting_ocr,
            "manual_attention": manual_attention + ocr_manual_attention,
            "model_manual_attention": manual_attention,
            "ocr_manual_attention": ocr_manual_attention,
            "non_candidate_not_requested": non_candidate_pending,
            "daily_limit": limit,
            "ocr_daily_limit": ocr_limit,
            "estimated_days": max(estimated_model_days, estimated_ocr_days),
        }

    def prepare_fetch_queue(
        self,
        batch_key: str,
        kols: list[dict[str, Any]],
        *,
        requested_count: int,
    ) -> None:
        if not batch_key.strip() or not kols:
            return
        timestamp = now_iso()
        ordered = list(kols)
        offset = int(hashlib.sha256(batch_key.encode("utf-8")).hexdigest()[:8], 16) % len(ordered)
        ordered = ordered[offset:] + ordered[:offset]
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue SET state='queued',started_at='',updated_at=?
                WHERE batch_key=? AND state='running' AND updated_at<?
                """,
                (
                    timestamp,
                    batch_key,
                    (datetime.now(SHANGHAI) - timedelta(minutes=60)).isoformat(timespec="seconds"),
                ),
            )
            for position, kol in enumerate(ordered):
                db.execute(
                    """
                    INSERT OR IGNORE INTO fetch_queue(
                        batch_key,kol_id,platform,handle,position,requested_count,
                        state,queued_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'queued',?,?)
                    """,
                    (
                        batch_key,
                        int(kol["id"]),
                        str(kol.get("platform") or "X"),
                        str(kol["handle"]),
                        position,
                        max(1, int(requested_count)),
                        timestamp,
                        timestamp,
                    ),
                )

    def pending_fetch_queue(
        self,
        batch_key: str,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(SHANGHAI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        timestamp = current.astimezone(SHANGHAI).isoformat(timespec="seconds")
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT k.*
                FROM fetch_queue q JOIN kols k ON k.id=q.kol_id
                WHERE q.batch_key=? AND k.status='active'
                  AND NOT EXISTS (SELECT 1 FROM fetch_batch_archive a WHERE a.batch_key=q.batch_key)
                  AND COALESCE(k.availability_status,'active') NOT IN ('suspended','deleted','protected','paused')
                  AND (
                    q.state='queued'
                    OR (q.state='cooldown' AND (q.not_before='' OR q.not_before<=?))
                  )
                ORDER BY q.position,q.kol_id
                """,
                (batch_key, timestamp),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_fetch_queue_running(self, batch_key: str, kol_id: int) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue
                SET state='running',attempts=attempts+1,started_at=?,updated_at=?
                WHERE batch_key=? AND kol_id=? AND state IN ('queued','cooldown')
                """,
                (timestamp, timestamp, batch_key, kol_id),
            )

    def complete_fetch_queue_item(self, batch_key: str, kol_id: int) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue
                SET state='completed',not_before='',last_error_code='',last_error='',
                    completed_at=?,updated_at=?
                WHERE batch_key=? AND kol_id=?
                """,
                (timestamp, timestamp, batch_key, kol_id),
            )

    def defer_fetch_queue_item(
        self,
        batch_key: str,
        kol_id: int,
        *,
        error_code: str,
        error: str,
        cooldown_seconds: int,
    ) -> None:
        current = datetime.now(SHANGHAI)
        not_before = (
            current + timedelta(seconds=max(0, int(cooldown_seconds)))
        ).isoformat(timespec="seconds")
        state = "cooldown" if error_code in {
            "rate_limited", "authentication_failed", "provider_incompatible"
        } else "queued"
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_queue
                SET state=?,not_before=?,last_error_code=?,last_error=?,updated_at=?
                WHERE batch_key=? AND kol_id=?
                """,
                (
                    state,
                    not_before,
                    error_code,
                    error[:1000],
                    current.isoformat(timespec="seconds"),
                    batch_key,
                    kol_id,
                ),
            )

    def fetch_queue_status(self, batch_key: str) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT state,COUNT(*) count FROM fetch_queue
                WHERE batch_key=? GROUP BY state
                """,
                (batch_key,),
            ).fetchall()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "batch_key": batch_key,
            "total": sum(counts.values()),
            "completed": counts.get("completed", 0),
            "pending": sum(
                counts.get(state, 0) for state in ("queued", "running", "cooldown")
            ),
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "cooldown": counts.get("cooldown", 0),
        }

    def fetch_queue_overview(self, platform: str = "") -> dict[str, Any]:
        """Return durable fetch backlog grouped by batch without changing state."""
        query = (
            "SELECT batch_key,platform,state,COUNT(*) AS items,"
            "COUNT(DISTINCT kol_id) AS unique_kols,MAX(updated_at) AS latest "
            "FROM fetch_queue WHERE state IN ('queued','running','cooldown') "
            "AND NOT EXISTS (SELECT 1 FROM fetch_batch_archive a WHERE a.batch_key=fetch_queue.batch_key)"
        )
        params: list[Any] = []
        if platform:
            query += " AND lower(platform)=?"
            params.append(platform.casefold())
        query += " GROUP BY batch_key,platform,state ORDER BY latest DESC"
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(query, params).fetchall()]
            distinct_query = (
                "SELECT COUNT(DISTINCT kol_id) FROM fetch_queue "
                "WHERE state IN ('queued','running','cooldown') "
                "AND NOT EXISTS (SELECT 1 FROM fetch_batch_archive a WHERE a.batch_key=fetch_queue.batch_key)"
            )
            if platform:
                distinct_query += " AND lower(platform)=?"
            distinct_kols = int(db.execute(distinct_query, params).fetchone()[0] or 0)
        batches: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["batch_key"])
            item = batches.setdefault(
                key,
                {
                    "batch_key": key,
                    "platform": str(row["platform"]),
                    "latest": str(row["latest"] or ""),
                    "items": 0,
                    "unique_kols": 0,
                    "queued": 0,
                    "running": 0,
                    "cooldown": 0,
                },
            )
            state = str(row["state"])
            count = int(row["items"] or 0)
            item[state] = count
            item["items"] += count
            item["unique_kols"] = max(item["unique_kols"], int(row["unique_kols"] or 0))
            item["latest"] = max(item["latest"], str(row["latest"] or ""))
        values = list(batches.values())
        values.sort(key=lambda value: value["latest"], reverse=True)
        return {
            "total_items": sum(int(value["items"]) for value in values),
            "unique_batches": len(values),
            "queued": sum(int(value["queued"]) for value in values),
            "running": sum(int(value["running"]) for value in values),
            "cooldown": sum(int(value["cooldown"]) for value in values),
            "unique_kols": distinct_kols,
            "batches": values[:100],
        }

    def latest_pending_fetch_batch(self, platform: str = "") -> str:
        query = (
            "SELECT batch_key,MAX(updated_at) latest FROM fetch_queue "
            "WHERE state<>'completed' AND NOT EXISTS "
            "(SELECT 1 FROM fetch_batch_archive a WHERE a.batch_key=fetch_queue.batch_key)"
        )
        params: list[Any] = []
        if platform:
            query += " AND lower(platform)=?"
            params.append(platform.casefold())
        query += " GROUP BY batch_key ORDER BY latest DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, params).fetchone()
        return str(row["batch_key"]) if row else ""

    def reset_interrupted_fetch_queue(self, batch_key: str) -> int:
        """Make queue items from a killed recovery worker resumable."""
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE fetch_queue
                SET state='queued',started_at='',updated_at=?
                WHERE batch_key=? AND state='running'
                """,
                (timestamp, batch_key),
            )
        return max(0, cursor.rowcount)

    def reopen_fetch_queue(self, batch_key: str) -> int:
        """Requeue completed rows for the next persisted cursor page.

        Historical recovery deliberately processes one page per invocation;
        the database cursor, not the queue's completed flag, is the source of
        truth for the next page.
        """
        timestamp = now_iso()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE fetch_queue SET state='queued',started_at='',completed_at='',updated_at=? "
                "WHERE batch_key=? AND state='completed' AND NOT EXISTS ("
                "SELECT 1 FROM x_page_checkpoints c WHERE c.kol_id=fetch_queue.kol_id AND c.phase='completed')",
                (timestamp, batch_key),
            )
        return max(0, cursor.rowcount)

    def archive_legacy_fetch_batches(self, older_than: datetime) -> list[str]:
        cutoff = older_than.astimezone(SHANGHAI).isoformat(timespec="seconds")
        timestamp = now_iso()
        with self.connect() as db:
            rows = db.execute(
                "SELECT batch_key FROM fetch_queue WHERE updated_at<? GROUP BY batch_key",
                (cutoff,),
            ).fetchall()
            keys = [str(row["batch_key"]) for row in rows]
            for key in keys:
                db.execute(
                    "INSERT OR IGNORE INTO fetch_batch_archive(batch_key,reason,archived_at) VALUES(?,?,?)",
                    (key, "superseded_legacy", timestamp),
                )
        return keys

    def create_fetch_batch(
        self,
        batch_id: str,
        *,
        batch_kind: str,
        platform: str,
        window_start: str,
        window_end: str,
        strategy_version: str,
        total_kols: int,
    ) -> None:
        if batch_kind not in {"freshness", "recent_recovery", "historical_recovery"}:
            raise ValueError(f"invalid fetch batch kind: {batch_kind}")
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO fetch_batches(
                    batch_id,batch_kind,platform,window_start,window_end,
                    strategy_version,status,total_kols,created_at,updated_at
                ) VALUES(?,?,?,?,?,?, 'running',?,?,?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status='running',
                    error=''
                """,
                (
                    batch_id,
                    batch_kind,
                    platform,
                    window_start,
                    window_end,
                    strategy_version,
                    max(0, int(total_kols)),
                    timestamp,
                    timestamp,
                ),
            )

    def finish_fetch_batch(
        self,
        batch_id: str,
        *,
        status: str,
        completed_kols: int,
        successful_kols: int,
        failed_kols: int,
        new_posts: int,
        error: str = "",
    ) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_batches SET status=?,completed_kols=?,successful_kols=?,
                    failed_kols=?,new_posts=?,error=?,updated_at=?,completed_at=?
                WHERE batch_id=?
                """,
                (
                    status,
                    max(0, int(completed_kols)),
                    max(0, int(successful_kols)),
                    max(0, int(failed_kols)),
                    max(0, int(new_posts)),
                    error[:2000],
                    timestamp,
                    timestamp,
                    batch_id,
                ),
            )

    def collection_coverage(
        self,
        *,
        platform: str = "",
        window_start: str = "",
        window_end: str = "",
    ) -> dict[str, Any]:
        conditions = ["k.status='active'", "COALESCE(k.availability_status,'active') NOT IN ('suspended','deleted','paused')"]
        params: list[Any] = []
        if platform:
            conditions.append("lower(k.platform)=?")
            params.append(platform.casefold())
        where = " AND ".join(conditions)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT k.id,k.display_name,k.platform,k.handle,k.last_success_at,
                       k.fetch_status,k.availability_status,k.availability_reason,
                       COUNT(CASE WHEN p.posted_at>=? AND p.posted_at<=? THEN 1 END) AS window_posts
                FROM kols k LEFT JOIN posts p ON p.kol_id=k.id
                WHERE {where}
                GROUP BY k.id
                ORDER BY lower(k.platform),lower(k.display_name)
                """,
                (window_start or "0000-01-01", window_end or "9999-12-31", *params),
            ).fetchall()
        items = [dict(row) for row in rows]
        total = len(items)
        successful = sum(
            1 for item in items
            if str(item.get("last_success_at") or "")
            and str(item.get("fetch_status") or "") in {"success", "gap_detected"}
            and (
                not window_start
                or str(item.get("last_success_at") or "")[:10] >= window_start
            )
        )
        return {
            "platform": platform or "all",
            "window_start": window_start,
            "window_end": window_end,
            "target": total,
            "successful": successful,
            "coverage": successful / total if total else 0.0,
            "items": items,
        }

    def open_collection_gap(
        self,
        kol_id: int,
        *,
        platform: str,
        window_start: str,
        window_end: str,
        last_post_id: str = "",
        recovery_depth: int = 0,
        status: str = "open",
        error: str = "",
    ) -> None:
        timestamp = now_iso()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO collection_gaps(
                    kol_id,platform,window_start,window_end,last_post_id,
                    recovery_depth,status,error,last_verified_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(kol_id,window_start,window_end) DO UPDATE SET
                    last_post_id=excluded.last_post_id,recovery_depth=excluded.recovery_depth,
                    status=excluded.status,error=excluded.error,
                    last_verified_at=excluded.last_verified_at,updated_at=excluded.updated_at
                """,
                (
                    kol_id,
                    platform,
                    window_start,
                    window_end,
                    last_post_id,
                    max(0, int(recovery_depth)),
                    status,
                    error[:2000],
                    timestamp if status == "closed" else "",
                    timestamp,
                    timestamp,
                ),
            )

    def list_collection_gaps(self, *, status: str = "open", limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT g.*,k.handle,k.display_name FROM collection_gaps g JOIN kols k ON k.id=g.kol_id "
                "WHERE g.status=? ORDER BY g.updated_at LIMIT ?",
                (status, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_fetch_run(self, requested_count: int, *, total_kols: int = 0) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO fetch_runs(
                    run_id,started_at,status,requested_count,total_kols,stage
                ) VALUES(?,?,?, ?,?,'fetching')
                """,
                (run_id, now_iso(), "running", requested_count, max(0, total_kols)),
            )
        return run_id

    def update_fetch_progress(
        self,
        run_id: str,
        *,
        processed_kols: int,
        successful_kols: int,
        failed_kols: int,
        new_posts: int,
        candidate_posts: int,
        stage: str = "fetching",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_runs SET processed_kols=?,successful_kols=?,failed_kols=?,
                    new_posts=?,candidate_posts=?,stage=?
                WHERE run_id=? AND status='running'
                """,
                (
                    max(0, processed_kols),
                    max(0, successful_kols),
                    max(0, failed_kols),
                    max(0, new_posts),
                    max(0, candidate_posts),
                    stage,
                    run_id,
                ),
            )

    def finish_fetch_run(self, summary: FetchSummary) -> None:
        status = (
            "blocked"
            if summary.blocked_platforms and summary.successful_kols == 0
            else "failed"
            if summary.successful_kols == 0 and (summary.failed_kols or summary.blocked_platforms)
            else "partial"
            if summary.failed_kols or summary.blocked_platforms
            else "success"
        )
        with self.connect() as db:
            db.execute(
                """
                UPDATE fetch_runs SET completed_at=?,status=?,stage='completed',
                    processed_kols=CASE WHEN total_kols>0 THEN total_kols ELSE ? END,
                    successful_kols=?,failed_kols=?,
                    new_posts=?,candidate_posts=?,auth_status=?,gap_kols_json=?,fallback_kols_json=?,
                    shadow_failed_kols_json=?,platform_breakdown_json=?,errors_json=? WHERE run_id=?
                """,
                (
                    now_iso(),
                    status,
                    summary.successful_kols + summary.failed_kols,
                    summary.successful_kols,
                    summary.failed_kols,
                    summary.new_posts,
                    summary.candidate_posts,
                    summary.auth_status,
                    _json(summary.gap_kols),
                    _json(summary.fallback_kols),
                    _json(summary.shadow_failed_kols),
                    _json(summary.platform_breakdown),
                    _json(summary.errors),
                    summary.run_id,
                ),
            )

    def save_fetch_model_result(self, run_id: str, completed: int, failed: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE fetch_runs SET codex_completed=?,codex_failed=? WHERE run_id=?",
                (max(0, completed), max(0, failed), run_id),
            )

    def codex_failure_streak(self) -> int:
        with self.connect() as db:
            rows = db.execute(
                "SELECT codex_completed,codex_failed FROM fetch_runs "
                "WHERE codex_completed + codex_failed > 0 ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
        streak = 0
        for row in rows:
            if int(row["codex_failed"]) > 0 and int(row["codex_completed"]) == 0:
                streak += 1
            else:
                break
        return streak

    def recent_fetch_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM fetch_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def interrupt_stale_fetch_runs(
        self,
        *,
        now: datetime | None = None,
        max_age_minutes: int = 60,
    ) -> int:
        if worker_active(self.path, "fetch"):
            return 0
        current = now or datetime.now(SHANGHAI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        cutoff = current.astimezone(SHANGHAI) - timedelta(minutes=max(1, max_age_minutes))
        recovered = 0
        with self.connect() as db:
            rows = db.execute(
                "SELECT run_id,started_at FROM fetch_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                try:
                    started = datetime.fromisoformat(str(row["started_at"]))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=SHANGHAI)
                except ValueError:
                    started = datetime.min.replace(tzinfo=SHANGHAI)
                if started.astimezone(SHANGHAI) > cutoff:
                    continue
                recovered += db.execute(
                    """
                    UPDATE fetch_runs SET status='failed',completed_at=?,errors_json=?
                    WHERE run_id=? AND status='running'
                    """,
                    (current.isoformat(timespec="seconds"), _json(["stale_run_recovered"]), row["run_id"]),
                ).rowcount
        return max(0, recovered)


def initialize_seed_kols(store: KolPostStore) -> int:
    created = 0
    for display_name, handle, domain in SEED_KOLS:
        _, was_created = store.add_kol(display_name, handle, domain)
        created += int(was_created)
    return created


def load_stock_aliases(*paths: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for path in paths:
        if not Path(path).exists():
            continue
        try:
            with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    symbol = str(row.get("symbol") or "").strip()
                    name = str(row.get("security_name") or row.get("name") or "").strip()
                    if re.fullmatch(r"\d{6}", symbol) and name:
                        aliases[symbol] = name
        except (OSError, UnicodeError, csv.Error):
            continue
    return aliases
