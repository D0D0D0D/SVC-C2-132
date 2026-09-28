"""AgentCore Platform v1.0 — SVC-C2-132 outer pre_process.

Mode canonicalisation, RBAC authorization (tenant/org fail-closed), mode-specific
closed-schema validation, and the layer-1 `payload_ref` split (docs/02_design.md
§2-1, §(a)). S-2 domain rejection lives in `_extra_security_gate_input()` (runs
before execute and short-circuits on reject) but is lenient on ABSENT input so the
framework PB-6 bare-state probe still reaches execute; execute handles the absent
case and produces `validated_input` for valid input.
"""

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services import input_gate


def _validate(state: dict, config: dict) -> tuple[bool, list, dict]:
    """Full request validation shared by the S-2 gate and execute().

    Returns (ok, rejected, out) where out = {mode, validated_input, payload_ref,
    tenant_id, org_id}. Pure — never raises, never mutates state.
    """
    raw = state.get("user_input")
    obj, rej = input_gate.parse_body(raw)
    if obj is None:
        return False, rej, {}

    mode = input_gate.canonical_mode(obj.get("mode"))
    if mode is None:
        return False, [{"field": "mode", "reason": "unknown mode (invoke|ingest|feedback required)"}], {}

    # RBAC: requested (tenant, org) must be inside the caller's authorized scope,
    # with the mode-appropriate permission (invoke=read, ingest/feedback=write).
    authorized = (state.get("input_context", {}) or {}).get("authorized_scopes", [])
    required_perm = "read" if mode == "invoke" else "write"
    ok_rbac, rbac_rej = input_gate.authorize(obj.get("tenant_id"), obj.get("org_id"), authorized, required_perm)
    if not ok_rbac:
        return False, rbac_rej, {"mode": mode}

    payload_ref = ""
    if mode == "invoke":
        ok, rejected, vi, payload_ref = input_gate.validate_invoke(obj, config)
    elif mode == "ingest":
        ok, rejected, vi = input_gate.validate_ingest(obj, config)
    else:  # feedback
        ok, rejected, vi = input_gate.validate_feedback(obj, config)

    if not ok:
        return False, rejected, {"mode": mode}
    return True, [], {
        "mode": mode,
        "validated_input": vi,
        "payload_ref": payload_ref,
        "tenant_id": obj.get("tenant_id", ""),
        "org_id": obj.get("org_id", ""),
    }


class PreProcessNode(FunctionNode):
    """Canonicalise mode, enforce RBAC, validate the mode payload, split payload_ref."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    # ── S-2 domain gate (before execute; short-circuits on reject) ───────────
    def _extra_security_gate_input(self, state: dict) -> dict:
        raw = state.get("user_input")
        if not raw:
            # Lenient on absent input — execute returns the empty-input error and
            # the framework PB-6 bare-state order stays intact.
            return state
        ok, rejected, _ = _validate(state, self._config)
        if not ok:
            emit_trace_event("input_rejected", {"count": len(rejected)}, state)
            state = dict(state)
            state["status"] = AgentStatus.ERROR.value
            state["rejected"] = rejected
            state["error_log"] = [f"S-2 rejected: {r['field']} — {r['reason']}" for r in rejected]
        return state

    def execute(self, state: dict) -> dict:
        raw = state.get("user_input")
        if not raw:
            emit_trace_event("input_rejected", {"reason": "empty_body"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "rejected": [{"field": "body", "reason": "empty request body"}],
                "error_log": ["pre_process: empty request body"],
            }

        ok, rejected, out = _validate(state, self._config)
        if not ok:
            emit_trace_event("input_rejected", {"count": len(rejected), "mode": out.get("mode")}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "rejected": rejected,
                "error_log": [f"pre_process rejected: {r['field']} — {r['reason']}" for r in rejected],
            }

        emit_trace_event(
            "input_validated",
            {"mode": out["mode"], "payload_ref": bool(out["payload_ref"])},
            state,
        )
        return {
            # raw body cleared — no raw text lingers in State/checkpoint.
            "user_input": "",
            "mode": out["mode"],
            "tenant_id": out["tenant_id"],
            "org_id": out["org_id"],
            "validated_input": out["validated_input"],
            "payload_ref": out["payload_ref"],
            "rejected": [],
            "status": AgentStatus.SUCCESS.value,
        }
