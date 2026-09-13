from __future__ import annotations
from .health_status import model_component_health
from .context import ApiServices
from .dependencies import (
    Any,
    CredentialStorageError,
    CredentialValidationError,
    DeepSeekCredentialRequest,
    DeepSeekPostClassifier,
    HTTPException,
    ModelDailyBudget,
    OcrDailyBudget,
    PublicBackupPolicyRequest,
    Query,
    RapidOcrBatchClassifier,
    ReviewAgentSettingsRequest,
    SHANGHAI,
    TwitterCredentialRequest,
    UnlimitedOcrBatchClassifier,
    XCollectionPolicyRequest,
    XSessionCredentialRequest,
    XSessionStatusRequest,
    XSessionUnavailableError,
    _fallback_mode,
    _resolve_twitter_command,
    date,
    datetime,
    now_iso,
    read_refresh_state,
    read_workers,
    reconcile_refresh_state,
    review_window_utc,
    rollback_decision,
    shutil,
    socket,
    time,
    timedelta,
    validate_twitter_credentials,
)


def register_routes(services: ApiServices) -> None:
    app = services.app
    build_system_diagnostics = services.build_system_diagnostics
    config = services.config
    credentials = services.credentials
    deepseek_credentials = services.deepseek_credentials
    diagnostics_cache = services.diagnostics_cache
    diagnostics_lock = services.diagnostics_lock
    event_store = services.event_store
    freestockdb_health_cache = services.freestockdb_health_cache
    market_admissions = services.market_admissions
    market_health_cache = services.market_health_cache
    market_recovery_mode = services.market_recovery_mode
    market_store = services.market_store
    market_writes_enabled = services.market_writes_enabled
    nitter_credentials = services.nitter_credentials
    ocr_root = services.ocr_root
    ocr_runner = services.ocr_runner
    post_store = services.post_store
    public_backup = services.public_backup
    rapid_ocr_python = services.rapid_ocr_python
    rapid_ocr_runner = services.rapid_ocr_runner
    reader_credentials = services.reader_credentials
    recommendation_drafts = services.recommendation_drafts
    review_agent = services.review_agent
    safe_market_health = services.safe_market_health
    x_sessions = services.x_sessions
    xtf_command = services.xtf_command
    zhihu_provider = services.zhihu_provider

    @app.get("/api/ready")
    def ready() -> dict[str, Any]:
        return {"ok": True, "service": "kol-research-console", "version": "4.0.0"}


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
        recommendation_drafts.interrupt_stale_runs()
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
        model_budget = ModelDailyBudget(post_store, daily_limit=250).status()
        deepseek_configured = deepseek_credentials.configured()
        component_status["ai"] = model_component_health(
            post_store,
            model_budget,
            credentials_configured=deepseek_configured,
        )
        ocr_budget = OcrDailyBudget(post_store, daily_limit=150).status()
        queue_status = {
            "workers": read_workers(post_store.path),
            "x": post_store.fetch_queue_overview("X"),
            "zhihu": post_store.fetch_queue_overview("Zhihu"),
            "ocr_model": post_store.model_queue_summary(daily_limit=250, ocr_daily_limit=150),
            "post_recovery": post_store.post_recovery_summary(),
            "market_admissions": market_admissions.summary(),
        }
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
            "deepseek_credentials_configured": deepseek_configured,
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
            "pipeline_refresh": reconcile_refresh_state(
                read_refresh_state(config.runtime_root), cached_market
            ),
            "review_agent": cached_diagnostics.get("review_agent", {}),
            "shadow_rollout": cached_diagnostics.get("shadow_rollout", post_store.shadow_rollout_status()),
            "component_status": component_status,
        }


    @app.get("/api/system/queues")
    def system_queues() -> dict[str, Any]:
        """Return all durable backlogs; this endpoint is read-only and cheap."""
        return {
            "ok": True,
            "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "workers": read_workers(post_store.path),
            "x": post_store.fetch_queue_overview("X"),
            "zhihu": post_store.fetch_queue_overview("Zhihu"),
            "ocr_model": post_store.model_queue_summary(daily_limit=250, ocr_daily_limit=150),
            "post_recovery": post_store.post_recovery_summary(),
            "market_admissions": market_admissions.summary(),
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
        source_market_status = str(market.get("market_status") or "")
        if source_market_status in {
            "market_locked",
            "market_unavailable",
            "stale",
            "freshness_unknown",
            "provider_pending",
            "checking",
        }:
            market_status = source_market_status
        elif session_status in {"pre_open", "trading"}:
            market_status = session_status
        elif session_status in {"post_close", "closed"}:
            market_status = session_status
        elif not session_status or session_status == "unknown":
            market_status = "unknown"
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
            "published_as_of": market.get("published_as_of", market.get("as_of", "")),
            "market_session_status": session_status or "unknown",
            "market_status": market_status,
            "lagging_symbols": market.get("lagging_symbols", []),
            "refresh": reconcile_refresh_state(
                read_refresh_state(config.runtime_root), market
            ),
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
        if body.enabled is None and body.paused is None:
            raise HTTPException(status_code=422, detail="enabled or paused is required")
        return x_sessions.set_policy(enabled=body.enabled, paused=body.paused, reason=body.reason)


    @app.get("/api/system/public-backup")
    def public_backup_status() -> dict[str, Any]:
        return public_backup.status()


    @app.patch("/api/system/public-backup")
    def patch_public_backup(body: PublicBackupPolicyRequest) -> dict[str, Any]:
        if body.enabled is None and body.paused is None:
            raise HTTPException(status_code=422, detail="enabled or paused is required")
        return public_backup.set_policy(enabled=body.enabled, paused=body.paused, reason=body.reason)


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

