"""Deterministic reconcile — source_registry construction + size bound (layer-1)."""

from src.services import reconcile

_EC = {"version": "2026.3", "rewards": [
    {"reward_id": "R1", "rule_text": "1日1回まで、在庫上限は100個。",
     "per_day_limit": 1, "stock_cap": 100, "distribution_delay_days": 1}]}
_VI = {"notice_text": "いつでも入手可能！",
       "reward_table": {"version": "2026.3", "entries": [{"reward_id": "R1", "approved": True, "eligibility": "全員"}]},
       "change_request": {"change_id": "CR1", "summary": "追加"}}


def test_registry_shape():
    reg = reconcile.build_source_registry(_VI, _EC)
    assert reg["cfg:R1"]["kind"] == "event_config" and reg["cfg:R1"]["facts"]["stock_cap"] == 100
    assert reg["reward:R1"]["facts"]["approved"] is True
    assert reg["notice"]["kind"] == "notice_text"
    assert reg["cr:CR1"]["kind"] == "change_request"


def test_source_text_is_capped():
    # a huge rule_text (payload_ref path) must not bloat State — text is capped.
    big = {"version": "v1", "rewards": [{"reward_id": "R1", "rule_text": "あ" * 100000}]}
    reg = reconcile.build_source_registry({"notice_text": "n"}, big, max_source_chars=20000)
    assert len(reg["cfg:R1"]["text"]) == 20000
    assert reg["cfg:R1"]["span"] == [0, 20000]


def test_source_count_is_capped():
    many = {"version": "v1", "rewards": [{"reward_id": f"R{i}", "rule_text": "x"} for i in range(1000)]}
    reg = reconcile.build_source_registry({"notice_text": "n"}, many, max_sources=50)
    assert len(reg) <= 50


def test_reconcile_missing_approval():
    ec2 = {"version": "v", "rewards": [{"reward_id": "R1"}, {"reward_id": "R2"}]}
    vi2 = {"reward_table": {"entries": [{"reward_id": "R1", "approved": True}]}}
    res = reconcile.deterministic_reconcile(vi2, ec2)
    assert "R2" in res["missing_approval_ids"] and "R1" not in res["missing_approval_ids"]
