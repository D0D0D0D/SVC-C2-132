"""KB store (layer-2) — RBAC isolation, retention barrier/job, index-axis separation."""

from src.services import kb_store as kb

NOW = "2026-08-05T00:00:00+00:00"


def _rec(created_at=NOW, category="reward", summary="報酬告知の相談", viewpoint="V1",
         verdict="corrected", rationale="理由A"):
    return {
        "notice_summary": summary, "actual_rule_summary": "実ルール", "gap_note": "乖離",
        "correction": "修正", "adjudication_rationale": rationale, "verdict": verdict,
        "category_key": category, "viewpoint_code": viewpoint,
        "retrieval_text": kb.derive_retrieval_text(category, summary, viewpoint),
        "created_at": created_at,
    }


def test_write_returns_ids():
    s = kb.KbStore()
    res = s.write("t1", "o1", [_rec(), _rec()])
    assert res["written_count"] == 2 and len(res["record_ids"]) == 2


def test_tenant_org_isolation():
    s = kb.KbStore()
    s.write("t1", "o1", [_rec(summary="scoped consult")])
    s.write("t1", "o2", [_rec(summary="scoped consult")])
    assert len(s.recall("t1", "o1", "scoped consult", 8, NOW, 365)) == 1
    assert len(s.recall("t1", "o2", "scoped consult", 8, NOW, 365)) == 1
    # a scope with no records never sees another scope's rows.
    assert s.recall("t9", "o9", "scoped consult", 8, NOW, 365) == []


def test_recall_item_namespaced_keys():
    s = kb.KbStore()
    s.write("t1", "o1", [_rec()])
    item = s.recall("t1", "o1", "報酬告知の相談", 8, NOW, 365)[0]
    assert item["past_verdict"] == "corrected"
    assert "verdict" not in item  # avoids the current-case verdict floor collision


def test_read_time_barrier_excludes_expired():
    s = kb.KbStore()
    s.write("t1", "o1", [_rec(created_at="2020-01-01T00:00:00+00:00", summary="古い相談")])
    # even though the redaction job has NOT run, recall must not return expired raw.
    assert s.recall("t1", "o1", "古い相談", 8, NOW, 365) == []


def test_retention_job_redacts_raw_keeps_structured():
    s = kb.KbStore()
    ids = s.write("t1", "o1", [_rec(created_at="2020-01-01T00:00:00+00:00")])["record_ids"]
    report = s.run_retention(NOW, 365)
    assert report["redacted_count"] == 1
    rec = s._records[("t1", "o1")][0]  # inspect the physical record after the job
    assert rec["notice_summary"] == "" and rec["gap_note"] == ""      # raw-text gone
    assert rec["verdict"] == "corrected" and rec["adjudication_rationale"] == "理由A"  # moat kept
    assert "報酬告知の相談" not in rec["retrieval_text"]              # raw-derived index gone
    assert ids  # record_ids were returned


def test_within_retention_survives():
    s = kb.KbStore()
    ids = s.write("t1", "o1", [_rec(created_at=NOW)])["record_ids"]
    s.run_retention(NOW, 365)
    rec = s.get_record("t1", "o1", ids[0], NOW, 365)
    assert rec["notice_summary"] != ""  # inside retention window — untouched


def test_index_axis_separation():
    # search matches retrieval_text ONLY — swapping every record's verdict/rationale
    # (conclusion vocabulary) must not change recall ranking.
    s = kb.KbStore()
    s.write("t1", "o1", [_rec(verdict="corrected", rationale="A"),
                         _rec(verdict="rejected", rationale="B")])
    before = [i["history_ref"] for i in s.recall("t1", "o1", "報酬告知の相談", 8, NOW, 365)]
    for bucket in s._records.values():
        for r in bucket:
            r["verdict"] = "accepted"
            r["adjudication_rationale"] = "全く別の結論語彙"
    after = [i["history_ref"] for i in s.recall("t1", "o1", "報酬告知の相談", 8, NOW, 365)]
    assert before == after and before


def test_undatable_record_treated_as_expired():
    assert kb.is_expired("", NOW, 365) is True
    assert kb.is_expired("not-a-date", NOW, 365) is True
