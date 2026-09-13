from __future__ import annotations
from .context import ApiServices
from .dependencies import (
    Any,
    DigestAuthorProfileBatch,
    DiscoveryDecisionRequest,
    DiscoveryRunRequest,
    HTTPException,
    KolBackfillRequest,
    KolCreate,
    KolPatch,
    Query,
    sqlite3,
)


def register_routes(services: ApiServices) -> None:
    app = services.app
    post_store = services.post_store
    require_discovery = services.require_discovery

    @app.get("/api/kols")
    def list_kols(status: str | None = None) -> list[dict[str, Any]]:
        return post_store.list_kols(status)


    @app.get("/api/discovery/platforms")
    def discovery_platforms() -> list[dict[str, Any]]:
        _store, _service, registry, _scorer = require_discovery()
        return registry.platforms()


    @app.post("/api/discovery/runs", status_code=201)
    def start_discovery(body: DiscoveryRunRequest) -> dict[str, Any]:
        _store, service, registry, _scorer = require_discovery()
        try:
            platform_info = next(
                item for item in registry.platforms() if item["platform"] == body.platform
            )
            health = platform_info.get("health") or {}
            if not platform_info.get("available"):
                status = str(health.get("status") or "untested")
                reason = str(health.get("reason") or health.get("mode") or "provider is not ready")
                raise HTTPException(
                    status_code=409,
                    detail=f"{body.platform} discovery is {status}: {reason}",
                )
            return service.run(body.platform, body.query, limit=body.limit)
        except KeyError as exc:
            raise HTTPException(503, str(exc)) from exc
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.get("/api/discovery/runs/{run_id}")
    def discovery_run(run_id: str) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.get("/api/discovery/candidates")
    def discovery_candidates(
        state: str | None = None,
        platform: str | None = None,
        query: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        store, _service, _registry, _scorer = require_discovery()
        return store.list_candidates(
            state=state,
            platform=platform,
            query=query,
            limit=limit,
            offset=offset,
        )


    @app.get("/api/discovery/candidates/{candidate_id}")
    def discovery_candidate(candidate_id: str) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.get_candidate(candidate_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.post("/api/discovery/candidates/{candidate_id}/score")
    def score_discovery_candidate(candidate_id: str) -> dict[str, Any]:
        _store, _service, _registry, scorer = require_discovery(require_scorer=True)
        try:
            return scorer.score(candidate_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


    @app.post("/api/discovery/candidates/{candidate_id}/accept")
    def accept_discovery_candidate(
        candidate_id: str,
        body: DiscoveryDecisionRequest,
    ) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.accept(candidate_id, note=body.note)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


    @app.post("/api/discovery/candidates/{candidate_id}/reject")
    def reject_discovery_candidate(
        candidate_id: str,
        body: DiscoveryDecisionRequest,
    ) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.reject(candidate_id, body.note)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


    @app.post("/api/discovery/candidates/{candidate_id}/retry")
    def retry_discovery_candidate(candidate_id: str) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        try:
            return store.retry(candidate_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


    @app.get("/api/discovery/kols/{kol_id}/profile")
    def discovery_kol_profile(kol_id: int) -> dict[str, Any]:
        store, _service, _registry, _scorer = require_discovery()
        kol = post_store.get_kol(kol_id)
        if kol is None:
            raise HTTPException(404, "KOL not found")
        return {"kol": kol, "history": store.profile_history(kol_id)}


    @app.post("/api/discovery/kols/{kol_id}/fetch", status_code=202)
    def discovery_kol_fetch(kol_id: int, body: KolBackfillRequest) -> dict[str, Any]:
        require_discovery()
        try:
            return post_store.queue_backfill(kol_id, body.count)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.get("/api/digest-authors")
    def digest_authors(limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
        return post_store.list_digest_authors(limit)


    @app.put("/api/digest-authors/profiles")
    def bind_digest_author_profiles(body: DigestAuthorProfileBatch) -> dict[str, Any]:
        try:
            items = post_store.upsert_digest_author_profiles(
                [item.model_dump() for item in body.profiles]
            )
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        all_authors = post_store.list_digest_authors(500)
        return {
            "updated": len(items),
            "resolved": sum(bool(item.get("profile_url")) for item in all_authors),
            "unresolved": sum(not item.get("profile_url") for item in all_authors),
            "items": items,
        }


    @app.post("/api/kols", status_code=201)
    def create_kol(body: KolCreate) -> dict[str, Any]:
        try:
            tracking_mode = body.tracking_mode
            if body.platform == "Zhihu" and tracking_mode == "all":
                tracking_mode = "direct_profile"
            kol_id, created = post_store.add_kol(
                body.display_name,
                body.handle,
                body.domain,
                tracking_mode=tracking_mode,
                platform=body.platform,
                profile_url=body.profile_url,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not created:
            raise HTTPException(409, "KOL handle already exists")
        return post_store.get_kol(kol_id) or {}


    @app.patch("/api/kols/{kol_id}")
    def patch_kol(kol_id: int, body: KolPatch) -> dict[str, Any]:
        try:
            values = body.model_dump(exclude_none=True)
            availability = values.pop("availability_status", None)
            reason = values.pop("availability_reason", "")
            result = post_store.update_kol(kol_id, values) if values else post_store.get_kol(kol_id)
            if result is None:
                raise KeyError(f"KOL not found: {kol_id}")
            if availability is not None:
                result = post_store.set_account_availability(
                    kol_id,
                    availability,
                    reason=reason,
                    source="manual_ui",
                )
            return result
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


    @app.post("/api/kols/{kol_id}/backfill")
    def queue_kol_backfill(kol_id: int, body: KolBackfillRequest) -> dict[str, Any]:
        try:
            return post_store.queue_backfill(kol_id, body.count)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

