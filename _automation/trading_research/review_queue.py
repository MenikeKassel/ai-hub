"""Bounded, read-only projections for the review workbench.

List payloads deliberately exclude source bodies, media and model evidence.
All counts and rows are read from one SQLite snapshot and use identical scopes.
"""
from __future__ import annotations

from typing import Any

from kol_posts import KolPostStore
from recommendation_drafts import review_window_utc


def failure_kind(model_error: str, ocr_error: str = "") -> str:
    text = f"{model_error} {ocr_error}".casefold()
    if any(word in text for word in ("credential", "authentication", "unauthorized", "401", "登录")):
        return "authentication"
    if any(word in text for word in ("budget", "quota", "usage limit", "额度", "预算")):
        return "budget"
    if any(word in text for word in ("rate limit", "429", "限流")):
        return "rate_limit"
    return "ocr" if ocr_error else "model" if model_error else ""


class ReviewQueueService:
    def __init__(self, post_store: KolPostStore):
        self.post_store = post_store

    def page(self, review_date: str, *, scope: str = "morning", view: str = "pending",
             attention_only: bool = False, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        if scope != "morning" or view not in {"new", "processed", "pending", "failed", "approved"}:
            raise ValueError("invalid review queue scope or view")
        start, end = review_window_utc(review_date)
        page, page_size = max(1, page), max(1, min(page_size, 100))
        draft_scope = "d.queue_scope='morning' AND d.review_date=:day"
        source_scope = "p.posted_at_utc>=:start AND p.posted_at_utc<:end"
        approval_scope = "substr(d.reviewed_at,1,10)=:day"
        sql = f"""
            WITH scoped AS (SELECT d.* FROM recommendation_drafts d WHERE {draft_scope}),
            draft_counts AS (
                SELECT post_id,COUNT(*) draft_count,
                    SUM(status IN ('ready','needs_attention')) pending_count,
                    SUM(status='needs_attention') attention_count
                FROM scoped WHERE status<>'superseded' GROUP BY post_id
            ), approved AS (
                SELECT d.post_id,COUNT(*) approved_count FROM recommendation_drafts d
                WHERE d.status='approved' AND {approval_scope} GROUP BY d.post_id
            ), flags AS (
                SELECT p.post_id,p.posted_at,p.posted_at_utc,p.platform,p.handle,k.display_name,
                    substr(CASE WHEN p.text<>'' THEN p.text ELSE p.article_text END,1,220) excerpt,
                    COALESCE(c.model_status,'not_requested') model_status,
                    COALESCE(c.ocr_status,'not_requested') ocr_status,
                    COALESCE(c.model_error,'') model_error,COALESCE(c.ocr_error,'') ocr_error,
                    COALESCE(d.draft_count,0) draft_count,COALESCE(d.pending_count,0) pending_count,
                    COALESCE(d.attention_count,0) attention_count,COALESCE(a.approved_count,0) approved_count,
                    ({source_scope}) is_new,
                    (({source_scope}) AND c.model_status='completed') is_processed,
                    (({source_scope}) AND (c.model_status='failed' OR c.ocr_status='failed')) is_failed,
                    (COALESCE(d.pending_count,0)>0) is_pending,
                    (COALESCE(a.approved_count,0)>0) is_approved
                FROM posts p JOIN kols k ON k.id=p.kol_id
                LEFT JOIN classifications c ON c.post_id=p.post_id
                LEFT JOIN draft_counts d ON d.post_id=p.post_id
                LEFT JOIN approved a ON a.post_id=p.post_id
                WHERE ({source_scope}) OR d.post_id IS NOT NULL OR a.post_id IS NOT NULL
            )
        """
        params = {"day": review_date, "start": start, "end": end, "limit": page_size, "offset": (page-1)*page_size}
        selected = f"is_{view}=1" + (" AND attention_count>0" if attention_only and view == "pending" else "")
        count_field = "pending_count" if view == "pending" else "approved_count" if view == "approved" else "draft_count"
        with self.post_store.connect() as db:
            db.execute("BEGIN")
            counts = dict(db.execute(sql + """ SELECT
                COALESCE(SUM(is_new),0) new,COALESCE(SUM(is_processed),0) processed,
                COALESCE(SUM(is_pending),0) pending,COALESCE(SUM(is_failed),0) failed,
                COALESCE(SUM(is_approved),0) approved FROM flags""", params).fetchone())
            totals = db.execute(sql + f"SELECT COUNT(*),COALESCE(SUM({count_field}),0) FROM flags WHERE {selected}", params).fetchone()
            rows = db.execute(sql + f"SELECT * FROM flags WHERE {selected} ORDER BY posted_at_utc DESC,post_id DESC LIMIT :limit OFFSET :offset", params).fetchall()
        items = []
        for row in rows:
            item = {key: row[key] for key in ("post_id", "posted_at", "platform", "handle", "display_name", "excerpt", "model_status", "attention_count")}
            kind = failure_kind(row["model_error"], row["ocr_error"])
            if not kind:
                if row["ocr_status"] == "failed":
                    kind = "ocr"
                elif row["model_status"] == "failed":
                    kind = "model"
            item.update(draft_count=row[count_field], failure_kind=kind)
            items.append(item)
        return {"review_date": review_date, "scope": scope, "view": view, "page": page,
                "page_size": page_size, "total_posts": totals[0], "total_drafts": totals[1],
                "has_more": page*page_size < totals[0], "counts": counts, "items": items}
