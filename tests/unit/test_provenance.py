"""S-3 Provenance — existence + version/span binding + verbatim + bidirectional facts.

The provenance PB set (docs/02_design.md §(b)): valid citation + ① short/non-
literal quote ② absent id ③ EXISTING but unrelated source_id ④ numeric mismatch
(bidirectional) ⑤ version/span mismatch.
"""

import copy

from src.services import provenance, reconcile

_RULE = "この報酬は1日1回まで、在庫上限は100個、配布は翌日となります。"
_VI = {
    "notice_text": "いつでも入手可能！",
    "reward_table": {"version": "2026.3", "entries": [{"reward_id": "R1", "approved": True, "eligibility": "全員"}]},
    "change_request": {"change_id": "CR1", "summary": "追加"},
}
_EC = {"version": "2026.3", "rewards": [
    {"reward_id": "R1", "rule_text": _RULE, "per_day_limit": 1, "stock_cap": 100,
     "distribution_delay_days": 1, "acquisition_type": "lottery"}]}
REG = reconcile.build_source_registry(_VI, _EC)

GOOD = {
    "gap_id": "G1", "beneficiary_impact": "1日1回制限で影響",
    "review_question": "在庫上限100個の表示要否を確認", "uncertainty_text": "配布翌日の記載不足",
    "citations": [{"source_id": "cfg:R1", "version": "2026.3", "span": [0, len(_RULE)],
                   "verbatim_quote": "在庫上限は100個", "asserted_facts": {"stock_cap": 100, "per_day_limit": 1}}],
}


def _cite(**over):
    f = copy.deepcopy(GOOD)
    f["citations"][0].update(over)
    return f


def test_valid_finding_passes():
    out = provenance.verify_findings([GOOD], REG, 8)
    assert out["terminal"] is None and len(out["findings"]) == 1


def test_short_quote_rejected():
    assert provenance.verify_findings([_cite(verbatim_quote="個")], REG, 8)["terminal"]


def test_non_literal_quote_rejected():
    assert provenance.verify_findings([_cite(verbatim_quote="存在しない引用文XYZ")], REG, 8)["terminal"]


def test_absent_source_id_rejected():
    assert provenance.verify_findings([_cite(source_id="cfg:GHOST")], REG, 8)["terminal"]


def test_existing_but_unrelated_source_id_rejected():
    # reward:R1 exists but its text is the eligibility string, not the rule text.
    assert provenance.verify_findings([_cite(source_id="reward:R1")], REG, 8)["terminal"]


def test_numeric_fact_mismatch_rejected():
    assert provenance.verify_findings([_cite(asserted_facts={"stock_cap": 999})], REG, 8)["terminal"]


def test_unbacked_prose_number_rejected():
    f = _cite(asserted_facts={"stock_cap": 100})
    f["review_question"] = "在庫上限500個の表示要否を確認"  # 500 not in asserted_facts
    assert provenance.verify_findings([f], REG, 8)["terminal"]


def test_version_binding():
    assert provenance.verify_findings([_cite(version="2026.9")], REG, 8)["terminal"]


def test_span_binding():
    assert provenance.verify_findings([_cite(span=[0, 99999])], REG, 8)["terminal"]


def test_no_citations_rejected():
    f = copy.deepcopy(GOOD)
    f["citations"] = []
    assert provenance.verify_findings([f], REG, 8)["terminal"]


def test_history_ref_cannot_be_cited():
    # a recalled past case (history_ref namespace) is never in source_registry.
    assert provenance.verify_findings([_cite(source_id="hist:t1:o1:1")], REG, 8)["terminal"]


def test_malformed_gap_id_rejected():
    # a nested dict smuggled into gap_id (attempted eligibility verdict) is rejected
    # structurally before it can be projected past the S-3 scan.
    f = copy.deepcopy(GOOD)
    f["gap_id"] = {"eligibility_decision": "このケースは補償対象です"}
    assert provenance.verify_findings([f], REG, 8)["terminal"]


def test_non_string_free_text_rejected():
    f = copy.deepcopy(GOOD)
    f["review_question"] = {"nested": "assertion"}
    assert provenance.verify_findings([f], REG, 8)["terminal"]


def test_man_magnitude_bidirectional_guard():
    # prose claims 1万 (10000) but asserts only 1 → unbacked, must reject.
    f = copy.deepcopy(GOOD)
    f["review_question"] = "上限は1万個か確認"
    f["citations"][0]["asserted_facts"] = {"per_day_limit": 1}
    assert provenance.verify_findings([f], REG, 8)["terminal"]


def test_kanji_idiom_in_prose_does_not_false_reject():
    # a legitimate finding whose prose contains the idiom `万一` must NOT be rejected
    # for a spurious number (regression for the arabic-only prose extractor).
    f = copy.deepcopy(GOOD)
    f["review_question"] = "万一プレイヤーが在庫上限は100個の条件で不利益を被る場合の表示要否を確認"
    f["citations"][0]["asserted_facts"] = {"stock_cap": 100, "per_day_limit": 1}
    assert provenance.verify_findings([f], REG, 8)["terminal"] is None
