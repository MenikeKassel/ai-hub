from __future__ import annotations
from .context import ApiServices
from .dependencies import (
    Any,
    BackgroundTasks,
    EventAmendmentRequest,
    EventMethodResearchService,
    EventUpdateRequest,
    FEATURE_VERSION,
    FreeStockDBMarketProvider,
    HTTPException,
    Instrument,
    MARK_FIELDS,
    Query,
    ROOT,
    backfill_event_intraday,
    date,
    event_amendment_changes,
    event_context_input_hash,
    now_iso,
    re,
    replace,
    run_post_approval_refresh,
    validate_event,
)


def run_post_approval_refresh(*args, **kwargs):
    import kol_api
    return kol_api.run_post_approval_refresh(*args, **kwargs)


def register_routes(services: ApiServices) -> None:
    app = services.app
    assert_research_updates_allowed = services.assert_research_updates_allowed
    config = services.config
    event_dossier = services.event_dossier
    event_research = services.event_research
    event_store = services.event_store
    market_store = services.market_store
    market_writes_enabled = services.market_writes_enabled
    post_store = services.post_store
    research_writes_enabled = services.research_writes_enabled
    returns_writes_enabled = services.returns_writes_enabled
    technical_context_for = services.technical_context_for

    @app.get("/api/events")
    def list_events() -> list[dict[str, Any]]:
        events = event_store.load_events()
        latest: dict[str, dict[str, str]] = {}
        for mark in event_store.load_marks():
            if mark["event_id"] not in latest or mark["trade_date"] > latest[mark["event_id"]]["trade_date"]:
                latest[mark["event_id"]] = mark

        technical_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        list_technical = getattr(market_store, "list_event_technical_contexts", None)
        if callable(list_technical):
            for row in list_technical():
                key = (str(row.get("event_id") or ""), str(row.get("input_hash") or ""))
                technical_by_key.setdefault(key, row)

        intraday_by_event: dict[str, dict[str, Any]] = {}
        list_intraday = getattr(market_store, "list_event_intraday_contexts", None)
        if callable(list_intraday):
            for row in list_intraday():
                intraday_by_event.setdefault(str(row.get("event_id") or ""), row)

        source_post_ids: dict[str, str] = {}
        for event in events:
            source_post_id = ""
            if event.source_note.startswith("post:"):
                source_post_id = event.source_note.removeprefix("post:").strip()
            if not re.fullmatch(r"\d{5,25}", source_post_id):
                match = re.search(r"/status/(\d{5,25})", event.source_url)
                source_post_id = match.group(1) if match else ""
            source_post_ids[event.event_id] = source_post_id
        list_followups = getattr(post_store, "list_retrospective_followups_by_source", None)
        followups_by_source = (
            list_followups(set(source_post_ids.values()))
            if callable(list_followups)
            else {}
        )

        values = []
        for event in events:
            source_post_id = source_post_ids[event.event_id]
            input_hash = event_context_input_hash(event.symbol, event.posted_at)
            values.append(
                {
                    **event.to_row(),
                    "latest_mark": latest.get(event.event_id),
                    "technical_context": technical_by_key.get((event.event_id, input_hash)),
                    "intraday_context": intraday_by_event.get(event.event_id),
                    "followups": followups_by_source.get(source_post_id, []),
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
            current = next(
                (event for event in event_store.load_events() if event.event_id == event_id),
                None,
            )
            if current is None:
                raise KeyError(f"event not found: {event_id}")
            effective_changes = event_amendment_changes(current, changes)
            recalculation_fields = {"posted_at", "symbol", "direction"}
            if recalculation_fields.intersection(effective_changes) and not returns_writes_enabled():
                raise HTTPException(
                    409,
                    "收益自动更新当前未启用；本次修订会撤下已有收益。请先恢复收益更新后再重试。",
                )
            updated, revision = event_store.amend_event(
                event_id,
                effective_changes,
                reason=body.reason,
            )
            refresh_status = "not_required"
            refresh_error = ""
            if revision.get("recalculation_required"):
                if not returns_writes_enabled():
                    refresh_status = "disabled"
                else:
                    try:
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
                    except Exception as exc:
                        refresh_status = "failed"
                        refresh_error = str(exc)[:1000]
            return {
                "event": updated.to_row(),
                "revision": revision,
                "refresh_status": refresh_status,
                "refresh_error": refresh_error,
            }
        except HTTPException:
            raise
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

