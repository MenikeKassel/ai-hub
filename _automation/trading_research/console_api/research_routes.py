"""Research evidence/delivery endpoints independent of formal stock approval."""
from datetime import datetime
from fastapi import HTTPException, Query
from pydantic import BaseModel
from kol_tracker import SHANGHAI
from kol_sources.coverage import surface_coverage
from kol_sources.observations import list_observations
from research_topics import candidates, decide_candidate, register_topic
from research_workflow import ResearchWorkflow


class CandidateDecision(BaseModel):
    action: str


class TopicCreate(BaseModel):
    term: str


def register_routes(services):
    app, store = services.app, services.post_store
    workflow = ResearchWorkflow(store)
    app.state.research_workflow = workflow
    # The worker owns only local evidence indexing and never invokes providers.
    from contextlib import asynccontextmanager
    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            workflow.start()
            try:
                yield
            finally:
                workflow.stop()
    app.router.lifespan_context = lifespan

    @app.get('/api/research-digest')
    def digest(review_date: str = '', limit: int = Query(default=100,ge=1,le=500), before_id: int = Query(default=0,ge=0)):
        try:
            return workflow.digest(review_date or datetime.now(SHANGHAI).date().isoformat(),limit=limit,before_id=before_id)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.post('/api/research-digest/{item_id}/acknowledge')
    def acknowledge(item_id: int):
        try:
            return workflow.acknowledge(item_id)
        except KeyError as exc:
            raise HTTPException(404,'current research item not found') from exc

    @app.get('/api/research-index/status')
    def index_status():
        return workflow.status()

    @app.get('/api/collection/surfaces')
    def coverage():
        return surface_coverage(store)

    @app.get('/api/posts/{post_id}/observations')
    def observations(post_id: str):
        if not store.has_post(post_id):
            raise HTTPException(404,'post not found')
        return {'post_id':post_id,'items':list_observations(store,post_id)}

    @app.get('/api/theme-candidates')
    def topic_candidates(limit: int = Query(default=50,ge=1,le=200)):
        return {'items':candidates(store,limit),'discovery_status':workflow.status()['proposal_status']}

    @app.post('/api/research-themes')
    def create_topic(body: TopicCreate):
        try:
            result = register_topic(store,body.term)
            from theme_leads import load_theme_catalog
            workflow.seed(load_theme_catalog(store=store))
            return result
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.post('/api/theme-candidates/{candidate_id}/decision')
    def decision(candidate_id: int, body: CandidateDecision):
        try:
            result = decide_candidate(store,candidate_id,body.action)
            # Queue the catalog change immediately, even if a CLI worker is active.
            from theme_leads import load_theme_catalog
            workflow.seed(load_theme_catalog(store=store))
            return result
        except KeyError as exc:
            raise HTTPException(404,'topic candidate not found') from exc
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
