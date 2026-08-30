"""Resumable reconciliation for unprocessed historical recommendation leads.

The command in this module is intentionally separate from the normal scheduler:
it works on a copied SQLite/event runtime, records every decision, and publishes
the candidate only after the integrity checks pass.  Provider and model calls
are still guarded by the existing durable leases and credential stores.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from kol_posts import (
    DeepSeekCredentialStore,
    KolPostStore,
    RapidOcrBatchClassifier,
    RuleClassifier,
    XSessionManager,
    ZhihuProfileProvider,
    build_batch_post_classifier,
    build_x_post_provider,
    classify_pending_in_batches,
    load_stock_aliases,
    normalise_zhihu_answer,
    process_pending_ocr_with_budget,
    run_post_fetch,
)
from kol_review import approve_recommendation_draft
from kol_tracker import KolStore, EventRecord, now_iso
from market_admissions import MarketAdmissionRepository
from market_data import MarketStore
from model_budget import ModelDailyBudget
from recommendation_drafts import (
    RecommendationDraftRepository,
    _attention_reasons,
    _is_reviewable_recommendation_draft,
)
from recommendation_processing import materialize_recommendation_drafts
from stock_leads import extract_stock_leads


EVENT_FILES = (
    "events.csv",
    "daily_marks.csv",
    "checkpoints.csv",
    "runs.jsonl",
    "event_revisions.jsonl",
    "checkpoint_revisions.jsonl",
)
TERMINAL_DRAFT_STATUSES = {"approved", "rejected"}
ACTIVE_DRAFT_STATUSES = {"ready", "needs_attention"}
DETERMINISTIC_REJECTION_REASONS = {
    "invalid_symbol",
    "instrument_not_found",
    "unsupported_instrument_type",
    "instrument_inactive",
    "invalid_direction",
    "invalid_action",
    "invalid_horizon",
    "invalid_strength",
    "missing_thesis",
    "not_original_pre_event",
    "not_recommendation",
    "secondhand_source",
    "missing_source_url",
    "posted_at_timezone",
    "posted_at_invalid",
    "missing_evidence",
    "evidence_not_found",
    "reply_context_required",
    "source_conflict",
    "not_currently_reviewable",
    "post_evidence_blocked",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OperationalReconciler:
    def __init__(self, repo_root: Path, *, runtime_root: Path, report_path: Path | None = None, resume_run_id: str = ""):
        self.repo_root = Path(repo_root).resolve()
        self.runtime_root = Path(runtime_root).resolve()
        self.live_kol_root = self.runtime_root / "kol"
        self.live_db = self.live_kol_root / "posts.db"
        self.media_root = self.live_kol_root / "media"
        self.report_path = report_path
        self.run_id = resume_run_id or ("reconcile-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        self.resuming = bool(resume_run_id)
        self.candidate_root = self.runtime_root / "reconcile-candidates" / self.run_id / "kol"
        self.candidate_db = self.candidate_root / "posts.db"
        self.backup_root = self.repo_root.parent / "_kol-repair-backups"
        self.lock_path = self.live_kol_root / "operational-reconcile.lock"
        self.report: dict[str, Any] = {
            "ok": False,
            "run_id": self.run_id,
            "scope": "unprocessed-history",
            "history_scope": "known-gaps",
            "model_limit_mode": "unlimited",
            "approval_gate": "strict-evidence",
            "dry_run": True,
            "phases": [],
            "counts": {},
            "decisions": [],
            "errors": [],
        }

    def _phase(self, name: str, **values: Any) -> None:
        self.report["phases"].append({"name": name, "at": now_iso(), **values})

    def stage(self) -> None:
        if not self.live_db.is_file():
            raise FileNotFoundError(f"KOL database not found: {self.live_db}")
        if self.resuming and self.candidate_db.is_file():
            self._phase("resumed", candidate_root=str(self.candidate_root))
            return
        self.candidate_root.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.live_db)
        target = sqlite3.connect(self.candidate_db)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        for name in EVENT_FILES:
            path = self.live_kol_root / name
            if path.is_file():
                shutil.copy2(path, self.candidate_root / name)
        self._phase("staged", candidate_root=str(self.candidate_root))

    def _store(self) -> KolPostStore:
        return KolPostStore(self.candidate_db, self.media_root)

    def _event_store(self) -> KolStore:
        return KolStore(self.candidate_root)

    def _market_store(self) -> MarketStore:
        return MarketStore(self.runtime_root / "market")

    @staticmethod
    def _event_keys(event_store: KolStore) -> set[tuple[str, str]]:
        return {(str(event.source_url), str(event.symbol)) for event in event_store.load_events()}

    @staticmethod
    def _terminal_draft_keys(store: KolPostStore) -> set[tuple[str, str]]:
        with store.connect() as db:
            rows = db.execute(
                "SELECT post_id,symbol FROM recommendation_drafts WHERE status IN ('approved','rejected')"
            ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}

    def _scope_keys(self, store: KolPostStore, event_store: KolStore) -> set[tuple[str, str]]:
        terminal = self._terminal_draft_keys(store)
        event_keys = self._event_keys(event_store)
        keys: set[tuple[str, str]] = set()
        with store.connect() as db:
            lead_rows = db.execute(
                """
                SELECT l.post_id,l.symbol FROM stock_leads l JOIN posts p ON p.post_id=l.post_id
                WHERE l.mention_kind='recommendation' AND p.review_status IN ('pending','capture_failed')
                """
            ).fetchall()
            draft_rows = db.execute(
                """
                SELECT d.post_id,d.symbol FROM recommendation_drafts d JOIN posts p ON p.post_id=d.post_id
                WHERE d.status IN ('ready','needs_attention') AND p.review_status IN ('pending','capture_failed')
                """
            ).fetchall()
        for row in [*lead_rows, *draft_rows]:
            key = (str(row[0]), str(row[1]))
            if key not in terminal and key not in event_keys:
                keys.add(key)
        return keys

    def _repair_processed_leads(self, store: KolPostStore) -> dict[str, int]:
        confirmed = ignored = 0
        with store.connect() as db:
            rows = db.execute(
                """
                SELECT id,post_id,symbol,status FROM stock_leads l
                WHERE status='pending' AND mention_kind='recommendation' AND (
                    EXISTS (SELECT 1 FROM recommendation_drafts d
                            WHERE d.post_id=l.post_id AND d.symbol=l.symbol AND d.status='approved')
                    OR EXISTS (SELECT 1 FROM recommendation_drafts d
                               WHERE d.post_id=l.post_id AND d.symbol=l.symbol AND d.status='rejected')
                )
                """
            ).fetchall()
        for row in rows:
            post_id, symbol, lead_id = str(row[1]), str(row[2]), int(row[0])
            with store.connect() as db:
                approved = db.execute(
                    "SELECT 1 FROM recommendation_drafts WHERE post_id=? AND symbol=? AND status='approved' LIMIT 1",
                    (post_id, symbol),
                ).fetchone()
                rejected = db.execute(
                    "SELECT 1 FROM recommendation_drafts WHERE post_id=? AND symbol=? AND status='rejected' LIMIT 1",
                    (post_id, symbol),
                ).fetchone()
            if approved:
                store.review_stock_lead(lead_id, "confirmed", "Reconciled from an existing approved event draft.")
                confirmed += 1
            elif rejected:
                store.review_stock_lead(lead_id, "ignored", "Reconciled from an existing rejected event draft.")
                ignored += 1
        return {"confirmed": confirmed, "ignored": ignored}

    def _supersede_terminal_post_drafts(self, store: KolPostStore) -> int:
        """Hide late drafts emitted after a post already reached a terminal review."""
        timestamp = now_iso()
        with store.connect() as db:
            rows = db.execute(
                """
                SELECT d.id FROM recommendation_drafts d JOIN posts p ON p.post_id=d.post_id
                WHERE d.status IN ('ready','needs_attention')
                  AND p.review_status IN ('approved','ignored','excluded')
                """
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE recommendation_drafts SET status='superseded',review_note=?,updated_at=? WHERE id=?",
                    ("Historical reconciliation: post already has a terminal review.", timestamp, int(row[0])),
                )
                db.execute(
                    "INSERT INTO recommendation_draft_reviews(draft_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                    (int(row[0]), "superseded", _json({"reason": "terminal_post_review"}), timestamp),
                )
        return len(rows)

    def _repair_unrecoverable_events(self, store: KolPostStore, event_store: KolStore) -> int:
        changed: list[EventRecord] = []
        for event in event_store.load_events():
            try:
                post = store.get_post(str(event.source_post_id)) if event.source_post_id else None
            except KeyError:
                post = None
            if not post:
                continue
            source_text = "".join(
                str(post.get(key) or "") for key in ("text", "article_text", "quoted_text", "ocr_text")
            ).strip()
            if source_text:
                continue
            warnings = [value for value in str(event.execution_warning or "").split(";") if value]
            if "source_unrecoverable" not in warnings:
                warnings.append("source_unrecoverable")
            changed.append(EventRecord(**{**event.__dict__, "execution_warning": ";".join(warnings), "updated_at": now_iso()}))
        if changed:
            changed_by_id = {event.event_id: event for event in changed}
            event_store.save_events([
                changed_by_id.get(event.event_id, event) for event in event_store.load_events()
            ])
        return len(changed)

    def _recover_known_gaps(self, store: KolPostStore) -> dict[str, Any]:
        """Drain only durable, currently-unarchived fetch batches.

        This deliberately does not start a lifetime account crawl.  Each
        existing batch keeps its key and cursor, so a retry is idempotent and
        old archived batches remain audit history.
        """
        xtf = self.repo_root / "_runtime" / "venv-x-fetcher" / "Scripts" / "xtf.exe"
        twitter = shutil.which("twitter") or str(Path.home() / ".local" / "bin" / "twitter.exe")
        results: list[dict[str, Any]] = []
        for platform in ("X", "Zhihu"):
            safety = 0
            while safety < 200:
                safety += 1
                batch_key = self._next_runnable_batch(store, platform)
                if not batch_key:
                    break
                if platform == "X":
                    provider = build_x_post_provider(
                        "auto",
                        twitter_command=twitter,
                        xtf_command=str(xtf),
                        nitter_url="http://127.0.0.1:9377",
                        fallback_mode="disabled",
                        twitter_timeout_seconds=90,
                        session_manager=XSessionManager(store),
                        batch_key=batch_key,
                        history_mode=False,
                    )
                    providers = {}
                    key_platforms = {"x"}
                else:
                    provider = ZhihuProfileProvider(
                        self.repo_root / "_automation" / "hermes-capture" / "zhihu_profile_capture.py",
                        python_command=sys.executable,
                        browser_path=os.environ.get("ZHIHU_BROWSER_PATH", ""),
                        profile_directory=os.environ.get("ZHIHU_PROFILE_DIRECTORY", "Default"),
                        user_data_dir=os.environ.get(
                            "ZHIHU_USER_DATA_DIR",
                            str(Path.home() / "AppData" / "Local" / "hermes" / "browser-profiles" / "zhihu-edge"),
                        ),
                        port=int(os.environ.get("ZHIHU_CDP_PORT", "9223")),
                    )
                    providers = {"zhihu": provider}
                    key_platforms = {"zhihu"}
                try:
                    result = run_post_fetch(
                        store,
                        provider,
                        platform_providers=providers,
                        platforms=key_platforms,
                        max_count=50,
                        classifier=RuleClassifier(load_stock_aliases(self.runtime_root / "watchlist.csv", self.candidate_root / "events.csv")),
                        download_media=True,
                        sleep_seconds=0,
                        retry_delays=(),
                        batch_key=batch_key,
                        reconcile_zhihu=False,
                    )
                    results.append({"platform": platform, "batch_key": batch_key, **result.__dict__})
                except Exception as exc:
                    self.report["errors"].append({"phase": "known_gap", "platform": platform, "batch_key": batch_key, "error": str(exc)[:2000]})
                    break
                # Continue with another runnable batch when this batch only
                # has deferred items.  The deferred rows remain durable and
                # are retried by the next scheduled window.
                if result.rate_limit_paused or result.blocked_platforms:
                    break
        self._phase("known_gaps", batches=len(results))
        return {
            "runs": len(results),
            "x_queue": store.fetch_queue_overview("X"),
            "zhihu_queue": store.fetch_queue_overview("Zhihu"),
            "results": results[-20:],
        }

    @staticmethod
    def _next_runnable_batch(store: KolPostStore, platform: str) -> str:
        now = now_iso()
        with store.connect() as db:
            row = db.execute(
                """
                SELECT q.batch_key
                FROM fetch_queue q
                WHERE lower(q.platform)=?
                  AND (q.state='queued' OR (q.state='cooldown' AND (q.not_before='' OR q.not_before<=?)))
                  AND NOT EXISTS (SELECT 1 FROM fetch_batch_archive a WHERE a.batch_key=q.batch_key)
                GROUP BY q.batch_key
                ORDER BY MAX(q.updated_at) DESC
                LIMIT 1
                """,
                (platform.casefold(), now),
            ).fetchone()
        return str(row[0]) if row else ""

    def _classify_and_materialize(self, store: KolPostStore, scope: set[tuple[str, str]], instruments: dict[str, dict[str, Any]]) -> dict[str, int]:
        post_ids = sorted({post_id for post_id, _ in scope})
        before = store.model_queue_summary(daily_limit=250)
        ocr_completed = ocr_failed = 0
        try:
            ocr_python = self.repo_root / "_runtime" / "venv-ocr-fast" / "Scripts" / "python.exe"
            ocr_runner = self.repo_root / "_automation" / "trading_research" / "rapid_ocr_batch.py"
            ocr_completed, ocr_failed = process_pending_ocr_with_budget(
                store,
                RapidOcrBatchClassifier(ocr_python, ocr_runner, timeout_seconds=90),
                RuleClassifier(load_stock_aliases(self.runtime_root / "watchlist.csv", self.candidate_root / "events.csv")),
                daily_limit=0,
                batch_size=50,
            )
        except Exception as exc:
            self.report["errors"].append({"phase": "ocr", "error": str(exc)[:2000]})
        classifier = build_batch_post_classifier(
            self.repo_root / "_automation" / "trading_research" / "kol_batch_classifier_schema.json",
            self.repo_root,
            deepseek_credentials=DeepSeekCredentialStore(),
        )
        aliases = load_stock_aliases(self.runtime_root / "watchlist.csv", self.candidate_root / "events.csv")
        rule_classifier = RuleClassifier(aliases)
        draft_repository = RecommendationDraftRepository(store)
        # Re-run the deterministic pass first. Legacy rows often contain rule
        # symbols but were never materialized, so the model queue skips them
        # until this pass produces a durable result.
        rule_materialized = 0
        for post_id in post_ids:
            try:
                materialize_recommendation_drafts(
                    store,
                    draft_repository,
                    rule_classifier,
                    post_id,
                    instruments=instruments,
                    queue_scope="backlog",
                    review_date=datetime.now().date().isoformat(),
                    rules_first=True,
                )
                rule_materialized += 1
            except Exception as exc:
                self.report["errors"].append({"phase": "rules", "post_id": post_id, "error": str(exc)[:1000]})
        reopened = self._prepare_unmaterialized_model_queue(store)
        completed = failed = 0
        try:
            # A zero daily_limit disables only the local model counter for this
            # maintenance process; the worker mutex and provider errors remain.
            completed, failed = classify_pending_in_batches(
                store, classifier, limit=0, daily_limit=0
            )
        except Exception as exc:
            failed += 1
            self.report["errors"].append({"phase": "model", "error": str(exc)[:2000]})
        materialized = 0
        for post_id in post_ids:
            try:
                current = store.get_post(post_id)
                if not current:
                    continue
                if str(current.get("model_status") or "") in {"completed", "not_needed"}:
                    materialize_recommendation_drafts(
                        store,
                        draft_repository,
                        rule_classifier,
                        post_id,
                        instruments=instruments,
                        queue_scope="backlog",
                        review_date=datetime.now().date().isoformat(),
                        rules_first=False,
                    )
                    materialized += 1
            except Exception as exc:
                self.report["errors"].append({"phase": "materialize", "post_id": post_id, "error": str(exc)[:1000]})
        after = store.model_queue_summary(daily_limit=250)
        return {
            "model_completed": completed,
            "model_failed": failed,
            "ocr_completed": ocr_completed,
            "ocr_failed": ocr_failed,
            "rule_materialized_posts": rule_materialized,
            "legacy_model_rows_reopened": reopened,
            "materialized_posts": materialized,
            "model_before_processable": int(before.get("processable_remaining") or 0),
            "model_after_processable": int(after.get("processable_remaining") or 0),
        }

    @staticmethod
    def _image_evidence_is_ready(post: dict[str, Any], draft: dict[str, Any]) -> bool:
        if str(post.get("ocr_status") or "") != "completed":
            return False
        media = list(post.get("local_media") or [])
        if not media or any(not Path(str(item.get("path") or "")).is_file() for item in media):
            return False
        source = "\n".join(str(post.get(key) or "") for key in ("text", "article_text", "quoted_text", "ocr_text"))
        return all(str(span).strip() and str(span).strip() in source for span in draft.get("evidence_spans") or [])

    def _validate_active_drafts(self, store: KolPostStore, scope: set[tuple[str, str]], instruments: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        repository = RecommendationDraftRepository(store)
        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for post_id, symbol in sorted(scope):
            for draft in repository.list_drafts(post_id=post_id, limit=100):
                if int(draft["id"]) in seen_ids or draft["status"] not in ACTIVE_DRAFT_STATUSES:
                    continue
                if str(draft.get("symbol") or "") != symbol:
                    continue
                seen_ids.add(int(draft["id"]))
                post = store.get_post(post_id)
                reasons = set(_attention_reasons(post, draft, instruments))
                if "image_dependency" in reasons and self._image_evidence_is_ready(post, draft):
                    reasons.remove("image_dependency")
                if not _is_reviewable_recommendation_draft(post, draft, instruments):
                    reasons.add("not_currently_reviewable")
                if str(post.get("evidence_type") or "") in {"retrospective", "secondhand"}:
                    reasons.add("post_evidence_blocked")
                if reasons:
                    item = {"draft_id": int(draft["id"]), "post_id": post_id, "symbol": symbol, "reasons": sorted(reasons)}
                    if reasons <= DETERMINISTIC_REJECTION_REASONS | {"image_dependency"} and "image_dependency" not in reasons:
                        rejected.append(item)
                    else:
                        self.report["decisions"].append({**item, "decision": "needs_attention"})
                    continue
                candidates.append({"draft": draft, "post": post, "instrument": instruments.get(symbol) or {}})
        return candidates, rejected

    def _deduplicate(self, candidates: list[dict[str, Any]], instruments: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            grouped[(str(item["post"]["url"]), str(item["draft"]["symbol"]))].append(item)
        selected: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for key, items in grouped.items():
            instrument_name = str((instruments.get(key[1]) or {}).get("name") or "")
            scored: list[tuple[tuple[int, int, float, int], dict[str, Any]]] = []
            for item in items:
                draft = item["draft"]
                source = "\n".join(str(item["post"].get(k) or "") for k in ("text", "article_text", "quoted_text", "ocr_text"))
                spans = list(draft.get("evidence_spans") or [])
                identity = int(str(draft.get("security_name") or "") == instrument_name)
                identity += int(instrument_name and instrument_name in source)
                identity += int(str(draft.get("symbol") or "") in source)
                evidence_len = sum(len(str(span)) for span in spans)
                score = (identity, int(bool(instrument_name and instrument_name in source)), float(draft.get("confidence") or 0), evidence_len)
                scored.append((score, item))
            scored.sort(key=lambda value: (value[0], int(value[1]["draft"]["id"])), reverse=True)
            best_score, best = scored[0]
            tied = [item for score, item in scored if score == best_score]
            if len(tied) > 1:
                fingerprints = {(str(item["draft"].get("thesis") or ""), tuple(item["draft"].get("evidence_spans") or [])) for item in tied}
                if len(fingerprints) > 1:
                    for item in tied:
                        rejected.append({"draft_id": int(item["draft"]["id"]), "post_id": item["post"]["post_id"], "symbol": key[1], "reasons": ["duplicate_conflict"]})
                    continue
            selected.append(best)
            for _, item in scored[1:]:
                rejected.append({"draft_id": int(item["draft"]["id"]), "post_id": item["post"]["post_id"], "symbol": key[1], "reasons": ["duplicate_noncanonical"]})
        return selected, rejected

    def _ensure_lead(self, store: KolPostStore, item: dict[str, Any]) -> None:
        draft = item["draft"]
        post = item["post"]
        instrument = item["instrument"]
        leads = store.list_stock_leads(post_id=post["post_id"], symbol=draft["symbol"], limit=10)
        if not leads:
            store.upsert_stock_lead({
                "post_id": post["post_id"],
                "kol_id": post["kol_id"],
                "symbol": draft["symbol"],
                "security_name": instrument["name"],
                "instrument_type": instrument["instrument_type"],
                "direction": draft["direction"],
                "mention_kind": "recommendation",
                "evidence_text": draft["thesis"],
                "extraction_method": "operational_reconcile",
                "confidence": draft.get("confidence") or 0,
                "status": "pending",
            })
            leads = store.list_stock_leads(post_id=post["post_id"], symbol=draft["symbol"], limit=10)
        if leads and leads[0]["status"] != "confirmed":
            store.review_stock_lead(
                int(leads[0]["id"]),
                "confirmed",
                "Confirmed during strict historical recommendation reconciliation.",
                symbol=draft["symbol"],
                security_name=instrument["name"],
            )

    def _resolve_unmaterialized_leads(self, store: KolPostStore) -> int:
        """Close deterministic no-draft recommendations without fabricating events."""
        closed = 0
        with store.connect() as db:
            rows = db.execute(
                """
                SELECT l.id,l.post_id,l.symbol,c.model_status,c.model_attempts,c.draft_generation_status,
                       c.ocr_attempts,p.review_status
                FROM stock_leads l JOIN classifications c ON c.post_id=l.post_id
                JOIN posts p ON p.post_id=l.post_id
                WHERE l.status='pending' AND l.mention_kind='recommendation'
                  AND NOT EXISTS (
                      SELECT 1 FROM recommendation_drafts d
                      WHERE d.post_id=l.post_id AND d.symbol=l.symbol
                        AND d.status IN ('ready','needs_attention','approved','rejected')
                  )
                """
            ).fetchall()
        terminal_states = {"completed", "not_needed"}
        no_draft_states = {"pending", "generated", "needs_attention", "not_applicable", "failed"}
        for row in rows:
            if str(row[7]) in {"ignored", "excluded", "approved"} or int(row[6] or 0) >= 3:
                store.review_stock_lead(
                    int(row[0]),
                    "ignored",
                    "Historical reconciliation: source post already has a terminal review state.",
                )
                closed += 1
                continue
            if str(row[3]) not in terminal_states or str(row[5]) not in no_draft_states:
                continue
            # A failed model is only closed when it has exhausted its retry
            # budget; transient failures remain pending for the next run.
            if str(row[3]) == "completed" and str(row[5]) == "failed" and int(row[4] or 0) < 3:
                continue
            store.review_stock_lead(
                int(row[0]),
                "ignored",
                "Historical reconciliation: no verifiable event draft was produced.",
            )
            closed += 1
        return closed

    def _prepare_unmaterialized_model_queue(self, store: KolPostStore) -> int:
        """Reopen legacy rule-only rows that never produced a draft."""
        with store.connect() as db:
            cursor = db.execute(
                """
                UPDATE classifications SET is_candidate=1,model_status='not_requested',model_name='',
                    rule_symbols_json='[]',rule_direction='',model_error='',updated_at=?
                WHERE post_id IN (
                    SELECT l.post_id FROM stock_leads l JOIN posts p ON p.post_id=l.post_id
                    WHERE l.status='pending' AND l.mention_kind='recommendation'
                      AND p.review_status IN ('pending','capture_failed')
                      AND NOT EXISTS (
                          SELECT 1 FROM recommendation_drafts d
                          WHERE d.post_id=l.post_id AND d.symbol=l.symbol
                            AND d.status IN ('ready','needs_attention','approved','rejected')
                      )
                )
                AND model_status IN ('not_needed','not_requested')
                AND draft_generation_status='pending'
                """,
                (now_iso(),),
            )
        return int(cursor.rowcount)

    def _apply_decisions(self, store: KolPostStore, event_store: KolStore, market_admissions: MarketAdmissionRepository, candidates: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> dict[str, int]:
        repository = RecommendationDraftRepository(store)
        approved = rejected_count = failed = 0
        for item in rejected:
            try:
                repository.reject(int(item["draft_id"]), "Historical reconciliation: " + ", ".join(item["reasons"]))
                rejected_count += 1
                self.report["decisions"].append({**item, "decision": "rejected"})
            except Exception as exc:
                failed += 1
                self.report["errors"].append({"phase": "reject", "draft_id": item["draft_id"], "error": str(exc)[:1000]})
        for item in candidates:
            draft = item["draft"]
            instrument = item["instrument"]
            try:
                self._ensure_lead(store, item)
                event_draft = {
                    "symbol": draft["symbol"],
                    "security_name": instrument["name"],
                    "direction": draft["direction"],
                    "thesis": draft["thesis"],
                    "evidence_type": draft["evidence_type"],
                    "conditions": draft.get("conditions") or [],
                }
                result = approve_recommendation_draft(
                    store,
                    event_store,
                    draft["post_id"],
                    event_draft,
                    note="Strict historical recommendation reconciliation.",
                    audit_detail={"reconcile_run_id": self.run_id, "draft_id": int(draft["id"])},
                )
                repository.mark_approved(int(draft["id"]), result.event_ids[0], "Strict historical recommendation reconciliation.")
                market_admissions.queue_confirmed_symbols([draft["symbol"]], as_of="2026-08-28")
                approved += 1
                self.report["decisions"].append({"draft_id": int(draft["id"]), "post_id": draft["post_id"], "symbol": draft["symbol"], "decision": "approved", "event_id": result.event_ids[0]})
            except Exception as exc:
                if isinstance(exc, ValueError) and any(
                    marker in str(exc) for marker in ("no longer approvable: ignored", "no longer approvable: excluded")
                ):
                    try:
                        repository.reject(int(draft["id"]), "Historical reconciliation: source post is archive-only.")
                        rejected_count += 1
                        self.report["decisions"].append({"draft_id": int(draft["id"]), "post_id": draft["post_id"], "symbol": draft["symbol"], "decision": "rejected", "reasons": ["archive_only_post"]})
                        continue
                    except Exception as reject_exc:
                        exc = reject_exc
                failed += 1
                self.report["errors"].append({"phase": "approve", "draft_id": int(draft["id"]), "error": str(exc)[:1000]})
        return {"approved": approved, "rejected": rejected_count, "failed": failed}

    def _write_report(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    def execute(self, *, apply: bool = False, report_path: Path | None = None) -> dict[str, Any]:
        self.report["dry_run"] = not apply
        if report_path:
            self.report_path = report_path
        self.stage()
        store = self._store()
        event_store = self._event_store()
        market_store = self._market_store()
        instruments = market_store.instrument_map()
        repaired = self._repair_processed_leads(store)
        superseded = self._supersede_terminal_post_drafts(store)
        old_warning_events = self._repair_unrecoverable_events(store, event_store)
        scope = self._scope_keys(store, event_store)
        self.report["counts"].update({"unprocessed_unique_keys": len(scope), "processed_leads_repaired_confirmed": repaired["confirmed"], "processed_leads_repaired_ignored": repaired["ignored"], "terminal_post_drafts_superseded": superseded, "source_unrecoverable_events_warned": old_warning_events})
        self._phase("scoped", count=len(scope))
        if not apply:
            repository = RecommendationDraftRepository(store)
            active_keys: set[tuple[str, str]] = set()
            active_rows = 0
            for post_id, symbol in scope:
                for item in repository.list_drafts(post_id=post_id, limit=100):
                    if item["symbol"] == symbol and item["status"] in ACTIVE_DRAFT_STATUSES:
                        active_rows += 1
                        active_keys.add((post_id, symbol))
            self.report["counts"]["active_draft_rows"] = active_rows
            self.report["counts"]["active_draft_keys"] = len(active_keys)
            self.report["counts"]["lead_only_keys"] = max(0, len(scope) - len(active_keys))
            self.report["counts"]["known_gap_queue"] = {
                "x": store.fetch_queue_overview("X"),
                "zhihu": store.fetch_queue_overview("Zhihu"),
            }
            self.report["ok"] = True
            self._write_report(self.report_path or self.runtime_root / "restore-reports" / f"kol-operational-reconcile-{self.run_id}.json")
            return self.report
        self.report["counts"]["known_gap_recovery"] = self._recover_known_gaps(store)
        self.report["counts"].update(self._classify_and_materialize(store, scope, instruments))
        # Re-extract only the scoped posts so draft-only items acquire a lead
        # and existing posts retain their original body/media/raw fields.
        aliases = load_stock_aliases(self.runtime_root / "watchlist.csv", self.candidate_root / "events.csv")
        extract_stock_leads(store, instruments=instruments, aliases=aliases, post_ids=sorted({post_id for post_id, _ in scope}))
        self.report["counts"]["unmaterialized_leads_ignored"] = self._resolve_unmaterialized_leads(store)
        scope = self._scope_keys(store, event_store)
        candidates, rejected = self._validate_active_drafts(store, scope, instruments)
        candidates, duplicate_rejected = self._deduplicate(candidates, instruments)
        rejected.extend(duplicate_rejected)
        self.report["counts"].update({"candidate_drafts": len(candidates), "deterministic_rejections": len(rejected)})
        market_admissions = MarketAdmissionRepository(store)
        self.report["counts"].update(self._apply_decisions(store, event_store, market_admissions, candidates, rejected))
        with store.connect() as db:
            db.execute(
                "UPDATE market_symbol_admissions SET status='failed',error='instrument delisted; no current daily series' WHERE symbol='000861' AND status='pending'"
            )
        self.report["counts"]["admissions"] = market_admissions.summary()
        known_gap = self.report["counts"].get("known_gap_recovery") or {}
        pending_gap_items = int((known_gap.get("x_queue") or {}).get("total_items") or 0) + int(
            (known_gap.get("zhihu_queue") or {}).get("total_items") or 0
        )
        self.report["counts"]["known_gaps_complete"] = pending_gap_items == 0
        self.report["status"] = "completed" if pending_gap_items == 0 and not self.report["errors"] else "partial"
        self.report["ok"] = not self.report["errors"]
        self._phase("completed", ok=self.report["ok"])
        self._write_report(self.report_path or self.runtime_root / "restore-reports" / f"kol-operational-reconcile-{self.run_id}.json")
        return self.report

    def publish(self) -> Path:
        if not self.report.get("ok") or self.report.get("dry_run"):
            raise RuntimeError("candidate is not ready to publish")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.backup_root / f"operational-reconcile-{timestamp}"
        backup.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.live_db)
        target = sqlite3.connect(backup / "posts.db")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        for name in EVENT_FILES:
            path = self.live_kol_root / name
            if path.is_file():
                shutil.copy2(path, backup / name)
        with FileLock(str(self.lock_path), timeout=60):
            tmp_db = self.live_db.with_suffix(".reconcile.tmp")
            shutil.copy2(self.candidate_db, tmp_db)
            os.replace(tmp_db, self.live_db)
            for name in EVENT_FILES:
                source_file = self.candidate_root / name
                if source_file.is_file():
                    tmp_file = self.live_kol_root / f"{name}.reconcile.tmp"
                    shutil.copy2(source_file, tmp_file)
                    os.replace(tmp_file, self.live_kol_root / name)
        self.report["published"] = True
        self.report["backup_dir"] = str(backup)
        if self.report_path:
            self._write_report(self.report_path)
        return backup
