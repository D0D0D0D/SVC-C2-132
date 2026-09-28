"""S-3 non-assertion lexicon — closed bidirectional phrases + prohibited keys.

Also proves the guard does NOT shred the accumulation moat: a decisive phrase inside
a PAST-adjudication reference block (recorded fact) or a verbatim source quote does
not trip (a recorded past decision is a fact, not a leak).
"""

import pytest

from framework.errors import SecurityViolationError

from src.services import lexicon


def test_neutral_review_output_passes():
    result = {"status": "success", "result": {
        "mode": "invoke", "provenance_verified": True,
        "gap_findings": [{"review_question": "PR表記の要否を確認してください",
                          "beneficiary_impact": "一部プレイヤーに影響の可能性"}],
    }}
    lexicon.gate_scan(result, {})  # must not raise


def test_positive_assertion_blocked():
    result = {"status": "success", "result": {"mode": "invoke", "provenance_verified": True,
              "gap_findings": [{"review_question": "この告知は景表法上問題ないと判断します"}]}}
    with pytest.raises(SecurityViolationError):
        lexicon.gate_scan(result, {})


def test_violation_assertion_blocked():
    result = {"status": "success", "result": {"mode": "invoke", "provenance_verified": True,
              "gap_findings": [{"beneficiary_impact": "本件は優良誤認に該当する"}]}}
    with pytest.raises(SecurityViolationError):
        lexicon.gate_scan(result, {})


def test_prohibited_key_blocked():
    result = {"status": "success", "result": {"mode": "invoke", "provenance_verified": True,
              "verdict": "compliant"}}
    with pytest.raises(SecurityViolationError):
        lexicon.gate_scan(result, {})


def test_past_reference_verdict_not_tripped():
    # A recorded PAST adjudication that found a violation must be surfacable — the
    # guard must not trip on it (moat preservation).
    result = {"status": "success", "result": {
        "mode": "invoke", "provenance_verified": True,
        "past_reference": [{"history_ref": "hist:1", "past_verdict": "corrected",
                            "past_rationale": "過去に優良誤認に該当すると判断した事例"}],
        "gap_findings": [{"review_question": "表示の要否を確認"}],
    }}
    lexicon.gate_scan(result, {})  # must NOT raise


def test_verbatim_quote_not_tripped():
    # a source quote is evidence, not the AG asserting.
    result = {"status": "success", "result": {
        "mode": "invoke", "provenance_verified": True,
        "gap_findings": [{"citations": [{"verbatim_quote": "違法であるとの通報があった"}],
                          "review_question": "確認してください"}],
    }}
    lexicon.gate_scan(result, {})  # must NOT raise


def test_eligibility_decision_key_blocked():
    result = {"status": "success", "result": {"mode": "invoke", "provenance_verified": True,
              "gap_findings": [{"eligibility_decision": "補償対象"}]}}
    with pytest.raises(SecurityViolationError):
        lexicon.gate_scan(result, {})


def test_added_phrase_blocked():
    result = {"status": "success", "result": {"mode": "invoke", "provenance_verified": True,
              "gap_findings": [{"review_question": "違法性はないため確認不要です"}]}}
    with pytest.raises(SecurityViolationError):
        lexicon.gate_scan(result, {})


def test_error_result_skipped():
    lexicon.gate_scan({"status": "error", "result": {"verdict": "x"}}, {})  # no raise on already-error
