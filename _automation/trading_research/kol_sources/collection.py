from __future__ import annotations
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from kol_tracker import SHANGHAI, now_iso
from runtime_jobs import initialize_schema, owned_worker, worker_active
from .core import FetchSummary, ProviderAttempt, ProviderFetchResult, TwitterAuthenticationError, TwitterProviderError, TwitterRateLimitError, XBudgetDeferredError, XPostProvider, XSessionUnavailableError
from .media import download_images as _download_images_impl
from .normalization import normalise_douyin_post, normalise_twitter_post, normalise_zhihu_answer
from .providers import _post_provider_warning, _provider_result
from .repository import KolPostStore
from .rules import RuleClassifier



def download_images(*args, **kwargs):
    # Resolve through the legacy kol_posts facade when it has been patched.
    # The facade normally points back to this proxy, so avoid recursive calls.
    try:
        import kol_posts
        legacy = getattr(kol_posts, "download_images", None)
        if legacy is not None and legacy is not download_images:
            return legacy(*args, **kwargs)
    except (ImportError, AttributeError):
        pass
    return _download_images_impl(*args, **kwargs)


def _fetch_with_retry(
    provider: XPostProvider,
    handle: str,
    requested: int,
    retry_delays: tuple[float, ...],
) -> ProviderFetchResult:
    attempts: list[ProviderAttempt] = []
    for attempt in range(len(retry_delays) + 1):
        started = time.perf_counter()
        try:
            result = _provider_result(provider.fetch_user_posts(handle, requested), provider.name)
            return ProviderFetchResult(
                provider=result.provider,
                posts=result.posts,
                attempts=[*attempts, *result.attempts],
                warnings=result.warnings,
                comparisons=result.comparisons,
                next_cursor=result.next_cursor,
                user_id=result.user_id,
                exhausted=result.exhausted,
            )
        except TwitterAuthenticationError as exc:
            if not getattr(exc, "attempts", None):
                exc.attempts = [
                    ProviderAttempt(
                        provider.name,
                        "failed",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        error_code="authentication_failed",
                        error=str(exc)[:1000],
                    )
                ]
            raise
        except XBudgetDeferredError as exc:
            if not getattr(exc, "attempts", None):
                exc.attempts = [
                    ProviderAttempt(
                        provider.name,
                        "deferred",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        error_code="budget_deferred",
                        error=str(exc)[:1000],
                    )
                ]
            raise
        except (TwitterRateLimitError, TwitterProviderError) as exc:
            captured = list(getattr(exc, "attempts", []))
            if not captured:
                captured = [
                    ProviderAttempt(
                        provider.name,
                        "failed",
                        duration_ms=int((time.perf_counter() - started) * 1000),
                        error_code=("rate_limited" if isinstance(exc, TwitterRateLimitError) else "provider_error"),
                        error=str(exc)[:1000],
                    )
                ]
            attempts.extend(captured)
            detail = str(exc).casefold()
            deterministic = any(
                marker in detail
                for marker in (
                    "all_backends_failed",
                    "invalid json",
                    "not configured",
                    "not a tweet list",
                    "not a post list",
                    "unsupported",
                    "missing",
                )
            )
            retryable = not isinstance(exc, TwitterRateLimitError) and not deterministic
            if attempt >= len(retry_delays) or not retryable:
                exc.attempts = attempts
                raise
            if retry_delays[attempt] > 0:
                time.sleep(retry_delays[attempt])
    raise AssertionError("unreachable retry state")


def _payload_post_ids(posts: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("id") or item.get("tweet_id") or "").strip()
        for item in posts
        if str(item.get("id") or item.get("tweet_id") or "").strip()
    }


def _fetch_with_cursor_search(
    provider: XPostProvider,
    handle: str,
    requested: int,
    retry_delays: tuple[float, ...],
    *,
    previous_last_id: str,
    max_pages: int,
) -> ProviderFetchResult:
    result = _fetch_with_retry(provider, handle, requested, retry_delays)
    # The guarded provider persists and advances one real cursor page per
    # invocation.  Never emulate pagination by re-requesting an expanded count
    # (that both defeats the request budget and can skip a page on restart).
    if getattr(provider, "session_manager", None) is not None:
        return result
    if (
        not previous_last_id
        or previous_last_id in _payload_post_ids(result.posts)
        or max_pages <= 1
    ):
        return result
    attempts = list(result.attempts)
    warnings = list(result.warnings)
    comparisons = list(result.comparisons)
    latest = result
    for page in range(2, max_pages + 1):
        try:
            expanded = _fetch_with_retry(
                provider,
                handle,
                requested * page,
                retry_delays,
            )
        except (TwitterAuthenticationError, TwitterRateLimitError, TwitterProviderError) as exc:
            attempts.extend(list(getattr(exc, "attempts", [])))
            warnings.append(
                "gap_search_incomplete:"
                + (
                    "rate_limited"
                    if isinstance(exc, TwitterRateLimitError)
                    else "authentication_failed"
                    if isinstance(exc, TwitterAuthenticationError)
                    else "provider_error"
                )
            )
            return ProviderFetchResult(
                latest.provider,
                latest.posts,
                attempts,
                list(dict.fromkeys(warnings)),
                comparisons,
                next_cursor=latest.next_cursor,
                user_id=latest.user_id,
                exhausted=latest.exhausted,
            )
        attempts.extend(expanded.attempts)
        warnings.extend(expanded.warnings)
        comparisons.extend(expanded.comparisons)
        latest = expanded
        if previous_last_id in _payload_post_ids(expanded.posts):
            return ProviderFetchResult(
                expanded.provider,
                expanded.posts,
                attempts,
                list(dict.fromkeys(warnings)),
                comparisons,
                next_cursor=expanded.next_cursor,
                user_id=expanded.user_id,
                exhausted=expanded.exhausted,
            )
        if len(expanded.posts) < requested * page:
            break
    warnings.append("gap_search_exhausted")
    return ProviderFetchResult(
        latest.provider,
        latest.posts,
        attempts,
        list(dict.fromkeys(warnings)),
        comparisons,
        next_cursor=latest.next_cursor,
        user_id=latest.user_id,
        exhausted=latest.exhausted,
    )


@owned_worker("fetch")
def run_post_fetch(
    store: KolPostStore,
    provider: XPostProvider,
    *,
    platform_providers: dict[str, XPostProvider] | None = None,
    platforms: set[str] | None = None,
    handles: set[str] | None = None,
    max_count: int = 50,
    classifier: RuleClassifier | None = None,
    download_media: bool = True,
    sleep_seconds: float = 2.0,
    retry_delays: tuple[float, ...] = (),
    batch_key: str = "",
    rate_limit_cooldown_seconds: int = 1800,
    max_gap_pages: int = 3,
    dry_run: bool = False,
    fresh_first_page: bool = False,
    reconcile_zhihu: bool = True,
) -> FetchSummary:
    rule_classifier = classifier or RuleClassifier()
    active_kols = store.list_kols("active")
    active_kols = [
        kol for kol in active_kols
        if str(kol.get("availability_status") or "active")
        not in {"suspended", "deleted", "protected", "paused"}
    ]
    if platforms:
        allowed_platforms = {value.casefold() for value in platforms}
        active_kols = [
            kol for kol in active_kols
            if str(kol.get("platform") or "X").casefold() in allowed_platforms
        ]
    if handles:
        allowed_handles = {
            str(value).strip().lstrip("@").casefold()
            for value in handles
            if str(value).strip()
        }
        active_kols = [
            kol for kol in active_kols
            if str(kol.get("handle") or "").strip().casefold() in allowed_handles
        ]
    initial_backfill = any(
        not str(kol.get("last_fetched_at") or "")
        and str(kol.get("platform") or "X").casefold() != "zhihu"
        for kol in active_kols
    )
    requested_depth = (
        max_count
        if fresh_first_page
        else max(
            [max_count, 100 if initial_backfill else max_count]
            + [
                int(kol.get("backfill_requested") or 0)
                for kol in active_kols
                if str(kol.get("backfill_status") or "") == "queued"
            ]
        )
    )
    if batch_key and not dry_run:
        store.prepare_fetch_queue(
            batch_key,
            active_kols,
            requested_count=requested_depth,
        )
        active_kols = store.pending_fetch_queue(batch_key)
    selected_kols = list(active_kols)
    run_id = (
        f"dry-run-{uuid.uuid4().hex}"
        if dry_run
        else store.start_fetch_run(requested_depth, total_kols=len(active_kols))
    )
    successful = 0
    failed = 0
    new_posts = 0
    candidate_posts = 0
    gaps: list[str] = []
    fallback_kols: list[str] = []
    shadow_failed_kols: list[str] = []
    errors: list[str] = []
    authentication_failed = False
    rate_limit_paused = False
    platform_auth_failures: set[str] = set()
    platform_blocked_errors: dict[str, str] = {}
    blocked_reported: set[str] = set()
    platform_breakdown: dict[str, dict[str, Any]] = {}
    guarded_provider = provider
    if getattr(guarded_provider, "session_manager", None) is None:
        guarded_provider = getattr(guarded_provider, "primary", guarded_provider)
    session_manager = getattr(guarded_provider, "session_manager", None)
    has_x_targets = any(
        str(kol.get("platform") or "X").casefold() == "x" for kol in selected_kols
    )
    if has_x_targets and session_manager is not None:
        session_status = session_manager.policy_status()
        ready_slots = [
            row for row in session_status.get("slots", [])
            if row.get("status") == "ready"
            and row.get("enabled")
            and row.get("credential_configured")
            and row.get("user_id")
        ]
        if not session_status.get("enabled"):
            platform_blocked_errors["x"] = "X collection is manually paused"
        elif session_status.get("paused_until"):
            platform_blocked_errors["x"] = (
                "X collection is cooling down until "
                + str(session_status.get("paused_until"))
            )
        elif not ready_slots:
            platform_blocked_errors["x"] = "no verified X session is currently available"
    for kol in selected_kols:
        platform_key = str(kol.get("platform") or "X").casefold()
        platform_breakdown.setdefault(
            platform_key,
            {
                "target": 0,
                "success": 0,
                "failed": 0,
                "blocked": 0,
                "rate_limited": 0,
                "provider_failed": 0,
                "pending": 0,
            },
        )["target"] += 1
    for value in platform_breakdown.values():
        value["pending"] = value["target"]
    providers_by_platform = {
        str(key).casefold(): value for key, value in (platform_providers or {}).items()
    }
    zhihu_kols = [
        kol for kol in active_kols
        if str(kol.get("platform") or "X").casefold() == "zhihu"
    ]
    zhihu_provider = providers_by_platform.get("zhihu", provider)
    if zhihu_kols and hasattr(zhihu_provider, "prepare_session"):
        try:
            zhihu_provider.prepare_session(str(zhihu_kols[0].get("handle") or ""))
        except Exception as exc:
            platform_blocked_errors["zhihu"] = str(exc)
    for index, kol in enumerate(active_kols):
        platform = str(kol.get("platform") or "X").casefold()
        selected_provider = providers_by_platform.get(platform, provider)
        if platform in platform_blocked_errors:
            if platform not in blocked_reported:
                errors.append(
                    f"{platform} batch blocked before account fetch: {platform_blocked_errors[platform]}"
                )
                blocked_reported.add(platform)
            if not dry_run:
                store.update_fetch_progress(
                    run_id,
                    processed_kols=index + 1,
                    successful_kols=successful,
                    failed_kols=failed,
                    new_posts=new_posts,
                    candidate_posts=candidate_posts,
                )
            platform_breakdown[platform]["blocked"] += 1
            platform_breakdown[platform]["pending"] = max(
                0, platform_breakdown[platform]["pending"] - 1
            )
            continue
        if platform in platform_auth_failures:
            if batch_key:
                break
            failed += 1
            errors.append(f"@{kol['handle']}: {kol.get('platform') or 'X'} authentication circuit is open")
            if not dry_run:
                store.mark_fetch_failed(kol["id"], "authentication_failed")
                store.update_fetch_progress(
                    run_id,
                    processed_kols=index + 1,
                    successful_kols=successful,
                    failed_kols=failed,
                    new_posts=new_posts,
                    candidate_posts=candidate_posts,
                )
            platform_breakdown[platform]["failed"] += 1
            platform_breakdown[platform]["provider_failed"] += 1
            platform_breakdown[platform]["pending"] = max(
                0, platform_breakdown[platform]["pending"] - 1
            )
            continue
        if batch_key and not dry_run:
            store.mark_fetch_queue_running(batch_key, int(kol["id"]))
        previous_last_id = str(kol.get("last_post_id") or "")
        last_fetched = str(kol.get("last_fetched_at") or "")
        # Morning freshness pass intentionally uses a small first page.  Gap
        # recovery is resumed by the persistent queue after every account had
        # a chance to contribute its newest post.
        requested = (
            max_count
            if platform == "zhihu" or last_fetched or fresh_first_page
            else 100
        )
        backfill_queued = (
            not fresh_first_page
            and str(kol.get("backfill_status") or "") == "queued"
        )
        archive_only_backfill = (
            platform == "zhihu"
            and str(kol.get("tracking_mode") or "") == "direct_profile"
            and backfill_queued
        )
        if backfill_queued:
            backfill_target = int(kol.get("backfill_requested") or 0)
            completed_depth = int(kol.get("backfill_completed_depth") or 0)
            requested = min(backfill_target, completed_depth + 100)
        if last_fetched and not backfill_queued and not fresh_first_page:
            try:
                if datetime.fromisoformat(last_fetched) < datetime.now(SHANGHAI) - timedelta(days=3):
                    requested = max(max_count, 100)
            except ValueError:
                requested = max_count
        rate_limited_this_account = False
        try:
            fetch_result = _fetch_with_cursor_search(
                selected_provider,
                kol["handle"],
                requested,
                retry_delays,
                previous_last_id=previous_last_id,
                # A freshness pass is deliberately one page.  Cursor search
                # belongs to the later recovery pass; expanding here turns a
                # requested 20-post sweep into 100/200-post calls and can
                # starve the remaining accounts under platform limits.
                max_pages=max_gap_pages if platform == "x" and not fresh_first_page else 1,
            )
            if not dry_run:
                store.record_fetch_attempts(run_id, kol["id"], kol["handle"], fetch_result.attempts)
                store.record_provider_comparisons(run_id, kol["id"], fetch_result.comparisons)
            if fetch_result.provider == "nitter":
                fallback_kols.append(kol["handle"])
            if "shadow_fallback_failed" in fetch_result.warnings:
                shadow_failed_kols.append(kol["handle"])
            seen_ids: list[str] = []
            for payload in fetch_result.posts:
                try:
                    if platform == "zhihu":
                        post = normalise_zhihu_answer(
                            payload,
                            kol,
                            provider=fetch_result.provider,
                        )
                    elif platform == "douyin":
                        post = normalise_douyin_post(
                            payload,
                            kol,
                            provider=fetch_result.provider,
                        )
                    else:
                        post = normalise_twitter_post(
                            payload,
                            kol,
                            provider=fetch_result.provider,
                            provider_warning=_post_provider_warning(payload, fetch_result.provider),
                        )
                except ValueError as exc:
                    errors.append(f"@{kol['handle']} skipped unusable post: {exc}")
                    continue
                seen_ids.append(post.post_id)
                created = not store.has_post(post.post_id) if dry_run else store.upsert_post(post, run_id=run_id)
                if not dry_run and post.platform == "Zhihu" and post.post_type == "aggregation":
                    store.replace_digest_attributions(
                        post.post_id,
                        list(post.raw_payload.get("attributions") or []),
                    )
                if created:
                    new_posts += 1
                if not dry_run and download_media and post.media:
                    existing_media = store.get_post(post.post_id).get("local_media", [])
                    expected_images = sum(
                        str(item.get("type") or "").lower() in {"photo", "image"}
                        for item in post.media
                    )
                    if len(existing_media) < expected_images:
                        local_media, media_errors = download_images(post, store.media_root)
                        if local_media:
                            store.save_local_media(post.post_id, local_media)
                        if media_errors:
                            store.record_review_action(
                                post.post_id,
                                "media_download_failed",
                                {"errors": media_errors[:10]},
                            )
                rule = rule_classifier.classify(post)
                if not dry_run:
                    store.save_rule_classification(post.post_id, rule)
                    if created and archive_only_backfill:
                        store.set_review(
                            post.post_id,
                            "ignored",
                            "Zhihu historical backfill is archive-only.",
                            {
                                "policy": "zhihu_direct_profile_backfill_archive_only",
                                "tracking_mode": kol.get("tracking_mode"),
                                "backfill_requested": kol.get("backfill_requested"),
                            },
                        )
                if created and rule.is_candidate and not archive_only_backfill:
                    candidate_posts += 1
            if fetch_result.posts and not seen_ids:
                raise TwitterProviderError(
                    "provider returned posts but none had usable content",
                    attempts=[
                        ProviderAttempt(
                            fetch_result.provider,
                            "unusable_content",
                            post_count=len(fetch_result.posts),
                            error_code="unusable_content",
                            error="all returned posts failed normalization",
                        )
                    ],
                )
            guarded_provider = provider
            if getattr(guarded_provider, "session_manager", None) is None:
                guarded_provider = getattr(guarded_provider, "primary", guarded_provider)
            session_manager = getattr(guarded_provider, "session_manager", None)
            session_slot = getattr(guarded_provider, "_slot_id", None)
            if not dry_run and platform == "x" and session_manager is not None and session_slot:
                checkpoint = session_manager.checkpoint(int(kol["id"]))
                history_mode = bool(getattr(guarded_provider, "history_mode", False))
                first_id = seen_ids[0] if seen_ids else ""
                page_end_id = seen_ids[-1] if seen_ids else ""
                session_manager.save_checkpoint(
                    int(kol["id"]),
                    int(session_slot),
                    phase="completed" if history_mode and fetch_result.exhausted else "history" if history_mode else "freshness",
                    user_id=fetch_result.user_id or str((checkpoint or {}).get("user_id") or ""),
                    latest_seen_post_id=first_id,
                    contiguous_post_id=page_end_id if history_mode else first_id,
                    cursor=fetch_result.next_cursor if history_mode else "",
                    pages_completed=int((checkpoint or {}).get("pages_completed") or 0) + 1,
                    last_page_new_ids=len(seen_ids),
                    stop_reason="source_exhausted" if history_mode and fetch_result.exhausted else "",
                )
            gap_search_incomplete = any(
                warning.startswith("gap_search_incomplete:")
                for warning in fetch_result.warnings
            )
            gap_detected = (
                "gap_search_exhausted" in fetch_result.warnings
                or gap_search_incomplete
                or (
                fetch_result.provider == "nitter"
                and bool(previous_last_id)
                and previous_last_id not in seen_ids
                )
            )
            if gap_detected:
                gaps.append(kol["handle"])
            newest = (
                previous_last_id
                if gap_detected and previous_last_id
                else seen_ids[0]
                if seen_ids
                else previous_last_id
            )
            if not dry_run:
                store.update_fetch_state(
                    kol["id"],
                    newest,
                    "gap_detected" if gap_detected else "success",
                    fetched_count=len(seen_ids),
                    requested_count=requested,
                )
                if batch_key:
                    if gap_search_incomplete:
                        store.defer_fetch_queue_item(
                            batch_key,
                            int(kol["id"]),
                            error_code="gap_search_incomplete",
                            error="cursor search interrupted before the saved post was found",
                            cooldown_seconds=60,
                        )
                    else:
                        store.complete_fetch_queue_item(batch_key, int(kol["id"]))
            successful += 1
            platform_breakdown[platform]["success"] += 1
            platform_breakdown[platform]["pending"] = max(
                0, platform_breakdown[platform]["pending"] - 1
            )
        except Exception as exc:
            attempts = list(getattr(exc, "attempts", []))
            rate_limited_this_account = isinstance(exc, TwitterRateLimitError) or any(
                attempt.error_code == "rate_limited" for attempt in attempts
            )
            blocked_account = isinstance(exc, (XSessionUnavailableError, XBudgetDeferredError))
            zhihu_incompatible = platform == "zhihu" and "10003" in str(exc)
            if not blocked_account:
                failed += 1
                errors.append(f"@{kol['handle']}: {exc}")
                if not dry_run:
                    store.mark_fetch_failed(kol["id"])
                    store.record_fetch_attempts(
                        run_id,
                        kol["id"],
                        kol["handle"],
                        attempts,
                    )
            if isinstance(exc, TwitterAuthenticationError):
                authentication_failed = True
                platform_auth_failures.add(platform)
            if blocked_account:
                platform_blocked_errors[platform] = str(exc)
            if not dry_run and not blocked_account:
                availability = "rate_limited" if rate_limited_this_account else "provider_failed"
                store.set_account_availability(
                    int(kol["id"]),
                    availability,
                    reason=str(exc),
                    source="fetch",
                )
            if blocked_account:
                platform_breakdown[platform]["blocked"] += 1
            else:
                platform_breakdown[platform]["failed"] += 1
                if rate_limited_this_account:
                    platform_breakdown[platform]["rate_limited"] += 1
                else:
                    platform_breakdown[platform]["provider_failed"] += 1
            platform_breakdown[platform]["pending"] = max(
                0, platform_breakdown[platform]["pending"] - 1
            )
            if batch_key and not dry_run:
                error_code = (
                    "budget_deferred"
                    if isinstance(exc, XBudgetDeferredError)
                    else "blocked_auth"
                    if isinstance(exc, XSessionUnavailableError)
                    else "provider_incompatible"
                    if zhihu_incompatible
                    else
                    "rate_limited"
                    if rate_limited_this_account
                    else "authentication_failed"
                    if isinstance(exc, TwitterAuthenticationError)
                    else "provider_error"
                )
                store.defer_fetch_queue_item(
                    batch_key,
                    int(kol["id"]),
                    error_code=error_code,
                    error=str(exc),
                    cooldown_seconds=(
                        7200
                        if error_code == "budget_deferred"
                        else 0
                        if error_code == "blocked_auth"
                        else 21600
                        if error_code == "provider_incompatible"
                        else
                        rate_limit_cooldown_seconds
                        if error_code in {"rate_limited", "authentication_failed"}
                        else 300
                    ),
                )
        if not dry_run:
            store.update_fetch_progress(
                run_id,
                processed_kols=index + 1,
                successful_kols=successful,
                failed_kols=failed,
                new_posts=new_posts,
                candidate_posts=candidate_posts,
            )
        if sleep_seconds and index < len(active_kols) - 1:
            time.sleep(sleep_seconds)
        if batch_key and rate_limited_this_account:
            rate_limit_paused = True
            break
    if not dry_run and reconcile_zhihu and any(
        str(kol.get("platform") or "X").casefold() == "zhihu" for kol in active_kols
    ):
        store.reconcile_zhihu_historical_backfill()
    queue_status = (
        store.fetch_queue_status(batch_key)
        if batch_key and not dry_run
        else {"total": 0, "completed": 0, "pending": 0}
    )
    summary = FetchSummary(
        run_id=run_id,
        successful_kols=successful,
        failed_kols=failed,
        new_posts=new_posts,
        candidate_posts=candidate_posts,
        pending_reviews=store.count_pending(),
        auth_status=(
            "failed"
            if authentication_failed or bool(getattr(provider, "primary_auth_failed", False))
            else "ok" if successful else "unknown"
        ),
        gap_kols=gaps,
        fallback_kols=fallback_kols,
        shadow_failed_kols=shadow_failed_kols,
        errors=errors,
        queue_total=int(queue_status["total"]),
        queue_completed=int(queue_status["completed"]),
        queue_pending=int(queue_status["pending"]),
        rate_limit_paused=rate_limit_paused,
        blocked_platforms=sorted(platform_blocked_errors),
        platform_breakdown=platform_breakdown,
    )
    if not dry_run:
        store.finish_fetch_run(summary)
    return summary
