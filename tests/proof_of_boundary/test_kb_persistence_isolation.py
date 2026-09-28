"""PB (layer-2): persistent-KB governance. State-safety PBs alone do NOT protect the
accumulation store — RBAC scope isolation and retention must hold at the persistence
layer itself (the two-layer data-governance split). This is the
KB-store counterpart of the checkpoint PB (two layers ⇒ two PBs)."""

from src.services import kb_store as kb

NOW = "2026-08-05T00:00:00+00:00"


def _rec(summary, created_at=NOW):
    return {
        "notice_summary": summary, "actual_rule_summary": "r", "gap_note": "g", "correction": "c",
        "adjudication_rationale": "rationale", "verdict": "corrected",
        "category_key": "reward", "viewpoint_code": "V1",
        "retrieval_text": kb.derive_retrieval_text("reward", summary, "V1"),
        "created_at": created_at,
    }


def test_cross_tenant_and_cross_org_invisibility():
    store = kb.KbStore()
    store.write("tenantA", "orgA", [_rec("secret A consult")])
    store.write("tenantB", "orgB", [_rec("secret B consult")])
    store.write("tenantA", "orgB", [_rec("secret AB consult")])

    # Each scope recalls ONLY its own rows, never another tenant's or another org's.
    assert [i["notice_summary"] for i in store.recall("tenantA", "orgA", "secret consult", 8, NOW, 365)] \
        == ["secret A consult"]
    assert [i["notice_summary"] for i in store.recall("tenantB", "orgB", "secret consult", 8, NOW, 365)] \
        == ["secret B consult"]
    # tenantA/orgB is a DIFFERENT scope than tenantA/orgA (org is a hard boundary too).
    assert [i["notice_summary"] for i in store.recall("tenantA", "orgB", "secret consult", 8, NOW, 365)] \
        == ["secret AB consult"]
    # unknown scope sees nothing.
    assert store.recall("tenantZ", "orgZ", "secret consult", 8, NOW, 365) == []


def test_retention_removes_raw_from_persistence_layer():
    store = kb.KbStore()
    ids = store.write("t", "o", [_rec("old raw consult", created_at="2019-01-01T00:00:00+00:00")])["record_ids"]
    store.run_retention(NOW, 365)
    # physical record still exists but raw text is gone at the persistence layer.
    raw = store._records[("t", "o")][0]
    assert raw["notice_summary"] == "" and raw["gap_note"] == "" and raw["correction"] == ""
    assert "old raw consult" not in raw["retrieval_text"]
    # structured-retained (the moat) is intact.
    assert raw["verdict"] == "corrected" and raw["adjudication_rationale"] == "rationale"
    # and the record is no longer recallable by its raw-derived text.
    assert store.get_record("t", "o", ids[0], NOW, 365)["notice_summary"] == ""
