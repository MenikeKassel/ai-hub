from __future__ import annotations

from typing import Any

from kol_posts import STRUCTURED_REVIEW_VERSION, KolPostStore, RuleClassifier
from recommendation_drafts import RecommendationDraftRepository


def _generation_state(post: dict[str, Any], drafts: list[dict[str, Any]]) -> tuple[str, str]:
    # Superseded drafts are audit history, not a usable materialized result.
    active_drafts = [
        draft for draft in drafts if str(draft.get("status") or "") != "superseded"
    ]
    expected_symbols = {
        str(draft.get("symbol") or "")
        for draft in post.get("drafts") or []
        if isinstance(draft, dict) and str(draft.get("symbol") or "")
    }
    materialized_symbols = {
        str(draft.get("symbol") or "")
        for draft in active_drafts
        if str(draft.get("symbol") or "")
    }
    if expected_symbols - materialized_symbols:
        return "needs_attention", "missing_recommendation_drafts"
    if active_drafts:
        return "generated", ""
    if post.get("model_status") == "failed":
        return "failed", str(post.get("model_error") or post.get("ocr_error") or "model_failed")
    # A rules-only pass can settle clearly irrelevant items before any model
    # call.  Mark them terminal so historical repair does not revisit the
    # same analysis, recap, advertisement, or secondhand item forever.
    content_type = str(post.get("content_type") or "")
    evidence_type = str(post.get("evidence_type") or "")
    if (
        content_type in {"other", "market_view", "methodology"}
        and not bool(post.get("is_candidate"))
    ) or evidence_type in {"retrospective", "secondhand"}:
        return "not_applicable", ""
    if (
        post.get("model_status") == "completed"
        and post.get("content_type") == "recommendation"
        and post.get("evidence_type") == "original_pre_event"
    ):
        return "needs_attention", "recommendation_without_drafts"
    if post.get("model_status") == "completed":
        return "not_applicable", ""
    return "pending", ""


def materialize_recommendation_drafts(
    post_store: KolPostStore,
    draft_repository: RecommendationDraftRepository,
    rule_classifier: RuleClassifier,
    post_id: str,
    *,
    instruments: dict[str, dict[str, Any]],
    queue_scope: str,
    review_date: str,
    rules_first: bool = True,
) -> dict[str, Any]:
    """Run deterministic extraction and materialize the current model result.

    AI providers remain outside this function. They classify unresolved posts;
    every caller then invokes this same materialization seam so a successful
    classification cannot disappear without producing recommendation drafts.
    """
    post = post_store.get_post(post_id)
    structured = None
    if rules_first:
        # Refresh the broad candidate flag before the structured pass. This
        # lets historical repair recover posts that were previously marked
        # non-candidates by an older rule set.
        post_store.save_rule_classification(post_id, rule_classifier.classify(post))
        post = post_store.get_post(post_id)
        structured = rule_classifier.classify_structured_text(post)
    if structured is not None:
        post_store.save_model_classification(
            post_id,
            structured,
            model_name="structured-rules",
            prompt_version=STRUCTURED_REVIEW_VERSION,
        )
        post = post_store.get_post(post_id)

    sync = draft_repository.sync_post(
        post_id,
        instruments,
        queue_scope=queue_scope,
        review_date=review_date,
    )
    drafts = draft_repository.list_drafts(post_id=post_id, limit=100)
    state, error = _generation_state(post, drafts)
    post_store.set_draft_generation_status(
        post_id,
        state,
        version=str(post.get("prompt_version") or STRUCTURED_REVIEW_VERSION),
        error=error,
    )
    return {
        "post_id": post_id,
        "structured": structured is not None,
        "sync": sync,
        "draft_generation_status": state,
        "draft_generation_error": error,
        "drafts": drafts,
    }
