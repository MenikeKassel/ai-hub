from __future__ import annotations
from .context import ApiServices
from .dependencies import (
    Any,
    FetchRequest,
    HTTPException,
    OPERATOR_TASKS,
    Query,
    ROOT,
    RuleClassifier,
    _fallback_mode,
    _start_scheduled_task as _start_scheduled_task_impl,
    _task_status,
    asdict,
    build_post_classifier,
    build_x_post_provider,
    classify_pending_with_codex,
    date,
    now_iso,
    os,
    run_post_fetch,
    subprocess,
    sys,
    timedelta,
    uuid,
)


def _start_scheduled_task(task_name: str) -> dict[str, Any]:
    import kol_api
    return kol_api._start_scheduled_task(task_name)


def register_routes(services: ApiServices) -> None:
    app = services.app
    classification_aliases = services.classification_aliases
    collection_runs = services.collection_runs
    config = services.config
    deepseek_credentials = services.deepseek_credentials
    extract_leads = services.extract_leads
    post_store = services.post_store
    recommendation_drafts = services.recommendation_drafts
    x_sessions = services.x_sessions
    xtf_command = services.xtf_command
    zhihu_provider = services.zhihu_provider

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


    @app.get("/api/fetch-runs")
    def fetch_runs() -> list[dict[str, Any]]:
        return post_store.recent_fetch_runs()


    @app.get("/api/tasks")
    def operator_tasks() -> dict[str, Any]:
        recommendation_drafts.interrupt_stale_runs()
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


    @app.get("/api/fetch-attempts")
    def fetch_attempts(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return post_store.recent_fetch_attempts(limit)

