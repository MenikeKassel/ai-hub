"""Explicit dependencies shared by the HTTP route groups."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiServices:
    app: Any
    approve_draft_record: Any
    assert_market_writes_allowed: Any
    assert_research_updates_allowed: Any
    assert_returns_updates_allowed: Any
    build_system_diagnostics: Any
    classification_aliases: Any
    collection_runs: Any
    config: Any
    confirm_approval_leads: Any
    credentials: Any
    deepseek_credentials: Any
    diagnostics_cache: Any
    diagnostics_lock: Any
    event_dossier: Any
    event_research: Any
    event_store: Any
    extract_leads: Any
    freestockdb_health_cache: Any
    market_admissions: Any
    market_health_cache: Any
    market_recovery_mode: Any
    market_store: Any
    market_writes_enabled: Any
    nitter_credentials: Any
    ocr_root: Any
    ocr_runner: Any
    performance: Any
    post_store: Any
    public_backup: Any
    rapid_ocr_python: Any
    rapid_ocr_runner: Any
    reader_credentials: Any
    recommendation_drafts: Any
    reprocess_recommendation_post: Any
    require_discovery: Any
    research_writes_enabled: Any
    returns_writes_enabled: Any
    review_agent: Any
    run_foundation_refresh_job: Any
    run_freestockdb_update_job: Any
    safe_freestockdb_health: Any
    safe_market_health: Any
    sanitize_recommendation_draft: Any
    technical_context_for: Any
    x_sessions: Any
    xtf_command: Any
    zhihu_provider: Any
