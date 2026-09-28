"""AgentCore Platform v1.0 — SVC-C2-132 outer graph.

Cat 2 — AgentBaseGraph (fixed 5-node backbone). The domain complexity lives in the
inner `PromiseReviewWorkflowGraph`, wrapped by `PromiseReviewGraphNode` in the
`main` slot. `add_edges()` / `route()` are NOT overridden (framework-owned backbone).

    START → initialize → pre_process → main(GraphNode) → {route} → post_process → finalize → END
"""

from framework.graph.agent_base_graph import AgentBaseGraph

from src.graph.promise_review_graph_node import PromiseReviewGraphNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State
from src.services.config_spec import validate_config


class PlayerPromiseImpactReviewGraph(AgentBaseGraph):
    """Game Live-Ops Player-Promise Impact Review Agent (SVC-C2-132)."""

    @property
    def name(self) -> str:
        return "svc_c2_132"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # Domain config validation (fail-closed — e.g. kb_raw_retention_days must be
        # a positive integer) before the framework's own backbone checks.
        validate_config(self.config or {})
        super()._validate_config()

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize
        cfg = self.config or {}
        self._nodes["pre_process"] = PreProcessNode(config=cfg)
        self._nodes["main"] = PromiseReviewGraphNode(config=cfg)
        self._nodes["post_process"] = PostProcessNode(config=cfg)

    # add_edges() is NOT overridden — the backbone wiring is framework-owned.
