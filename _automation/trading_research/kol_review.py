from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from filelock import FileLock

from kol_posts import KolPostStore, validate_event_draft
from kol_tracker import EventRecord, KolStore, now_iso


@dataclass(frozen=True)
class ApprovalResult:
    post_id: str
    event_ids: list[str]
    created_event_ids: list[str]
    source_ref: str
    created_events: int


def _next_number(events: list[EventRecord]) -> int:
    values = []
    for event in events:
        match = re.fullmatch(r"KOL-(\d+)", event.event_id)
        if match:
            values.append(int(match.group(1)))
    return max(values, default=0) + 1


def post_review_lock(store: KolPostStore, post_id: str, timeout: int = 60) -> FileLock:
    if not re.fullmatch(r"\d{5,25}", post_id):
        raise ValueError("invalid post id")
    lock_root = store.path.parent / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_root / f"approval-{post_id}.lock"), timeout=timeout)


def approve_post(
    post_store: KolPostStore,
    event_store: KolStore,
    post_id: str,
    drafts: list[dict[str, Any]],
    *,
    note: str = "",
    audit_detail: dict[str, Any] | None = None,
) -> ApprovalResult:
    with post_review_lock(post_store, post_id):
        return _approve_post_locked(
            post_store,
            event_store,
            post_id,
            drafts,
            note=note,
            audit_detail=audit_detail,
        )


def approve_recommendation_draft(
    post_store: KolPostStore,
    event_store: KolStore,
    post_id: str,
    draft: dict[str, Any],
    *,
    note: str = "",
    audit_detail: dict[str, Any] | None = None,
) -> ApprovalResult:
    """Approve one stock from a post without closing the other draft reviews."""
    with post_review_lock(post_store, post_id):
        post = post_store.get_post(post_id)
        if post["review_status"] not in {"pending", "capture_failed", "approved"}:
            raise ValueError(f"post is no longer approvable: {post['review_status']}")
        if post["post_type"] in {"retweet", "aggregation"}:
            raise ValueError("secondhand or aggregated posts cannot be approved as direct KOL events")
        if "source_conflict" in str(post.get("provider_warning") or "").split(";"):
            raise ValueError("provider timestamps conflict; reconcile the source before approval")
        if post.get("evidence_type") in {"retrospective", "secondhand"}:
            raise ValueError(f"post evidence cannot be approved: {post.get('evidence_type')}")
        errors = validate_event_draft(draft)
        if errors:
            raise ValueError("invalid recommendation draft: " + ", ".join(errors))

        source_ref = f"post:{post_id}"
        created: list[EventRecord] = []
        with FileLock(str(event_store.root / "events.lock"), timeout=60):
            existing = event_store.load_events()
            match = next(
                (
                    event
                    for event in existing
                    if event.source_url == post["url"] and event.symbol == str(draft["symbol"])
                ),
                None,
            )
            if match is None:
                event = EventRecord(
                    event_id=f"KOL-{_next_number(existing):04d}",
                    kol_name=post["display_name"],
                    platform=str(post.get("platform") or "X"),
                    source_url=post["url"],
                    source_note=source_ref,
                    posted_at=post["posted_at"],
                    symbol=str(draft["symbol"]),
                    security_name=str(draft.get("security_name") or ""),
                    direction=str(draft["direction"]),
                    thesis=str(draft["thesis"]),
                    status="active",
                    execution_warning=(
                        "conditional_intraday_entry_unverified"
                        if draft.get("conditions")
                        else ""
                    ),
                    activated_at=now_iso(),
                    updated_at=now_iso(),
                    kol_id=str(post.get("kol_id") or ""),
                    kol_handle=str(post.get("handle") or ""),
                    source_post_id=str(post.get("post_id") or post_id),
                )
                event_store.save_events([*existing, event])
                event_store.log_run(
                    "register_event",
                    {"event_id": event.event_id, "status": "active", "post_id": post_id},
                )
                created.append(event)
                match = event
                existing = [*existing, event]

        source_events = [event.event_id for event in existing if event.source_url == post["url"]]
        post_store.set_local_source_ref(post_id, source_ref)
        post_store.set_review(
            post_id,
            "approved",
            note,
            {
                "event_ids": source_events,
                "created_event_ids": [event.event_id for event in created],
                "drafts": [draft],
                "source_ref": source_ref,
                "review_unit": "recommendation_draft",
                **(audit_detail or {}),
            },
        )
        assert match is not None
        return ApprovalResult(
            post_id=post_id,
            event_ids=[match.event_id],
            created_event_ids=[event.event_id for event in created],
            source_ref=source_ref,
            created_events=len(created),
        )


def _approve_post_locked(
    post_store: KolPostStore,
    event_store: KolStore,
    post_id: str,
    drafts: list[dict[str, Any]],
    *,
    note: str = "",
    audit_detail: dict[str, Any] | None = None,
) -> ApprovalResult:
    post = post_store.get_post(post_id)
    previous = post_store.latest_review(post_id, "approved")
    if post["review_status"] == "approved" and previous:
        detail = previous.get("detail") or {}
        return ApprovalResult(
            post_id=post_id,
            event_ids=list(detail.get("event_ids") or []),
            created_event_ids=list(detail.get("created_event_ids") or []),
            source_ref=str(detail.get("source_ref") or post.get("source_note") or f"post:{post_id}"),
            created_events=0,
        )
    if post["review_status"] not in {"pending", "capture_failed"}:
        raise ValueError(f"post is no longer pending: {post['review_status']}")
    if post["post_type"] in {"retweet", "aggregation"}:
        raise ValueError("secondhand or aggregated posts cannot be approved as direct KOL events")
    if "source_conflict" in str(post.get("provider_warning") or "").split(";"):
        raise ValueError("provider timestamps conflict; reconcile the source before approval")
    if post.get("evidence_type") in {"retrospective", "secondhand", "ambiguous"}:
        raise ValueError(f"post evidence cannot be approved: {post.get('evidence_type')}")
    if not drafts:
        raise ValueError("at least one event draft is required")
    errors = {index: validate_event_draft(draft) for index, draft in enumerate(drafts)}
    errors = {index: value for index, value in errors.items() if value}
    if errors:
        labels = {
            "one_symbol_per_event": "股票代码必须是 6 位数字",
            "direction": "方向必须是看多或看空",
            "thesis": "推荐理由不能为空",
            "evidence_type": "只能批准事前原始推荐",
        }
        details = [
            f"第 {index + 1} 条：{'；'.join(labels.get(error, error) for error in values)}"
            for index, values in errors.items()
        ]
        raise ValueError("事件草稿不完整：" + " ".join(details))

    active_attempt = post_store.active_approval_attempt(post_id)
    if active_attempt:
        attempt_detail = active_attempt.get("detail") or {}
        if attempt_detail.get("drafts") != drafts:
            raise ValueError("an interrupted approval must resume with its original drafts")
        attempt_id = str(attempt_detail.get("attempt_id") or "")
    else:
        attempt_id = uuid.uuid4().hex
        post_store.record_review_action(
            post_id,
            "approval_started",
            {"attempt_id": attempt_id, "drafts": drafts, "note": note},
        )
    source_ref = f"post:{post_id}"
    post_store.record_review_action(
        post_id,
        "local_source_bound",
        {"attempt_id": attempt_id, "source_ref": source_ref, "media_count": len(post.get("local_media") or [])},
    )

    lock_path = event_store.root / "events.lock"
    with FileLock(str(lock_path), timeout=60):
        existing = event_store.load_events()
        by_key = {(event.source_url, event.symbol): event for event in existing}
        event_ids: list[str] = []
        new_events: list[EventRecord] = []
        next_number = _next_number(existing)
        for draft in drafts:
            key = (post["url"], str(draft["symbol"]))
            if key in by_key:
                event_ids.append(by_key[key].event_id)
                continue
            event_id = f"KOL-{next_number:04d}"
            next_number += 1
            event = EventRecord(
                event_id=event_id,
                kol_name=post["display_name"],
                platform=str(post.get("platform") or "X"),
                source_url=post["url"],
                source_note=source_ref,
                posted_at=post["posted_at"],
                symbol=str(draft["symbol"]),
                security_name=str(draft.get("security_name") or ""),
                direction=str(draft["direction"]),
                thesis=str(draft["thesis"]),
                status="active",
                activated_at=now_iso(),
                updated_at=now_iso(),
                kol_id=str(post.get("kol_id") or ""),
                kol_handle=str(post.get("handle") or ""),
                source_post_id=str(post.get("post_id") or post_id),
            )
            new_events.append(event)
            by_key[key] = event
            event_ids.append(event_id)
        if new_events:
            event_store.save_events([*existing, *new_events])
            for event in new_events:
                event_store.log_run("register_event", {"event_id": event.event_id, "status": "active", "post_id": post_id})
    post_store.record_review_action(
        post_id,
        "events_saved",
        {"attempt_id": attempt_id, "event_ids": event_ids},
    )

    post_store.set_local_source_ref(post_id, source_ref)
    post_store.set_review(
        post_id,
        "approved",
        note,
        {
            "event_ids": event_ids,
            "created_event_ids": [event.event_id for event in new_events],
            "attempt_id": attempt_id,
            "drafts": drafts,
            "source_ref": source_ref,
            **(audit_detail or {}),
        },
    )
    return ApprovalResult(
        post_id=post_id,
        event_ids=event_ids,
        created_event_ids=[event.event_id for event in new_events],
        source_ref=source_ref,
        created_events=len(new_events),
    )
