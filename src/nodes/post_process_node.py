"""AgentCore Platform v1.0 — SVC-C2-132 outer post_process.

Verify-in-execute / enforce-in-gate (docs/02_design.md §(b),(c), DDR):

* `execute()` runs the S-3 PROVENANCE verification for /invoke — it needs the State
  `source_registry` (deterministic, non-caller-controlled) to check every gap
  finding's citations (existence + version/span binding + verbatim + bidirectional
  numeric facts). A terminal violation sets status=error and surfaces NO findings.
  It then builds the non-assertive projection and DECLARES `provenance_verified`.
* `_extra_security_gate_output(result)` runs the S-3 non-assertion LEXICON on
  `result` alone (no State) and re-asserts the declared `provenance_verified`
  marker (fail-closed — a success projection that skipped verification cannot pass).
"""

from typing import ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import lexicon, provenance

_FINDING_PROJECTION_KEYS = ("gap_id", "beneficiary_impact", "review_question",
                            "uncertainty_text", "citations")


class PostProcessNode(FunctionNode):
    """Provenance verify (invoke) + non-assertion boundary + final projection."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    # ── S-3 domain gate: non-assertion lexicon + marker re-assertion ─────────
    def _extra_security_gate_output(self, result: dict) -> dict:
        lexicon.gate_scan(result, self._config)
        if result.get("status") != AgentStatus.ERROR.value:
            proj = result.get("result")
            if isinstance(proj, dict) and proj.get("mode") == "invoke" \
                    and proj.get("provenance_verified") is not True:
                raise SecurityViolationError(
                    "S-3: successful invoke projection missing provenance_verified=True"
                )
        return result

    def execute(self, state: dict) -> dict:
        mode = state.get("mode")
        if mode == "invoke":
            return self._invoke(state)
        if mode in ("ingest", "feedback"):
            return self._write(state)
        # PB-6 bare-state probe / unknown mode — benign empty projection.
        return {"result": {}, "status": AgentStatus.SUCCESS.value}

    def _invoke(self, state: dict) -> dict:
        source_registry = state.get("source_registry", {}) or {}
        gap_findings = state.get("gap_findings", []) or []
        packet = state.get("exception_packet", {}) or {}
        min_quote_len = int(self._config.get("provenance_min_quote_len", 8))

        outcome = provenance.verify_findings(gap_findings, source_registry, min_quote_len)
        if outcome["terminal"] is not None:
            emit_trace_event("provenance_rejected", {"reason": outcome["terminal"]}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "result": {"mode": "invoke", "provenance_verified": False},
                "error_log": [f"S-3 provenance terminal reject: {outcome['terminal']}"],
            }

        verified = [
            {k: f.get(k) for k in _FINDING_PROJECTION_KEYS if k in f}
            for f in outcome["findings"]
        ]
        projection = {
            "mode": "invoke",
            "provenance_verified": True,
            "gap_findings": verified,
            "review_questions": packet.get("review_questions", []),
            "missing_approval_ids": packet.get("missing_approval_ids", []),
            "past_reference": packet.get("past_reference", []),
            "notice": packet.get(
                "notice",
                "Neutral review support only — the AG does not decide legality, "
                "eligibility, or publish/compensate. Human review required.",
            ),
        }
        emit_trace_event("output_finalized", {"findings": len(verified)}, state)
        return {"result": projection, "status": AgentStatus.SUCCESS.value}

    def _write(self, state: dict) -> dict:
        write_result = state.get("write_result", {}) or {}
        emit_trace_event("write_finalized", {"written": write_result.get("written_count", 0)}, state)
        return {
            "result": {
                "mode": state.get("mode"),
                "written_count": write_result.get("written_count", 0),
                "record_ids": write_result.get("record_ids", []),
            },
            "status": AgentStatus.SUCCESS.value,
        }
