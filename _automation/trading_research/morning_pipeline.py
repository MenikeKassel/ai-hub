from __future__ import annotations

import time
from datetime import date, datetime, time as clock_time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from kol_posts import (
    STRUCTURED_REVIEW_VERSION,
    KolPostStore,
    ModelProviderUnavailableError,
    RuleClassifier,
)
from recommendation_processing import materialize_recommendation_drafts
from recommendation_drafts import RecommendationDraftRepository


SHANGHAI = ZoneInfo("Asia/Shanghai")
MIN_MODEL_ATTEMPT_SECONDS = 90


class MorningPipeline:
    def __init__(
        self,
        post_store: KolPostStore,
        market_store: Any,
        *,
        rule_classifier: RuleClassifier,
        batch_classifier: Any,
        ocr_classifier: Any | None = None,
        fetcher: Callable[[], Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        active_kol_count: int | None = None,
    ):
        self.post_store = post_store
        self.market_store = market_store
        self.rule_classifier = rule_classifier
        self.batch_classifier = batch_classifier
        self.ocr_classifier = ocr_classifier
        self.fetcher = fetcher
        self.now_provider = now_provider or (lambda: datetime.now(SHANGHAI))
        self.active_kol_count = active_kol_count
        self.drafts = RecommendationDraftRepository(post_store)

    def run(
        self,
        *,
        as_of: date,
        fetch: bool = True,
        backlog_limit: int = 20,
        max_runtime_minutes: float = 85,
        phase: str = "initial",
    ) -> dict[str, Any]:
        scheduled_end = datetime.combine(as_of, clock_time(9, 0), SHANGHAI)
        current = self.now_provider()
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        current = current.astimezone(SHANGHAI)
        # A delivery owns a fixed 09:00-to-09:00 window. A late catch-up must
        # not pull posts from the next delivery into an already closed review.
        window_end = scheduled_end
        window_start = scheduled_end - timedelta(days=1)
        run_id = self.drafts.start_morning_run(
            as_of.isoformat(),
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            phase=phase,
        )
        deadline = time.monotonic() + max(1, max_runtime_minutes * 60)
        stages: dict[str, int] = {
            "fetched_posts": 0,
            "reviewed_posts": 0,
            "ready_drafts": 0,
            "attention_drafts": 0,
            "failed_posts": 0,
            "ocr_completed": 0,
            "ocr_failed": 0,
            "active_kols": (
                self.active_kol_count
                if self.active_kol_count is not None
                else len(self.post_store.list_kols("active"))
            ),
            "successful_kols": 0,
            "failed_kols": 0,
            "platform_breakdown": {},
        }
        errors: list[str] = []
        try:
            self.drafts.update_morning_run(
                run_id,
                stage="starting",
                progress_total=stages["active_kols"],
                stages=stages,
            )
            if fetch and self.fetcher:
                try:
                    self.drafts.update_morning_run(
                        run_id,
                        stage="fetching",
                        progress_total=stages["active_kols"],
                        stages=stages,
                    )
                    fetched = self.fetcher()
                    stages["fetched_posts"] = int(
                        getattr(fetched, "new_posts", 0)
                        if not isinstance(fetched, dict)
                        else fetched.get("new_posts", 0)
                    )
                    stages["successful_kols"] = int(
                        getattr(fetched, "successful_kols", 0)
                        if not isinstance(fetched, dict)
                        else fetched.get("successful_kols", 0)
                    )
                    stages["failed_kols"] = int(
                        getattr(fetched, "failed_kols", 0)
                        if not isinstance(fetched, dict)
                            else fetched.get("failed_kols", 0)
                    )
                    stages["platform_breakdown"] = (
                        getattr(fetched, "platform_breakdown", {})
                        if not isinstance(fetched, dict)
                        else fetched.get("platform_breakdown", {})
                    ) or {}
                    fetch_errors = (
                        getattr(fetched, "errors", [])
                        if not isinstance(fetched, dict)
                        else fetched.get("errors", [])
                    ) or []
                    errors.extend(f"fetch: {str(error)[:1000]}" for error in fetch_errors)
                    blocked_platforms = (
                        getattr(fetched, "blocked_platforms", [])
                        if not isinstance(fetched, dict)
                        else fetched.get("blocked_platforms", [])
                    ) or []
                    errors.extend(
                        f"fetch: {platform} provider circuit is blocked"
                        for platform in blocked_platforms
                        if not any(str(platform) in str(error) for error in errors)
                    )
                    queue_total = int(
                        getattr(fetched, "queue_total", 0)
                        if not isinstance(fetched, dict)
                        else fetched.get("queue_total", 0)
                    )
                    queue_completed = int(
                        getattr(fetched, "queue_completed", 0)
                        if not isinstance(fetched, dict)
                        else fetched.get("queue_completed", 0)
                    )
                    if queue_total:
                        stages["active_kols"] = queue_total
                        stages["successful_kols"] = queue_completed
                    attempted_kols = stages["successful_kols"] + stages["failed_kols"]
                    if attempted_kols and not queue_total:
                        stages["active_kols"] = attempted_kols
                except Exception as exc:
                    errors.append(f"fetch: {str(exc)[:1000]}")

            pending = self._pending_candidates()
            morning = [
                post for post in pending
                if window_start <= self._posted_at(post) < window_end
            ]
            morning_ids = {post["post_id"] for post in morning}
            backlog = [post for post in pending if post["post_id"] not in morning_ids][:max(0, backlog_limit)]
            total_review_posts = len(morning) + len(backlog)
            self.drafts.update_morning_run(
                run_id,
                stage="ai_review",
                progress_total=total_review_posts,
                stages=stages,
            )
            self._process_scope(morning, "morning", as_of, deadline, stages, errors)
            self.drafts.update_morning_run(
                run_id,
                stage="ai_review",
                progress_current=stages["reviewed_posts"] + stages["failed_posts"],
                progress_total=total_review_posts,
                stages=stages,
            )
            if time.monotonic() < deadline and backlog:
                self._process_scope(backlog, "backlog", as_of, deadline, stages, errors)

            self.drafts.update_morning_run(
                run_id,
                stage="materializing_drafts",
                progress_current=stages["reviewed_posts"] + stages["failed_posts"],
                progress_total=total_review_posts,
                stages=stages,
            )

            visible = self.drafts.list_drafts(review_date=as_of.isoformat(), limit=1000)
            morning_visible = [item for item in visible if item["queue_scope"] == "morning"]
            stages["ready_drafts"] = sum(item["status"] == "ready" for item in morning_visible)
            stages["attention_drafts"] = sum(
                item["status"] == "needs_attention" for item in morning_visible
            )
            stages["backlog_drafts"] = sum(item["queue_scope"] == "backlog" for item in visible)
            prefetch_symbols = sorted(
                {item["symbol"] for item in visible if item["status"] in {"ready", "needs_attention"}}
            )
            for symbol in prefetch_symbols:
                if self.market_store.get_instrument(symbol):
                    self.market_store.enqueue_sync(symbol, reason=f"morning_review:{as_of.isoformat()}")
            stages["prefetched_symbols"] = len(prefetch_symbols)
            morning_failures = sum(
                self.post_store.get_post(post["post_id"]).get("model_status") == "failed"
                for post in morning
            )
            alert_required = (
                any(error.startswith("fetch:") for error in errors)
                or any(error.startswith("ai unavailable:") for error in errors)
                or bool(morning and morning_failures == len(morning))
            )
            status = "completed_with_errors" if errors else "completed"
            self.drafts.finish_morning_run(run_id, status=status, stages=stages, errors=errors)
            return {
                "ok": True,
                "degraded": bool(errors),
                "alert_required": alert_required,
                "run_id": run_id,
                **stages,
                "errors": errors,
            }
        except Exception as exc:
            errors.append(str(exc)[:1000])
            self.drafts.finish_morning_run(run_id, status="failed", stages=stages, errors=errors)
            return {
                "ok": False,
                "degraded": True,
                "alert_required": True,
                "run_id": run_id,
                **stages,
                "errors": errors,
            }

    def _process_scope(
        self,
        posts: list[dict[str, Any]],
        queue_scope: str,
        as_of: date,
        deadline: float,
        stages: dict[str, int],
        errors: list[str],
    ) -> None:
        unresolved: list[dict[str, Any]] = []
        for post in posts:
            if time.monotonic() >= deadline:
                break
            structured = self.rule_classifier.classify_structured_text(post)
            if structured:
                self.post_store.save_model_classification(
                    post["post_id"],
                    structured,
                    model_name="structured-rules",
                    prompt_version=STRUCTURED_REVIEW_VERSION,
                )
            else:
                unresolved.append(post)

        self._run_ocr(unresolved, deadline, stages, errors)
        unresolved = [self.post_store.get_post(post["post_id"]) for post in unresolved]
        needs_model = [post for post in unresolved if post.get("model_status") != "completed"]
        self._run_batches(needs_model, deadline, stages, errors)

        instruments = self.market_store.instrument_map()
        for initial in posts:
            post = self.post_store.get_post(initial["post_id"])
            if post.get("model_status") == "failed":
                self.post_store.set_draft_generation_status(
                    post["post_id"],
                    "failed",
                    version=str(post.get("prompt_version") or ""),
                    error=str(post.get("model_error") or post.get("ocr_error") or "model_failed"),
                )
                stages["failed_posts"] += 1
                continue
            if post.get("model_status") != "completed":
                continue
            materialize_recommendation_drafts(
                self.post_store,
                self.drafts,
                self.rule_classifier,
                post["post_id"],
                instruments=instruments,
                queue_scope=queue_scope,
                review_date=as_of.isoformat(),
                rules_first=False,
            )
            stages["reviewed_posts"] += 1

    def _run_ocr(
        self,
        posts: list[dict[str, Any]],
        deadline: float,
        stages: dict[str, int],
        errors: list[str],
    ) -> None:
        selected = [
            post for post in posts
            if post.get("local_media")
            and post.get("ocr_status") in {"not_requested", "failed"}
            and int(post.get("ocr_attempts") or 0) < 3
        ][:8]
        if not selected or not self.ocr_classifier or time.monotonic() >= deadline:
            return
        previous_timeout = getattr(self.ocr_classifier, "timeout_seconds", None)
        self.ocr_classifier.timeout_seconds = min(
            180,
            max(1, deadline - time.monotonic()),
            previous_timeout or 180,
        )
        try:
            results = self.ocr_classifier.classify(selected)
        except Exception as exc:
            for post in selected:
                self.post_store.save_ocr_result(
                    post["post_id"],
                    error=str(exc),
                    provider=str(getattr(self.ocr_classifier, "provider_name", self.ocr_classifier.__class__.__name__)),
                )
            stages["ocr_failed"] += len(selected)
            errors.append(f"ocr: {str(exc)[:1000]}")
            return
        finally:
            self.ocr_classifier.timeout_seconds = previous_timeout
        for post in selected:
            result = results.get(post["post_id"], {})
            text = str(result.get("text") or "")
            error = str(result.get("error") or "")
            if error or not text.strip():
                self.post_store.save_ocr_result(
                    post["post_id"],
                    error=error or "OCR returned no text",
                    provider=str(result.get("provider") or getattr(self.ocr_classifier, "provider_name", self.ocr_classifier.__class__.__name__)),
                )
                stages["ocr_failed"] += 1
                continue
            self.post_store.save_ocr_result(
                post["post_id"],
                text,
                provider=str(result.get("provider") or getattr(self.ocr_classifier, "provider_name", self.ocr_classifier.__class__.__name__)),
                confidence=float(result.get("average_confidence") or 0),
                details=list(result.get("lines") or []),
            )
            self.post_store.save_rule_classification(
                post["post_id"],
                self.rule_classifier.classify(post, text),
            )
            stages["ocr_completed"] += 1

    def _run_batches(
        self,
        posts: list[dict[str, Any]],
        deadline: float,
        stages: dict[str, int],
        errors: list[str],
    ) -> None:
        if not posts or not self.batch_classifier:
            return
        with self.post_store.model_worker():
            for index in range(0, len(posts), 10):
                if deadline - time.monotonic() < MIN_MODEL_ATTEMPT_SECONDS:
                    break
                original = posts[index:index + 10]
                claimed = []
                for post in original:
                    claimed.extend(
                        self.post_store.claim_posts_for_model(1, post_id=post["post_id"], force=True)
                    )
                if not claimed:
                    continue
                remaining = claimed
                item_errors: dict[str, str] = {}
                for _ in range(2):
                    if not remaining or deadline - time.monotonic() < MIN_MODEL_ATTEMPT_SECONDS:
                        break
                    try:
                        previous_timeout = getattr(self.batch_classifier, "timeout_seconds", None)
                        self.batch_classifier.timeout_seconds = min(
                            240,
                            max(1, deadline - time.monotonic()),
                            previous_timeout or 240,
                        )
                        results = self.batch_classifier.classify_many(remaining)
                        retry: list[dict[str, Any]] = []
                        for post in remaining:
                            payload = results.get(post["post_id"])
                            if payload is None:
                                item_errors[post["post_id"]] = "classifier returned no result"
                                retry.append(post)
                                continue
                            self.post_store.save_model_classification(
                                post["post_id"],
                                payload,
                                model_name=str(getattr(self.batch_classifier, "model_name", "ai-batch")),
                                prompt_version=self.batch_classifier.prompt_version,
                            )
                            item_errors.pop(post["post_id"], None)
                        remaining = retry
                    except ModelProviderUnavailableError as exc:
                        error = str(exc)
                        for post in remaining:
                            self.post_store.save_model_classification(
                                post["post_id"],
                                None,
                                model_name=str(getattr(self.batch_classifier, "model_name", "ai-batch")),
                                prompt_version=self.batch_classifier.prompt_version,
                                error=error,
                            )
                        errors.append(f"ai unavailable: {error}")
                        return
                    except Exception as exc:
                        for post in remaining:
                            item_errors[post["post_id"]] = str(exc)
                    finally:
                        if 'previous_timeout' in locals():
                            self.batch_classifier.timeout_seconds = previous_timeout
                if remaining:
                    for post in remaining:
                        error = item_errors.get(post["post_id"], "classifier returned no result")
                        self.post_store.save_model_classification(
                            post["post_id"],
                            None,
                            model_name=str(getattr(self.batch_classifier, "model_name", "ai-batch")),
                            prompt_version=self.batch_classifier.prompt_version,
                            error=error,
                        )
                    errors.append(
                        "ai batch: "
                        + "; ".join(
                            f"{post['post_id']}={item_errors.get(post['post_id'], 'missing result')}"
                            for post in remaining
                        )[:1000]
                    )

    def _pending_candidates(self) -> list[dict[str, Any]]:
        with self.post_store.connect() as db:
            rows = db.execute(
                """
                SELECT p.post_id FROM posts p JOIN classifications c ON c.post_id=p.post_id
                WHERE p.review_status='pending' AND c.is_candidate=1
                  AND (
                      c.draft_generation_status IN ('pending','failed','needs_attention')
                      OR (c.model_status='completed'
                          AND c.content_type='recommendation'
                          AND c.evidence_type='original_pre_event')
                  )
                ORDER BY p.posted_at DESC
                """
            ).fetchall()
        return [self.post_store.get_post(str(row[0])) for row in rows]

    @staticmethod
    def _posted_at(post: dict[str, Any]) -> datetime:
        value = datetime.fromisoformat(str(post["posted_at"]).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        return value.astimezone(SHANGHAI)
