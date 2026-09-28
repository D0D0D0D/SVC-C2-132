"""AgentCore Platform v1.0 — SVC-C2-132 inner pre_process (deterministic).

Unpacks the parent state from the GraphNode string envelope (the inner graph is
invoked with only `user_input`; see framework GraphNode.execute) and re-emits it
into the inner State so the inner main/post nodes can read `mode` / `validated_input`
/ scope. Then runs the non-LLM, non-caller-controlled steps:

* invoke  — resolve the event_config (inline, or via `payload_ref` in LOCAL scope so
  a large export never enters State), build the immutable `source_registry`
  provenance anchor (#2), reconcile config↔approved table (missing approvals), and
  reconstruct the actual reward-execution rule (#4).
* feedback — resolve the referenced case WITHIN the caller's (tenant, org) scope
  (fail-closed: an unresolved case is rejected, never widened to another scope).
* ingest  — pass through (records are sanitised in the inner main step).
"""

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import reconcile
from src.services.export_store import get_export_store
from src.services.kb_store import get_kb_store


class InnerPreNode(FunctionNode):
    """Deterministic reconcile / reconstruct (invoke) · case resolution (feedback)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    @staticmethod
    def _unpack(state: dict) -> dict:
        """Recover the parent envelope from user_input (GraphNode string boundary)."""
        try:
            env = json.loads(state.get("user_input") or "{}")
        except (ValueError, TypeError):
            env = {}
        return env if isinstance(env, dict) else {}

    def execute(self, state: dict) -> dict:
        env = self._unpack(state)
        mode = env.get("mode")
        if mode not in ("invoke", "ingest", "feedback"):
            # PB-6 bare-state probe / unknown mode — nothing to do.
            return {"status": AgentStatus.SUCCESS.value}

        # Re-emit parent fields into the inner State for downstream inner nodes.
        base = {
            "mode": mode,
            "validated_input": env.get("validated_input", {}) or {},
            "tenant_id": env.get("tenant_id", ""),
            "org_id": env.get("org_id", ""),
            "payload_ref": env.get("payload_ref", ""),
            "input_context": {"request_ts": env.get("request_ts", "")},
        }

        if mode == "invoke":
            return self._invoke(state, base)
        if mode == "feedback":
            return self._feedback(state, base)
        return {**base, "status": AgentStatus.SUCCESS.value}  # ingest

    def _invoke(self, state: dict, base: dict) -> dict:
        vi = base["validated_input"]
        payload_ref = base["payload_ref"]

        if payload_ref:
            event_config = get_export_store().resolve(payload_ref, base["tenant_id"], base["org_id"])
            if not isinstance(event_config, dict):
                emit_trace_event("payload_ref_unresolved", {"ref": payload_ref}, state)
                return {**base, "status": AgentStatus.ERROR.value,
                        "error_log": [f"payload_ref {payload_ref!r} did not resolve for this scope (fail-closed)"]}
        else:
            event_config = vi.get("event_config", {}) or {}

        max_chars = int(self._config.get("max_field_chars", 20000))
        source_registry = reconcile.build_source_registry(vi, event_config, max_source_chars=max_chars)
        reconcile_result = reconcile.deterministic_reconcile(vi, event_config)
        actual_rule = reconcile.reconstruct_actual_rule(event_config)

        emit_trace_event(
            "reconcile",
            {"sources": len(source_registry),
             "missing_approvals": len(reconcile_result.get("missing_approval_ids", []))},
            state,
        )
        return {
            **base,
            "source_registry": source_registry,
            "reconcile_result": reconcile_result,
            "actual_rule": actual_rule,
            "status": AgentStatus.SUCCESS.value,
        }

    def _feedback(self, state: dict, base: dict) -> dict:
        case_id = base["validated_input"].get("case_id", "")
        if not get_kb_store().case_exists(base["tenant_id"], base["org_id"], case_id):
            emit_trace_event("feedback_case_unresolved", {"case_id": case_id}, state)
            return {**base, "status": AgentStatus.ERROR.value,
                    "error_log": [f"feedback case_id {case_id!r} not found in scope (fail-closed)"]}
        emit_trace_event("feedback_case_resolved", {"case_id": case_id}, state)
        return {**base, "status": AgentStatus.SUCCESS.value}
