from __future__ import annotations
import argparse
from types import ModuleType


def kol_init(context: ModuleType, _: argparse.Namespace) -> None:
    store = context.KolStore(context.KOL_ROOT)
    created = context.initialize_seed_events(store)
    context.generate_dashboard(store, context.KOL_DASHBOARD)
    print(context.json.dumps({"ok": True, "created": created, "events": len(store.load_events())}, ensure_ascii=False))


def kol_register(context: ModuleType, args: argparse.Namespace) -> None:
    store = context.KolStore(context.KOL_ROOT)
    source_path, source_note = context._source_note_path(args.source_note)
    text = source_path.read_text(encoding="utf-8")
    metadata = context._frontmatter(text)
    source_url = args.source_url or metadata.get("source_url", "") or context._first_url(text)
    platform = args.platform or metadata.get("source_type", "") or metadata.get("platform", "")
    if not platform:
        platform = "X" if context.re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/", source_url) else "web"
    thesis = args.thesis or context._section_summary(text)
    event = context.EventRecord(
        event_id=args.event_id or context._next_event_id(store.load_events()),
        kol_name=args.kol,
        platform=platform,
        source_url=source_url,
        source_note=source_note,
        posted_at=args.posted_at,
        symbol=args.symbol,
        security_name=args.name or "",
        direction=args.direction,
        thesis=thesis,
        status="active",
        activated_at=context.now_iso(),
        updated_at=context.now_iso(),
    )
    errors = context.validate_event(event)
    if errors:
        raise ValueError(f"cannot activate event; missing or invalid: {', '.join(errors)}")
    created = store.register_event(event)
    if created:
        store.queue_notification(
            {
                "kind": "activation",
                "key": f"activation:{event.event_id}",
                "message": f"KOL事件 {event.event_id} 已激活：{event.kol_name} / {event.symbol} {event.security_name}。",
                "event_id": event.event_id,
            }
        )
    context.generate_dashboard(store, context.KOL_DASHBOARD)
    print(context.json.dumps({"ok": True, "created": created, "event_id": event.event_id}, ensure_ascii=False))


def kol_update(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_returns_writes("kol-update", dry_run=bool(args.dry_run))
    context.KOL_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = context.KOL_ROOT / "kol-update.lock"
    lock = context.FileLock(str(lock_path), timeout=1)
    try:
        lock.acquire()
    except context.Timeout:
        print(context.json.dumps({
            "ok": True,
            "status": "already_running",
            "dry_run": bool(args.dry_run),
            "lock": str(lock_path),
        }, ensure_ascii=False))
        return
    try:
        context._kol_update_locked(args)
    finally:
        lock.release()


def kol_surge_alerts(context: ModuleType, args: argparse.Namespace) -> None:
    """Report posts whose mentioned stocks gained within the forward window."""
    from kol_surge_alerts import format_surge_message, scan_surge_alerts
    from kol_tracker import WarehousePriceProvider

    store = context._post_store()
    provider = WarehousePriceProvider(context.MARKET_ROOT / "warehouse")
    state_path = context.KOL_ROOT / "surge_alerts.json"
    window_days = max(1, int(args.window_days))
    threshold = max(0.0, float(args.threshold))
    result = scan_surge_alerts(
        store,
        provider,
        state_path=state_path,
        lookback_days=max(1, int(args.lookback_days)),
        window_days=window_days,
        threshold=threshold,
        dry_run=bool(args.dry_run),
    )
    if args.notify and not args.dry_run and result["new_alerts"]:
        message = format_surge_message(result["new_alerts"], window_days=window_days, threshold=threshold)
        result["notified"] = context._send_feishu(message)
    print(context.json.dumps(result, ensure_ascii=False, indent=2))


def kol_performance_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    context._require_returns_writes("kol-performance-doctor")
    service = context._performance_service()
    migration = service.migrate_event_identities()
    checks = service.doctor()
    checks["migration"] = migration
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if not checks["ok"]:
        raise SystemExit(2)


def kol_performance_refresh(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_returns_writes("kol-performance-refresh")
    as_of = context.date.fromisoformat(args.as_of or context.date.today().isoformat())
    service = context._performance_service()
    migration = service.migrate_event_identities()
    result = service.refresh(as_of=as_of)
    print(context.json.dumps({
        "ok": True,
        "as_of": as_of.isoformat(),
        "migration": migration,
        "run_id": result["run_id"],
        "snapshots_inserted": result["snapshots_inserted"],
        "coverage": result["coverage"],
    }, ensure_ascii=False))


def kol_performance_backfill(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_returns_writes("kol-performance-backfill")
    service = context._performance_service()
    service.migrate_event_identities()
    checkpoints = service.event_store.load_checkpoints()
    dates = sorted({row.get("trade_date", "") for row in checkpoints if row.get("trade_date")})
    start = args.start or (dates[0] if dates else context.date.today().isoformat())
    end = args.end or context.date.today().isoformat()
    targets = [context.date.fromisoformat(value) for value in dates if start <= value <= end]
    if context.date.fromisoformat(end) not in targets:
        targets.append(context.date.fromisoformat(end))
    results = [service.refresh(as_of=value) for value in targets]
    print(context.json.dumps({
        "ok": True,
        "from": start,
        "to": end,
        "as_of_count": len(results),
        "runs": [result["run_id"] for result in results],
        "snapshots_inserted": sum(result["snapshots_inserted"] for result in results),
    }, ensure_ascii=False))


def kol_performance_report(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_returns_writes("kol-performance-report")
    as_of = context.date.fromisoformat(args.as_of or context.date.today().isoformat())
    service = context._performance_service()
    result = service.refresh(as_of=as_of)
    if args.with_ai:
        result = service.explain(result, context.DeepSeekPerformanceInterpreter())
    message = context.build_weekly_message(result)
    sent = context._send_feishu(message) if args.notify else False
    print(context.json.dumps({
        "ok": True,
        "as_of": as_of.isoformat(),
        "weekly": bool(args.weekly),
        "message": message,
        "notification_sent": sent,
        "run_id": result["run_id"],
    }, ensure_ascii=False))


def kol_returns_backfill(context: ModuleType, args: argparse.Namespace) -> None:
    context.kol_update(
        context.argparse.Namespace(
            as_of=args.as_of or context.date.today().isoformat(),
            notify=False,
            dry_run=bool(args.dry_run),
        )
    )


def kol_report(context: ModuleType, _: argparse.Namespace) -> None:
    context._require_returns_writes("kol-report")
    store = context.KolStore(context.KOL_ROOT)
    context.generate_dashboard(store, context.KOL_DASHBOARD)
    print(context.json.dumps({"ok": True, "dashboard": str(context.KOL_DASHBOARD)}, ensure_ascii=False))


def kol_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    store = context.KolStore(context.KOL_ROOT)
    events = store.load_events()
    invalid = {event.event_id: context.validate_event(event) for event in events if context.validate_event(event)}
    checks = {
        "runtime": str(context.KOL_ROOT),
        "runtime_exists": context.KOL_ROOT.exists(),
        "events": len(events),
        "active": sum(event.status == "active" for event in events),
        "marks": len(store.load_marks()),
        "checkpoints": len(store.load_checkpoints()),
        "pending_notifications": len(store.pending_notifications()),
        "invalid_active_events": invalid,
        "dashboard": str(context.KOL_DASHBOARD),
        "dashboard_exists": context.KOL_DASHBOARD.exists(),
        "baostock": context.dependency_available("baostock"),
        "akshare": context.dependency_available("akshare"),
        "pandas": context.dependency_available("pandas"),
    }
    print(context.json.dumps(checks, ensure_ascii=False, indent=2))
    if invalid or not all(checks[name] for name in ["baostock", "akshare", "pandas"]):
        raise SystemExit(2)


def kol_data_refresh(context: ModuleType, args: argparse.Namespace) -> None:
    """Refresh the shared release, then update every KOL consumer from one pointer."""
    lock_path = context.KOL_ROOT / "foundation-refresh.lock"
    lock = context.FileLock(str(lock_path), timeout=1)
    try:
        lock.acquire()
    except context.Timeout:
        print(context.json.dumps({"ok": True, "status": "already_running", "lock": str(lock_path)}))
        return
    try:
        target = context._resolve_foundation_as_of(args.as_of)
        before = context._foundation_status()
        before_as_of = str(before.get("as_of") or "")
        refresh = {"ok": True, "status": "not_needed"}
        refresh_required = bool(
            before_as_of
            and (
                context.date.fromisoformat(before_as_of) < target
                or before.get("coverage_complete") is False
            )
        )
        if refresh_required:
            refresh = context._run_foundation_refresh(target)
        after = context._foundation_status()
        effective_text = str(after.get("as_of") or before_as_of)
        steps: list[dict[str, Any]] = [{"step": "foundation_before", **before}, {"step": "foundation_refresh", **refresh}]
        if effective_text:
            effective = min(target, context.date.fromisoformat(effective_text))
        else:
            effective = target
        update = {
            "ok": False,
            "status": "skipped",
            "reason": "foundation release unavailable",
        }
        if (
            after.get("ok")
            and effective_text
            and (not refresh_required or refresh.get("ok"))
        ):
            command = [
                context.sys.executable,
                str(context.Path(context.__file__).resolve()),
                "kol-update",
                "--as-of",
                effective.isoformat(),
            ]
            if args.notify:
                command.append("--notify")
            if args.dry_run:
                command.append("--dry-run")
            try:
                completed = context.subprocess.run(
                    command,
                    cwd=str(context.ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=float(context.os.environ.get("KOL_UPDATE_TIMEOUT_SECONDS", "1800")),
                    check=False,
                )
                update = {
                    "ok": completed.returncode == 0,
                    "status": "updated" if completed.returncode == 0 else "failed",
                    "effective_as_of": effective.isoformat(),
                    "output_tail": (completed.stdout or completed.stderr or "")[-4000:],
                }
            except context.subprocess.TimeoutExpired:
                update = {
                    "ok": False,
                    "status": "timed_out",
                    "effective_as_of": effective.isoformat(),
                }
        technical = {
            "ok": False,
            "status": "skipped",
            "reason": "returns update did not complete",
        }
        if update.get("ok") and not args.dry_run:
            context_result = context._backfill_event_contexts(
                context._market_store(),
                context.KolStore(context.KOL_ROOT),
                stale_only=True,
            )
            technical = {
                "ok": bool(context_result.get("ok")),
                "status": "updated" if context_result.get("ok") else "failed",
                "created": len(context_result.get("created", [])),
                "updated": len(context_result.get("updated", [])),
                "skipped": len(context_result.get("skipped", [])),
                "pending": len(context_result.get("pending", [])),
                "errors": context_result.get("errors", []),
            }
        elif args.dry_run:
            technical = {"ok": True, "status": "dry_run"}
        steps.extend([
            {"step": "foundation_after", **after},
            {"step": "kol_update", **update},
            {"step": "technical_context", **technical},
        ])
        payload = {
            "ok": bool(
                after.get("ok")
                and update.get("ok")
                and technical.get("ok")
                and (not refresh_required or refresh.get("ok"))
            ),
            "status": (
                "completed"
                if update.get("ok") and technical.get("ok") and (not refresh_required or refresh.get("ok"))
                else "degraded"
            ),
            "requested_as_of": target.isoformat(),
            "effective_as_of": effective.isoformat(),
            "foundation_release_id": after.get("release_id") or before.get("release_id", ""),
            "steps": steps,
        }
        print(context.json.dumps(payload, ensure_ascii=False, indent=2))
        if not payload["ok"]:
            raise SystemExit(2)
    finally:
        lock.release()


def kol_context_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    market = context._market_store()
    events = [event for event in context.KolStore(context.KOL_ROOT).load_events() if event.status in {"active", "completed"}]
    current = []
    pending = []
    failed = []
    for event in events:
        value = market.get_event_technical_context(
            event.event_id,
            input_hash=context.event_context_input_hash(event.symbol, event.posted_at),
        )
        if value is None or value.get("status") == "pending":
            pending.append(event.event_id)
        if value is not None:
            current.append(value)
            if value.get("status") == "failed":
                failed.append({"event_id": event.event_id, "error": value.get("error", "")})
    with market.connect() as db:
        schema_version = int(db.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)
    payload = {
        "ok": schema_version >= 4 and not failed,
        "feature_version": context.FEATURE_VERSION,
        "schema_version": schema_version,
        "formal_events": len(events),
        "contexts": len(current),
        "complete": sum(value.get("status") == "complete" for value in current),
        "partial": sum(value.get("status") == "partial" for value in current),
        "pending_event_ids": pending,
        "failed": failed,
    }
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_context_backfill(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_research_writes("kol-context-backfill")
    event_ids = {args.event_id} if args.event_id else None
    event_store = context.KolStore(context.KOL_ROOT)
    if event_ids and not any(event.event_id in event_ids for event in event_store.load_events()):
        raise SystemExit(f"event not found: {args.event_id}")
    payload = context._backfill_event_contexts(
        context._market_store(),
        event_store,
        event_ids=event_ids,
        force=args.force,
    )
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_event_data_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    event_store = context.KolStore(context.KOL_ROOT)
    market_store = context._market_store()
    service = context.EventDossierService(context._post_store(), event_store, market_store)
    events = [event for event in event_store.load_events() if event.status in {"active", "completed"}]
    snapshots = market_store.list_event_dossier_snapshots()
    latest: dict[str, dict[str, Any]] = {}
    for item in snapshots:
        event_id = str(item.get("event_id") or "")
        if event_id and event_id not in latest:
            latest[event_id] = item
    status_counts: context.Counter[str] = context.Counter()
    errors: list[dict[str, str]] = []
    for event in events:
        snapshot = latest.get(event.event_id)
        if snapshot:
            snapshot_status = str(snapshot.get("status") or "unknown")
            status_counts[snapshot_status] += 1
            if snapshot_status == "failed":
                errors.append({"event_id": event.event_id, "error": "latest dossier snapshot status=failed"})
            continue
        try:
            dossier_status = str(service.build(event.event_id).get("status") or "unknown")
            status_counts[dossier_status] += 1
            if dossier_status == "failed":
                errors.append({"event_id": event.event_id, "error": "dossier status=failed"})
        except Exception as exc:
            status_counts["failed"] += 1
            errors.append({"event_id": event.event_id, "error": str(exc)[:1000]})
    with market_store.connect() as db:
        schema_version = int(db.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0)
    payload = {
        "ok": schema_version >= 7 and not errors,
        "schema_version": schema_version,
        "formal_events": len(events),
        "snapshots": len(snapshots),
        "status_counts": dict(status_counts),
        "missing_snapshot_event_ids": [event.event_id for event in events if event.event_id not in latest],
        "errors": errors,
    }
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_event_data_backfill(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_research_writes("kol-event-data-backfill")
    event_store = context.KolStore(context.KOL_ROOT)
    market_store = context._market_store()
    service = context.EventDossierService(context._post_store(), event_store, market_store)
    wanted = {args.event_id} if args.event_id else None
    events = [
        event for event in event_store.load_events()
        if event.status in {"active", "completed"}
        and (wanted is None or event.event_id in wanted)
    ]
    if wanted and not events:
        raise SystemExit(f"event not found: {args.event_id}")
    snapshots: dict[str, dict[str, Any]] = {}
    for item in market_store.list_event_dossier_snapshots():
        event_id = str(item.get("event_id") or "")
        if event_id and event_id not in snapshots:
            snapshots[event_id] = item
    result: dict[str, Any] = {"processed": 0, "created": [], "skipped": [], "errors": []}
    for event in events:
        if args.missing_only and event.event_id in snapshots and snapshots[event.event_id].get("status") not in {"failed", "pending"}:
            result["skipped"].append(event.event_id)
            continue
        result["processed"] += 1
        try:
            dossier = service.refresh(event.event_id)
            result["created"].append({"event_id": event.event_id, "status": dossier["status"]})
        except Exception as exc:
            result["errors"].append({"event_id": event.event_id, "error": str(exc)[:1000]})
    result["ok"] = not result["errors"]
    print(context.json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(2)


def kol_method_research_doctor(context: ModuleType, _: argparse.Namespace) -> None:
    service, provider = context._method_research_service(
        with_market_providers=False,
        with_ai=True,
    )
    try:
        payload = service.doctor()
        interpreter = service.interpreter
        primary_available = context.DeepSeekCredentialStore().configured()
        payload["ai"] = {
            "primary": str(interpreter.model_name) if interpreter else "",
            "prompt_version": (
                str(interpreter.prompt_version) if interpreter else ""
            ),
            "primary_available": primary_available,
            "backup": "",
            "backup_configured": False,
            "automatic_codex_fallback": False,
        }
        payload["ok"] = bool(payload["ok"] and primary_available)
    finally:
        if provider is not None:
            provider.close()
    print(context.json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_method_research_backfill(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_research_writes("kol-method-research-backfill")
    service, provider = context._method_research_service(
        with_market_providers=not args.skip_cross_section or args.with_minute,
        with_ai=args.with_ai,
    )
    event_store = service.event_store
    events = context._selected_formal_events(event_store, event_id=args.event_id)
    result: dict[str, Any] = {
        "ok": True,
        "formal_events": len(events),
        "processed": 0,
        "created": [],
        "skipped": [],
        "errors": [],
        "ai": None,
    }
    try:
        for event in events:
            existing = service.market_store.get_event_method_research(event.event_id)
            if existing and args.missing_only and not args.force:
                result["skipped"].append(event.event_id)
                continue
            result["processed"] += 1
            print(
                f"event-research {result['processed']}/{len(events)} {event.event_id}",
                file=context.sys.stderr,
                flush=True,
            )
            try:
                value = service.refresh(
                    event.event_id,
                    fetch_cross_section=not args.skip_cross_section,
                    fetch_minute=args.with_minute,
                )
                result["created"].append(
                    {
                        "event_id": event.event_id,
                        "snapshot_id": value["snapshot_id"],
                        "created": value["created"],
                        "status": value["research"]["status"],
                        "fetch": value["fetch"],
                    }
                )
            except Exception as exc:
                result["errors"].append(
                    {"event_id": event.event_id, "error": str(exc)[:2000]}
                )
        if args.with_ai:
            result["ai"] = context._run_method_ai_batches(
                service,
                event_ids=[event.event_id for event in events],
                max_events=args.max_ai,
            )
            result["errors"].extend(result["ai"].get("errors") or [])
    finally:
        if provider is not None:
            provider.close()
    result["ok"] = not result["errors"]
    print(context.json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["ok"]:
        raise SystemExit(2)


def kol_method_research_run(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_research_writes("kol-method-research-run")
    service, provider = context._method_research_service(
        with_market_providers=True,
        with_ai=not args.skip_ai,
    )
    events = context._selected_formal_events(service.event_store, event_id=args.event_id)
    pending = [
        event
        for event in events
        if service.market_store.get_event_method_research(event.event_id) is None
        or not service.has_ready_interpretation(
            event.event_id,
            str(
                (
                    service.market_store.get_event_method_research(
                        event.event_id
                    )
                    or {}
                ).get("snapshot_id")
                or ""
            ),
        )
    ][: max(1, args.max_events)]
    result: dict[str, Any] = {
        "ok": True,
        "selected": len(pending),
        "objective": [],
        "ai": None,
        "errors": [],
    }
    try:
        for event in pending:
            print(
                f"event-research {len(result['objective']) + len(result['errors']) + 1}/{len(pending)} {event.event_id}",
                file=context.sys.stderr,
                flush=True,
            )
            try:
                value = service.refresh(
                    event.event_id,
                    fetch_cross_section=True,
                    fetch_minute=args.with_minute,
                )
                result["objective"].append(
                    {
                        "event_id": event.event_id,
                        "snapshot_id": value["snapshot_id"],
                        "status": value["research"]["status"],
                    }
                )
            except Exception as exc:
                result["errors"].append(
                    {"event_id": event.event_id, "error": str(exc)[:2000]}
                )
        if not args.skip_ai:
            result["ai"] = context._run_method_ai_batches(
                service,
                event_ids=[event.event_id for event in pending],
                max_events=len(pending),
            )
            result["errors"].extend(result["ai"].get("errors") or [])
    finally:
        if provider is not None:
            provider.close()
    result["ok"] = not result["errors"]
    print(context.json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result["ok"]:
        raise SystemExit(2)


def kol_intraday_backfill(context: ModuleType, args: argparse.Namespace) -> None:
    context._require_research_writes("kol-intraday-backfill")
    event_store = context.KolStore(context.KOL_ROOT)
    event_ids = None if args.all else {args.event_id}
    if event_ids and not any(event.event_id in event_ids for event in event_store.load_events()):
        raise SystemExit(f"event not found: {args.event_id}")
    payload = context.backfill_event_intraday(
        context._market_store(),
        event_store,
        event_ids=event_ids,
        force=args.force,
    )
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)


def kol_intraday_audit(context: ModuleType, _: argparse.Namespace) -> None:
    payload = context.audit_event_intraday(context._market_store(), context.KolStore(context.KOL_ROOT))
    print(context.json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(2)
