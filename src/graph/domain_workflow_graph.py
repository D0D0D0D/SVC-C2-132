"""AgentCore Platform v1.0 — SVC-C2-132 inner domain workflow graph.

Inherits AgentBaseGraph (standard 3-slot backbone). Instantiated by
`PromiseReviewGraphNode.get_subgraph()`. The 7 conceptual invoke steps
(docs/02_design.md §Inner domain workflow) collapse into the 3 fixed slots by
grouping the deterministic steps together:

    pre_process  (InnerPreNode)   ①validate ②reconcile[source_registry] ④reconstruct
    main         (InnerMainNode)  ③promise_normalize(LLM) ⑤gap_judgment(LLM)
                                    · (ingest/feedback: sanitize_records)
    post_process (InnerPostNode)  ⑥kb_recall ⑦assemble_packet
                                    · (ingest/feedback: kb_write)

`get_output()` surfaces the mode-specific subset for the outer `merge_output()`.
`add_edges()` / `route()` are NOT overridden — the backbone is framework-owned.
"""

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.inner_pre_node import InnerPreNode
from src.nodes.inner_main_node import InnerMainNode
from src.nodes.inner_post_node import InnerPostNode
from src.schemas.state import State


class PromiseReviewWorkflowGraph(AgentBaseGraph):
    """Inner multi-step promise-review workflow (fixed 3-slot backbone)."""

    @property
    def name(self) -> str:
        return "promise_review_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize
        cfg = self.config or {}
        self._nodes["pre_process"] = InnerPreNode(config=cfg)
        self._nodes["main"] = InnerMainNode(config=cfg)
        self._nodes["post_process"] = InnerPostNode(config=cfg)

    # add_edges() / route() are NOT overridden — the backbone is framework-owned.

    def get_output(self, state: dict) -> dict:
        """Surface the mode-specific subset to the outer GraphNode.merge_output()."""
        return {
            "mode": state.get("mode"),
            "source_registry": state.get("source_registry", {}),
            "gap_findings": state.get("gap_findings", []),
            "exception_packet": state.get("exception_packet", {}),
            "write_result": state.get("write_result", {}),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
