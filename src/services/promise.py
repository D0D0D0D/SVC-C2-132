"""LLM steps — promise normalisation (#3) and gap judgment (#5).

The LLM fills structured VALUES; the schema is FIXED and embedded verbatim in the
prompt (a prompt that names a schema but omits its body makes the model emit `{}` —
the MunicipalContractVariationEvidenceBriefAgent live-LLM defect). gap_judgment is instructed to produce ONLY neutral
review questions with source-bound citations — never a legal/eligibility verdict —
and to cite ONLY this-case sources (past cases are a reference-only block that must
never be cited: docs/02_design.md §(b),(c)).
"""

from __future__ import annotations

import json

_PROMISE_SCHEMA = (
    '{"audience": "<who the notice targets>", '
    '"probabilistic": "lottery|guaranteed|unclear", '
    '"caps": "<any stated limit, or empty>", '
    '"deadlines": "<any stated deadline, or empty>", '
    '"distribution_timing": "<when the reward is actually delivered, or empty>"}'
)

_GAP_SCHEMA = (
    '{"findings": [\n'
    '  {"gap_id": "G1",\n'
    '   "beneficiary_impact": "<which player group is affected and how>",\n'
    '   "review_question": "<a NEUTRAL review question for a human — NEVER a legal/'
    'eligibility verdict>",\n'
    '   "uncertainty_text": "<what is uncertain / needs human confirmation>",\n'
    '   "citations": [\n'
    '     {"source_id": "<one of the THIS-CASE source_ids listed above>",\n'
    '      "version": "<that source\'s version, copied exactly>",\n'
    '      "span": [<start>, <end>],\n'
    '      "verbatim_quote": "<an EXACT substring of that source\'s text, '
    '>= MIN_QUOTE_LEN characters>",\n'
    '      "asserted_facts": {"<fact_key>": <value copied from that source\'s facts>}}\n'
    '   ]}\n'
    ']}'
)


def build_promise_prompt(notice_text: str) -> str:
    return (
        "You normalise a player-facing game live-ops notice into structured promise "
        "parameters. Output ONLY a JSON object of exactly this shape:\n"
        f"{_PROMISE_SCHEMA}\n\n"
        "NOTICE TEXT:\n"
        f"{notice_text}\n\n"
        "Rules: infer only from the notice text; use empty strings for anything not "
        "stated; do not add keys. Output ONLY the JSON object."
    )


def build_gap_prompt(promise_params: dict, actual_rule: dict, source_registry: dict,
                     min_quote_len: int) -> str:
    # THIS-CASE citable sources (id / kind / version / span / text / facts).
    # Past adjudications are deliberately NOT in this prompt — they are recalled
    # downstream (#6) as a human reference block, never fed to the gap LLM, so a
    # recalled free-text value can never become a prompt-injection vector here
    # (docs/02_design.md §Inner domain workflow order; §(b) history_ref split).
    src_lines = []
    for sid, entry in source_registry.items():
        src_lines.append(json.dumps({
            "source_id": sid, "kind": entry.get("kind"), "version": entry.get("version"),
            "span": entry.get("span"), "text": entry.get("text"), "facts": entry.get("facts"),
        }, ensure_ascii=False))
    sources_block = "\n".join(src_lines) if src_lines else "(none)"

    schema = _GAP_SCHEMA.replace("MIN_QUOTE_LEN", str(min_quote_len))
    return (
        "You review whether a player-facing notice's PROMISE diverges from the game's "
        "ACTUAL reward-execution rules, and surface each divergence as a NEUTRAL review "
        "question for a human live-ops manager. You never state a legal, 景表法, gacha-"
        "display, eligibility, or publish/compensate verdict — only questions and impacts.\n\n"
        f"PROMISE (normalised):\n{json.dumps(promise_params, ensure_ascii=False)}\n\n"
        f"ACTUAL RULES (reconstructed from config):\n{json.dumps(actual_rule, ensure_ascii=False)}\n\n"
        f"THIS-CASE SOURCES (cite ONLY these source_ids):\n{sources_block}\n\n"
        "Output ONLY a JSON object of exactly this shape:\n"
        f"{schema}\n\n"
        "Hard rules:\n"
        "- Cite ONLY source_ids from THIS-CASE SOURCES; copy version and span from the source.\n"
        "- verbatim_quote MUST be an exact substring of that source's text and inside its span.\n"
        "- asserted_facts MUST be a subset of that source's facts (copy the values exactly).\n"
        "- Every number you mention in review_question/beneficiary_impact/uncertainty_text MUST "
        "also appear in an asserted_facts value.\n"
        "- review_question is a question, not a ruling. No verdict keys. Output ONLY the JSON object."
    )


def _extract_json(content: str):
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(content[start: end + 1])
        except (ValueError, TypeError):
            return None
    return None


def parse_promise(content: str) -> dict | None:
    obj = _extract_json(content)
    if isinstance(obj, dict):
        return obj
    return None


def parse_gap_findings(content: str) -> list | None:
    obj = _extract_json(content)
    if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
        return obj["findings"]
    if isinstance(obj, list):
        return obj
    return None


def fallback_promise() -> dict:
    """No LLM injected — cannot infer a promise from free text; empty (degraded)."""
    return {"audience": "", "probabilistic": "unclear", "caps": "",
            "deadlines": "", "distribution_timing": ""}


def fallback_gap_findings() -> list:
    """No LLM injected — gap judgment requires the LLM; return no findings (degraded,
    honest) rather than fabricating a divergence deterministically."""
    return []
