from __future__ import annotations
from .context import ApiServices
from . import system_routes, reviews_routes, accounts_routes, performance_routes, collection_routes, market_routes, events_routes
from .health_status import model_component_health
from .dependencies import (
    Any,
    ApiSettings,
    DeepSeekCredentialStore,
    DeepSeekPostClassifier,
    Draft,
    EventDossierService,
    EventMethodResearchService,
    FOUNDATION_ROOT,
    FastAPI,
    FileResponse,
    FoundationBackedMarketStore,
    FoundationMarketReader,
    FreeStockDBRuntime,
    HTTPException,
    KeyringCredentialStore,
    KolPerformanceService,
    KolPostStore,
    KolStore,
    MarketAdmissionRepository,
    MarketStore,
    MarketWriteBlockedError,
    ModelDailyBudget,
    ModelWorkerBusyError,
    NitterCredentialStore,
    Path,
    PublicBackupGate,
    ROOT,
    RapidOcrBatchClassifier,
    ReaderCredentialStore,
    RecommendationDraftRepository,
    ReviewAgentRepository,
    RuleClassifier,
    SHANGHAI,
    SOURCE_FILE,
    STRUCTURED_REVIEW_VERSION,
    StaticFiles,
    TrustedHostMiddleware,
    UnlimitedOcrBatchClassifier,
    XSessionManager,
    ZhihuProfileProvider,
    _fallback_mode,
    _read_process_pid,
    _resolve_twitter_command,
    _task_status,
    approve_recommendation_draft,
    asdict,
    assert_market_runtime_writes_allowed,
    assert_research_writes_allowed,
    assert_returns_writes_allowed,
    build_event_research_interpreter,
    build_post_classifier,
    build_x_post_provider,
    date,
    datetime,
    docker_container_health,
    docker_health as _docker_health_impl,
    event_context_input_hash,
    extract_stock_leads,
    http_health,
    initialize_seed_kols,
    json,
    load_market_recovery_mode,
    load_stock_aliases,
    logger,
    market_runtime_writes_enabled,
    materialize_recommendation_drafts,
    media_disk_usage,
    now_iso,
    os,
    read_published_manifest,
    read_refresh_state,
    read_workers,
    reconcile_refresh_state,
    reconcile_exact_stock_leads,
    review_window_utc,
    shutil,
    socket,
    subprocess,
    sys,
    threading,
    time,
    timedelta,
    xtf_version,
)

def docker_health(*args, **kwargs):
    # Keep the old kol_api.docker_health patch surface while allowing the
    # split factory to be used directly.
    try:
        import kol_api
        return kol_api.docker_health(*args, **kwargs)
    except (ImportError, AttributeError):
        return _docker_health_impl(*args, **kwargs)


def create_app(
    settings: ApiSettings | None = None,
    credential_store: KeyringCredentialStore | None = None,
    nitter_credential_store: NitterCredentialStore | None = None,
    deepseek_credential_store: DeepSeekCredentialStore | None = None,
) -> FastAPI:
    config = settings or ApiSettings.default()
    kol_root = config.runtime_root / "kol"
    post_store = KolPostStore(kol_root / "posts.db", kol_root / "media")
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
            SOURCE_FILE.with_name("event_research_schema.json"),
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
    ocr_runner = SOURCE_FILE.with_name("unlimited_ocr_batch.py")
    rapid_ocr_python = Path(os.environ.get(
        "RAPID_OCR_PYTHON",
        ROOT / "_runtime" / "venv-ocr-fast" / "Scripts" / "python.exe",
    ))
    rapid_ocr_runner = SOURCE_FILE.with_name("rapid_ocr_batch.py")
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
            value["publication_complete"] = bool(cutoff and not formal_lagging)
            if cutoff and not formal_lagging:
                value["published_as_of"] = cutoff
                value["lagging_symbols"] = []
                value["lagging_symbol_count"] = 0
                latest_open = str(value.get("latest_open_date") or "")
                session_status = str(value.get("market_session_status") or "unknown")
                if latest_open and cutoff < latest_open:
                    value["daily_data_status"] = "stale"
                    value["market_status"] = "stale"
                elif session_status == "unknown" and cutoff < date.today().isoformat():
                    value["daily_data_status"] = "freshness_unknown"
                    value["market_status"] = "freshness_unknown"
                else:
                    value["daily_data_status"] = "current"
                    value["market_status"] = "current"
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


    def run_freestockdb_update_job(dry_run: bool) -> None:
        try:
            freestockdb_runtime.update(dry_run=dry_run, timeout=3300)
        except Exception as exc:
            logger.error("FreeStockDB background update failed: %s", exc)
        finally:
            freestockdb_health_cache["expires_at"] = 0.0


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
            "workers": read_workers(post_store.path),
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
        model_budget = ModelDailyBudget(post_store, daily_limit=250).status()
        ai_component = model_component_health(
            post_store,
            model_budget,
            credentials_configured=deepseek_credentials.configured(),
        )
        refresh_state = read_refresh_state(config.runtime_root)
        refresh_state = reconcile_refresh_state(refresh_state, market_component)
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
            "model_daily_budget": model_budget,
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
                "ai": ai_component,
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


    services = ApiServices(
        app=app,
        approve_draft_record=approve_draft_record,
        assert_market_writes_allowed=assert_market_writes_allowed,
        assert_research_updates_allowed=assert_research_updates_allowed,
        assert_returns_updates_allowed=assert_returns_updates_allowed,
        build_system_diagnostics=build_system_diagnostics,
        classification_aliases=classification_aliases,
        collection_runs=collection_runs,
        config=config,
        confirm_approval_leads=confirm_approval_leads,
        credentials=credentials,
        deepseek_credentials=deepseek_credentials,
        diagnostics_cache=diagnostics_cache,
        diagnostics_lock=diagnostics_lock,
        event_dossier=event_dossier,
        event_research=event_research,
        event_store=event_store,
        extract_leads=extract_leads,
        freestockdb_health_cache=freestockdb_health_cache,
        market_admissions=market_admissions,
        market_health_cache=market_health_cache,
        market_recovery_mode=market_recovery_mode,
        market_store=market_store,
        market_writes_enabled=market_writes_enabled,
        nitter_credentials=nitter_credentials,
        ocr_root=ocr_root,
        ocr_runner=ocr_runner,
        performance=performance,
        post_store=post_store,
        public_backup=public_backup,
        rapid_ocr_python=rapid_ocr_python,
        rapid_ocr_runner=rapid_ocr_runner,
        reader_credentials=reader_credentials,
        recommendation_drafts=recommendation_drafts,
        reprocess_recommendation_post=reprocess_recommendation_post,
        require_discovery=require_discovery,
        research_writes_enabled=research_writes_enabled,
        returns_writes_enabled=returns_writes_enabled,
        review_agent=review_agent,
        run_foundation_refresh_job=run_foundation_refresh_job,
        run_freestockdb_update_job=run_freestockdb_update_job,
        safe_freestockdb_health=safe_freestockdb_health,
        safe_market_health=safe_market_health,
        sanitize_recommendation_draft=sanitize_recommendation_draft,
        technical_context_for=technical_context_for,
        x_sessions=x_sessions,
        xtf_command=xtf_command,
        zhihu_provider=zhihu_provider,
    )
    system_routes.register_routes(services)
    reviews_routes.register_routes(services)
    accounts_routes.register_routes(services)
    performance_routes.register_routes(services)
    collection_routes.register_routes(services)
    market_routes.register_routes(services)
    events_routes.register_routes(services)

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
