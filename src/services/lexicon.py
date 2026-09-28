"""S-3 non-assertion boundary — legal-conclusion lexicon + prohibited output keys.

Position (docs/02_design.md §(c)): a PRODUCT-SAFETY / non-authoritative-output
mitigation, NOT an establishment of 景表法 / gacha-display compliance. The AG must
not state a legal/eligibility verdict on the current case; the final judgment stays
with the customer's live-ops manager + legal team downstream.

The PRIMARY defense is structural — verdict / legal-conclusion keys are excluded
from the output projection and rejected here (`find_prohibited_keys`). The lexicon
is the SECONDARY net: a closed, BIDIRECTIONAL, phrase-based (NFKC+casefold) scan for
decisive-ruling wording in BOTH directions (permit-assertions AND
violation/obligation-assertions). Phrase-based so the allowed review-question form
("PR 表記の要否を確認", "合理的根拠の確認対象") does not trip on a bare word.

Runs in `post_process._extra_security_gate_output(result)` (result only, no State);
raises `SecurityViolationError` (→ status=error) on any violation — fail-closed.
"""

from __future__ import annotations

import unicodedata

from framework.errors import SecurityViolationError

# Non-removable structural floor: these keys must never appear in the output
# projection (the AG does not emit a legal/eligibility/publish verdict).
PROHIBITED_KEY_FLOOR = (
    "verdict", "legal_conclusion", "compliance_status", "violation_determination",
    "approval_decision", "publishable", "landable", "is_legal", "is_compliant",
    "compensation_decision", "eligibility_ruling", "eligibility_decision",
    "legal_verdict", "ruling", "determination",
)

# Closed, bidirectional decisive-assertion phrases (secondary net).
_ASSERTIVE_PHRASES = (
    # permit / clearance direction
    "掲載してよい", "掲載して問題ない", "掲載可能です", "掲載可と判断", "掲載可能と判断",
    "問題ありません", "問題はありません", "問題ないと判断", "適法である", "適法です",
    "違反はありません", "違反しません", "コンプライアンス上問題な", "補償は不要", "補償する必要はな",
    "クリアしています", "合法である", "景表法上問題な", "適法と判断", "違法性はない", "違法性は無い",
    "違法ではない", "違法ではありません", "問題となりません", "補償対象外", "確認不要です",
    # violation / obligation direction
    "景表法違反である", "景表法違反です", "景表法に違反する", "優良誤認である", "優良誤認です",
    "優良誤認に該当する", "有利誤認である", "有利誤認に該当する", "違法である", "違法です",
    "違反が確定", "違反していると判断", "違反していると結論", "行政処分の対象となる",
    "補償が必要である", "補償しなければならない", "掲載してはならない", "掲載不可と判断",
    "補償対象です", "補償対象となります", "違法と判断", "違反と判断",
    # English decisive forms (defense in depth)
    "is illegal", "is unlawful", "violates the", "is a violation", "is compliant",
    "is legal", "may be published", "must compensate", "no compensation is required",
)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def effective_prohibited_keys(config: dict) -> set[str]:
    """Floor merged with additive config keys (removing a floor key has no effect)."""
    result = set(PROHIBITED_KEY_FLOOR)
    extra = (config or {}).get("output_prohibited_keys") or []
    for k in extra:
        if isinstance(k, str):
            result.add(k)
    return result


def find_prohibited_keys(obj, prohibited: set[str]) -> list[str]:
    """Recursively collect any prohibited dict keys at any nesting level."""
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in prohibited:
                    found.append(k)
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return found


def iter_string_leaves(obj, path: str = "") -> list[tuple[str, str]]:
    """(path, text) for every string leaf, recursively (dict values + list items)."""
    out: list[tuple[str, str]] = []

    def walk(o, p):
        if isinstance(o, str):
            out.append((p or "<root>", o))
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{p}.{k}" if p else str(k))
        elif isinstance(o, list):
            for i, item in enumerate(o):
                walk(item, f"{p}[{i}]")

    walk(obj, path)
    return out


# Paths excluded from the ASSERTIVE-PHRASE scan (not the key scan). These carry
# content that is NOT the AG's current-case authored conclusion, so a decisive
# phrase there is not the AG asserting:
#   * past_reference — recorded PAST adjudication facts (past decisions are facts,
#     not leakage — a recorded past decision is an organizational fact). The
#     guard must not shred the accumulation moat by tripping on a past verdict.
#   * verbatim_quote / citations — literal SOURCE evidence, not the AG's words.
_ASSERTION_SKIP_PATHS = ("past_reference", "verbatim_quote", "citations")


def assertive_violations(obj, skip_paths: tuple = _ASSERTION_SKIP_PATHS) -> list[dict]:
    """Return {path, phrase} for each AG-authored string leaf containing a decisive
    phrase (source evidence and recorded past facts are skipped by path)."""
    norm_phrases = [(_norm(p), p) for p in _ASSERTIVE_PHRASES]
    out: list[dict] = []
    for path, text in iter_string_leaves(obj):
        if any(s in path for s in skip_paths):
            continue
        n = _norm(text)
        for np, orig in norm_phrases:
            if np in n:
                out.append({"path": path, "phrase": orig})
    return out


def gate_scan(result: dict, config: dict) -> None:
    """Enforce the non-assertion boundary on `result` (no State). Raise
    SecurityViolationError on any prohibited output key (whole result — a
    current-case verdict key is structurally excluded) or any decisive-assertion
    phrase in an AG-authored field (past facts / source quotes are path-skipped)."""
    if result.get("status") == "error":
        # a terminal error already declared status=error; nothing to enforce.
        return
    prohibited = effective_prohibited_keys(config)
    bad_keys = find_prohibited_keys(result, prohibited)
    if bad_keys:
        raise SecurityViolationError(
            f"S-3 non-assertion boundary: prohibited output key(s) {sorted(set(bad_keys))}"
        )
    violations = assertive_violations(result)
    if violations:
        v = violations[0]
        raise SecurityViolationError(
            f"S-3 non-assertion boundary: decisive-assertion wording {v['phrase']!r} in {v['path']}"
        )
