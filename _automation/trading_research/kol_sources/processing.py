from __future__ import annotations
from typing import Any, Callable, Protocol
from model_budget import ModelDailyBudget, OcrDailyBudget
from .classifiers import CodexPostClassifier
from .core import ModelProviderUnavailableError
from .repository import KolPostStore


def classify_pending_with_codex(
    store: KolPostStore,
    classifier: CodexPostClassifier,
    limit: int = 0,
    *,
    daily_limit: int = 250,
) -> tuple[int, int]:
    completed = 0
    failed = 0
    remaining = max(0, limit)
    budget = ModelDailyBudget(store, daily_limit=daily_limit) if daily_limit else None
    with store.model_worker():
        store.prepare_model_queue()
        while limit == 0 or remaining > 0:
            if budget is not None and budget.status()["remaining"] <= 0:
                break
            candidates = store.claim_posts_for_model(1)
            if not candidates:
                break
            for post in candidates:
                if budget is not None and not budget.reserve():
                    store.release_model_claim(post["post_id"])
                    return completed, failed
                try:
                    payload = classifier.classify(post)
                    store.save_model_classification(
                        post["post_id"],
                        payload,
                        model_name=str(getattr(classifier, "model_name", "codex")),
                        prompt_version=classifier.prompt_version,
                    )

                    completed += 1
                    if budget is not None:
                        budget.finish(success=True)
                except Exception as exc:
                    store.save_model_classification(
                        post["post_id"],
                        None,
                        model_name=str(getattr(classifier, "model_name", "codex")),
                        prompt_version=classifier.prompt_version,
                        error=str(exc),
                    )
                    failed += 1
                    if budget is not None:
                        budget.finish(success=False)
                if limit:
                    remaining -= 1
                    if remaining <= 0:
                        break
    return completed, failed


def classify_pending_in_batches(
    store: KolPostStore,
    classifier: Any,
    limit: int = 0,
    *,
    daily_limit: int = 250,
    batch_size: int = 10,
) -> tuple[int, int]:
    """Drain the durable candidate queue with bounded model batch requests."""
    completed = 0
    failed = 0
    remaining = max(0, int(limit))
    budget = ModelDailyBudget(store, daily_limit=daily_limit) if daily_limit else None
    safe_batch_size = max(1, min(int(batch_size), 20))
    with store.model_worker():
        store.prepare_model_queue()
        while limit == 0 or remaining > 0:
            available = budget.status()["remaining"] if budget is not None else safe_batch_size
            if available <= 0:
                break
            requested = min(safe_batch_size, int(available))
            if limit:
                requested = min(requested, remaining)
            candidates = store.claim_posts_for_model(requested)
            if not candidates:
                break
            claimed: list[dict[str, Any]] = []
            for post in candidates:
                if budget is not None and not budget.reserve():
                    store.release_model_claim(post["post_id"])
                    continue
                claimed.append(post)
            if not claimed:
                break
            stop_after_batch = False
            try:
                results = classifier.classify_many(claimed)
            except Exception as exc:
                results = {}
                batch_error = str(exc)
                stop_after_batch = isinstance(exc, ModelProviderUnavailableError)
            else:
                batch_error = ""
            for post in claimed:
                payload = results.get(post["post_id"]) if isinstance(results, dict) else None
                if isinstance(payload, dict):
                    store.save_model_classification(
                        post["post_id"],
                        payload,
                        model_name=str(getattr(classifier, "model_name", "codex-batch")),
                        prompt_version=classifier.prompt_version,
                    )
                    completed += 1
                    if budget is not None:
                        budget.finish(success=True)
                else:
                    error = batch_error or "batch classifier returned no result"
                    store.save_model_classification(
                        post["post_id"],
                        None,
                        model_name=str(getattr(classifier, "model_name", "codex-batch")),
                        prompt_version=classifier.prompt_version,
                        error=error,
                    )
                    failed += 1
                    if budget is not None:
                        budget.finish(success=False)
            if limit:
                remaining -= len(claimed)
            if stop_after_batch:
                break
    return completed, failed
