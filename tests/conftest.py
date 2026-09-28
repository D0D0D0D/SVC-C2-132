# SVC-C2-132 — shared test fixtures.

import copy
import json

import pytest

from shared.services.llm.base_llm import BaseLLM

# ── canonical valid payloads ──────────────────────────────────────────────────
_RULE_TEXT = "この報酬は1日1回まで、在庫上限は100個、配布は翌日となります。"

EVENT_CONFIG = {
    "version": "2026.3",
    "rewards": [
        {"reward_id": "R1", "rule_text": _RULE_TEXT,
         "per_day_limit": 1, "stock_cap": 100, "distribution_delay_days": 1,
         "acquisition_type": "lottery"},
    ],
}

INVOKE_BODY = {
    "mode": "invoke",
    "tenant_id": "t1", "org_id": "o1",
    "event_config": EVENT_CONFIG,
    "reward_table": {"version": "2026.3",
                     "entries": [{"reward_id": "R1", "approved": True, "eligibility": "全プレイヤー対象"}]},
    "change_request": {"change_id": "CR-1", "summary": "限定報酬の告知文を追加"},
    "notice_text": "イベント期間中いつでも入手可能な限定報酬をプレゼント！",
}

INGEST_BODY = {
    "mode": "ingest", "tenant_id": "t1", "org_id": "o1",
    "records": [{
        "notice_summary": "いつでも入手可能と告知した限定報酬",
        "actual_rule_summary": "実際は1日1回・在庫上限あり",
        "gap_note": "常時入手可能という表現が実ルールと乖離",
        "correction": "『期間中、条件を満たした場合に入手可能』へ修正",
        "adjudication_rationale": "入手条件の明示不足を是正",
        "verdict": "corrected", "category_key": "always_available", "viewpoint_code": "V-CAP",
        "source_of_truth_ref": "TICKET-1",
    }],
}

FEEDBACK_BODY = {
    "mode": "feedback", "tenant_id": "t1", "org_id": "o1",
    "case_id": "t1:o1:1", "manager_action": "corrected", "final_verdict": "corrected",
    "correction_note": "告知文に入手条件を追記した",
}

# A provenance-valid gap analysis whose citation is grounded in EVENT_CONFIG R1.
_QUOTE = "在庫上限は100個"
VALID_GAP = {
    "findings": [{
        "gap_id": "G1",
        "beneficiary_impact": "「いつでも入手可能」と受け取ったプレイヤーが、1日1回・在庫上限100個の実ルールで期待を裏切られ得る",
        "review_question": "在庫上限100個・1日1回という条件の表示要否を確認してください",
        "uncertainty_text": "配布が翌日になる点の告知有無が不明",
        "citations": [{
            "source_id": "cfg:R1", "version": "2026.3", "span": [0, len(_RULE_TEXT)],
            "verbatim_quote": _QUOTE, "asserted_facts": {"stock_cap": 100, "per_day_limit": 1},
        }],
    }],
}

VALID_PROMISE = {
    "audience": "全プレイヤー", "probabilistic": "unclear", "caps": "",
    "deadlines": "イベント期間中", "distribution_timing": "",
}


class FakeLLM(BaseLLM):
    """Deterministic BaseLLM: returns the promise payload for the promise prompt and
    the provenance-valid gap payload for the gap prompt (detected by prompt marker)."""

    def __init__(self, gap: dict | None = None, promise: dict | None = None) -> None:
        self._gap = gap if gap is not None else VALID_GAP
        self._promise = promise if promise is not None else VALID_PROMISE

    def complete(self, messages: list) -> dict:
        content = messages[-1].get("content", "") if messages else ""
        payload = self._gap if "THIS-CASE SOURCES" in content else self._promise
        return {"content": json.dumps(payload, ensure_ascii=False), "tool_calls": [], "model": "fake", "usage": {}}

    def stream(self, messages: list):
        yield json.dumps(self._gap, ensure_ascii=False)

    def bind_tools(self, tools: list) -> "FakeLLM":
        return self


def load_config() -> dict:
    import pathlib
    import yaml
    p = pathlib.Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def scopes(perms=("read", "write"), tenant="t1", org="o1") -> list:
    return [{"tenant_id": tenant, "org_id": org, "permissions": list(perms)}]


def input_context(perms=("read", "write"), request_ts="2026-08-05T00:00:00+00:00") -> dict:
    return {"authorized_scopes": scopes(perms), "request_ts": request_ts}


@pytest.fixture
def config() -> dict:
    return load_config()


@pytest.fixture
def invoke_body() -> dict:
    return copy.deepcopy(INVOKE_BODY)


@pytest.fixture
def ingest_body() -> dict:
    return copy.deepcopy(INGEST_BODY)


@pytest.fixture
def feedback_body() -> dict:
    return copy.deepcopy(FEEDBACK_BODY)


@pytest.fixture
def fake_llm():
    """Inject FakeLLM into the module-level seam; reset afterwards."""
    from src.services.llm_seam import set_llm
    llm = FakeLLM()
    set_llm(llm)
    yield llm
    set_llm(None)


@pytest.fixture
def fresh_stores():
    """Reset the KB and export store seams to fresh reference stores per test."""
    from src.services.kb_store import set_kb_store, KbStore
    from src.services.export_store import set_export_store, ExportStore
    store = KbStore()
    export = ExportStore()
    set_kb_store(store)
    set_export_store(export)
    yield store, export
    set_kb_store(None)
    set_export_store(None)


@pytest.fixture
def agent_config(fresh_stores) -> dict:
    """Use the integration happy path's fresh request-local store seams."""
    return {}


@pytest.fixture
def case_payload() -> dict:
    """Exercise the same invoke payload as the integration happy path."""
    return copy.deepcopy(INVOKE_BODY)


@pytest.fixture
def live_input_context() -> dict:
    """Supply the authorized scope and fixed timestamp required by invoke mode."""
    return input_context()


@pytest.fixture
def live_llm_injector():
    """Connect the live-check client to the module-level LLM seam."""
    from src.services.llm_seam import set_llm

    def _inject(agent, llm):
        set_llm(llm)
        return agent

    yield _inject
    set_llm(None)
