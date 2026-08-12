from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from kol_posts import (
    RECOMMENDATION_ACTIONS,
    RECOMMENDATION_HORIZONS,
    RECOMMENDATION_STRENGTHS,
    KolPostStore,
)
from kol_tracker import SHANGHAI, now_iso


ACTIVE_DRAFT_STATUSES = {"ready", "needs_attention"}
EDITABLE_DRAFT_STATUSES = {"ready", "needs_attention"}
CORRECTION_TYPES = {
    "missed_stock",
    "wrong_mapping",
    "wrong_direction",
    "wrong_thesis",
    "wrong_evidence",
    "wrong_content_type",
}
REVISION_FIELDS = (
    "symbol", "security_name", "direction", "action", "horizon", "strength",
    "thesis", "evidence_type", "evidence_spans", "conditions", "mention_kind",
)


def review_window_utc(review_date: str) -> tuple[str, str]:
    end = datetime.fromisoformat(f"{review_date}T09:00:00+08:00").astimezone(timezone.utc)
    start = end - timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _source_signature(post: Mapping[str, Any], draft: Mapping[str, Any]) -> str:
    value = {
        "content_hash": post.get("content_hash"),
        "prompt_version": post.get("prompt_version"),
        "model_name": post.get("model_name"),
        "draft": draft,
    }
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _revision_snapshot(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {field: draft.get(field) for field in REVISION_FIELDS}


def _infer_correction_type(changes: Mapping[str, Any]) -> str:
    fields = set(changes)
    if fields & {"symbol", "security_name"}:
        return "wrong_mapping"
    if "direction" in fields:
        return "wrong_direction"
    if fields & {"evidence_spans", "evidence_type"}:
        return "wrong_evidence"
    if "mention_kind" in fields:
        return "wrong_content_type"
    return "wrong_thesis"


def _evidence_is_ocr_dependent(post: Mapping[str, Any], draft: Mapping[str, Any]) -> bool:
    if draft.get("depends_on_ocr") or draft.get("evidence_source") in {"ocr", "image"}:
        return True
    plain_source_text = "\n".join(
        str(post.get(key) or "") for key in ("text", "article_text", "quoted_text")
    )
    ocr_text = str(post.get("ocr_text") or "")
    spans = [str(value).strip() for value in draft.get("evidence_spans") or [] if str(value).strip()]
    return bool(spans and ocr_text) and all(
        span in ocr_text and (span not in plain_source_text or not plain_source_text.strip())
        for span in spans
    )


def _attention_reasons(
    post: Mapping[str, Any],
    draft: Mapping[str, Any],
    instruments: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    symbol = str(draft.get("symbol") or "")
    instrument = instruments.get(symbol)
    if not re.fullmatch(r"\d{6}", symbol):
        errors.append("invalid_symbol")
    if instrument is None:
        errors.append("instrument_not_found")
    elif str(instrument.get("instrument_type") or "stock") != "stock":
        errors.append("unsupported_instrument_type")
    elif str(instrument.get("status") or "active") not in {"active", "1"}:
        errors.append("instrument_inactive")
    if str(draft.get("direction") or "") not in {"long", "short"}:
        errors.append("invalid_direction")
    if str(draft.get("action") or "") not in RECOMMENDATION_ACTIONS:
        errors.append("invalid_action")
    if str(draft.get("horizon") or "") not in RECOMMENDATION_HORIZONS:
        errors.append("invalid_horizon")
    if str(draft.get("strength") or "") not in RECOMMENDATION_STRENGTHS:
        errors.append("invalid_strength")
    if not str(draft.get("thesis") or "").strip():
        errors.append("missing_thesis")
    if draft.get("evidence_type") != "original_pre_event":
        errors.append("not_original_pre_event")
    if draft.get("mention_kind") != "recommendation":
        errors.append("not_recommendation")
    if post.get("post_type") in {"retweet", "aggregation"}:
        errors.append("secondhand_source")
    if not str(post.get("url") or "").strip():
        errors.append("missing_source_url")
    try:
        posted_at = datetime.fromisoformat(str(post.get("posted_at") or ""))
        if posted_at.tzinfo is None:
            errors.append("posted_at_timezone")
    except ValueError:
        errors.append("posted_at_invalid")
    plain_source_text = "\n".join(
        str(post.get(key) or "") for key in ("text", "article_text", "quoted_text")
    )
    ocr_text = str(post.get("ocr_text") or "")
    source_text = f"{plain_source_text}\n{ocr_text}"
    spans = [str(value).strip() for value in draft.get("evidence_spans") or [] if str(value).strip()]
    if not spans:
        errors.append("missing_evidence")
    elif not any(span in source_text for span in spans):
        errors.append("evidence_not_found")
    if _evidence_is_ocr_dependent(post, draft):
        errors.append("image_dependency")
    if post.get("reply_to_id"):
        errors.append("reply_context_required")
    warnings = {value for value in str(post.get("provider_warning") or "").split(";") if value}
    if "source_conflict" in warnings:
        errors.append("source_conflict")
    return sorted(set(errors))


def _is_theme_index_draft(
    post: Mapping[str, Any],
    draft: Mapping[str, Any],
    instruments: Mapping[str, Mapping[str, Any]],
) -> bool:
    symbol = str(draft.get("symbol") or "")
    if not symbol.startswith("88") or symbol in instruments:
        return False
    context = "\n".join(
        str(value or "")
        for value in (
            post.get("text"),
            post.get("article_text"),
            post.get("model_summary"),
            post.get("summary"),
            draft.get("security_name"),
            draft.get("thesis"),
        )
    )
    return any(marker in context for marker in ("题材", "概念", "板块", "行业", "指数", "不是个股"))


def _is_retrospective_only_source(
    post: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> bool:
    text = "\n".join(
        str(value or "")
        for value in (post.get("text"), post.get("article_text"), post.get("model_summary"))
    )
    evidence_text = "\n".join(
        [
            *[str(value or "") for value in draft.get("evidence_spans") or []],
            str(draft.get("thesis") or ""),
        ]
    )
    retrospective_markers = (
        "收盘了",
        "收盘总结",
        "涨停复盘",
        "应声大涨",
        "市场都给出了答案",
        "今天三只票涨停",
        "吃肉了",
        "吃到了",
    )
    prospective_markers = (
        "明日",
        "明天",
        "下周",
        "可建仓",
        "建仓计划",
        "明日观察",
        "明日关注",
        "明日参考",
        "马前炮",
        "个股分享",
    )
    evidence_retrospective = any(marker in evidence_text for marker in retrospective_markers)
    evidence_prospective = any(marker in evidence_text for marker in prospective_markers)
    if evidence_retrospective:
        return not evidence_prospective
    retrospective = any(marker in text for marker in retrospective_markers)
    prospective = any(marker in text for marker in prospective_markers)
    return retrospective and not prospective


def _security_identity_is_present(post: Mapping[str, Any], draft: Mapping[str, Any]) -> bool:
    source_text = "\n".join(
        str(post.get(key) or "")
        for key in ("text", "article_text", "quoted_text", "ocr_text")
    )
    symbol = str(draft.get("symbol") or "").strip()
    name = str(draft.get("security_name") or "").strip()
    return bool((symbol and symbol in source_text) or (name and name in source_text))


def _is_reviewable_recommendation_draft(
    post: Mapping[str, Any],
    draft: Mapping[str, Any],
    instruments: Mapping[str, Mapping[str, Any]],
) -> bool:
    if str(post.get("content_type") or "") != "recommendation":
        return False
    if str(draft.get("evidence_source") or "") == "quoted_text":
        return False
    if _is_retrospective_only_source(post, draft):
        return False
    if not _security_identity_is_present(post, draft):
        return False
    return not _is_theme_index_draft(post, draft, instruments)


class RecommendationDraftRepository:
    def __init__(self, post_store: KolPostStore):
        self.post_store = post_store

    def sync_post(
        self,
        post_id: str,
        instruments: Mapping[str, Mapping[str, Any]],
        *,
        queue_scope: str,
        review_date: str,
    ) -> dict[str, int]:
        if queue_scope not in {"morning", "backlog"}:
            raise ValueError("invalid queue scope")
        post = self.post_store.get_post(post_id)
        created = updated = superseded = 0
        desired_versions: set[str] = set()
        for raw in post.get("drafts") or []:
            draft = dict(raw)
            draft.setdefault("action", "watch")
            draft.setdefault("horizon", "unspecified")
            draft.setdefault("strength", "unspecified")
            if (
                draft.get("mention_kind") != "recommendation"
                or draft.get("evidence_type") != "original_pre_event"
            ):
                continue
            if not _is_reviewable_recommendation_draft(post, draft, instruments):
                continue
            symbol = str(draft.get("symbol") or "")
            signature = _source_signature(post, draft)
            base_version = str(post.get("prompt_version") or post.get("model_name") or "rules")
            extraction_version = f"{base_version}:{signature[:12]}"
            desired_versions.add(extraction_version)
            attention = _attention_reasons(post, draft, instruments)
            status = "needs_attention" if attention else "ready"
            timestamp = now_iso()
            with self.post_store.connect() as db:
                existing = db.execute(
                    """
                    SELECT id,status FROM recommendation_drafts
                    WHERE post_id=? AND extraction_version=?
                    """,
                    (post_id, extraction_version),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """
                        INSERT INTO recommendation_drafts(
                            post_id,symbol,security_name,direction,action,horizon,strength,thesis,evidence_type,
                            evidence_spans_json,evidence_source,depends_on_ocr,conditions_json,
                            mention_kind,confidence,
                            model_name,extraction_version,source_signature,status,
                            attention_reasons_json,queue_scope,review_date,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            post_id,
                            symbol,
                            str(draft.get("security_name") or ""),
                            str(draft.get("direction") or ""),
                            str(draft.get("action") or "watch"),
                            str(draft.get("horizon") or "unspecified"),
                            str(draft.get("strength") or "unspecified"),
                            str(draft.get("thesis") or ""),
                            str(draft.get("evidence_type") or "ambiguous"),
                            _json(draft.get("evidence_spans") or []),
                            str(draft.get("evidence_source") or "text"),
                            int(bool(draft.get("depends_on_ocr"))),
                            _json(draft.get("conditions") or []),
                            str(draft.get("mention_kind") or "recommendation"),
                            float(draft.get("confidence") or post.get("confidence") or 0),
                            str(post.get("model_name") or ""),
                            extraction_version,
                            signature,
                            status,
                            _json(attention),
                            queue_scope,
                            review_date,
                            timestamp,
                            timestamp,
                        ),
                    )
                    created += 1
                elif existing["status"] in ACTIVE_DRAFT_STATUSES:
                    db.execute(
                        """
                        UPDATE recommendation_drafts
                        SET status=?,attention_reasons_json=?,queue_scope=?,review_date=?,updated_at=?
                        WHERE id=?
                        """,
                        (
                            status,
                            _json(attention),
                            queue_scope,
                            review_date,
                            timestamp,
                            int(existing["id"]),
                        ),
                    )
                    updated += 1
        with self.post_store.connect() as db:
            stale = db.execute(
                """
                SELECT id,symbol,extraction_version FROM recommendation_drafts
                WHERE post_id=? AND status IN ('ready','needs_attention')
                """,
                (post_id,),
            ).fetchall()
            stale_ids = [
                int(row["id"])
                for row in stale
                if str(row["extraction_version"]) not in desired_versions
                and not str(row["extraction_version"]).startswith("human-")
            ]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                db.execute(
                    f"UPDATE recommendation_drafts SET status='superseded',updated_at=? "
                    f"WHERE id IN ({placeholders})",
                    (now_iso(), *stale_ids),
                )
                superseded += len(stale_ids)
        return {"created": created, "updated": updated, "superseded": superseded}

    def start_morning_run(
        self,
        review_date: str,
        *,
        window_start: str,
        window_end: str,
        phase: str = "initial",
    ) -> str:
        run_id = f"morning-{review_date}-{now_iso().replace(':', '').replace('+', '-')}"
        with self.post_store.connect() as db:
            running = db.execute(
                "SELECT run_id FROM morning_runs WHERE status='running' LIMIT 1"
            ).fetchone()
            if running is not None:
                raise RuntimeError(f"morning pipeline already running: {running['run_id']}")
            db.execute(
                """
                INSERT INTO morning_runs(
                    run_id,review_date,window_start,window_end,started_at,status,phase,errors_json
                ) VALUES(?,?,?,?,?,'running',?,'[]')
                """,
                (run_id, review_date, window_start, window_end, now_iso(), phase),
            )
        return run_id

    def interrupt_stale_runs(
        self,
        *,
        now: datetime | None = None,
        max_age_minutes: int = 70,
    ) -> int:
        current = now or datetime.now(SHANGHAI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        current = current.astimezone(SHANGHAI)
        cutoff = current - timedelta(minutes=max(1, max_age_minutes))
        interrupted = 0
        with self.post_store.connect() as db:
            rows = db.execute(
                "SELECT run_id,started_at FROM morning_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                try:
                    started = datetime.fromisoformat(str(row["started_at"]))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=SHANGHAI)
                except ValueError:
                    started = datetime.min.replace(tzinfo=SHANGHAI)
                if started.astimezone(SHANGHAI) > cutoff:
                    continue
                db.execute(
                    """
                    UPDATE morning_runs SET status='interrupted',completed_at=?,errors_json=?
                    WHERE run_id=? AND status='running'
                    """,
                    (current.isoformat(timespec="seconds"), _json(["stale_run_recovered"]), row["run_id"]),
                )
                interrupted += 1
        return interrupted

    def update_morning_run(
        self,
        run_id: str,
        *,
        stage: str,
        progress_current: int = 0,
        progress_total: int = 0,
        stages: Mapping[str, Any] | None = None,
    ) -> None:
        values = dict(stages or {})
        allowed = (
            "fetched_posts",
            "reviewed_posts",
            "ready_drafts",
            "attention_drafts",
            "failed_posts",
            "active_kols",
            "successful_kols",
            "failed_kols",
        )
        assignments = ["stage=?", "progress_current=?", "progress_total=?"]
        params: list[Any] = [stage, max(0, progress_current), max(0, progress_total)]
        for column in allowed:
            if column not in values:
                continue
            assignments.append(f"{column}=?")
            params.append(max(0, int(values[column])))
        params.append(run_id)
        with self.post_store.connect() as db:
            db.execute(
                f"UPDATE morning_runs SET {','.join(assignments)} WHERE run_id=? AND status='running'",
                params,
            )

    def finish_morning_run(
        self,
        run_id: str,
        *,
        status: str,
        stages: Mapping[str, Any],
        errors: list[str],
    ) -> None:
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE morning_runs SET status=?,completed_at=?,stage='completed',
                    progress_current=CASE WHEN progress_total>0 THEN progress_total ELSE progress_current END,
                    fetched_posts=?,reviewed_posts=?,
                    ready_drafts=?,attention_drafts=?,failed_posts=?,active_kols=?,successful_kols=?,
                    failed_kols=?,errors_json=? WHERE run_id=?
                """,
                (
                    status,
                    now_iso(),
                    int(stages.get("fetched_posts", 0)),
                    int(stages.get("reviewed_posts", 0)),
                    int(stages.get("ready_drafts", 0)),
                    int(stages.get("attention_drafts", 0)),
                    int(stages.get("failed_posts", 0)),
                    int(stages.get("active_kols", 0)),
                    int(stages.get("successful_kols", 0)),
                    int(stages.get("failed_kols", 0)),
                    _json(errors),
                    run_id,
                ),
            )

    def recent_morning_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.post_store.connect() as db:
            rows = db.execute(
                "SELECT * FROM morning_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["errors"] = _loads(value.pop("errors_json", "[]"), [])
            values.append(value)
        return values

    def morning_delivery(self, review_date: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(SHANGHAI)
        deadline = datetime.fromisoformat(f"{review_date}T09:00:00+08:00")
        with self.post_store.connect() as db:
            row = db.execute(
                """
                SELECT * FROM morning_runs
                WHERE review_date=? AND phase='final' AND status != 'running'
                ORDER BY started_at DESC LIMIT 1
                """,
                (review_date,),
            ).fetchone()
            coverage_row = db.execute(
                """
                SELECT active_kols,successful_kols,failed_kols FROM morning_runs
                WHERE review_date=? AND successful_kols + failed_kols > 0
                ORDER BY started_at DESC LIMIT 1
                """,
                (review_date,),
            ).fetchone()
            latest = db.execute(
                """
                SELECT * FROM morning_runs WHERE review_date=?
                ORDER BY started_at DESC LIMIT 1
                """,
                (review_date,),
            ).fetchone()
        if row is None:
            progress = dict(latest) if latest is not None else {}
            active_kols = int(progress.get("active_kols") or 0)
            successful_kols = int(progress.get("successful_kols") or 0)
            return {
                "status": "pending" if current <= deadline else "missed",
                "deadline": deadline.isoformat(),
                "completed_at": "",
                "coverage": successful_kols / active_kols if active_kols else 0.0,
                "active_kols": active_kols,
                "successful_kols": successful_kols,
                "failed_kols": int(progress.get("failed_kols") or 0),
                "errors": _loads(progress.get("errors_json", "[]"), []),
                "latest_run_id": str(progress.get("run_id") or ""),
                "latest_phase": str(progress.get("phase") or ""),
                "latest_status": str(progress.get("status") or "waiting"),
                "stage": str(progress.get("stage") or "waiting"),
                "progress_current": int(progress.get("progress_current") or 0),
                "progress_total": int(progress.get("progress_total") or 0),
            }
        value = dict(row)
        completed_at = datetime.fromisoformat(str(value.get("completed_at") or value["started_at"]))
        errors = _loads(value.get("errors_json", "[]"), [])
        attempted_kols = int(value.get("successful_kols") or 0) + int(value.get("failed_kols") or 0)
        active_kols = attempted_kols or int(value.get("active_kols") or 0)
        successful_kols = int(value.get("successful_kols") or 0)
        failed_kols = int(value.get("failed_kols") or 0)
        if attempted_kols == 0 and coverage_row is not None:
            successful_kols = int(coverage_row["successful_kols"] or 0)
            failed_kols = int(coverage_row["failed_kols"] or 0)
            active_kols = successful_kols + failed_kols or int(coverage_row["active_kols"] or 0)
        if completed_at > deadline:
            status = "late"
        elif value["status"] == "completed" and not errors and failed_kols == 0:
            status = "ready"
        else:
            status = "degraded"
        return {
            "status": status,
            "deadline": deadline.isoformat(),
            "completed_at": value.get("completed_at") or "",
            "coverage": successful_kols / active_kols if active_kols else 0.0,
            "active_kols": active_kols,
            "successful_kols": successful_kols,
            "failed_kols": failed_kols,
            "errors": errors,
            "latest_run_id": str(value.get("run_id") or ""),
            "latest_phase": str(value.get("phase") or "final"),
            "latest_status": str(value.get("status") or ""),
            "stage": str(value.get("stage") or "completed"),
            "progress_current": int(value.get("progress_current") or 0),
            "progress_total": int(value.get("progress_total") or 0),
        }

    def migrate_legacy_review_queue(self) -> dict[str, int]:
        """Remove old non-candidates from the user queue without deleting source posts."""
        timestamp = now_iso()
        with self.post_store.connect() as db:
            rows = db.execute(
                """
                SELECT p.post_id FROM posts p JOIN classifications c ON c.post_id=p.post_id
                WHERE p.review_status='pending' AND c.is_candidate=0
                """
            ).fetchall()
            for row in rows:
                post_id = str(row[0])
                db.execute(
                    "UPDATE posts SET review_status='ignored',review_note=?,updated_at=? WHERE post_id=?",
                    ("System-screened non-recommendation during morning workflow migration.", timestamp, post_id),
                )
                db.execute(
                    "INSERT INTO reviews(post_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                    (
                        post_id,
                        "ignored",
                        _json({"reason": "legacy_non_candidate_migration", "workflow": "morning-v1"}),
                        timestamp,
                    ),
                )
        return {"screened_non_candidates": len(rows)}

    def list_drafts(
        self,
        *,
        post_id: str | None = None,
        review_date: str | None = None,
        reviewed_date: str | None = None,
        status: str | None = None,
        queue_scope: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if post_id is not None:
            conditions.append("d.post_id=?")
            params.append(post_id)
        if review_date is not None:
            conditions.append("d.review_date=?")
            params.append(review_date)
        if reviewed_date is not None:
            conditions.append("substr(d.reviewed_at,1,10)=?")
            params.append(reviewed_date)
        if status is not None:
            conditions.append("d.status=?")
            params.append(status)
        if queue_scope is not None:
            conditions.append("d.queue_scope=?")
            params.append(queue_scope)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        with self.post_store.connect() as db:
            rows = db.execute(
                f"""
                SELECT d.*,p.platform,p.url,p.text,p.article_title,p.article_text,p.quoted_text,p.posted_at,
                    p.post_type,p.reply_to_id,p.provider_warning,p.review_status,p.local_media_json,
                    c.ocr_text,k.display_name,k.handle
                FROM recommendation_drafts d
                JOIN posts p ON p.post_id=d.post_id
                JOIN classifications c ON c.post_id=d.post_id
                JOIN kols k ON k.id=p.kol_id
                {where}
                ORDER BY p.posted_at DESC,d.id ASC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def create_bulk_snapshot(
        self,
        *,
        review_date: str,
        queue_scope: str,
        status_filter: str = "ready",
        ttl_seconds: int = 300,
        limit: int = 200,
    ) -> dict[str, Any]:
        if queue_scope not in {"morning", "backlog"}:
            raise ValueError("invalid queue scope")
        if status_filter != "ready":
            raise ValueError("bulk approval only supports ready drafts")
        drafts = self.list_drafts(
            review_date=review_date,
            queue_scope=queue_scope,
            status="ready",
            limit=max(1, min(limit, 200)),
        )
        approvable: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        for draft in drafts:
            reasons = list(draft.get("attention_reasons") or [])
            if reasons:
                for reason in reasons:
                    skipped[reason] = skipped.get(reason, 0) + 1
                continue
            approvable.append(draft)
        token = secrets.token_urlsafe(24)
        created = datetime.now(timezone.utc)
        expires = created + timedelta(seconds=max(30, min(ttl_seconds, 900)))
        with self.post_store.connect() as db:
            db.execute(
                """
                INSERT INTO recommendation_bulk_snapshots(
                    token,review_date,queue_scope,status_filter,draft_ids_json,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    token,
                    review_date,
                    queue_scope,
                    status_filter,
                    _json([int(item["id"]) for item in approvable]),
                    created.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                ),
            )
            db.execute(
                "DELETE FROM recommendation_bulk_snapshots WHERE expires_at<? OR used_at<>''",
                (created.isoformat(timespec="seconds"),),
            )
        return {
            "snapshot_token": token,
            "review_date": review_date,
            "queue_scope": queue_scope,
            "status_filter": status_filter,
            "expires_at": expires.isoformat(timespec="seconds"),
            "drafts": approvable,
            "count": len(approvable),
            "skipped": skipped,
        }

    def consume_bulk_snapshot(self, token: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.post_store.connect() as db:
            row = db.execute(
                "SELECT * FROM recommendation_bulk_snapshots WHERE token=?",
                (token,),
            ).fetchone()
            if row is None:
                raise ValueError("bulk approval snapshot not found or expired")
            if str(row["used_at"] or ""):
                raise ValueError("bulk approval snapshot has already been used")
            if str(row["expires_at"]) < now:
                raise ValueError("bulk approval snapshot has expired")
            db.execute(
                "UPDATE recommendation_bulk_snapshots SET used_at=? WHERE token=? AND used_at=''",
                (now, token),
            )
        value = dict(row)
        value["draft_ids"] = _loads(value.pop("draft_ids_json", "[]"), [])
        return value

    def get_draft(self, draft_id: int) -> dict[str, Any]:
        with self.post_store.connect() as db:
            row = db.execute(
                """
                SELECT d.*,p.platform,p.url,p.text,p.article_title,p.article_text,p.quoted_text,p.posted_at,
                    p.post_type,p.reply_to_id,p.provider_warning,p.review_status,p.local_media_json,
                    c.ocr_text,k.display_name,k.handle
                FROM recommendation_drafts d
                JOIN posts p ON p.post_id=d.post_id
                JOIN classifications c ON c.post_id=d.post_id
                JOIN kols k ON k.id=p.kol_id WHERE d.id=?
                """,
                (draft_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"recommendation draft not found: {draft_id}")
        return self._row(row)

    def morning_summary(self, review_date: str) -> dict[str, int]:
        window_start, window_end = review_window_utc(review_date)
        with self.post_store.connect() as db:
            new_posts = int(
                db.execute(
                    "SELECT COUNT(*) FROM posts WHERE posted_at_utc>=? AND posted_at_utc<?",
                    (window_start, window_end),
                ).fetchone()[0]
            )
            ai_processed = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM posts p JOIN classifications c ON c.post_id=p.post_id
                    WHERE p.posted_at_utc>=? AND p.posted_at_utc<? AND c.model_status='completed'
                    """,
                    (window_start, window_end),
                ).fetchone()[0]
            )
            ai_failed = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM posts p JOIN classifications c ON c.post_id=p.post_id
                    WHERE p.posted_at_utc>=? AND p.posted_at_utc<? AND c.model_status='failed'
                    """,
                    (window_start, window_end),
                ).fetchone()[0]
            )
            waiting_review = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM recommendation_drafts
                    WHERE review_date=? AND queue_scope='morning'
                      AND status IN ('ready','needs_attention')
                    """,
                    (review_date,),
                ).fetchone()[0]
            )
            approved_today = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM recommendation_drafts
                    WHERE substr(reviewed_at,1,10)=? AND status='approved'
                    """,
                    (review_date,),
                ).fetchone()[0]
            )
        return {
            "new_posts": new_posts,
            "ai_processed": ai_processed,
            "waiting_review": waiting_review,
            "ai_failed": ai_failed,
            "approved_today": approved_today,
        }

    def update_draft(
        self,
        draft_id: int,
        changes: Mapping[str, Any],
        instruments: Mapping[str, Mapping[str, Any]],
        *,
        note: str = "",
        correction_type: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_draft(draft_id)
        if current["status"] not in EDITABLE_DRAFT_STATUSES:
            raise ValueError("only pending recommendation drafts can be edited")
        allowed = {
            "symbol",
            "security_name",
            "direction",
            "action",
            "horizon",
            "strength",
            "thesis",
            "evidence_type",
            "evidence_spans",
            "conditions",
            "mention_kind",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported recommendation draft fields: " + ", ".join(sorted(unknown)))
        correction = correction_type or _infer_correction_type(changes)
        if correction not in CORRECTION_TYPES:
            raise ValueError("invalid correction type")
        before = _revision_snapshot(current)
        draft = {**current, **dict(changes)}
        if _evidence_is_ocr_dependent(current, draft):
            draft["evidence_source"] = "ocr"
            draft["depends_on_ocr"] = True
        attention = _attention_reasons(current, draft, instruments)
        status = "needs_attention" if attention else "ready"
        timestamp = now_iso()
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE recommendation_drafts SET symbol=?,security_name=?,direction=?,thesis=?,
                    action=?,horizon=?,strength=?,evidence_type=?,evidence_spans_json=?,evidence_source=?,depends_on_ocr=?,
                    conditions_json=?,mention_kind=?,
                    status=?,attention_reasons_json=?,review_note=?,updated_at=? WHERE id=?
                """,
                (
                    str(draft.get("symbol") or ""),
                    str(draft.get("security_name") or ""),
                    str(draft.get("direction") or ""),
                    str(draft.get("thesis") or ""),
                    str(draft.get("action") or "watch"),
                    str(draft.get("horizon") or "unspecified"),
                    str(draft.get("strength") or "unspecified"),
                    str(draft.get("evidence_type") or "ambiguous"),
                    _json(draft.get("evidence_spans") or []),
                    str(draft.get("evidence_source") or "text"),
                    int(bool(draft.get("depends_on_ocr"))),
                    _json(draft.get("conditions") or []),
                    str(draft.get("mention_kind") or "recommendation"),
                    status,
                    _json(attention),
                    note[:2000],
                    timestamp,
                    draft_id,
                ),
            )
            self._audit(db, draft_id, "edited", {"changes": dict(changes), "note": note})
        updated = self.get_draft(draft_id)
        if before != _revision_snapshot(updated):
            self._record_revision(
                draft_id,
                current["post_id"],
                correction,
                before,
                _revision_snapshot(updated),
                note,
            )
        return updated

    def create_manual_draft(
        self,
        post_id: str,
        draft: Mapping[str, Any],
        instruments: Mapping[str, Mapping[str, Any]],
        *,
        review_date: str,
        correction_type: str,
        note: str = "",
    ) -> dict[str, Any]:
        if correction_type != "missed_stock":
            raise ValueError("manual drafts require missed_stock correction type")
        post = self.post_store.get_post(post_id)
        value = {
            "symbol": str(draft.get("symbol") or ""),
            "security_name": str(draft.get("security_name") or ""),
            "direction": str(draft.get("direction") or "long"),
            "action": str(draft.get("action") or "watch"),
            "horizon": str(draft.get("horizon") or "unspecified"),
            "strength": str(draft.get("strength") or "explicit"),
            "thesis": str(draft.get("thesis") or ""),
            "evidence_type": "original_pre_event",
            "evidence_spans": list(draft.get("evidence_spans") or []),
            "conditions": list(draft.get("conditions") or []),
            "mention_kind": "recommendation",
            "evidence_source": str(draft.get("evidence_source") or "text"),
            "depends_on_ocr": bool(draft.get("depends_on_ocr")),
        }
        extraction_version = "human-v1"
        with self.post_store.connect() as db:
            active = db.execute(
                """
                SELECT id FROM recommendation_drafts
                WHERE post_id=? AND symbol=? AND status NOT IN ('rejected','superseded')
                ORDER BY id LIMIT 1
                """,
                (post_id, value["symbol"]),
            ).fetchone()
            if active is not None:
                return self.get_draft(int(active["id"]))
            existing = db.execute(
                """
                SELECT id FROM recommendation_drafts
                WHERE post_id=? AND symbol=? AND extraction_version=?
                """,
                (post_id, value["symbol"], extraction_version),
            ).fetchone()
        if existing is not None:
            return self.get_draft(int(existing["id"]))
        attention = _attention_reasons(post, value, instruments)
        status = "needs_attention" if attention else "ready"
        timestamp = now_iso()
        signature = _source_signature(post, value)
        with self.post_store.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO recommendation_drafts(
                    post_id,symbol,security_name,direction,action,horizon,strength,thesis,
                    evidence_type,evidence_spans_json,evidence_source,depends_on_ocr,
                    conditions_json,mention_kind,confidence,model_name,extraction_version,
                    source_signature,status,attention_reasons_json,queue_scope,review_date,
                    review_note,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'human',?,?,?,?,'morning',?,?,?,?)
                """,
                (
                    post_id, value["symbol"], value["security_name"], value["direction"],
                    value["action"], value["horizon"], value["strength"], value["thesis"],
                    value["evidence_type"], _json(value["evidence_spans"]), value["evidence_source"],
                    int(value["depends_on_ocr"]), _json(value["conditions"]), value["mention_kind"],
                    extraction_version, signature, status, _json(attention), review_date,
                    note[:2000], timestamp, timestamp,
                ),
            )
            draft_id = int(cursor.lastrowid)
            self._audit(db, draft_id, "created_manual", {"note": note})
        created = self.get_draft(draft_id)
        self._record_revision(
            draft_id,
            post_id,
            correction_type,
            {},
            _revision_snapshot(created),
            note,
        )
        return created

    def list_revisions(self, draft_id: int) -> list[dict[str, Any]]:
        with self.post_store.connect() as db:
            rows = db.execute(
                "SELECT * FROM draft_revisions WHERE draft_id=? ORDER BY id DESC",
                (draft_id,),
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["before"] = _loads(value.pop("before_json", "{}"), {})
            value["after"] = _loads(value.pop("after_json", "{}"), {})
            values.append(value)
        return values

    def _record_revision(
        self,
        draft_id: int,
        post_id: str,
        correction_type: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        note: str,
    ) -> None:
        with self.post_store.connect() as db:
            db.execute(
                """
                INSERT INTO draft_revisions(
                    draft_id,post_id,correction_type,before_json,after_json,note,actor,created_at
                ) VALUES(?,?,?,?,?,?,'human',?)
                """,
                (draft_id, post_id, correction_type, _json(dict(before)), _json(dict(after)), note[:2000], now_iso()),
            )

    def reject(self, draft_id: int, note: str = "") -> dict[str, Any]:
        current = self.get_draft(draft_id)
        if current["status"] == "rejected":
            return current
        if current["status"] == "approved":
            raise ValueError("approved recommendation drafts require event reconciliation")
        if current["status"] == "superseded":
            raise ValueError("superseded recommendation drafts cannot be reviewed")
        timestamp = now_iso()
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE recommendation_drafts SET status='rejected',review_note=?,reviewed_at=?,updated_at=?
                WHERE id=?
                """,
                (note[:2000], timestamp, timestamp, draft_id),
            )
            self._audit(db, draft_id, "rejected", {"note": note})
        return self.get_draft(draft_id)

    def mark_approved(self, draft_id: int, event_id: str, note: str = "") -> dict[str, Any]:
        current = self.get_draft(draft_id)
        if current["status"] == "approved":
            if current["event_id"] != event_id:
                raise ValueError("recommendation draft is bound to another event")
            return current
        if current["status"] not in EDITABLE_DRAFT_STATUSES:
            raise ValueError("recommendation draft is not approvable")
        timestamp = now_iso()
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE recommendation_drafts SET status='approved',event_id=?,review_note=?,
                    reviewed_at=?,updated_at=? WHERE id=?
                """,
                (event_id, note[:2000], timestamp, timestamp, draft_id),
            )
            self._audit(db, draft_id, "approved", {"event_id": event_id, "note": note})
        return self.get_draft(draft_id)

    @staticmethod
    def _audit(db: Any, draft_id: int, action: str, detail: Mapping[str, Any]) -> None:
        db.execute(
            """
            INSERT INTO recommendation_draft_reviews(draft_id,action,detail_json,created_at)
            VALUES(?,?,?,?)
            """,
            (draft_id, action, _json(dict(detail)), now_iso()),
        )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["evidence_spans"] = _loads(value.pop("evidence_spans_json", "[]"), [])
        value["conditions"] = _loads(value.pop("conditions_json", "[]"), [])
        value["attention_reasons"] = _loads(value.pop("attention_reasons_json", "[]"), [])
        value["depends_on_ocr"] = bool(value.get("depends_on_ocr"))
        if "local_media_json" in value:
            value["local_media"] = _loads(value.pop("local_media_json", "[]"), [])
        return value
