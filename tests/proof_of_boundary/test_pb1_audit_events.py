"""PB-1: BaseNode → AuditLogger. Every node's execute() must emit at least one
DOMAIN event (not just the framework-auto node_start/node_complete). We patch each
node module's `emit_trace_event` binding and assert the domain events actually fire
across a full invoke — a PB that watched only framework auto-events would stay green
even if every domain emit were deleted — framework events are not proof."""

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import PlayerPromiseImpactReviewGraph
from tests.conftest import INVOKE_BODY, input_context, load_config

_NODE_MODULES = (
    "src.nodes.pre_process_node",
    "src.nodes.inner_pre_node",
    "src.nodes.inner_main_node",
    "src.nodes.inner_post_node",
    "src.nodes.post_process_node",
)


def test_domain_events_fire_across_invoke(monkeypatch, fake_llm, fresh_stores):
    seen: list[str] = []

    def spy(event_type, payload, state):
        seen.append(event_type)

    import importlib
    for mod in _NODE_MODULES:
        m = importlib.import_module(mod)
        monkeypatch.setattr(m, "emit_trace_event", spy)

    agent = PlayerPromiseImpactReviewGraph(config=load_config())
    agent.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    agent.invoke(json.dumps(INVOKE_BODY, ensure_ascii=False), ctx=ctx, input_context=input_context())

    # domain events from every stage of the invoke pipeline must be present.
    for expected in ("input_validated", "reconcile", "gap_judgment", "kb_recall", "output_finalized"):
        assert expected in seen, f"missing domain event {expected!r}; saw {sorted(set(seen))}"
