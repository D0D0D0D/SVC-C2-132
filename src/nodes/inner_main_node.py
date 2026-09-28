"""AgentCore Platform v1.0 — SVC-C2-132 inner main.

* invoke — the two LLM steps: promise normalisation (#3) and gap judgment (#5).
  The LLM fills structured values only; it cites this-case sources produced by the
  deterministic reconcile. Degrades to empty (honest) when no LLM is injected — it
  never fabricates a divergence deterministically.
* ingest / feedback — write-path SANITISE (#sanitize_records): every free-text
  field is PII-masked and instruction-override-rejected before it can be persisted
  (fail-closed: a rejected field aborts the whole write).
"""

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import promise, sanitizer
from src.services.llm_seam import get_llm

# Every caller-supplied string field on an ingest record is sanitised before
# persistence — including `source_of_truth_ref` (a caller-controlled pointer that is
# otherwise stored raw and never redacted).
_INGEST_SANITIZE_FIELDS = (
    "notice_summary", "actual_rule_summary", "gap_note", "correction",
    "adjudication_rationale", "category_key", "viewpoint_code", "source_of_truth_ref",
)


class InnerMainNode(FunctionNode):
    """LLM promise+gap (invoke) · write-path sanitiser (ingest/feedback)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "inner_main"}, state)
            return {}  # upstream (inner pre) reject — do not override the error status
        mode = state.get("mode")
        if mode == "invoke":
            return self._invoke(state)
        if mode in ("ingest", "feedback"):
            return self._sanitize(state)
        return {"status": AgentStatus.SUCCESS.value}

    # ── invoke: LLM promise normalise + gap judgment ─────────────────────────
    def _invoke(self, state: dict) -> dict:
        vi = state.get("validated_input", {}) or {}
        source_registry = state.get("source_registry", {}) or {}
        actual_rule = state.get("actual_rule", {}) or {}
        min_quote_len = int(self._config.get("provenance_min_quote_len", 8))
        llm = get_llm()

        if llm is None:
            emit_trace_event("gap_judgment", {"mode": "degraded_no_llm", "findings": 0}, state)
            return {
                "promise_params": promise.fallback_promise(),
                "gap_findings": promise.fallback_gap_findings(),
                "status": AgentStatus.SUCCESS.value,
            }

        promise_params = self._call_llm(
            promise.build_promise_prompt(vi.get("notice_text", "")),
            promise.parse_promise, promise.fallback_promise(), "promise_normalize", state,
        )
        gap_findings = self._call_llm(
            promise.build_gap_prompt(promise_params, actual_rule, source_registry, min_quote_len),
            promise.parse_gap_findings, promise.fallback_gap_findings(), "gap_judgment", state,
        )
        emit_trace_event("gap_judgment", {"findings": len(gap_findings)}, state)
        return {
            "promise_params": promise_params,
            "gap_findings": gap_findings,
            "status": AgentStatus.SUCCESS.value,
        }

    def _call_llm(self, prompt: str, parse, fallback, event: str, state: dict):
        try:
            response = get_llm().complete([{"role": "user", "content": prompt}])
            content = response.get("content", "") if isinstance(response, dict) else str(response)
            parsed = parse(content)
            model = response.get("model") if isinstance(response, dict) else None
            emit_trace_event("llm_call", {"step": event, "parsed": parsed is not None, "model": model}, state)
            return parsed if parsed is not None else fallback
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash the workflow
            emit_trace_event("llm_error", {"step": event, "error": type(exc).__name__}, state)
            return fallback

    # ── ingest / feedback: sanitise free text before persistence ─────────────
    def _sanitize(self, state: dict) -> dict:
        mode = state.get("mode")
        vi = dict(state.get("validated_input", {}) or {})

        if mode == "ingest":
            clean: list[dict] = []
            for i, rec in enumerate(vi.get("records", []) or []):
                ok, reason, safe = sanitizer.sanitize_fields(rec, _INGEST_SANITIZE_FIELDS, self._config)
                if not ok:
                    emit_trace_event("sanitize_rejected", {"record": i, "reason": reason}, state)
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [f"ingest record[{i}] sanitize rejected: {reason} (fail-closed — wrote nothing)"],
                    }
                clean.append(safe)
            vi["records"] = clean
            emit_trace_event("sanitize", {"mode": "ingest", "records": len(clean)}, state)
        else:  # feedback
            ok, reason, safe = sanitizer.sanitize_free_text(
                "correction_note", vi.get("correction_note", ""), self._config)
            if not ok:
                emit_trace_event("sanitize_rejected", {"field": "correction_note", "reason": reason}, state)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"feedback correction_note sanitize rejected: {reason} (fail-closed)"],
                }
            vi["correction_note"] = safe
            emit_trace_event("sanitize", {"mode": "feedback"}, state)

        return {"validated_input": vi, "status": AgentStatus.SUCCESS.value}
