"""AgentCore Platform v1.0 — SVC-C2-132 State schema.

ADR-005: State is a flat TypedDict. LangGraph checkpoints use msgpack
serialization, so only plain serializable fields (primitives, list, dict) are
allowed — no Pydantic / dataclass / arbitrary objects, and never credentials.

Two-layer sensitive-data governance (docs/02_design.md §(a),
the two-layer data-governance split for accumulation-type agents):

* Layer 1 (checkpoint isolation) — the large read-only `event_config` export is
  NEVER checkpointed inline. It is either kept inline only when small
  (`<= max_input_bytes`) or referenced through the opaque `payload_ref` handle.
  All State fields are business-key allowlisted (mode / ids / JSON-serializable
  structured findings); no player-PII column exists in core.
* Layer 2 (persistent KB governance) — lives in the injected KB store, NOT in
  State (never checkpointed). See `src/services/kb_store.py`.

The outer backbone (5 nodes) and the inner `PromiseReviewWorkflowGraph` share this
schema (both are AgentBaseGraph). The inner-only working fields are declared here so
LangGraph threads them between the inner nodes; the outer `merge_output()` surfaces
ONLY the mode-specific subset (docs/02_design.md §State Definition).
"""

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Player-Promise Impact Review agent state (flat TypedDict).

    Shared fields (`user_input`, `status`, `session_id`, `node_history`,
    `error_log`, `caller_trust_level`, ...) are inherited from `AgentState`.
    """

    # ── Request identity / routing (caller → pre_process) ────────────────────
    mode: str                 # canonical "invoke" | "ingest" | "feedback"
    tenant_id: str            # RBAC primary scope key (fail-closed)
    org_id: str               # RBAC secondary scope key (fail-closed)

    # ── Derived by outer pre_process ─────────────────────────────────────────
    validated_input: dict     # normalised, closed-schema-validated mode payload
    payload_ref: str          # layer-1 handle for a large read-only export; "" when inline
    rejected: list            # [{field, reason}] — S-2 / RBAC rejections (with status=error)

    # ── invoke outputs (inner → merge_output surfaces these) ─────────────────
    source_registry: dict     # provenance anchor: source_id -> {kind, version, span, text, facts}
    gap_findings: list        # [{gap_id, citations[], version, beneficiary_impact, review_question, uncertainty_text}]
    exception_packet: dict     # gaps + missing approvals + past-adjudication reference block + review questions

    # ── ingest / feedback output (inner → merge_output surfaces this) ────────
    write_result: dict        # {written_count, record_ids[]}

    # ── Final projection (post_process) ──────────────────────────────────────
    result: dict              # non-assertive projection (invoke) / write summary (ingest·feedback)

    # ── Inner-workflow intermediates (declared as channels so the StateGraph
    #    threads them between the inner nodes; NOT surfaced to outer State by
    #    merge_output). No raw player-PII, no credentials.
    reconcile_result: dict    # inner #2 deterministic reconcile (matched ids / missing approvals)
    promise_params: dict      # inner #3 LLM promise normalization (audience / caps / deadlines / ...)
    actual_rule: dict         # inner #4 reconstructed actual reward-execution conditions
    kb_recall: list           # inner #6 recalled past adjudications (history_ref namespace — reference only)
