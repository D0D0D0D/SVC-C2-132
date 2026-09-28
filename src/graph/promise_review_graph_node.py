"""AgentCore Platform v1.0 — SVC-C2-132 outer `main` GraphNode.

Wraps the inner `PromiseReviewWorkflowGraph`. Defined in `src/graph/` (NOT
`src/nodes/`) so the PB-6 node-discovery probe does not instantiate a GraphNode and
run a whole subgraph inside the single-node order check.

`error_strategy="propagate"` — an inner error is re-raised as `SubgraphError` and
routes the outer pipeline to finalize (fail-fast: a review agent must not ship a
partially-computed divergence set, docs/02_design.md §Composition Pattern).
`extract_input` packs the parent state into a JSON envelope (the inner graph is
invoked with a string only); `merge_output` surfaces the mode-specific subset with
explicit field mapping (no raw pass-through across the subgraph boundary).
"""

import json
from typing import ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class PromiseReviewGraphNode(GraphNode):
    """Run the inner promise-review workflow and merge its mode-specific result."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def execute(self, state: dict) -> dict:
        # Upstream (pre_process) reject short-circuit: pre→main is unconditional, so
        # without this a rejected input would still run the inner workflow.
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {}, state)
            return {}
        emit_trace_event("promise_review_invoked", {"mode": state.get("mode")}, state)
        return super().execute(state)

    def get_subgraph(self):
        from src.graph.domain_workflow_graph import PromiseReviewWorkflowGraph
        agent = PromiseReviewWorkflowGraph(config=self._config)
        agent.compile()
        return agent

    def extract_input(self, state: dict) -> str:
        # The inner graph receives a string only; pack the parent fields it needs.
        return json.dumps({
            "mode": state.get("mode"),
            "validated_input": state.get("validated_input", {}),
            "tenant_id": state.get("tenant_id", ""),
            "org_id": state.get("org_id", ""),
            "payload_ref": state.get("payload_ref", ""),
            "request_ts": (state.get("input_context", {}) or {}).get("request_ts", ""),
        }, default=str)

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        # Explicit field mapping per mode — no raw sub_result pass-through.
        mode = state.get("mode")
        merged = {"status": sub_result.get("status", AgentStatus.SUCCESS.value)}
        if mode == "invoke":
            merged["source_registry"] = sub_result.get("source_registry", {}) or {}
            merged["gap_findings"] = sub_result.get("gap_findings", []) or []
            merged["exception_packet"] = sub_result.get("exception_packet", {}) or {}
        elif mode in ("ingest", "feedback"):
            merged["write_result"] = sub_result.get("write_result", {}) or {}
        return merged
