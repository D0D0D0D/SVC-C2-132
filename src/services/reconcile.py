"""Deterministic reconcile (internal Tool — non-LLM, non-caller-controlled).

Builds the `source_registry` provenance anchor and reconstructs the actual reward-
execution conditions from the versioned config. Everything here is deterministic:
the same inputs always produce the same registry (no wall-clock / random).

`source_registry[source_id] = {kind, version, span, text, facts}` is the ONLY set
of ids a gap finding may cite (docs/02_design.md §(b)); it is produced here, never
by the LLM and never from caller-controlled free text.
"""

from __future__ import annotations

_NUMERIC_FACT_KEYS = ("per_day_limit", "stock_cap", "distribution_delay_days")

# The provenance anchor is bounded so a large read-only export resolved via
# payload_ref cannot re-enter (checkpointed) State through source_registry
# (docs/02 §2-1 layer-1). A quote beyond the cap simply fails verification
# (fail-closed) — the registry is an anchor, not a store for arbitrary blobs.
_DEFAULT_MAX_SOURCE_CHARS = 20000
_DEFAULT_MAX_SOURCES = 500


def _reward_facts(reward: dict) -> dict:
    facts: dict = {}
    for k in _NUMERIC_FACT_KEYS:
        v = reward.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            facts[k] = v
    at = reward.get("acquisition_type")
    if isinstance(at, str) and at.strip():
        facts["acquisition_type"] = at
    return facts


def build_source_registry(validated_input: dict, event_config: dict,
                          max_source_chars: int = _DEFAULT_MAX_SOURCE_CHARS,
                          max_sources: int = _DEFAULT_MAX_SOURCES) -> dict:
    """Return the immutable, SIZE-BOUNDED provenance anchor for this /invoke request.

    Each source text is capped to `max_source_chars` and the total source count to
    `max_sources`, so a large read-only export (payload_ref path) cannot bloat State.
    """
    registry: dict[str, dict] = {}
    version = str(event_config.get("version", "")) if isinstance(event_config, dict) else ""

    def _add(sid: str, kind: str, ver: str, text: str, facts: dict) -> None:
        if len(registry) >= max_sources:
            return
        capped = (text or "")[:max_source_chars]
        registry[sid] = {"kind": kind, "version": ver, "span": [0, len(capped)],
                         "text": capped, "facts": facts}

    _add("notice", "notice_text", version, validated_input.get("notice_text", "") or "", {})

    for reward in (event_config.get("rewards", []) or []) if isinstance(event_config, dict) else []:
        if not isinstance(reward, dict):
            continue
        rid = reward.get("reward_id")
        if isinstance(rid, str) and rid.strip():
            _add(f"cfg:{rid}", "event_config", version, reward.get("rule_text", "") or "", _reward_facts(reward))

    reward_table = validated_input.get("reward_table", {}) or {}
    rt_version = str(reward_table.get("version", "")) if isinstance(reward_table, dict) else ""
    for entry in (reward_table.get("entries", []) or []) if isinstance(reward_table, dict) else []:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("reward_id")
        if isinstance(rid, str) and rid.strip():
            _add(f"reward:{rid}", "reward_table", rt_version, entry.get("eligibility", "") or "",
                 {"approved": bool(entry.get("approved", False))})

    cr = validated_input.get("change_request", {}) or {}
    if isinstance(cr, dict) and isinstance(cr.get("change_id"), str) and cr["change_id"].strip():
        _add(f"cr:{cr['change_id']}", "change_request", version, cr.get("summary", "") or "", {})
    return registry


def deterministic_reconcile(validated_input: dict, event_config: dict) -> dict:
    """Match config rewards against the approved reward table; detect missing
    approvals. Deterministic — no LLM, no wall-clock."""
    rewards = (event_config.get("rewards", []) or []) if isinstance(event_config, dict) else []
    cfg_ids = {r.get("reward_id") for r in rewards if isinstance(r, dict) and isinstance(r.get("reward_id"), str)}

    reward_table = validated_input.get("reward_table", {}) or {}
    approved_ids = set()
    table_ids = set()
    for e in (reward_table.get("entries", []) or []) if isinstance(reward_table, dict) else []:
        if isinstance(e, dict) and isinstance(e.get("reward_id"), str):
            table_ids.add(e["reward_id"])
            if e.get("approved") is True:
                approved_ids.add(e["reward_id"])

    matched = sorted(cfg_ids & table_ids)
    missing_approval = sorted(cfg_ids - approved_ids)  # in config but not approved
    unlisted = sorted(cfg_ids - table_ids)             # in config, absent from table
    return {
        "matched_ids": matched,
        "missing_approval_ids": missing_approval,
        "unlisted_ids": unlisted,
    }


def reconstruct_actual_rule(event_config: dict) -> dict:
    """Reconstruct the actual reward-execution conditions per reward (deterministic)."""
    out: dict[str, dict] = {}
    for reward in (event_config.get("rewards", []) or []) if isinstance(event_config, dict) else []:
        if not isinstance(reward, dict):
            continue
        rid = reward.get("reward_id")
        if not isinstance(rid, str) or not rid.strip():
            continue
        out[rid] = _reward_facts(reward)
    return out
