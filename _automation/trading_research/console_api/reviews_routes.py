from __future__ import annotations
from .context import ApiServices
from .dependencies import (
    Any,
    BackgroundTasks,
    DeepSeekPostClassifier,
    HTTPException,
    Instrument,
    ManualRecommendationDraftRequest,
    ModelWorkerBusyError,
    Query,
    ROOT,
    RecommendationDraftAction,
    RecommendationDraftBulkApproveRequest,
    RecommendationDraftBulkPreviewRequest,
    RecommendationDraftPatch,
    ReviewQueueService,
    ReviewRequest,
    RuleClassifier,
    STRUCTURED_REVIEW_VERSION,
    _sanitize_post,
    approve_post,
    asdict,
    date,
    materialize_recommendation_drafts,
    post_review_lock,
    review_window_utc,
    run_post_approval_refresh,
)


def run_post_approval_refresh(*args, **kwargs):
    # Resolve through the compatibility facade at call time so existing
    # operators/tests patching kol_api.run_post_approval_refresh still work.
    import kol_api
    return kol_api.run_post_approval_refresh(*args, **kwargs)


def register_routes(services: ApiServices) -> None:
    app = services.app
    approve_draft_record = services.approve_draft_record
    classification_aliases = services.classification_aliases
    config = services.config
    confirm_approval_leads = services.confirm_approval_leads
    deepseek_credentials = services.deepseek_credentials
    event_dossier = services.event_dossier
    event_store = services.event_store
    market_store = services.market_store
    market_writes_enabled = services.market_writes_enabled
    post_store = services.post_store
    recommendation_drafts = services.recommendation_drafts
    reprocess_recommendation_post = services.reprocess_recommendation_post
    research_writes_enabled = services.research_writes_enabled
    returns_writes_enabled = services.returns_writes_enabled
    review_agent = services.review_agent
    sanitize_recommendation_draft = services.sanitize_recommendation_draft

    @app.get("/api/review-queue")
    def review_queue(review_date: date = Query(default_factory=date.today),
                     scope: str = Query(default="morning", pattern="^morning$"),
                     view: str = Query(default="pending", pattern="^(new|processed|pending|failed|approved)$"),
                     attention_only: bool = False, page: int = Query(default=1, ge=1),
                     page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
        result = ReviewQueueService(post_store).page(review_date.isoformat(), scope=scope, view=view,
            attention_only=attention_only, page=page, page_size=page_size)
        result["delivery"] = recommendation_drafts.morning_delivery(review_date.isoformat())
        return result


    @app.get("/api/review-queue/{post_id}")
    def review_queue_detail(post_id: str) -> dict[str, Any]:
        try:
            post = post_store.get_post(post_id)
        except KeyError as exc:
            raise HTTPException(404, "post not found") from exc
        return {"post": _sanitize_post(post), "drafts": [sanitize_recommendation_draft(d)
            for d in recommendation_drafts.list_drafts(post_id=post_id, limit=1000) if d["status"] != "superseded"]}


    @app.get("/api/posts")
    def list_posts(
        review_status: str | None = None,
        kol_id: int | None = None,
        candidate_only: bool = False,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [
            _sanitize_post(value)
            for value in post_store.list_posts(
                review_status=review_status,
                kol_id=kol_id,
                candidate_only=candidate_only,
                limit=limit,
                offset=offset,
            )
        ]


    @app.get("/api/posts/{post_id}")
    def get_post(post_id: str) -> dict[str, Any]:
        try:
            return _sanitize_post(post_store.get_post(post_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.post("/api/posts/{post_id}/classify")
    def classify_post(post_id: str) -> dict[str, Any]:
        try:
            post_store.get_post(post_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            result = reprocess_recommendation_post(post_id)
        except ModelWorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"recommendation processing failed: {exc}") from exc
        response = _sanitize_post(post_store.get_post(post_id))
        response["recommendation_drafts"] = [
            sanitize_recommendation_draft(item) for item in result["drafts"]
        ]
        response["draft_generation_status"] = result["draft_generation_status"]
        return response


    @app.post("/api/posts/{post_id}/recommendation-reprocess")
    def reprocess_recommendation(post_id: str) -> dict[str, Any]:
        try:
            result = reprocess_recommendation_post(post_id)
            return {
                "ok": True,
                "post": _sanitize_post(post_store.get_post(post_id)),
                "draft_generation_status": result["draft_generation_status"],
                "draft_generation_error": result["draft_generation_error"],
                "drafts": [sanitize_recommendation_draft(item) for item in result["drafts"]],
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ModelWorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"recommendation reprocess failed: {exc}") from exc


    @app.get("/api/morning-review")
    def morning_review(
        review_date: date = Query(default_factory=date.today),
        status: str | None = Query(default=None, pattern="^(ready|needs_attention|approved|rejected)$"),
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        include_history: bool = Query(default=False),
        history_page: int = Query(default=1, ge=1),
        history_page_size: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        day = review_date.isoformat()
        drafts = recommendation_drafts.list_drafts(
            review_date=day,
            status=status,
            queue_scope="morning",
            limit=limit,
            offset=offset,
        )
        window_start, window_end = review_window_utc(day)
        posts = post_store.list_posts(
            posted_from_utc=window_start,
            posted_to_utc=window_end,
            limit=500,
        )
        approved_drafts = recommendation_drafts.list_drafts(
            reviewed_date=day,
            status="approved",
            limit=1000,
        )
        history_drafts: list[dict[str, Any]] = []
        if include_history:
            history_drafts = recommendation_drafts.list_drafts(
                status=status,
                queue_scope="backlog",
                limit=history_page_size,
                offset=(history_page - 1) * history_page_size,
            )
        return {
            "review_date": day,
            "summary": recommendation_drafts.morning_summary(day),
            "delivery": recommendation_drafts.morning_delivery(day),
            "posts": [_sanitize_post(item) for item in posts],
            "drafts": [sanitize_recommendation_draft(item) for item in drafts],
            "history_drafts": [sanitize_recommendation_draft(item) for item in history_drafts],
            "history_page": history_page,
            "history_page_size": history_page_size,
            "history_has_more": len(history_drafts) == history_page_size,
            "approved_drafts": [sanitize_recommendation_draft(item) for item in approved_drafts],
        }


    @app.post("/api/recommendation-drafts/bulk-preview")
    def bulk_preview_recommendation_drafts(
        body: RecommendationDraftBulkPreviewRequest,
    ) -> dict[str, Any]:
        try:
            return {
                **recommendation_drafts.create_bulk_snapshot(
                    review_date=body.review_date,
                    queue_scope=body.queue_scope,
                    status_filter=body.status,
                    limit=body.limit,
                ),
                "ok": True,
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.post("/api/recommendation-drafts/bulk-approve")
    def bulk_approve_recommendation_drafts(
        body: RecommendationDraftBulkApproveRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            snapshot = recommendation_drafts.consume_bulk_snapshot(body.snapshot_token)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        approved: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        symbols: set[str] = set()
        event_ids: list[str] = []
        for raw_id in snapshot["draft_ids"]:
            draft_id = int(raw_id)
            try:
                current = recommendation_drafts.get_draft(draft_id)
                if current["status"] != "ready" or current.get("attention_reasons"):
                    skipped.append({
                        "draft_id": draft_id,
                        "reason": "changed_since_preview",
                        "status": current["status"],
                        "attention_reasons": current.get("attention_reasons", []),
                    })
                    continue
                result, approval = approve_draft_record(draft_id, body.note)
                symbols.add(str(result["symbol"]))
                if approval is not None:
                    event_ids.extend(approval.event_ids)
                approved.append({
                    "draft_id": draft_id,
                    "event_id": result.get("event_id") or (approval.event_ids[0] if approval else ""),
                    "status": result["status"],
                    "symbol": result["symbol"],
                })
            except KeyError as exc:
                failed.append({"draft_id": draft_id, "error": str(exc)})
            except Exception as exc:
                failed.append({"draft_id": draft_id, "error": str(exc)[:1000]})
        if symbols and returns_writes_enabled():
            background_tasks.add_task(
                run_post_approval_refresh,
                config.runtime_root,
                ROOT,
                sorted(symbols),
                as_of=date.today(),
            )
        if event_ids and research_writes_enabled():
            background_tasks.add_task(
                lambda ids=sorted(set(event_ids)): [event_dossier.refresh(event_id) for event_id in ids]
            )
        return {
            "ok": not failed,
            "snapshot_consumed": True,
            "processed": len(approved) + len(skipped) + len(failed),
            "approved": approved,
            "skipped": skipped,
            "failed": failed,
            "queued_symbols": sorted(symbols),
            "refresh_status": "queued" if symbols else "not_needed",
        }


    @app.get("/api/recommendation-drafts/{draft_id}")
    def get_recommendation_draft(draft_id: int) -> dict[str, Any]:
        try:
            return sanitize_recommendation_draft(recommendation_drafts.get_draft(draft_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.get("/api/recommendation-drafts/{draft_id}/revisions")
    def recommendation_draft_revisions(draft_id: int) -> list[dict[str, Any]]:
        try:
            recommendation_drafts.get_draft(draft_id)
            return recommendation_drafts.list_revisions(draft_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.post("/api/posts/{post_id}/recommendation-drafts", status_code=201)
    def create_manual_recommendation_draft(
        post_id: str,
        body: ManualRecommendationDraftRequest,
    ) -> dict[str, Any]:
        values = body.model_dump(exclude={"review_date", "correction_type", "note"})
        try:
            created = recommendation_drafts.create_manual_draft(
                post_id,
                values,
                market_store.instrument_map(),
                review_date=body.review_date,
                correction_type=body.correction_type,
                note=body.note,
            )
            return sanitize_recommendation_draft(created)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.patch("/api/recommendation-drafts/{draft_id}")
    def patch_recommendation_draft(
        draft_id: int,
        body: RecommendationDraftPatch,
    ) -> dict[str, Any]:
        values = body.model_dump(exclude_none=True)
        note = str(values.pop("note", ""))
        correction_type = values.pop("correction_type", None)
        try:
            updated = recommendation_drafts.update_draft(
                draft_id,
                values,
                market_store.instrument_map(),
                note=note,
                correction_type=correction_type,
            )
            return sanitize_recommendation_draft(updated)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.post("/api/recommendation-drafts/{draft_id}/approve")
    def approve_draft(
        draft_id: int,
        body: RecommendationDraftAction,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            current = recommendation_drafts.get_draft(draft_id)
            approved, result = approve_draft_record(draft_id, body.note)
            if result is None:
                return sanitize_recommendation_draft(approved)
            refresh_status = "not_requested"
            if returns_writes_enabled():
                background_tasks.add_task(
                    run_post_approval_refresh,
                    config.runtime_root,
                    ROOT,
                    [approved["symbol"]],
                    as_of=date.today(),
                )
                refresh_status = "queued"
            if research_writes_enabled():
                background_tasks.add_task(event_dossier.refresh, result.event_ids[0])
            return {
                **sanitize_recommendation_draft(approved),
                "created_event": bool(result.created_events),
                "refresh_status": refresh_status,
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc


    @app.post("/api/recommendation-drafts/{draft_id}/reject")
    def reject_draft(draft_id: int, body: RecommendationDraftAction) -> dict[str, Any]:
        try:
            return sanitize_recommendation_draft(recommendation_drafts.reject(draft_id, body.note))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


    @app.post("/api/recommendation-drafts/{draft_id}/retry")
    def retry_draft(draft_id: int) -> dict[str, Any]:
        try:
            current = recommendation_drafts.get_draft(draft_id)
            if current["queue_scope"] != "morning":
                raise ValueError("historical drafts are audit-only and cannot be retried")
            with post_store.model_worker():
                claimed = post_store.claim_posts_for_model(
                    1,
                    post_id=current["post_id"],
                    force=True,
                )
                if not claimed:
                    raise ModelWorkerBusyError("post classification is already running")
                post = claimed[0]
                classifier = RuleClassifier(classification_aliases())
                payload = classifier.classify_structured_text(post)
                model_name = "structured-rules"
                prompt_version = STRUCTURED_REVIEW_VERSION
                if payload is None:
                    model = DeepSeekPostClassifier(
                        config.codex_schema,
                        deepseek_credentials,
                    )
                    payload = model.classify(post)
                    model_name = model.model_name
                    prompt_version = model.prompt_version
                post_store.save_model_classification(
                    current["post_id"],
                    payload,
                    model_name=model_name,
                    prompt_version=prompt_version,
                )
                result = materialize_recommendation_drafts(
                    post_store,
                    recommendation_drafts,
                    classifier,
                    current["post_id"],
                    instruments=market_store.instrument_map(),
                    queue_scope=current["queue_scope"],
                    review_date=current["review_date"],
                    rules_first=False,
                )
            return {
                "ok": True,
                "drafts": [
                    sanitize_recommendation_draft(item)
                    for item in result["drafts"]
                ],
                "draft_generation_status": result["draft_generation_status"],
            }
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ModelWorkerBusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"draft retry failed: {exc}") from exc


    @app.post("/api/posts/{post_id}/review")
    def review_post(
        post_id: str,
        body: ReviewRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        if body.action == "approve":
            try:
                queued_symbols: list[str] = []
                if body.confirm_leads:
                    queued_symbols = confirm_approval_leads(post_id, body.drafts, body.note)
                confirmed_symbols = {
                    str(lead["symbol"])
                    for lead in post_store.list_stock_leads(
                        post_id=post_id,
                        status="confirmed",
                        limit=1000,
                    )
                }
                draft_symbols = {draft.symbol for draft in body.drafts}
                missing = sorted(draft_symbols - confirmed_symbols)
                if missing:
                    raise ValueError(
                        "请先在股票线索中确认这些标的，再批准推荐事件："
                        + ", ".join(missing)
                    )
                result = approve_post(
                    post_store,
                    event_store,
                    post_id,
                    [draft.model_dump() for draft in body.drafts],
                    note=body.note,
                )
                review_agent.mark_overridden_for_post(
                    post_id,
                    "approved",
                    [draft.model_dump() for draft in body.drafts],
                )
                if market_writes_enabled():
                    for draft in body.drafts:
                        instrument = market_store.get_instrument(draft.symbol)
                        if instrument is None:
                            continue
                        market_store.upsert_instrument(
                            Instrument(
                                symbol=draft.symbol,
                                name=draft.security_name or instrument["name"],
                                instrument_type=instrument["instrument_type"],
                                exchange=instrument["exchange"],
                                status=instrument["status"],
                                list_date=instrument["list_date"],
                                lifecycle="pinned",
                                source="kol_event",
                                first_seen_at=instrument["first_seen_at"],
                                last_mentioned_at=str(post_store.get_post(post_id)["posted_at"])[:10],
                            )
                        )
                refresh_status = "not_requested"
                if body.refresh_returns and queued_symbols and returns_writes_enabled():
                    background_tasks.add_task(
                        run_post_approval_refresh,
                        config.runtime_root,
                        ROOT,
                        queued_symbols,
                        as_of=date.today(),
                    )
                    refresh_status = "queued"
                for event_id in result.event_ids:
                    if research_writes_enabled():
                        background_tasks.add_task(event_dossier.refresh, event_id)
                return {
                    **asdict(result),
                    "queued_symbols": queued_symbols,
                    "refresh_status": refresh_status,
                }
            except KeyError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, str(exc)) from exc
        action_map = {"exclude": "excluded", "ignore": "ignored", "pending": "pending"}
        if body.action not in action_map:
            raise HTTPException(422, "invalid review action")
        try:
            with post_review_lock(post_store, post_id):
                post = post_store.get_post(post_id)
                if post["review_status"] == "approved":
                    raise HTTPException(409, "approved posts require explicit event reconciliation")
                target_status = action_map[body.action]
                if post["review_status"] == target_status:
                    return _sanitize_post(post)
                post_store.set_review(post_id, target_status, body.note)
                review_agent.mark_overridden_for_post(post_id, target_status)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _sanitize_post(post_store.get_post(post_id))

