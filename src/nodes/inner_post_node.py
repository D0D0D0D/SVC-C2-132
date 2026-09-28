"""AgentCore Platform v1.0 — SVC-C2-132 inner post_process.

* invoke — recall similar past adjudications (#6) via the read-time retention
  barrier, then assemble the source-linked exception packet (#7): gap findings +
  missing approvals + a past-adjudication REFERENCE block (history_ref namespace,
  never a citation) + neutral review questions.
* ingest / feedback — build the retention-aware KB records (retrieval_text derived
  from category + summary + viewpoint; `created_at` stamped from the request's fixed
  timestamp) and persist them within the caller's (tenant, org) scope (#kb_write).
"""

from datetime import datetime, timezone
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import kb_store as kb


def _now_iso(state: dict) -> str:
    """Request-fixed, TZ-aware timestamp (adapter-stamped when available)."""
    ts = (state.get("input_context", {}) or {}).get("request_ts")
    if isinstance(ts, str) and ts.strip():
        return ts
    return datetime.now(timezone.utc).isoformat()


class InnerPostNode(FunctionNode):
    """kb_recall + assemble (invoke) · kb_write (ingest/feedback)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "inner_post"}, state)
            return {}  # upstream reject — do not persist / override the error status
        mode = state.get("mode")
        if mode == "invoke":
            return self._invoke(state)
        if mode in ("ingest", "feedback"):
            return self._write(state)
        return {"status": AgentStatus.SUCCESS.value}

    # ── invoke: recall + assemble packet ─────────────────────────────────────
    def _invoke(self, state: dict) -> dict:
        vi = state.get("validated_input", {}) or {}
        tenant_id = state.get("tenant_id", "")
        org_id = state.get("org_id", "")
        gap_findings = state.get("gap_findings", []) or []
        reconcile_result = state.get("reconcile_result", {}) or {}
        top_k = int(self._config.get("kb_recall_top_k", 8))
        retention_days = int(self._config.get("kb_raw_retention_days", 365))

        recalled = kb.get_kb_store().recall(
            tenant_id, org_id, vi.get("notice_text", ""), top_k, _now_iso(state), retention_days)
        emit_trace_event("kb_recall", {"recalled": len(recalled)}, state)

        review_questions = [f.get("review_question", "") for f in gap_findings
                            if isinstance(f, dict) and f.get("review_question")]
        exception_packet = {
            "gap_findings": gap_findings,
            "missing_approval_ids": reconcile_result.get("missing_approval_ids", []),
            "review_questions": review_questions,
            "past_reference": recalled,  # history_ref namespace — reference only, not citable
            "notice": "Neutral review support only — the AG does not decide legality, "
                      "eligibility, or publish/compensate. Human review required.",
        }
        return {
            "kb_recall": recalled,
            "exception_packet": exception_packet,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── ingest / feedback: build records + persist ───────────────────────────
    def _write(self, state: dict) -> dict:
        mode = state.get("mode")
        vi = state.get("validated_input", {}) or {}
        tenant_id = state.get("tenant_id", "")
        org_id = state.get("org_id", "")
        now_iso = _now_iso(state)

        records = self._build_ingest_records(vi, now_iso) if mode == "ingest" \
            else self._build_feedback_records(vi, now_iso)

        write_result = kb.get_kb_store().write(tenant_id, org_id, records)
        emit_trace_event("kb_write", {"mode": mode, "written": write_result["written_count"]}, state)
        return {"write_result": write_result, "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _build_ingest_records(vi: dict, now_iso: str) -> list[dict]:
        out: list[dict] = []
        for rec in vi.get("records", []) or []:
            out.append({
                "notice_summary": rec.get("notice_summary", ""),
                "actual_rule_summary": rec.get("actual_rule_summary", ""),
                "gap_note": rec.get("gap_note", ""),
                "correction": rec.get("correction", ""),
                "adjudication_rationale": rec.get("adjudication_rationale", ""),
                "verdict": rec.get("verdict", ""),
                "category_key": rec.get("category_key", ""),
                "viewpoint_code": rec.get("viewpoint_code", ""),
                "source_of_truth_ref": rec.get("source_of_truth_ref", ""),
                "retrieval_text": kb.derive_retrieval_text(
                    rec.get("category_key", ""), rec.get("notice_summary", ""), rec.get("viewpoint_code", "")),
                "created_at": now_iso,
            })
        return out

    @staticmethod
    def _build_feedback_records(vi: dict, now_iso: str) -> list[dict]:
        category_key = "feedback"
        viewpoint_code = vi.get("manager_action", "")
        return [{
            "notice_summary": "",
            "actual_rule_summary": "",
            "gap_note": "",
            "correction": vi.get("correction_note", ""),
            "adjudication_rationale": f"manager_action={vi.get('manager_action', '')}",
            "verdict": vi.get("final_verdict", ""),
            "category_key": category_key,
            "viewpoint_code": viewpoint_code,
            "source_of_truth_ref": vi.get("case_id", ""),
            "retrieval_text": kb.derive_retrieval_text(category_key, "", viewpoint_code),
            "created_at": now_iso,
        }]
