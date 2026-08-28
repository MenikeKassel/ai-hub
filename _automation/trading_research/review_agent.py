from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from kol_posts import STRUCTURED_REVIEW_VERSION, KolPostStore, RuleClassifier, now_iso


POLICY_VERSION = "review-agent-v1"
AUTO_APPROVE_THRESHOLD = 0.95
AUTO_DISMISS_THRESHOLD = 0.98
MAX_OCR_STAGE_SECONDS = 90
DECISIONS = {"auto_approve", "auto_exclude", "auto_ignore", "needs_human", "failed"}
CONDITIONAL_PATTERN = re.compile(r"(?:如果|若|等待|站稳|突破|回踩|企稳|确认后|再买|再加仓)")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def decision_input_signature(post: dict[str, Any]) -> str:
    fields = {
        "post_id": post.get("post_id"),
        "posted_at": post.get("posted_at"),
        "post_type": post.get("post_type"),
        "text": post.get("text"),
        "article_text": post.get("article_text"),
        "quoted_text": post.get("quoted_text"),
        "provider_warning": post.get("provider_warning"),
        "content_type": post.get("content_type"),
        "evidence_type": post.get("evidence_type"),
        "model_status": post.get("model_status"),
        "confidence": post.get("confidence"),
        "drafts": post.get("drafts", []),
        "ocr_status": post.get("ocr_status"),
        "ocr_text": post.get("ocr_text"),
    }
    return hashlib.sha256(_json(fields).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolicyDecision:
    decision: str
    confidence: float
    reason_codes: list[str]
    validator_errors: list[str]
    drafts: list[dict[str, Any]]
    evidence: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReviewPolicy:
    version = POLICY_VERSION

    def evaluate(
        self,
        post: dict[str, Any],
        instrument_lookup: Callable[[str], dict[str, Any] | None],
    ) -> PolicyDecision:
        confidence = float(post.get("confidence") or 0)
        drafts = [dict(value) for value in post.get("drafts", []) if isinstance(value, dict)]
        evidence_type = str(post.get("evidence_type") or "ambiguous")
        content_type = str(post.get("content_type") or "other")
        model_status = str(post.get("model_status") or "not_requested")

        if model_status == "failed":
            return PolicyDecision("failed", confidence, ["model_failed"], ["model_failed"], drafts, [])
        if post.get("post_type") == "retweet":
            return PolicyDecision("auto_ignore", 1.0, ["pure_retweet"], [], drafts, [])
        if evidence_type in {"retrospective", "secondhand"} and confidence >= AUTO_DISMISS_THRESHOLD:
            return PolicyDecision(
                "auto_exclude",
                confidence,
                [f"high_confidence_{evidence_type}"],
                [],
                drafts,
                [],
            )
        if content_type in {"other", "methodology"} and not drafts and confidence >= AUTO_DISMISS_THRESHOLD:
            return PolicyDecision(
                "auto_ignore",
                confidence,
                [f"high_confidence_{content_type}"],
                [],
                drafts,
                [],
            )

        errors: list[str] = []
        reasons: list[str] = []
        evidence: list[str] = []
        warnings = {value for value in str(post.get("provider_warning") or "").split(";") if value}
        if "source_conflict" in warnings:
            errors.append("source_conflict")
        if post.get("reply_to_id"):
            errors.append("reply_context_required")
        if post.get("post_type") != "original":
            errors.append("non_original_post")
        try:
            posted_at = datetime.fromisoformat(str(post.get("posted_at") or ""))
            if posted_at.tzinfo is None:
                errors.append("posted_at_timezone")
        except ValueError:
            errors.append("posted_at_invalid")
        if model_status != "completed":
            errors.append("model_not_completed")
        if content_type != "recommendation":
            errors.append("not_recommendation")
        if evidence_type != "original_pre_event":
            errors.append("not_original_pre_event")
        if confidence < AUTO_APPROVE_THRESHOLD:
            errors.append("low_overall_confidence")
        if len(drafts) != 1:
            errors.append("multiple_drafts" if len(drafts) > 1 else "missing_draft")

        source_text = "\n".join(
            value for value in [str(post.get("text") or ""), str(post.get("article_text") or "")] if value
        )
        if post.get("local_media"):
            errors.append("image_or_ocr_dependent")
        mentioned_symbols = {
            symbol
            for symbol in {
                *re.findall(r"(?<!\d)(\d{6})(?!\d)", source_text),
                *(str(value) for value in post.get("rule_symbols", [])),
            }
            if instrument_lookup(symbol)
            and str((instrument_lookup(symbol) or {}).get("instrument_type") or "") == "stock"
        }
        if len(mentioned_symbols) > 1:
            errors.append("multiple_stock_mentions")
        if len(drafts) == 1:
            draft = drafts[0]
            symbol = str(draft.get("symbol") or "")
            security_name = str(draft.get("security_name") or "").strip()
            if not re.fullmatch(r"\d{6}", symbol):
                errors.append("invalid_symbol")
            if draft.get("direction") != "long":
                errors.append("short_direction")
            if draft.get("evidence_type") != "original_pre_event":
                errors.append("draft_not_original_pre_event")
            if float(draft.get("confidence") or 0) < AUTO_APPROVE_THRESHOLD:
                errors.append("low_draft_confidence")
            thesis = str(draft.get("thesis") or "").strip()
            if len(re.sub(r"\s+", "", thesis)) < 4:
                errors.append("missing_thesis")
            if draft.get("mention_kind") != "recommendation":
                errors.append("not_recommendation_mention")
            if bool(draft.get("depends_on_ocr")) or draft.get("evidence_source") in {"ocr", "image"}:
                errors.append("image_or_ocr_dependent")
            conditions = [str(value).strip() for value in draft.get("conditions", []) if str(value).strip()]
            if conditions or CONDITIONAL_PATTERN.search(source_text):
                errors.append("conditional_entry")
            spans = [str(value).strip() for value in draft.get("evidence_spans", []) if str(value).strip()]
            if not spans:
                errors.append("missing_evidence_spans")
            for span in spans:
                if span not in source_text:
                    errors.append("evidence_span_missing")
                else:
                    evidence.append(span)
            instrument = instrument_lookup(symbol) if re.fullmatch(r"\d{6}", symbol) else None
            if not instrument:
                errors.append("instrument_not_found")
            elif instrument.get("instrument_type") != "stock":
                errors.append("unsupported_instrument_type")
            elif str(instrument.get("status") or "active") not in {"active", "1"}:
                errors.append("instrument_inactive")
            instrument_name = str((instrument or {}).get("name") or security_name)
            if not security_name:
                errors.append("missing_security_name")
            if symbol not in source_text and not (instrument_name and instrument_name in source_text):
                errors.append("instrument_not_explicit")
            if security_name and instrument_name and security_name != instrument_name:
                errors.append("instrument_name_conflict")

        errors = sorted(set(errors))
        if errors:
            return PolicyDecision("needs_human", confidence, ["policy_gate_failed"], errors, drafts, evidence)
        reasons.append("strict_text_recommendation")
        return PolicyDecision("auto_approve", confidence, reasons, [], drafts, evidence)


class ReviewAgentRepository:
    def __init__(self, post_store: KolPostStore):
        self.post_store = post_store

    def get_settings(self) -> dict[str, Any]:
        with self.post_store.connect() as db:
            row = db.execute("SELECT * FROM review_agent_settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("review agent settings are not initialized")
        return dict(row)

    def start_run(self, mode: str) -> str:
        if mode not in {"shadow", "enabled"}:
            raise ValueError("invalid review agent mode")
        run_id = uuid.uuid4().hex
        with self.post_store.connect() as db:
            db.execute(
                "INSERT INTO review_agent_runs(run_id,mode,policy_version,started_at,status) VALUES(?,?,?,?,?)",
                (run_id, mode, POLICY_VERSION, now_iso(), "running"),
            )
        return run_id

    def recover_interrupted_runs(self) -> int:
        with self.post_store.connect() as db:
            cursor = db.execute(
                """
                UPDATE review_agent_runs SET completed_at=?,status='failed',
                    errors_json=? WHERE status='running'
                """,
                (now_iso(), _json(["worker exited before the run was finalized"])),
            )
        return max(0, cursor.rowcount)

    def finish_run(self, run_id: str, counts: dict[str, int], errors: list[str]) -> None:
        status = (
            "failed"
            if errors and not counts.get("processed_count")
            else "partial"
            if errors or counts.get("failed")
            else "success"
        )
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE review_agent_runs SET completed_at=?,status=?,processed_count=?,
                    auto_approved=?,auto_excluded=?,auto_ignored=?,needs_human=?,failed=?,errors_json=?
                WHERE run_id=?
                """,
                (
                    now_iso(),
                    status,
                    counts.get("processed_count", 0),
                    counts.get("auto_approve", 0),
                    counts.get("auto_exclude", 0),
                    counts.get("auto_ignore", 0),
                    counts.get("needs_human", 0),
                    counts.get("failed", 0),
                    _json(errors),
                    run_id,
                ),
            )

    def save_decision(
        self,
        post: dict[str, Any],
        run_id: str,
        mode: str,
        result: PolicyDecision,
    ) -> dict[str, Any]:
        if result.decision not in DECISIONS:
            raise ValueError("invalid review agent decision")
        signature = decision_input_signature(post)
        with self.post_store.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO review_agent_decisions(
                    post_id,run_id,mode,policy_version,input_signature,decision,confidence,
                    reason_codes_json,evidence_json,drafts_json,validator_errors_json,status,
                    model_name,prompt_version,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    post["post_id"],
                    run_id,
                    mode,
                    POLICY_VERSION,
                    signature,
                    result.decision,
                    result.confidence,
                    _json(result.reason_codes),
                    _json(result.evidence),
                    _json(result.drafts),
                    _json(result.validator_errors),
                    "failed" if result.decision == "failed" else "proposed",
                    str(post.get("model_name") or ""),
                    str(post.get("prompt_version") or ""),
                    now_iso(),
                ),
            )
            row = db.execute(
                """
                SELECT * FROM review_agent_decisions
                WHERE post_id=? AND input_signature=? AND policy_version=? AND mode=?
                """,
                (post["post_id"], signature, POLICY_VERSION, mode),
            ).fetchone()
        if row is None:
            raise RuntimeError("review agent decision was not saved")
        return self._decision_row(row)

    def get_decision(self, decision_id: int) -> dict[str, Any]:
        with self.post_store.connect() as db:
            row = db.execute("SELECT * FROM review_agent_decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(f"review agent decision not found: {decision_id}")
        return self._decision_row(row)

    def current_decision(self, post: dict[str, Any], mode: str) -> dict[str, Any] | None:
        signature = decision_input_signature(post)
        with self.post_store.connect() as db:
            row = db.execute(
                """
                SELECT * FROM review_agent_decisions
                WHERE post_id=? AND input_signature=? AND policy_version=? AND mode=?
                """,
                (post["post_id"], signature, POLICY_VERSION, mode),
            ).fetchone()
        return self._decision_row(row) if row else None

    def list_decisions(
        self,
        *,
        decision: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if decision:
            clauses.append("d.decision=?")
            params.append(decision)
        if status:
            clauses.append("d.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self.post_store.connect() as db:
            rows = db.execute(
                f"""
                SELECT d.*,p.url,p.text,p.posted_at,p.review_status,k.display_name,k.handle
                FROM review_agent_decisions d
                JOIN posts p ON p.post_id=d.post_id
                JOIN kols k ON k.id=p.kol_id
                {where} ORDER BY d.created_at DESC,d.id DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._decision_row(row) for row in rows]

    def mark_applied(
        self,
        decision_id: int,
        event_ids: list[str],
        *,
        created_event_ids: list[str] | None = None,
        side_effects: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE review_agent_decisions SET status='applied',event_ids_json=?,
                    created_event_ids_json=?,side_effects_json=?,applied_at=? WHERE id=?
                """,
                (
                    _json(event_ids),
                    _json(created_event_ids or []),
                    _json(side_effects or {}),
                    now_iso(),
                    decision_id,
                ),
            )
        return self.get_decision(decision_id)

    def note_apply_error(self, decision_id: int, error: str) -> dict[str, Any]:
        with self.post_store.connect() as db:
            row = db.execute(
                "SELECT validator_errors_json FROM review_agent_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"review agent decision not found: {decision_id}")
            errors = [
                value for value in _loads(row[0], [])
                if not str(value).startswith("apply_error:")
            ]
            errors.append(f"apply_error:{str(error)[:900]}")
            db.execute(
                "UPDATE review_agent_decisions SET validator_errors_json=? WHERE id=?",
                (_json(errors), decision_id),
            )
        return self.get_decision(decision_id)

    def save_side_effects(self, decision_id: int, side_effects: dict[str, Any]) -> dict[str, Any]:
        with self.post_store.connect() as db:
            cursor = db.execute(
                "UPDATE review_agent_decisions SET side_effects_json=? WHERE id=? AND status='proposed'",
                (_json(side_effects), decision_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("review agent decision is no longer applicable")
        return self.get_decision(decision_id)

    def mark_rolled_back(self, decision_id: int) -> dict[str, Any]:
        with self.post_store.connect() as db:
            db.execute(
                "UPDATE review_agent_decisions SET status='rolled_back',rolled_back_at=? WHERE id=?",
                (now_iso(), decision_id),
            )
        return self.get_decision(decision_id)

    def mark_failed(self, decision_id: int, error: str) -> dict[str, Any]:
        with self.post_store.connect() as db:
            row = db.execute(
                "SELECT validator_errors_json FROM review_agent_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"review agent decision not found: {decision_id}")
            errors = _loads(row[0], [])
            errors.append(str(error)[:1000])
            db.execute(
                "UPDATE review_agent_decisions SET status='failed',validator_errors_json=? WHERE id=?",
                (_json(errors), decision_id),
            )
        return self.get_decision(decision_id)

    def mark_overridden_for_post(
        self,
        post_id: str,
        action: str,
        human_drafts: list[dict[str, Any]] | None = None,
    ) -> int:
        with self.post_store.connect() as db:
            rows = db.execute(
                """
                SELECT id,decision,drafts_json,validator_errors_json
                FROM review_agent_decisions
                WHERE post_id=? AND status IN ('proposed','applied')
                """,
                (post_id,),
            ).fetchall()
            ids = [int(row[0]) for row in rows]
            for row in rows:
                if row["decision"] != "auto_approve":
                    continue
                errors = _loads(row["validator_errors_json"], [])
                if action != "approved":
                    errors.append("human_rejected_auto_approval")
                elif human_drafts is not None:
                    predicted = {
                        (str(value.get("symbol") or ""), str(value.get("direction") or ""))
                        for value in _loads(row["drafts_json"], [])
                    }
                    actual = {
                        (str(value.get("symbol") or ""), str(value.get("direction") or ""))
                        for value in human_drafts
                    }
                    if predicted != actual:
                        errors.append("human_corrected_stock_or_direction")
                db.execute(
                    "UPDATE review_agent_decisions SET validator_errors_json=? WHERE id=?",
                    (_json(sorted(set(errors))), int(row["id"])),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE review_agent_decisions SET status='overridden',overridden_at=? "
                    f"WHERE id IN ({placeholders})",
                    (now_iso(), *ids),
                )
        if ids:
            self.post_store.record_review_action(
                post_id,
                "agent_decision_overridden",
                {"decision_ids": ids, "human_action": action},
            )
        return len(ids)

    def set_mode(
        self,
        mode: str,
        *,
        force: bool = False,
        reset_validation: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"shadow", "enabled"}:
            raise ValueError("invalid review agent mode")
        summary = self.summary()
        if mode == "enabled" and not force and not summary["activation_ready"]:
            raise ValueError("shadow validation gate has not passed")
        timestamp = now_iso()
        with self.post_store.connect() as db:
            db.execute(
                """
                UPDATE review_agent_settings SET mode=?,policy_version=?,
                    enabled_at=CASE WHEN ?='enabled' THEN ? ELSE enabled_at END,
                    shadow_started_at=CASE WHEN ? THEN ? ELSE shadow_started_at END,
                    updated_at=? WHERE id=1
                """,
                (
                    mode,
                    POLICY_VERSION,
                    mode,
                    timestamp,
                    int(reset_validation),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_settings()

    def requires_random_audit(self, post_id: str) -> bool:
        enabled_at = str(self.get_settings().get("enabled_at") or "")
        if not enabled_at:
            return False
        enabled = datetime.fromisoformat(enabled_at)
        if datetime.now().astimezone() - enabled >= timedelta(days=30):
            return False
        sample = int(hashlib.sha256(f"{POLICY_VERSION}:{post_id}".encode("utf-8")).hexdigest()[:8], 16)
        return sample % 10 == 0

    def summary(self) -> dict[str, Any]:
        settings = self.get_settings()
        with self.post_store.connect() as db:
            decision_counts = {
                str(row["decision"]): int(row["count"])
                for row in db.execute(
                    "SELECT decision,COUNT(*) count FROM review_agent_decisions GROUP BY decision"
                ).fetchall()
            }
            status_counts = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    "SELECT status,COUNT(*) count FROM review_agent_decisions GROUP BY status"
                ).fetchall()
            }
            last_run = db.execute(
                "SELECT * FROM review_agent_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            validation_counts = {
                str(row["decision"]): int(row["count"])
                for row in db.execute(
                    """
                    SELECT decision,COUNT(*) count FROM review_agent_decisions
                    WHERE mode='shadow' AND created_at>=? GROUP BY decision
                    """,
                    (settings["shadow_started_at"],),
                ).fetchall()
            }
            comparable = db.execute(
                """
                SELECT d.decision,p.review_status FROM review_agent_decisions d
                JOIN posts p ON p.post_id=d.post_id
                WHERE d.mode='shadow' AND d.decision IN ('auto_approve','auto_exclude','auto_ignore')
                    AND d.created_at>=?
                    AND d.status='overridden'
                    AND p.review_status IN ('approved','excluded','ignored')
                """,
                (settings["shadow_started_at"],),
            ).fetchall()
            critical_error_count = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM review_agent_decisions
                    WHERE mode='shadow' AND created_at>=? AND decision='auto_approve'
                        AND (validator_errors_json LIKE '%human_rejected_auto_approval%'
                            OR validator_errors_json LIKE '%human_corrected_stock_or_direction%')
                    """,
                    (settings["shadow_started_at"],),
                ).fetchone()[0]
            )
        expected = {
            "auto_approve": "approved",
            "auto_exclude": "excluded",
            "auto_ignore": "ignored",
        }
        matches = sum(expected.get(str(row["decision"])) == row["review_status"] for row in comparable)
        agreement = matches / len(comparable) if comparable else 0.0
        comparable_approvals = [row for row in comparable if row["decision"] == "auto_approve"]
        approval_matches = sum(row["review_status"] == "approved" for row in comparable_approvals)
        approval_agreement = (
            approval_matches / len(comparable_approvals) if comparable_approvals else 0.0
        )
        started = datetime.fromisoformat(settings["shadow_started_at"]) if settings["shadow_started_at"] else None
        shadow_days = max(0, (datetime.now().astimezone() - started).days) if started else 0
        predicted_approvals = validation_counts.get("auto_approve", 0)
        validation_total = sum(validation_counts.values())
        activation_ready = bool(
            shadow_days >= 7
            and validation_total >= 100
            and predicted_approvals >= 30
            and len(comparable_approvals) >= predicted_approvals
            and approval_agreement >= 0.98
            and critical_error_count == 0
        )
        return {
            "settings": settings,
            "decision_counts": decision_counts,
            "validation_decision_counts": validation_counts,
            "status_counts": status_counts,
            "total_decisions": sum(decision_counts.values()),
            "validation_total_decisions": validation_total,
            "shadow_days": shadow_days,
            "comparable_decisions": len(comparable),
            "comparable_approvals": len(comparable_approvals),
            "agreement": agreement,
            "approval_agreement": approval_agreement,
            "critical_error_count": critical_error_count,
            "activation_ready": activation_ready,
            "last_run": dict(last_run) if last_run else None,
        }

    @staticmethod
    def _decision_row(row: Any) -> dict[str, Any]:
        value = dict(row)
        for source, target in [
            ("reason_codes_json", "reason_codes"),
            ("evidence_json", "evidence"),
            ("drafts_json", "drafts"),
            ("validator_errors_json", "validator_errors"),
            ("event_ids_json", "event_ids"),
            ("created_event_ids_json", "created_event_ids"),
        ]:
            value[target] = _loads(value.pop(source, "[]"), [])
        value["side_effects"] = _loads(value.pop("side_effects_json", "{}"), {})
        return value


class ReviewAgentRunner:
    def __init__(
        self,
        post_store: KolPostStore,
        event_store: Any,
        market_store: Any,
        *,
        classifier: Any | None = None,
        ocr_classifier: Any | None = None,
        rule_classifier: RuleClassifier | None = None,
        policy: ReviewPolicy | None = None,
    ):
        self.post_store = post_store
        self.event_store = event_store
        self.market_store = market_store
        self.classifier = classifier
        self.ocr_classifier = ocr_classifier
        self.rule_classifier = rule_classifier or RuleClassifier()
        self.policy = policy or ReviewPolicy()
        self.repository = ReviewAgentRepository(post_store)

    def run(
        self,
        *,
        mode: str | None = None,
        max_runtime_minutes: float = 25,
        max_items: int = 20,
        post_id: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        selected_mode = mode or str(self.repository.get_settings()["mode"])
        if selected_mode not in {"shadow", "enabled"}:
            raise ValueError("invalid review agent mode")
        if selected_mode == "enabled" and not self.repository.summary()["activation_ready"]:
            raise ValueError("shadow validation gate has not passed")
        started = time.monotonic()
        deadline = started + max(1, max_runtime_minutes * 60)
        run_id = "dry-run" if dry_run else ""
        counts = {
            "processed_count": 0,
            "auto_approve": 0,
            "auto_exclude": 0,
            "auto_ignore": 0,
            "needs_human": 0,
            "failed": 0,
        }
        errors: list[str] = []
        queued_symbols: set[str] = set()
        decisions: list[dict[str, Any]] = []
        ocr_count = 0
        posts = [self.post_store.get_post(post_id)] if post_id else self._pending_posts()
        worker = self.post_store.model_worker() if self.classifier and not dry_run else nullcontext()
        try:
            with worker:
                if not dry_run:
                    self.repository.recover_interrupted_runs()
                    run_id = self.repository.start_run(selected_mode)
                    self.post_store.recover_interrupted_classification()
                for initial in posts:
                    if counts["processed_count"] >= max(1, max_items):
                        break
                    if time.monotonic() - started >= max(1, max_runtime_minutes * 60):
                        break
                    if initial["review_status"] != "pending":
                        continue
                    needs_ocr = bool(
                        initial.get("local_media")
                        and initial.get("ocr_status") not in {"completed", "failed"}
                        and self.ocr_classifier
                    )
                    if needs_ocr and ocr_count >= 8:
                        continue
                    applying_decision_id: int | None = None
                    try:
                        current = initial if dry_run else self._ensure_classification(
                            initial,
                            timeout_seconds=max(1, deadline - time.monotonic()),
                        )
                        if needs_ocr:
                            ocr_count += 1
                        existing = self.repository.current_decision(current, selected_mode)
                        if existing:
                            if (
                                selected_mode == "enabled"
                                and existing["status"] == "proposed"
                                and existing["decision"].startswith("auto_")
                                and "random_audit_first_30_days" not in existing["reason_codes"]
                            ):
                                counts["processed_count"] += 1
                                counts[existing["decision"]] += 1
                                applying_decision_id = int(existing["id"])
                                symbols = self.apply_decision(applying_decision_id)
                                queued_symbols.update(symbols)
                            continue
                        result = self.policy.evaluate(current, self.market_store.get_instrument)
                        random_audit = (
                            selected_mode == "enabled"
                            and result.decision == "auto_approve"
                            and self.repository.requires_random_audit(current["post_id"])
                        )
                        if random_audit:
                            result = replace(
                                result,
                                reason_codes=[*result.reason_codes, "random_audit_first_30_days"],
                            )
                        counts["processed_count"] += 1
                        counts[result.decision] += 1
                        if dry_run:
                            decisions.append({"post_id": current["post_id"], **result.to_dict()})
                            continue
                        decision = self.repository.save_decision(current, run_id, selected_mode, result)
                        decisions.append(decision)
                        if selected_mode == "enabled" and result.decision.startswith("auto_") and not random_audit:
                            applying_decision_id = int(decision["id"])
                            symbols = self.apply_decision(applying_decision_id)
                            queued_symbols.update(symbols)
                    except Exception as exc:
                        if applying_decision_id is not None:
                            self.repository.note_apply_error(applying_decision_id, str(exc))
                        counts["failed"] += 1
                        errors.append(f"{initial.get('post_id')}: {str(exc)[:1000]}")
        finally:
            if not dry_run and run_id:
                self.repository.finish_run(run_id, counts, errors)
        return {
            "ok": not errors,
            "run_id": run_id,
            "mode": selected_mode,
            "dry_run": dry_run,
            "counts": counts,
            "queued_symbols": sorted(queued_symbols),
            "decisions": decisions,
            "errors": errors,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def _pending_posts(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        offset = 0
        while len(values) < 1000:
            batch = self.post_store.list_posts(review_status="pending", limit=500, offset=offset)
            if not batch:
                break
            values.extend(batch)
            offset += len(batch)
            if len(batch) < 500:
                break
        values.sort(key=lambda value: value["posted_at"], reverse=True)
        values.sort(key=lambda value: bool(value.get("local_media")))
        values.sort(key=lambda value: not bool(value.get("is_candidate")))
        return values

    def _ensure_classification(self, post: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        stage_started = time.monotonic()
        structured = self.rule_classifier.classify_structured_text(post)
        if structured:
            if (
                post.get("model_name") != "structured-rules"
                or post.get("prompt_version") != STRUCTURED_REVIEW_VERSION
            ):
                self.post_store.save_model_classification(
                    post["post_id"],
                    structured,
                    model_name="structured-rules",
                    prompt_version=STRUCTURED_REVIEW_VERSION,
                )
            return self.post_store.get_post(post["post_id"])
        if post.get("post_type") == "retweet":
            return post
        if post.get("local_media") and post.get("ocr_status") not in {"completed", "failed"} and self.ocr_classifier:
            previous_timeout = getattr(self.ocr_classifier, "timeout_seconds", None)
            self.ocr_classifier.timeout_seconds = min(
                timeout_seconds,
                MAX_OCR_STAGE_SECONDS,
                previous_timeout or timeout_seconds,
            )
            try:
                result = self.ocr_classifier.classify([post]).get(post["post_id"], {})
                text = str(result.get("text") or "")
                error = str(result.get("error") or "")
                if error or not text.strip():
                    self.post_store.save_ocr_result(
                        post["post_id"],
                        error=error or "OCR returned no text",
                        provider=str(result.get("provider") or getattr(self.ocr_classifier, "provider_name", self.ocr_classifier.__class__.__name__)),
                    )
                else:
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
            except Exception as exc:
                self.post_store.save_ocr_result(
                    post["post_id"],
                    error=str(exc),
                    provider=str(getattr(self.ocr_classifier, "provider_name", self.ocr_classifier.__class__.__name__)),
                )
            finally:
                self.ocr_classifier.timeout_seconds = previous_timeout
            post = self.post_store.get_post(post["post_id"])
        needs_v2 = (
            post.get("model_status") != "completed"
            or post.get("prompt_version") != getattr(self.classifier, "prompt_version", "")
        )
        if not needs_v2 or not self.classifier:
            return post
        remaining = max(1, timeout_seconds - (time.monotonic() - stage_started) - 1)
        previous_timeout = getattr(self.classifier, "timeout_seconds", None)
        self.classifier.timeout_seconds = min(remaining, previous_timeout or remaining)
        claimed = self.post_store.claim_posts_for_model(1, post_id=post["post_id"], force=True)
        if not claimed:
            raise RuntimeError("post classification is already running")
        try:
            payload = self.classifier.classify(claimed[0])
            self.post_store.save_model_classification(
                post["post_id"],
                payload,
                model_name=str(getattr(self.classifier, "model_name", "ai")),
                prompt_version=self.classifier.prompt_version,
            )
        except Exception as exc:
            self.post_store.save_model_classification(
                post["post_id"],
                None,
                model_name=str(getattr(self.classifier, "model_name", "ai")),
                prompt_version=self.classifier.prompt_version,
                error=str(exc),
            )
        finally:
            self.classifier.timeout_seconds = previous_timeout
        return self.post_store.get_post(post["post_id"])

    def apply_decision(self, decision_id: int) -> list[str]:
        from kol_review import _approve_post_locked, post_review_lock
        from market_data import Instrument

        event_ids: list[str] = []
        created_event_ids: list[str] = []
        symbols: list[str] = []
        side_effects: dict[str, Any] = {
            "confirmed_lead_ids": [],
            "instrument_states": [],
        }
        with post_review_lock(self.post_store, self.repository.get_decision(decision_id)["post_id"]):
            decision = self.repository.get_decision(decision_id)
            if decision.get("side_effects"):
                side_effects = dict(decision["side_effects"])
                side_effects.setdefault("confirmed_lead_ids", [])
                side_effects.setdefault("instrument_states", [])
            if decision["status"] == "applied":
                return [str(value.get("symbol")) for value in decision["drafts"] if value.get("symbol")]
            if decision["status"] != "proposed":
                raise ValueError("only proposed decisions can be applied")
            post = self.post_store.get_post(decision["post_id"])
            if post["review_status"] != "pending":
                if (
                    decision["decision"] == "auto_approve"
                    and post["review_status"] == "approved"
                ):
                    previous = self.post_store.latest_review(post["post_id"], "approved") or {}
                    detail = previous.get("detail") or {}
                    if int(detail.get("decision_id") or 0) == decision_id:
                        event_ids = list(detail.get("event_ids") or [])
                        created_event_ids = list(detail.get("created_event_ids") or [])
                        self.repository.mark_applied(
                            decision_id,
                            event_ids,
                            created_event_ids=created_event_ids,
                            side_effects=decision.get("side_effects") or side_effects,
                        )
                        return [str(value.get("symbol")) for value in decision["drafts"] if value.get("symbol")]
                self.repository.mark_overridden_for_post(post["post_id"], post["review_status"])
                return []
            if decision["decision"] == "auto_approve":
                for draft in decision["drafts"]:
                    symbol = str(draft["symbol"])
                    instrument = self.market_store.get_instrument(symbol)
                    if not instrument or instrument.get("instrument_type") != "stock":
                        raise ValueError(f"stock master rejected {symbol}")
                    if not any(value.get("symbol") == symbol for value in side_effects["instrument_states"]):
                        side_effects["instrument_states"].append(
                            {
                                "symbol": symbol,
                                "lifecycle": str(instrument.get("lifecycle") or "tracking"),
                                "last_mentioned_at": str(instrument.get("last_mentioned_at") or ""),
                            }
                        )
                        self.repository.save_side_effects(decision_id, side_effects)
                    leads = self.post_store.list_stock_leads(post_id=post["post_id"], symbol=symbol, limit=10)
                    if not leads:
                        original_text = f"{post.get('text', '')}\n{post.get('article_text', '')}"
                        self.post_store.upsert_stock_lead(
                            {
                                "post_id": post["post_id"],
                                "kol_id": post["kol_id"],
                                "symbol": symbol,
                                "security_name": draft.get("security_name") or instrument["name"],
                                "instrument_type": "stock",
                                "direction": "long",
                                "mention_kind": "recommendation",
                                "evidence_text": "；".join(draft.get("evidence_spans") or []),
                                "extraction_method": "exact_code" if symbol in original_text else "name_match",
                                "confidence": draft.get("confidence") or 0,
                            }
                        )
                        leads = self.post_store.list_stock_leads(post_id=post["post_id"], symbol=symbol, limit=10)
                    lead = leads[0]
                    if lead["status"] == "pending":
                        self.post_store.review_stock_lead(
                            int(lead["id"]),
                            "confirmed",
                            f"审核智能体自动确认 / {POLICY_VERSION}",
                        )
                        if int(lead["id"]) not in side_effects["confirmed_lead_ids"]:
                            side_effects["confirmed_lead_ids"].append(int(lead["id"]))
                        self.repository.save_side_effects(decision_id, side_effects)
                    elif (
                        lead["status"] == "confirmed"
                        and str(lead.get("review_note") or "").startswith("审核智能体自动确认")
                    ):
                        if int(lead["id"]) not in side_effects["confirmed_lead_ids"]:
                            side_effects["confirmed_lead_ids"].append(int(lead["id"]))
                            self.repository.save_side_effects(decision_id, side_effects)
                    self.market_store.upsert_instrument(
                        Instrument(
                            symbol=symbol,
                            name=str(draft.get("security_name") or instrument["name"]),
                            instrument_type="stock",
                            exchange=instrument["exchange"],
                            status=instrument["status"],
                            list_date=instrument["list_date"],
                            lifecycle="pinned",
                            source="kol_event",
                            first_seen_at=instrument["first_seen_at"],
                            last_mentioned_at=str(post["posted_at"])[:10],
                        )
                    )
                    self.market_store.enqueue_sync(symbol, reason=f"review_agent:{post['post_id']}")
                    symbols.append(symbol)
                result = _approve_post_locked(
                    self.post_store,
                    self.event_store,
                    post["post_id"],
                    decision["drafts"],
                    note=f"审核智能体自动批准 / {POLICY_VERSION}",
                    audit_detail={"decision_id": decision_id, "policy_version": POLICY_VERSION},
                )
                event_ids = result.event_ids
                created_event_ids = result.created_event_ids
                self.post_store.record_review_action(
                    post["post_id"],
                    "agent_auto_approved",
                    {
                        "decision_id": decision_id,
                        "event_ids": event_ids,
                        "created_event_ids": created_event_ids,
                        "policy_version": POLICY_VERSION,
                    },
                )
            elif decision["decision"] in {"auto_exclude", "auto_ignore"}:
                target = "excluded" if decision["decision"] == "auto_exclude" else "ignored"
                self.post_store.set_review(
                    post["post_id"],
                    target,
                    f"审核智能体{target} / {POLICY_VERSION}",
                    {
                        "decision_id": decision_id,
                        "reason_codes": decision["reason_codes"],
                        "policy_version": POLICY_VERSION,
                    },
                )
            else:
                raise ValueError("decision is not automatically applicable")
        self.repository.mark_applied(
            decision_id,
            event_ids,
            created_event_ids=created_event_ids,
            side_effects=side_effects,
        )
        return symbols


def rollback_decision(
    repository: ReviewAgentRepository,
    post_store: KolPostStore,
    event_store: Any,
    decision_id: int,
    market_store: Any | None = None,
) -> dict[str, Any]:
    from kol_review import post_review_lock

    decision = repository.get_decision(decision_id)
    if decision["status"] == "rolled_back":
        return decision
    if decision["status"] != "applied":
        raise ValueError("only applied decisions can be rolled back")
    if decision["decision"] == "auto_approve":
        # Fail closed before touching the decision. If a later rollback step fails,
        # retries can resume while automatic approvals remain disabled.
        repository.set_mode("shadow", reset_validation=True)
    events = {event.event_id: event for event in event_store.load_events()}
    for event_id in decision.get("created_event_ids", []):
        event = events.get(event_id)
        if event and event.status in {"active", "completed"}:
            event_store.update_event(
                replace(
                    event,
                    status="excluded",
                    exclusion_reason=f"审核智能体决定已撤销 / decision:{decision_id}",
                    updated_at=now_iso(),
                ),
                action="agent_rollback",
            )
    for lead_id in decision.get("side_effects", {}).get("confirmed_lead_ids", []):
        try:
            lead = post_store.get_stock_lead(int(lead_id))
        except KeyError:
            continue
        if (
            lead.get("status") == "confirmed"
            and str(lead.get("review_note") or "").startswith("审核智能体自动确认")
        ):
            post_store.review_stock_lead(
                int(lead_id),
                "pending",
                f"审核智能体决定已撤销 / decision:{decision_id}",
            )
    if market_store is not None and hasattr(market_store, "restore_research_state"):
        for state in decision.get("side_effects", {}).get("instrument_states", []):
            market_store.restore_research_state(
                str(state["symbol"]),
                lifecycle=str(state["lifecycle"]),
                last_mentioned_at=str(state.get("last_mentioned_at") or ""),
            )
    with post_review_lock(post_store, decision["post_id"]):
        post_store.set_review(
            decision["post_id"],
            "pending",
            "审核智能体决定已撤销",
            {"decision_id": decision_id, "event_ids": decision["event_ids"]},
        )
    post_store.record_review_action(
        decision["post_id"],
        "agent_decision_rolled_back",
        {"decision_id": decision_id, "event_ids": decision["event_ids"]},
    )
    rolled_back = repository.mark_rolled_back(decision_id)
    if decision["decision"] == "auto_approve":
        if hasattr(event_store, "queue_notification"):
            event_store.queue_notification(
                {
                    "kind": "review_agent_wrong_approval",
                    "key": f"review-agent-rollback-{decision_id}",
                    "message": (
                        f"[KOL审核智能体告警] 自动批准已撤销并回退影子模式："
                        f"帖子 {decision['post_id']}，决定 {decision_id}。"
                    ),
                }
            )
        post_store.record_review_action(
            decision["post_id"],
            "agent_returned_to_shadow",
            {"decision_id": decision_id, "reason": "automatic approval rolled back"},
        )
    return rolled_back
