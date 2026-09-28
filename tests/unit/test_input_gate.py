"""S-2 input gate — mode canon, RBAC (read/write), closed schema, payload_ref split."""

from src.services import input_gate


def test_canonical_mode():
    assert input_gate.canonical_mode("/invoke") == "invoke"
    assert input_gate.canonical_mode("INGEST") == "ingest"
    assert input_gate.canonical_mode("nope") is None
    assert input_gate.canonical_mode(None) is None


def test_rbac_read_write_scope():
    ro = [{"tenant_id": "t1", "org_id": "o1", "permissions": ["read"]}]
    rw = [{"tenant_id": "t1", "org_id": "o1", "permissions": ["read", "write"]}]
    assert input_gate.authorize("t1", "o1", ro, "read")[0] is True
    assert input_gate.authorize("t1", "o1", ro, "write")[0] is False   # read-only scope, write denied
    assert input_gate.authorize("t1", "o1", rw, "write")[0] is True
    assert input_gate.authorize("t1", "o2", rw, "read")[0] is False    # wrong org
    assert input_gate.authorize("t2", "o1", rw, "read")[0] is False    # wrong tenant
    assert input_gate.authorize("t1", "o1", [], "read")[0] is False    # empty scopes -> deny (fail-closed)
    assert input_gate.authorize("", "o1", rw, "read")[0] is False      # missing tenant


def test_default_scope_is_read_only():
    # a scope with no explicit permissions defaults to read-only; write needs explicit grant.
    s = [{"tenant_id": "t1", "org_id": "o1"}]
    assert input_gate.authorize("t1", "o1", s, "read")[0] is True
    assert input_gate.authorize("t1", "o1", s, "write")[0] is False


def test_invoke_closed_schema_and_required(invoke_body):
    ok, rej, vi, ref = input_gate.validate_invoke(invoke_body, {})
    assert ok and ref == "" and "event_config" in vi
    bad = dict(invoke_body, unexpected_key=1)
    ok2, rej2, _, _ = input_gate.validate_invoke(bad, {})
    assert not ok2 and any(r["field"] == "unexpected_key" for r in rej2)


def test_invoke_requires_all_docs(invoke_body):
    for missing in ("reward_table", "change_request", "notice_text"):
        b = dict(invoke_body)
        b.pop(missing)
        ok, rej, _, _ = input_gate.validate_invoke(b, {})
        assert not ok, missing


def test_payload_ref_exclusive(invoke_body):
    # both inline + ref -> reject
    both = dict(invoke_body, event_config_ref="ref-1")
    ok, rej, _, _ = input_gate.validate_invoke(both, {})
    assert not ok
    # ref only -> ok, event_config kept OUT of validated_input (layer-1 isolation)
    ref_only = {k: v for k, v in invoke_body.items() if k != "event_config"}
    ref_only["event_config_ref"] = "ref-1"
    ok2, _, vi2, pref = input_gate.validate_invoke(ref_only, {})
    assert ok2 and pref == "ref-1" and "event_config" not in vi2


def test_oversize_inline_rejected(invoke_body):
    invoke_body["event_config"] = {"version": "v1", "rewards": [{"reward_id": "R", "rule_text": "x" * 300000}]}
    ok, rej, _, _ = input_gate.validate_invoke(invoke_body, {"max_input_bytes": 262144})
    assert not ok and any("max_input_bytes" in r["reason"] for r in rej)


def test_oversize_reward_table_rejected(invoke_body):
    # reward_table/change_request/context are also size-bounded (not just event_config).
    invoke_body["reward_table"] = {"version": "v1", "junk": "x" * 300000}
    ok, rej, _, _ = input_gate.validate_invoke(invoke_body, {"max_input_bytes": 262144})
    assert not ok and any(r["field"] == "reward_table" for r in rej)


def test_ingest_record_verdict_enum(ingest_body):
    ok, _, _ = input_gate.validate_ingest(ingest_body, {})
    assert ok
    ingest_body["records"][0]["verdict"] = "maybe"
    ok2, rej2, _ = input_gate.validate_ingest(ingest_body, {})
    assert not ok2 and any(r["field"].endswith("verdict") for r in rej2)


def test_feedback_manager_action_enum(feedback_body):
    ok, _, _ = input_gate.validate_feedback(feedback_body, {})
    assert ok
    feedback_body["manager_action"] = "ignore"
    ok2, rej2, _ = input_gate.validate_feedback(feedback_body, {})
    assert not ok2 and any(r["field"] == "manager_action" for r in rej2)


def test_non_json_body_rejected():
    obj, rej = input_gate.parse_body("<<<not json>>>")
    assert obj is None and rej
