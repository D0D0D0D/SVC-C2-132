"""Write-path sanitiser (layer-2, docs/02_design.md §(a) / three-value split
the KB free-text three-value split).

Free text written to the KB is later recalled INTO the gap_judgment LLM prompt, so
it is both a PII surface AND a prompt-injection persistence vector. Every free-text
field on a write (ingest / feedback) is:

1. NFKC-normalised and length-capped,
2. rejected (fail-closed — write NOTHING) if it carries an instruction-override
   pattern ("ignore previous instructions" / "以後の指示は無視" / "システムプロンプト" …),
3. PII-masked via the framework's `detect_pii` (reuse the shipped detector; no
   hand-rolled Japanese-name dictionary).

"Sanitise failed → keep the raw text" is explicitly NOT allowed: a failure means
the record is not written at all.
"""

from __future__ import annotations

import unicodedata

from shared.security.pii_detector import detect_pii

# Instruction-override / prompt-injection markers (NFKC+casefold compared).
_INJECTION_PATTERNS = (
    "ignore previous", "ignore all previous", "ignore the above", "disregard previous",
    "system prompt", "you are now", "new instructions", "act as",
    "以後の指示", "以降の指示", "前の指示を無視", "上記を無視", "指示を無視",
    "システムプロンプト", "命令を無視", "これまでの指示",
)


def _mask_pii(text: str) -> str:
    findings = detect_pii(text)
    if not findings:
        return text
    masked = text
    for f in reversed(findings):  # reverse to preserve offsets
        masked = masked[: f["start"]] + "[MASKED]" + masked[f["end"]:]
    return masked


def sanitize_free_text(field: str, text: str, config: dict) -> tuple[bool, str, str]:
    """Return (ok, reason, safe_text). ok=False → reject the whole write."""
    if not isinstance(text, str):
        return False, f"{field}: not a string", ""
    max_chars = int((config or {}).get("max_field_chars", 20000))
    norm = unicodedata.normalize("NFKC", text)[:max_chars]
    low = norm.casefold()
    for pat in _INJECTION_PATTERNS:
        if unicodedata.normalize("NFKC", pat).casefold() in low:
            return False, f"{field}: instruction-override pattern detected ({pat!r})", ""
    return True, "", _mask_pii(norm)


def sanitize_fields(record: dict, fields: tuple[str, ...], config: dict) -> tuple[bool, str, dict]:
    """Sanitise the named free-text fields of `record`. Any rejection fails the
    whole record (fail-closed). Returns (ok, reason, sanitised_record_copy)."""
    out = dict(record)
    for f in fields:
        if f not in out:
            continue
        ok, reason, safe = sanitize_free_text(f, out.get(f, ""), config)
        if not ok:
            return False, reason, {}
        out[f] = safe
    return True, "", out
