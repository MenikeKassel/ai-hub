from __future__ import annotations
import argparse
from types import ModuleType


def _platform_providers(context: ModuleType) -> dict[str, object]:
    """Providers for the platforms that are not the primary X provider.

    Douyin has no live adapter by design: its provider replays JSON captures
    produced by ``kol-douyin-capture-sync`` from the local archive.
    """
    return {
        "zhihu": context._zhihu_provider(),
        "douyin": context._douyin_provider(),
    }


def kol_post_doctor(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    x_sessions = context.XSessionManager(store)
    x_policy = x_sessions.policy_status()
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
        "media_bytes": context.media_disk_usage(store.media_root),
        "twitter_cli": context._resolve_twitter_command("twitter"),
        "twitter_credentials_configured": context.KeyringCredentialStore().configured(),
        "twitter_reader_credentials_configured": context.ReaderCredentialStore().configured(),
        "x_sessions": x_policy,
        "zhihu_capture_available": context.ZHIHU_PROFILE_CAPTURE.is_file(),
        "zhihu_active_kols": len(active_zhihu),
        "zhihu_paused_kols": len(paused_zhihu),
        "zhihu_failed_kols": len(failed_zhihu),
        "zhihu_failure_handles": [str(item.get("handle")) for item in failed_zhihu[:20]],
        "failed_kols": len(failed_scoped),
        "failure_handles": [str(item.get("handle")) for item in failed_scoped[:50]],
        "nitter_credentials_configured": context.NitterCredentialStore().configured(),
        "xtf": str(context.XTF_COMMAND) if context.XTF_COMMAND.exists() else "",
        "xtf_version": context.xtf_version(context.XTF_COMMAND) if context.XTF_COMMAND.exists() else "",
        "nitter": context.timeline_health(context.NITTER_URL),
        "docker": context.docker_health(),
        "fallback_mode": context._fallback_mode(),
        "shadow_rollout": store.shadow_rollout_status(),
        "codex_cli": context.shutil.which("codex") or "",
        "classifier_schema": context.KOL_CLASSIFIER_SCHEMA.exists(),
        "unlimited_ocr_root": str(context.UNLIMITED_OCR_ROOT),
        "unlimited_ocr_available": context.UnlimitedOcrBatchClassifier(
            context.UNLIMITED_OCR_ROOT, context.UNLIMITED_OCR_RUNNER
        ).available(),
        "rapid_ocr_available": context._ocr_classifier().available(),
        "ocr_provider": "rapidocr",
    }
    platform_ok = {
        "all": bool(checks["twitter_cli"] and checks["zhihu_capture_available"]),
        "x": bool(checks["twitter_cli"] and any(item["status"] == "ready" for item in x_policy["slots"])),
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
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_post_db_backup(context: ModuleType, _: argparse.Namespace) -> None:
    if not context.KOL_POST_DB.exists():
        print(context.json.dumps({"ok": True, "skipped": True, "reason": "database_missing"}))
        return
    backup_root = context.KOL_ROOT / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / f"posts-{context.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    with context.sqlite3.connect(context.KOL_POST_DB) as source, context.sqlite3.connect(target) as destination:
        source.backup(destination)
    backups = sorted(backup_root.glob("posts-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for expired in backups[14:]:
        expired.unlink(missing_ok=True)
    print(context.json.dumps({"ok": True, "backup": str(target)}, ensure_ascii=False))


def kol_reader_migrate(context: ModuleType, _: argparse.Namespace) -> None:
    """Copy the existing Nitter reader session into its isolated X reader slot."""
    source = context.NitterCredentialStore()
    target = context.ReaderCredentialStore()
    auth_token, ct0 = source.load_values()
    target.save(auth_token, ct0)
    print(context.json.dumps({
        "ok": True,
        "source": source.service_name,
        "target": target.service_name,
        "credentials_configured": target.configured(),
    }, ensure_ascii=False))


def kol_collection_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    store = context._post_store()
    x_policy = context.XSessionManager(store).policy_status()
    end = context.date.today().isoformat()
    start = (context.date.today() - context.timedelta(days=7)).isoformat()
    checks = {
        "ok": any(item["status"] == "ready" for item in x_policy["slots"]),
        "x_sessions": x_policy,
        "database": str(store.path),
        "reader_credentials_configured": context.ReaderCredentialStore().configured(),
        "main_credentials_configured": context.KeyringCredentialStore().configured(),
        "nitter_optional": True,
        "nitter": context.timeline_health(context.NITTER_URL),
        "x_recent": store.collection_coverage(platform="X", window_start=start, window_end=end),
        "zhihu_recent": store.collection_coverage(platform="Zhihu", window_start=start, window_end=end),
        "open_gaps": store.list_collection_gaps(status="open", limit=100),
        "latest_runs": store.recent_fetch_runs(limit=5),
    }
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_gap_audit(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    end = context.date.today().isoformat() if args.to_date == "auto" else args.to_date
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
            last_success = str(item.get("last_success_at") or "")[:10]
            fetch_status = str(item.get("fetch_status") or "")
            if not last_success or last_success < start or fetch_status not in {"success", "gap_detected"}:
                kol = store.get_kol(int(item["id"]))
                store.open_collection_gap(
                    int(item["id"]),
                    platform=platform,
                    window_start=start,
                    window_end=end,
                    last_post_id=str((kol or {}).get("last_post_id") or ""),
                    status="open",
                    error="no successful fetch observed in recovery window",
                )
                payload["gaps"].append({"platform": platform, **item})
            else:
                # Zero posts after a successful fetch is valid inactivity, not
                # a collection gap. Close an older false-positive gap while
                # keeping its row and verification timestamp for audit.
                store.open_collection_gap(
                    int(item["id"]),
                    platform=platform,
                    window_start=start,
                    window_end=end,
                    last_post_id="",
                    status="closed",
                    error="fetch succeeded; no activity is not a gap",
                )
    payload["open_gaps"] = store.list_collection_gaps(status="open", limit=1000)
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))


def kol_gap_recover(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    end = context.date.today().isoformat()
    start = (context.date.today() - context.timedelta(days=7)).isoformat() if args.scope == "recent" else "2026-01-01"
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
    aggregate: list[Any] = []
    reset_items = 0
    skipped_platforms: list[dict[str, Any]] = []
    for platform in ("x", "zhihu"):
        platform_key = platform.casefold()
        fresh_key = f"{batch_id}:{platform_key}:fresh"
        history_key = f"{batch_id}:{platform_key}:history"
        platform_name = "X" if platform_key == "x" else "Zhihu"
        platform_coverage = store.collection_coverage(
            platform=platform_name,
            window_start=start,
            window_end=end,
        )
        if args.resume and platform_coverage.get("target", 0) and platform_coverage.get("coverage", 0.0) >= 0.9:
            skipped_platforms.append({
                "platform": platform_name,
                "reason": "coverage_already_at_least_90_percent",
                "coverage": platform_coverage,
            })
            continue
        reset_items += store.reset_interrupted_fetch_queue(fresh_key)
        reset_items += store.reset_interrupted_fetch_queue(history_key)
        if args.scope == "historical":
            reset_items += store.reopen_fetch_queue(fresh_key)
        provider = (
            context._post_provider(
                "auto",
                timeout_seconds=90 if args.scope == "historical" else 20,
                batch_key=fresh_key,
                history_mode=args.scope == "historical",
            )
            if platform_key == "x"
            else context._post_provider("auto", timeout_seconds=20)
        )
        first = context.run_post_fetch(
            store,
            provider,
            platform_providers=_platform_providers(context),
            platforms={platform_key},
            max_count=20 if args.scope == "recent" else 500,
            fresh_first_page=args.scope == "recent",
            reconcile_zhihu=False,
            sleep_seconds=1.0,
            # The shared X budget treats ordinary provider failures as
            # deferred work; retrying inside the same window would consume a
            # second request and can turn a local failure into a false 429.
            retry_delays=(),
            batch_key=fresh_key,
            classifier=context.RuleClassifier(context._classification_aliases()),
        )
        aggregate.append(first)
        if args.scope == "recent" and not first.rate_limit_paused and first.queue_pending == 0:
            reset_items += store.reopen_fetch_queue(history_key)
            provider = context._post_provider(
                "auto",
                timeout_seconds=20,
                batch_key=history_key,
                history_mode=True,
            ) if platform_key == "x" else provider
            second = context.run_post_fetch(
                store,
                provider,
                platform_providers=_platform_providers(context),
                platforms={platform_key},
                max_count=100,
                reconcile_zhihu=False,
                sleep_seconds=1.0,
                retry_delays=(),
                batch_key=history_key,
                classifier=context.RuleClassifier(context._classification_aliases()),
            )
            aggregate.append(second)
    successful = sum(item.successful_kols for item in aggregate)
    failed = sum(item.failed_kols for item in aggregate)
    new_posts = sum(item.new_posts for item in aggregate)
    errors = [error for item in aggregate for error in item.errors]
    pending_queue = sum(item.queue_pending for item in aggregate)
    if pending_queue:
        errors.append(f"{pending_queue} recovery queue items remain pending")
    status = "completed" if not pending_queue and not errors else "degraded"
    store.finish_fetch_batch(
        batch_id,
        status=status,
        completed_kols=successful + failed,
        successful_kols=successful,
        failed_kols=failed,
        new_posts=new_posts,
        error="; ".join(errors[:5]),
    )
    print(context.json.dumps({
        "ok": bool(successful),
        "scope": args.scope,
        "batch_id": batch_id,
        "reset_interrupted_queue_items": reset_items,
        "skipped_platforms": skipped_platforms,
        "runs": [item.__dict__ for item in aggregate],
        "coverage": {
            "X": store.collection_coverage(platform="X", window_start=start, window_end=end),
            "Zhihu": store.collection_coverage(platform="Zhihu", window_start=start, window_end=end),
        },
    }, ensure_ascii=False, indent=2))
    if not successful and failed:
        raise SystemExit(2)


def kol_post_recovery(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    queued = store.enqueue_post_recovery(platform="" if args.platform == "all" else ("X" if args.platform == "x" else "Zhihu"))
    reconciled = store.reconcile_post_recovery_queue()
    summary_before = store.post_recovery_summary(platform="" if args.platform == "all" else ("X" if args.platform == "x" else "Zhihu"))
    payload: dict[str, Any] = {
        "ok": True,
        "scope": args.scope,
        "platform": args.platform,
        "queued": queued,
        "reconciled_bulk": reconciled,
        "dry_run": not bool(args.apply),
        "before": summary_before,
        "processed": 0,
        "hydrated": 0,
        "terminal": 0,
        "retryable": 0,
        "errors": [],
    }
    if not args.apply:
        print(context.json.dumps(payload, ensure_ascii=False, indent=2))
        return
    classifier = context.RuleClassifier(context._classification_aliases())
    platform_filter = "" if args.platform == "all" else ("X" if args.platform == "x" else "Zhihu")
    x_sessions = context.XSessionManager(store) if args.platform in {"all", "x"} else None
    x_batch_key = f"post-recovery:{context.uuid.uuid4().hex}"
    x_slot_id: int | None = None
    if args.platform in {"all", "zhihu"}:
        # A single authenticated browser batch is shared by all per-answer
        # requests. Failure here is reported once and remains retryable.
        try:
            context._ensure_zhihu_browser_session()
        except Exception as exc:
            if args.platform == "zhihu":
                payload["ok"] = False
                payload["errors"].append(f"auth_or_browser_preflight: {exc}")
                print(context.json.dumps(payload, ensure_ascii=False, indent=2))
                raise SystemExit(2)
    # Process in bounded durable batches.  With no explicit limit the command
    # drains the queue; an interruption leaves remaining rows queued/cooldown
    # for the next --resume invocation.
    budget = max(0, int(args.limit or 0))
    processed_budget = 0
    while True:
        batch_limit = min(1000, budget - processed_budget) if budget else 1000
        if batch_limit <= 0:
            break
        queue = store.list_post_recovery_queue(platform=platform_filter, limit=batch_limit, resume=args.resume)
        if not queue:
            break
        for item in queue:
            if budget and processed_budget >= budget:
                break
            if not store.mark_post_recovery_running(str(item["post_id"])):
                # Another worker claimed this row between SELECT and UPDATE.
                # This makes bounded parallel recovery safe and idempotent.
                continue
            processed_budget += 1
            payload["processed"] += 1
            try:
                post_row = store.get_post(str(item["post_id"]))
                if not post_row or (str(post_row.get("text") or "").strip() or str(post_row.get("article_text") or "").strip()):
                    store.finish_post_recovery(str(item["post_id"]), state="hydrated", provider="existing")
                    payload["hydrated"] += 1
                    continue
                kol = {"id": item["kol_id"], "handle": item["handle"], "display_name": post_row.get("display_name") or item["handle"], "tracking_mode": "direct_profile"}
                if item["platform"] == "X":
                    if x_sessions is not None and x_slot_id is None:
                        x_slot_id = x_sessions.batch_slot(x_batch_key, "history")
                    raw, provider_name, warning = context._twitter_single_payload(
                        post_row,
                        session_manager=x_sessions,
                        batch_key=x_batch_key,
                        slot_id=x_slot_id,
                    )
                    record = context.normalise_twitter_post(raw, kol, provider=provider_name, provider_warning=warning)
                else:
                    raw = context._zhihu_single_payload(post_row)
                    record = context.normalise_zhihu_answer(raw, kol, provider="zhihu-local")
                store.upsert_post(record)
                store.save_rule_classification(record.post_id, classifier.classify(record))
                store.finish_post_recovery(str(item["post_id"]), state="hydrated", provider=record.canonical_provider)
                payload["hydrated"] += 1
            except Exception as exc:
                detail = str(exc)
                state, error_code, retry_after = context._post_recovery_error_state(detail)
                store.finish_post_recovery(str(item["post_id"]), state=state, error_code=error_code, error=detail, retry_after_seconds=retry_after)
                if state == "terminal":
                    payload["terminal"] += 1
                else:
                    payload["retryable"] += 1
                payload["errors"].append({"post_id": item["post_id"], "code": error_code, "error": detail[:500]})
        if budget and processed_budget >= budget:
            break
    payload["after"] = store.post_recovery_summary(platform=platform_filter)
    payload["ok"] = not bool(payload["errors"]) or payload["hydrated"] > 0
    report_path = getattr(args, "report", None)
    if report_path:
        context.Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        context.Path(report_path).write_text(context.json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_fetch_queue_compact(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    cutoff = context.datetime.now(context.SHANGHAI) - context.timedelta(hours=max(1, args.older_than_hours))
    keys = store.archive_legacy_fetch_batches(cutoff)
    print(context.json.dumps({"ok": True, "archived_batches": len(keys), "batch_keys": keys}, ensure_ascii=False, indent=2))


def kol_ai_resume(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    completed, failed = context.classify_pending_in_batches(
        store,
        context.build_batch_post_classifier(
            context.KOL_BATCH_CLASSIFIER_SCHEMA,
            context.ROOT,
            deepseek_credentials=context.DeepSeekCredentialStore(),
        ),
        limit=args.limit,
        daily_limit=args.daily_limit,
    )
    print(context.json.dumps({
        "ok": failed == 0,
        "completed": completed,
        "failed": failed,
        "daily_budget": context.ModelDailyBudget(store, daily_limit=args.daily_limit).status(),
    }, ensure_ascii=False))


def kol_ai_queue_maintain(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    before = store.model_queue_summary(daily_limit=args.daily_limit)
    recovered = store.recover_stale_classification() if args.recover_stale else {"ocr": 0, "model": 0}
    changed = store.prepare_model_queue() if args.apply else 0
    after = store.model_queue_summary(daily_limit=args.daily_limit) if args.apply else before
    print(context.json.dumps({
        "ok": True,
        "dry_run": not bool(args.apply),
        "changed": changed,
        "recovered_stale": recovered,
        "before": before,
        "after": after,
        "daily_budget": context.ModelDailyBudget(store, daily_limit=args.daily_limit).status(),
    }, ensure_ascii=False, indent=2))


def kol_import(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
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
        context.json.dumps(
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


def kol_zhihu_onboard(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
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
        context.json.dumps(
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


def kol_fallback_mode(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    if not args.set:
        print(context.json.dumps({"mode": context._fallback_mode(), "shadow_rollout": store.shadow_rollout_status()}, ensure_ascii=False, indent=2))
        return
    if args.set == "enabled":
        rollout = store.shadow_rollout_status()
        if not rollout["ready"]:
            print(context.json.dumps({"ok": False, "mode": context._fallback_mode(), "shadow_rollout": rollout}, ensure_ascii=False, indent=2))
            raise SystemExit(2)
    context.FALLBACK_MODE_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = context.FALLBACK_MODE_STATE.with_suffix(".tmp")
    temporary.write_text(context.json.dumps({"mode": args.set, "updated_at": context.now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")
    context.os.replace(temporary, context.FALLBACK_MODE_STATE)
    print(context.json.dumps({"ok": True, "mode": args.set}, ensure_ascii=False))


def kol_douyin_capture_sync(context: ModuleType, args: argparse.Namespace) -> None:
    """Regenerate Douyin captures from the local archive for registered KOLs."""
    from pathlib import Path

    from kol_discovery_runtime import DEFAULT_CAPTURE_ROOT
    from kol_sources.douyin_capture import sync_douyin_captures

    store = context._post_store()
    capture_root = Path(str(getattr(args, "capture_root", "") or "") or DEFAULT_CAPTURE_ROOT)
    manifest_arg = str(getattr(args, "manifest", "") or "")
    transcripts_arg = str(getattr(args, "transcripts", "") or "")
    dry_run = bool(getattr(args, "dry_run", False))
    result = sync_douyin_captures(
        store,
        manifest=Path(manifest_arg) if manifest_arg else None,
        transcripts=Path(transcripts_arg) if transcripts_arg else None,
        capture_root=capture_root,
        dry_run=dry_run,
    )
    if not dry_run:
        result["next_step"] = "kol-post-fetch --platform douyin"
    print(context.json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(2)


def kol_post_fetch(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    requested = args.backfill or 50
    handles = {
        value.strip().lstrip("@").casefold()
        for value in str(getattr(args, "handles", "") or "").split(",")
        if value.strip()
    }
    batch_key = str(getattr(args, "batch_key", "") or f"scheduled:{args.as_of or context.date.today().isoformat()}:{args.platform}")
    result = context.run_post_fetch(
        store,
        context._post_provider(
            args.provider,
            batch_key=batch_key,
            history_mode="history" in batch_key or "gap-" in batch_key,
        ),
        platform_providers=_platform_providers(context),
        platforms=None if args.platform == "all" else {args.platform},
        handles=handles or None,
        max_count=requested,
        classifier=context.RuleClassifier(context._classification_aliases()),
        dry_run=args.dry_run,
        batch_key=batch_key,
        fresh_first_page=bool(getattr(args, "fresh_first_page", False)),
    )
    codex_completed = codex_failed = 0
    if not args.dry_run and not args.skip_classify:
        codex_completed, codex_failed = context.classify_pending_with_codex(
            store,
            context.build_post_classifier(context.KOL_CLASSIFIER_SCHEMA, context.ROOT),
            limit=args.classify_limit,
        )
        store.save_fetch_model_result(result.run_id, codex_completed, codex_failed)
    lead_payload: dict[str, Any] = {}
    if not args.dry_run and not getattr(args, "skip_leads", False):
        lead_payload = context._extract_leads_to_market(store)
    codex_failure_streak = store.codex_failure_streak()
    payload = {
        **result.__dict__,
        "as_of": args.as_of or context.date.today().isoformat(),
        "codex_completed": codex_completed,
        "codex_failed": codex_failed,
        "codex_failure_streak": codex_failure_streak,
        "provider": args.provider,
        "dry_run": args.dry_run,
        "fallback_mode": context._fallback_mode(),
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
        payload["notification_sent"] = context._send_feishu(digest)
        payload["auth_alert_sent"] = context._send_transition_alert(
            "primary_auth_failed",
            result.auth_status == "failed",
            "[KOL自动采集告警] X Cookie 已失效，请在本地工作台系统页更新 auth_token 和 ct0。",
        )
        payload["fallback_alert_sent"] = context._send_transition_alert(
            "nitter_fallback_used",
            bool(result.fallback_kols),
            "[KOL自动采集告警] 主采集源不可用，今日已启用本地 Nitter 备用源。",
        )
        payload["all_failed_alert_sent"] = context._send_transition_alert(
            "all_providers_failed",
            result.successful_kols == 0 and result.failed_kols > 0,
            "[KOL自动采集告警] 主源与备用源均失败，本次未推进抓取游标。",
        )
        payload["gap_alert_sent"] = context._send_transition_alert(
            "gap_detected",
            bool(result.gap_kols),
            "[KOL自动采集告警] 检测到时间线缺口，请在本地工作台检查抓取审计。",
        )
        payload["shadow_alert_sent"] = context._send_transition_alert(
            "shadow_fallback_failed",
            bool(result.shadow_failed_kols),
            "[KOL自动采集告警] Nitter shadow 比对失败，请检查备用账号会话和容器状态。",
        )
        payload["codex_alert_sent"] = context._send_transition_alert(
            "codex_failure_streak",
            codex_failure_streak >= 3,
            f"[KOL自动采集告警] Codex 已连续 {codex_failure_streak} 次分类运行全部失败，请检查 Codex CLI。",
        )
    if args.alerts_only:
        payload["auth_alert_sent"] = context._send_transition_alert(
            "primary_auth_failed",
            result.auth_status == "failed",
            "[KOL采集告警] X会话已失效，请在本地工作台更新凭据。",
        )
        payload["all_failed_alert_sent"] = context._send_transition_alert(
            "all_providers_failed",
            result.successful_kols == 0 and result.failed_kols > 0,
            "[KOL采集告警] 本次所有采集源均失败，抓取游标没有推进。",
        )
        payload["gap_alert_sent"] = context._send_transition_alert(
            "gap_detected",
            bool(result.gap_kols),
            "[KOL采集告警] 检测到时间线缺口，请在本地工作台检查采集审计。",
        )
        payload["shadow_alert_sent"] = context._send_transition_alert(
            "shadow_fallback_failed",
            bool(result.shadow_failed_kols),
            "[KOL采集告警] Nitter影子比对失败，请检查备用会话和容器。",
        )
    print(context.json.dumps(payload, ensure_ascii=False))
    if result.successful_kols == 0 and result.failed_kols:
        raise SystemExit(2)


def kol_fetch_resume(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    batch_key = args.batch_key or store.latest_pending_fetch_batch(
        "" if args.platform == "all" else args.platform
    )
    if not batch_key:
        print(context.json.dumps({"ok": True, "status": "nothing_pending"}, ensure_ascii=False))
        return
    result = context.run_post_fetch(
        store,
        context._post_provider(
            args.provider,
            batch_key=batch_key,
            history_mode="history" in batch_key or "gap-" in batch_key,
        ),
        platform_providers=_platform_providers(context),
        platforms=None if args.platform == "all" else {args.platform},
        max_count=args.fetch_count,
        classifier=context.RuleClassifier(context._classification_aliases()),
        batch_key=batch_key,
    )
    print(
        context.json.dumps(
            {"ok": not result.errors, "batch_key": batch_key, **result.__dict__},
            ensure_ascii=False,
            indent=2,
        )
    )
    if result.errors and result.queue_pending == 0:
        raise SystemExit(2)


def kol_nitter_materialize(context: ModuleType, _: argparse.Namespace) -> None:
    files = context.materialize_nitter_runtime(context.NITTER_RUNTIME, context.NITTER_TEMPLATE, context.NitterCredentialStore())
    print(
        context.json.dumps(
            {
                "ok": True,
                "config_path": str(files.config_path),
                "sessions_path": str(files.sessions_path),
            },
            ensure_ascii=False,
        )
    )


def kol_nitter_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    docker = context.docker_health()
    nitter = context.timeline_health(context.NITTER_URL)
    checks = {
        "ok": False,
        "docker": docker,
        "nitter": nitter,
        "nitter_url": context.NITTER_URL,
        "nitter_credentials_configured": context.NitterCredentialStore().configured(),
        "xtf_command": str(context.XTF_COMMAND) if context.XTF_COMMAND.exists() else "",
        "xtf_version": context.xtf_version(context.XTF_COMMAND) if context.XTF_COMMAND.exists() else "",
        "config_template": str(context.NITTER_TEMPLATE),
        "config_template_exists": context.NITTER_TEMPLATE.exists(),
    }
    checks["ok"] = bool(
        docker["ready"]
        and nitter["ready"]
        and checks["nitter_credentials_configured"]
        and checks["xtf_command"]
        and checks["config_template_exists"]
    )
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_post_classify(context: ModuleType, args: argparse.Namespace) -> None:
    store = context._post_store()
    aliases = context._classification_aliases()
    ocr_completed, ocr_failed = context.process_pending_ocr_with_budget(
        store,
        context._ocr_classifier(),
        context.RuleClassifier(aliases),
        daily_limit=args.ocr_limit,
        batch_size=10,
    )
    if args.skip_codex:
        store.prepare_model_queue()
        completed = failed = 0
    else:
        try:
            completed, failed = context.classify_pending_in_batches(
                store,
                context.build_batch_post_classifier(
                    context.KOL_BATCH_CLASSIFIER_SCHEMA,
                    context.ROOT,
                    deepseek_credentials=context.DeepSeekCredentialStore(),
                ),
                limit=args.limit,
                daily_limit=args.daily_limit,
            )
        except context.ModelWorkerBusyError:
            print(context.json.dumps({"ok": True, "skipped": "model_worker_busy"}, ensure_ascii=False))
            return
    leads = context._extract_leads_to_market(store)
    print(
        context.json.dumps(
            {
                "ok": True,
                "degraded": bool(ocr_failed or failed),
                "ocr_completed": ocr_completed,
                "ocr_failed": ocr_failed,
                "codex_completed": completed,
                "codex_failed": failed,
                "stock_leads": leads,
                "daily_budget": context.ModelDailyBudget(store, daily_limit=args.daily_limit).status(),
            },
            ensure_ascii=False,
        )
    )
