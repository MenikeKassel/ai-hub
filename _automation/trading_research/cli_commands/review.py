from __future__ import annotations
import argparse
from types import ModuleType


def kol_recommendation_repair(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    review_date = context.date.today().isoformat()
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
        print(context.json.dumps({
            "ok": True,
            "database": str(store.path),
            "schema": {str(row[0]): int(row[1]) for row in status_rows},
            "recommendation_without_drafts": anomaly_count,
            "repair_candidates": len(context._recommendation_repair_ids(store)),
            "full_rules_scan_candidates": len(
                context._recommendation_repair_ids(store, include_non_candidates=True)
            ),
        }, ensure_ascii=False, indent=2))
        return

    if args.post_id:
        post_ids = [args.post_id]
    elif args.all or args.pending_ai:
        post_ids = context._recommendation_repair_ids(
            store,
            limit=args.limit,
            include_non_candidates=bool(args.all),
        )
    else:
        raise SystemExit("use --doctor, --all, --pending-ai, or --post-id")

    if args.rules_only:
        result = context._run_recommendation_rules_repair(store, post_ids, review_date=review_date)
        mode = "rules"
    elif args.pending_ai or args.post_id:
        result = context._run_recommendation_ai_repair(
            store,
            post_ids,
            review_date=review_date,
            max_runtime=float(args.max_runtime),
        )
        mode = "ai"
    else:
        result = context._run_recommendation_rules_repair(store, post_ids, review_date=review_date)
        mode = "rules"
    result["ok"] = not result.get("errors")
    result["partial"] = bool(result.get("stopped_reason"))
    result["mode"] = mode
    result["review_date"] = review_date
    lead_result = context._extract_leads_to_market(store)
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
    print(context.json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


def kol_review_agent_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    store = context._post_store()
    repository = context.ReviewAgentRepository(store)
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
        "codex_cli": context.shutil.which("codex") or "",
        "classifier_schema": context.KOL_CLASSIFIER_SCHEMA.exists(),
        "unlimited_ocr_available": context.UnlimitedOcrBatchClassifier(
            context.UNLIMITED_OCR_ROOT, context.UNLIMITED_OCR_RUNNER
        ).available(),
        "rapid_ocr_available": context._ocr_classifier().available(),
        "ocr_provider": "rapidocr",
    }
    checks["ok"] = bool(
        checks["codex_cli"]
        and checks["classifier_schema"]
        and checks["rapid_ocr_available"]
    )
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_review_agent_run(context: ModuleType, args: argparse.Namespace) -> None:
    runner = context._review_agent_runner()
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
            refresh = context.run_post_approval_refresh(
                context.RUNTIME,
                context.ROOT,
                result["queued_symbols"],
                as_of=context.date.today(),
                timeout_seconds=240,
                raise_on_error=True,
            )
            refresh_status = str(refresh["status"])
        except Exception as exc:
            refresh_status = "failed"
            result["errors"].append(f"post-approval refresh: {str(exc)[:1000]}")
    result["refresh_status"] = refresh_status
    result["notification_errors"] = (
        context._send_pending_notifications(context.KolStore(context.KOL_ROOT)) if not args.dry_run else []
    )
    print(context.json.dumps(result, ensure_ascii=False, indent=2))
    if result["errors"]:
        raise SystemExit(2)


def kol_review_agent_report(context: ModuleType, _: argparse.Namespace) -> None:
    repository = context.ReviewAgentRepository(context._post_store())
    print(context.json.dumps(repository.summary(), ensure_ascii=False, indent=2))


def kol_morning_pipeline(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    market = context._market_store()
    review_date = context.date.fromisoformat(args.as_of) if args.as_of else context.date.today()

    def fetcher():
        batch_key = f"morning:{review_date.isoformat()}:{args.platform}"
        return context.run_post_fetch(
            store,
            context._post_provider(args.provider, batch_key=batch_key),
            platform_providers={
                "zhihu": context._zhihu_provider(),
                "douyin": context._douyin_provider(),
            },
            platforms=None if args.platform == "all" else {args.platform},
            max_count=args.fetch_count,
            classifier=context.RuleClassifier(context._classification_aliases()),
            batch_key=batch_key,
            fresh_first_page=True,
        )

    pipeline = context.MorningPipeline(
        store,
        market,
        rule_classifier=context.RuleClassifier(context._classification_aliases()),
        batch_classifier=context.build_batch_post_classifier(
            context.KOL_BATCH_CLASSIFIER_SCHEMA,
            context.ROOT,
            deepseek_credentials=context.DeepSeekCredentialStore(),
        ),
        ocr_classifier=context._ocr_classifier(),
        fetcher=fetcher,
        market_writes_enabled=context._market_writes_enabled(),
        active_kol_count=sum(
            str(item.get("availability_status") or "active") not in {"suspended", "deleted", "protected", "paused"}
            for item in store.list_kols("active", None if args.platform == "all" else args.platform)
        ),
    )
    repository = context.RecommendationDraftRepository(store)
    repository.interrupt_stale_runs()
    result = pipeline.run(
        as_of=review_date,
        fetch=not args.skip_fetch,
        backlog_limit=args.backlog_limit,
        max_runtime_minutes=args.max_runtime,
        phase=args.phase,
    )
    print(context.json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"] or result.get("alert_required"):
        raise SystemExit(2)


def kol_morning_orchestrate(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    market = context._market_store()
    review_date = context.date.today()
    capture_sync: dict[str, Any] | None = None
    if args.platform in {"douyin", "all"} and not args.skip_fetch:
        # Douyin has no live adapter by design: refresh the archive captures
        # first so the fetch phase below ingests whatever the local
        # douyin-collection-archive currently holds.
        from kol_discovery_runtime import DEFAULT_CAPTURE_ROOT
        from kol_sources.douyin_capture import sync_douyin_captures

        try:
            capture_sync = sync_douyin_captures(store, capture_root=DEFAULT_CAPTURE_ROOT)
        except Exception as exc:  # pragma: no cover - defensive report
            capture_sync = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    def fetcher():
        batch_key = f"morning:{review_date.isoformat()}:{args.platform}"
        return context.run_post_fetch(
            store,
            context._post_provider(args.provider, batch_key=batch_key),
            platform_providers={
                "zhihu": context._zhihu_provider(),
                "douyin": context._douyin_provider(),
            },
            platforms=None if args.platform == "all" else {args.platform},
            max_count=args.fetch_count,
            classifier=context.RuleClassifier(context._classification_aliases()),
            batch_key=batch_key,
            fresh_first_page=True,
        )

    pipeline = context.MorningPipeline(
        store,
        market,
        rule_classifier=context.RuleClassifier(context._classification_aliases()),
        batch_classifier=context.build_batch_post_classifier(
            context.KOL_BATCH_CLASSIFIER_SCHEMA,
            context.ROOT,
            deepseek_credentials=context.DeepSeekCredentialStore(),
        ),
        ocr_classifier=context._ocr_classifier(),
        fetcher=fetcher,
        market_writes_enabled=context._market_writes_enabled(),
        active_kol_count=sum(
            str(item.get("availability_status") or "active") not in {"suspended", "deleted", "protected", "paused"}
            for item in store.list_kols("active", None if args.platform == "all" else args.platform)
        ),
    )
    repository = context.RecommendationDraftRepository(store)
    interrupted = repository.interrupt_stale_runs()

    def run_phase(phase: str, runtime: float) -> dict[str, Any]:
        return pipeline.run(
            as_of=review_date,
            fetch=not args.skip_fetch,
            # Archived Douyin items can be months old, so their phase also
            # drains the pending backlog instead of relying on the 09:00-to-
            # 09:00 review window alone.
            backlog_limit=50 if args.platform == "douyin" else 0,
            max_runtime_minutes=runtime,
            phase=phase,
        )

    result = context.MorningOrchestrator(runner=run_phase).run()
    result["interrupted_stale_runs"] = interrupted
    if capture_sync is not None:
        result["douyin_capture_sync"] = capture_sync
    print(context.json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"] or any(item.get("alert_required") for item in result["results"]):
        raise SystemExit(2)


def kol_morning_migrate(context: ModuleType, _: argparse.Namespace) -> None:
    store = context._post_store()
    result = context.RecommendationDraftRepository(store).migrate_legacy_review_queue()
    print(context.json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


def kol_morning_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    store = context._post_store()
    repository = context.RecommendationDraftRepository(store)
    checks = {
        "ok": True,
        "database": str(store.path),
        "schema": context.KOL_BATCH_CLASSIFIER_SCHEMA.exists(),
        "codex_cli": context.shutil.which("codex") or "",
        "deepseek_configured": context.DeepSeekCredentialStore().configured(),
        "ocr_available": context._ocr_classifier().available(),
        "ocr_provider": "rapidocr",
        "recent_runs": repository.recent_morning_runs(3),
        "today": repository.morning_summary(context.date.today().isoformat()),
    }
    checks["ok"] = bool(
        checks["schema"]
        and (checks["codex_cli"] or checks["deepseek_configured"])
        and checks["ocr_available"]
    )
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_ui_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    checks = {
        "ok": True,
        "frontend_dist": str(context.KOL_UI_DIST),
        "frontend_exists": (context.KOL_UI_DIST / "index.html").exists(),
        "fastapi": context.dependency_available("fastapi"),
        "uvicorn": context.dependency_available("uvicorn"),
        "database": str(context.KOL_POST_DB),
        "database_exists": context.KOL_POST_DB.exists(),
        "url": "http://127.0.0.1:8123",
    }
    checks["ok"] = bool(checks["frontend_exists"] and checks["fastapi"] and checks["uvicorn"])
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_leads_extract(context: ModuleType, _: argparse.Namespace) -> None:
    print(context.json.dumps(context._extract_leads_to_market(), ensure_ascii=False, indent=2))
