"""S-3 Provenance — citation existence + version/span binding + verbatim +
bidirectional numeric fact coverage (docs/02_design.md §(b)).

`source_registry` is produced ONLY by the deterministic reconcile step (non-LLM,
non-caller-controlled): `source_id -> {kind, version, span, text, facts}`. A gap
finding may only cite an entry that exists there, at the matching version, inside
the recorded span, with a verbatim quote that is a literal substring of the cited
span — and every number the finding asserts in prose must trace back to an
`asserted_facts` value that itself matches the source (both directions).

Runs in `post_process.execute()` (it needs the State `source_registry`); the node
raises `SecurityViolationError` when `terminal` is not None.
"""

from __future__ import annotations

from .numerics import extract_prose_numbers, numeric_tokens_of

# kind is set by the deterministic reconcile; a registry entry with an unknown
# kind is itself a defect (never let an unknown kind claim to be "non-empty").
_ALLOWED_KINDS = ("event_config", "reward_table", "notice_text", "change_request")

_FREE_TEXT_FIELDS = ("review_question", "beneficiary_impact", "uncertainty_text")


def _facts_match(asserted: dict, source_facts: dict) -> str | None:
    """claims→source: every asserted (k, v) must equal the source fact k.

    Numeric values compare by canonical numeric token so `1` / `"1"` / `"1回"`
    agree; non-numeric values compare by exact equality."""
    for k, v in asserted.items():
        if k not in source_facts:
            return f"asserted fact {k!r} not present in source facts"
        sv = source_facts[k]
        v_nums = numeric_tokens_of(v)
        s_nums = numeric_tokens_of(sv)
        if v_nums or s_nums:
            if v_nums != s_nums:
                return f"asserted fact {k!r}={v!r} does not match source {sv!r}"
        elif v != sv:
            return f"asserted fact {k!r}={v!r} does not match source {sv!r}"
    return None


def _check_citation(cit, source_registry: dict, min_quote_len: int) -> tuple[str | None, set[str]]:
    """Return (terminal_reason_or_None, asserted_numeric_tokens)."""
    if not isinstance(cit, dict):
        return "citation is not an object", set()
    sid = cit.get("source_id")
    if not isinstance(sid, str) or not sid.strip():
        return "citation.source_id must be a non-empty string", set()
    entry = source_registry.get(sid)
    if not isinstance(entry, dict):
        # covers hallucinated ids AND past-adjudication history_ref (never in registry).
        return f"citation.source_id {sid!r} not in source_registry", set()
    if entry.get("kind") not in _ALLOWED_KINDS:
        return f"citation {sid!r}: source kind {entry.get('kind')!r} not in allowlist", set()

    # version binding
    if cit.get("version") != entry.get("version"):
        return (f"citation {sid!r}: version {cit.get('version')!r} != source version "
                f"{entry.get('version')!r}"), set()

    # span binding — citation span must lie inside the source's recorded span
    span = cit.get("span")
    reg_span = entry.get("span") or [0, len(entry.get("text", ""))]
    if not (isinstance(span, (list, tuple)) and len(span) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in span)):
        return f"citation {sid!r}: span must be [start, end] ints", set()
    s, e = span
    rs, re_ = reg_span[0], reg_span[1]
    if not (rs <= s <= e <= re_):
        return f"citation {sid!r}: span {span} outside source span {list(reg_span)}", set()

    quote = cit.get("verbatim_quote")
    if not isinstance(quote, str):
        return f"citation {sid!r}: verbatim_quote must be a string", set()
    if len(quote) < min_quote_len:
        return f"citation {sid!r}: verbatim_quote shorter than min_quote_len {min_quote_len}", set()
    source_text = entry.get("text", "") or ""
    # the quote must appear inside the cited span (ties span + verbatim together)
    if quote not in source_text[s:e]:
        return f"citation {sid!r}: verbatim_quote is not a substring of the cited span", set()

    asserted = cit.get("asserted_facts", {})
    if not isinstance(asserted, dict):
        return f"citation {sid!r}: asserted_facts must be an object", set()
    reason = _facts_match(asserted, entry.get("facts", {}) or {})
    if reason:
        return f"citation {sid!r}: {reason}", set()

    asserted_nums: set[str] = set()
    for v in asserted.values():
        asserted_nums |= numeric_tokens_of(v)
    return None, asserted_nums


def verify_findings(gap_findings, source_registry: dict, min_quote_len: int) -> dict:
    """Verify every finding's citations. Returns {"terminal": reason|None,
    "findings": [...]}. On a non-None terminal the caller MUST raise
    SecurityViolationError and surface NO findings."""
    if not isinstance(source_registry, dict):
        return {"terminal": "source_registry missing or malformed", "findings": []}
    verified: list[dict] = []
    for finding in (gap_findings or []):
        if not isinstance(finding, dict):
            return {"terminal": "gap finding is not an object", "findings": []}
        # Structural type check: the projected fields MUST be strings. A non-string
        # (e.g. a nested dict in gap_id) would otherwise be projected verbatim and
        # could smuggle a current-case verdict past the S-3 key/phrase scan.
        gid = finding.get("gap_id")
        if not isinstance(gid, str) or not gid.strip():
            return {"terminal": "finding gap_id must be a non-empty string", "findings": []}
        for field in _FREE_TEXT_FIELDS:
            if field in finding and not isinstance(finding[field], str):
                return {"terminal": f"finding {gid!r}: {field} must be a string", "findings": []}
        cites = finding.get("citations")
        if not isinstance(cites, list) or not cites:
            return {"terminal": f"finding {finding.get('gap_id')!r} has no citations", "findings": []}

        asserted_nums: set[str] = set()
        for cit in cites:
            reason, nums = _check_citation(cit, source_registry, min_quote_len)
            if reason:
                return {"terminal": reason, "findings": []}
            asserted_nums |= nums

        # bidirectional coverage: every number stated in the finding's prose must
        # be backed by a verified asserted_fact (source→claims), so a claim cannot
        # smuggle an unbacked number into free text.
        prose_nums: set[str] = set()
        for field in _FREE_TEXT_FIELDS:
            prose_nums |= extract_prose_numbers(finding.get(field, ""))
        unbacked = prose_nums - asserted_nums
        if unbacked:
            return {"terminal": (f"finding {finding.get('gap_id')!r}: numbers {sorted(unbacked)} in "
                                 f"prose not backed by any asserted_fact"), "findings": []}

        verified.append(finding)
    return {"terminal": None, "findings": verified}
