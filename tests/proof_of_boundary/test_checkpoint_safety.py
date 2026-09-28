"""PB-2/PB-5 (layer-1): checkpoint isolation. State written by the nodes must be
JSON/msgpack-serialisable primitives only, and the large read-only `event_config`
export passed via `payload_ref` must NEVER land in the checkpointed State
(`validated_input`). The assertion DESERIALISES the state rather than byte-scanning a
blob — the PB must deserialize it, not inspect the opaque bytes."""

import json

from src.nodes.pre_process_node import PreProcessNode
from src.services.export_store import ExportStore, set_export_store


def _prime_state(body: dict) -> dict:
    return {
        "user_input": json.dumps(body, ensure_ascii=False),
        "input_context": {"authorized_scopes": [{"tenant_id": "t1", "org_id": "o1",
                                                  "permissions": ["read", "write"]}],
                          "request_ts": "2026-08-05T00:00:00+00:00"},
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "correlation_id": "pb-checkpoint",
    }


def test_payload_ref_keeps_raw_export_out_of_state():
    # Stage a LARGE export behind a ref; the raw event_config must not enter State.
    big_rule = "秘密の詳細ルール " * 5000
    set_export_store(ExportStore())  # (staging not needed for the pre_process assertion)
    body = {
        "mode": "invoke", "tenant_id": "t1", "org_id": "o1",
        "event_config_ref": "export-handle-1",
        "reward_table": {"version": "v1", "entries": [{"reward_id": "R1", "approved": True}]},
        "change_request": {"change_id": "CR1", "summary": "s"},
        "notice_text": "告知文",
    }
    node = PreProcessNode(config={"max_input_bytes": 262144})
    result = node(_prime_state(body))

    assert result["status"] == "success"
    vi = result["validated_input"]
    assert "event_config" not in vi                    # raw export NOT in checkpointed state
    assert result["payload_ref"] == "export-handle-1"  # only the opaque handle
    assert big_rule not in json.dumps(vi, ensure_ascii=False)

    # DESERIALISE round-trip proves primitives-only (no Pydantic/objects/credentials).
    serialisable = {k: v for k, v in result.items() if k not in ("node_history", "execution_time")}
    restored = json.loads(json.dumps(serialisable, ensure_ascii=False))
    assert restored["validated_input"]["tenant_id"] == "t1"
    set_export_store(None)


def test_inline_small_config_carries_no_credentials():
    body = {
        "mode": "invoke", "tenant_id": "t1", "org_id": "o1",
        "event_config": {"version": "v1", "rewards": [{"reward_id": "R1", "rule_text": "1日1回"}]},
        "reward_table": {"version": "v1", "entries": [{"reward_id": "R1", "approved": True}]},
        "change_request": {"change_id": "CR1", "summary": "s"},
        "notice_text": "告知文",
    }
    node = PreProcessNode(config={})
    result = node(_prime_state(body))
    assert result["status"] == "success" and result["payload_ref"] == ""
    blob = json.dumps(result.get("validated_input", {}), ensure_ascii=False).lower()
    for bad in ("bearer ", "api_key", "secret", "authorization"):
        assert bad not in blob
