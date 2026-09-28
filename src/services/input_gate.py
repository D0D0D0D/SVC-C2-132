"""Input validation gate (S-2 domain layer) — deterministic, fail-closed.

Implements docs/02_design.md §2-1 (mode-specific closed schemas), §(a) RBAC
(requested scope must be a subset of the caller's authorized scope), and the
layer-1 `payload_ref` split for a large read-only `event_config` export.

All functions are pure — they never mutate State and never raise; they return the
list of `{field, reason}` rejections. The caller sets `status=error` on any reject.
No binary/file channel: the body must be a JSON object (a non-decodable body is
rejected — the parser-less choice; extension / Content-Type self-report distrusted).
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

_MODES = ("invoke", "ingest", "feedback")
_VERDICTS = ("corrected", "accepted", "rejected")
_MANAGER_ACTIONS = ("corrected", "accepted", "rejected")

# Per-object key allowlists (unknown key at any level → reject).
_INVOKE_KEYS = {
    "mode", "event_config", "event_config_ref", "reward_table", "change_request",
    "notice_text", "tenant_id", "org_id", "context",
}
_INGEST_KEYS = {"mode", "records", "tenant_id", "org_id"}
_INGEST_RECORD_KEYS = {
    "notice_summary", "actual_rule_summary", "gap_note", "correction",
    "adjudication_rationale", "verdict", "category_key", "viewpoint_code",
    "source_of_truth_ref",
}
_INGEST_RECORD_REQUIRED = {
    "notice_summary", "actual_rule_summary", "gap_note", "correction",
    "adjudication_rationale", "verdict", "category_key", "viewpoint_code",
}
_FEEDBACK_KEYS = {
    "mode", "case_id", "manager_action", "final_verdict", "tenant_id", "org_id",
    "correction_note",
}


def parse_body(raw: Any) -> tuple[dict | None, list[dict]]:
    """Decode the request body to a JSON object (or already-decoded dict)."""
    if isinstance(raw, dict):
        return raw, []
    if not isinstance(raw, str) or not raw.strip():
        return None, [{"field": "body", "reason": "empty or non-string request body"}]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None, [{"field": "body", "reason": "body is not JSON-decodable (no binary/file channel)"}]
    if not isinstance(obj, dict):
        return None, [{"field": "body", "reason": "request body must be a JSON object"}]
    return obj, []


def canonical_mode(raw_mode: Any) -> str | None:
    """Canonicalise "/invoke" / "invoke" → "invoke". Unknown → None (caller rejects)."""
    if not isinstance(raw_mode, str):
        return None
    m = raw_mode.strip().lstrip("/").lower()
    return m if m in _MODES else None


# ── input-quality gate ───────────────────────────────────────────────────────

def _bad_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        if ch in ("\t", "\n", "\r"):
            continue
        if ch == "�" or unicodedata.category(ch) == "Cc":
            bad += 1
    return bad / len(text)


def _quality_reject(field: str, text: str, max_bad_ratio: float, max_chars: int) -> list[dict]:
    out: list[dict] = []
    if len(text) > max_chars:
        out.append({"field": field, "reason": f"free-text length {len(text)} > max_field_chars {max_chars}"})
    ratio = _bad_char_ratio(text)
    if ratio >= max_bad_ratio:
        out.append({"field": field, "reason": f"bad-char ratio {ratio:.3f} >= max_bad_char_ratio {max_bad_ratio}"})
    return out


def _limits(config: dict) -> tuple[float, int, int]:
    cfg = config or {}
    max_bad = float(cfg.get("max_bad_char_ratio", 0.10))
    max_chars = int(cfg.get("max_field_chars", 20000))
    max_bytes = int(cfg.get("max_input_bytes", 262144))
    return max_bad, max_chars, max_bytes


# ── RBAC (requested scope ⊆ authorized scope; fail-closed) ────────────────────

def authorize(tenant_id: Any, org_id: Any, authorized_scopes: Any,
              required_perm: str) -> tuple[bool, list[dict]]:
    """Return (ok, rejections). Both tenant_id and org_id are required; the
    requested (tenant, org) pair MUST be present in the caller's authorized scope
    list AND that scope must grant `required_perm` ("read" for invoke, "write" for
    ingest/feedback — docs/02 §(a) mode-specific permission).

    Fail-closed: an absent/empty authorized-scope list denies everything (an omitted
    scope must never widen to a full-table match); a scope with no explicit
    `permissions` defaults to read-only, so a write needs an EXPLICIT "write" grant.
    """
    rej: list[dict] = []
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        rej.append({"field": "tenant_id", "reason": "required non-empty string (RBAC primary scope)"})
    if not isinstance(org_id, str) or not org_id.strip():
        rej.append({"field": "org_id", "reason": "required non-empty string (RBAC secondary scope)"})
    if rej:
        return False, rej
    scopes = authorized_scopes if isinstance(authorized_scopes, list) else []
    matched = next(
        (s for s in scopes
         if isinstance(s, dict) and s.get("tenant_id") == tenant_id and s.get("org_id") == org_id),
        None,
    )
    if matched is None:
        return False, [{
            "field": "tenant_id/org_id",
            "reason": f"caller not authorized for scope ({tenant_id!r},{org_id!r})",
        }]
    perms = matched.get("permissions")
    perms = perms if isinstance(perms, list) else ["read"]  # default read-only (write must be explicit)
    if required_perm not in perms:
        return False, [{
            "field": "mode",
            "reason": f"scope ({tenant_id!r},{org_id!r}) lacks {required_perm!r} permission for this mode",
        }]
    return True, []


# ── mode-specific closed-schema validation ───────────────────────────────────

def _reject_unknown_keys(obj: dict, allowed: set[str], prefix: str = "") -> list[dict]:
    return [
        {"field": f"{prefix}{k}", "reason": "unknown key (closed schema)"}
        for k in obj
        if k not in allowed
    ]


def validate_invoke(obj: dict, config: dict) -> tuple[bool, list[dict], dict, str]:
    """Validate an /invoke body. Returns (ok, rejected, validated_input, payload_ref).

    Layer-1 split: `event_config` inline (<= max_input_bytes) stays in
    validated_input with payload_ref=""; a `> max_input_bytes` inline body is
    rejected (caller must pre-stage and pass `event_config_ref`); an
    `event_config_ref` is carried as payload_ref and NOT resolved here (the inner
    reconcile resolves it in local scope so the large export never enters State).
    """
    max_bad, max_chars, max_bytes = _limits(config)
    rejected: list[dict] = _reject_unknown_keys(obj, _INVOKE_KEYS)

    has_inline = "event_config" in obj
    has_ref = "event_config_ref" in obj
    payload_ref = ""
    if has_inline == has_ref:  # both or neither
        rejected.append({
            "field": "event_config", "reason":
            "exactly one of event_config (inline) or event_config_ref (handle) is required",
        })
    elif has_ref:
        ref = obj.get("event_config_ref")
        if not isinstance(ref, str) or not ref.strip():
            rejected.append({"field": "event_config_ref", "reason": "must be a non-empty string handle"})
        else:
            payload_ref = ref
    else:  # inline
        ec = obj.get("event_config")
        if not isinstance(ec, dict):
            rejected.append({"field": "event_config", "reason": "required object (inline)"})
        else:
            size = len(json.dumps(ec, ensure_ascii=False).encode("utf-8"))
            if size > max_bytes:
                rejected.append({
                    "field": "event_config",
                    "reason": f"inline event_config {size}B > max_input_bytes {max_bytes} — pass event_config_ref",
                })

    reward_table = obj.get("reward_table")
    if not isinstance(reward_table, dict):
        rejected.append({"field": "reward_table", "reason": "required object"})

    change_request = obj.get("change_request")
    if not isinstance(change_request, dict):
        rejected.append({"field": "change_request", "reason": "required object"})

    notice_text = obj.get("notice_text")
    if not isinstance(notice_text, str) or not notice_text.strip():
        rejected.append({"field": "notice_text", "reason": "required non-empty string"})
    elif isinstance(notice_text, str):
        rejected += _quality_reject("notice_text", notice_text, max_bad, max_chars)

    context = obj.get("context", {})
    if "context" in obj and not isinstance(context, dict):
        rejected.append({"field": "context", "reason": "must be an object when present"})
        context = {}

    # Size-bound the OTHER inline structured fields too — event_config has the
    # payload_ref path, but reward_table / change_request / context would otherwise
    # be an unbounded channel into (checkpointed) State (each capped to max_input_bytes).
    for fname, fval in (("reward_table", reward_table), ("change_request", change_request),
                        ("context", context)):
        if isinstance(fval, dict):
            fsize = len(json.dumps(fval, ensure_ascii=False).encode("utf-8"))
            if fsize > max_bytes:
                rejected.append({"field": fname, "reason": f"{fname} {fsize}B > max_input_bytes {max_bytes}"})

    validated_input: dict = {
        "mode": "invoke",
        "reward_table": reward_table if isinstance(reward_table, dict) else {},
        "change_request": change_request if isinstance(change_request, dict) else {},
        "notice_text": notice_text if isinstance(notice_text, str) else "",
        "tenant_id": obj.get("tenant_id", ""),
        "org_id": obj.get("org_id", ""),
        "context": context if isinstance(context, dict) else {},
    }
    # Only carry the inline event_config into State when it is small; a ref keeps
    # the large export OUT of State (layer-1 checkpoint isolation).
    if not payload_ref and isinstance(obj.get("event_config"), dict):
        validated_input["event_config"] = obj["event_config"]

    return (len(rejected) == 0), rejected, validated_input, payload_ref


def validate_ingest(obj: dict, config: dict) -> tuple[bool, list[dict], dict]:
    """Validate an /ingest body (past-adjudication records to accumulate)."""
    max_bad, max_chars, _ = _limits(config)
    rejected: list[dict] = _reject_unknown_keys(obj, _INGEST_KEYS)

    records = obj.get("records")
    clean_records: list[dict] = []
    if not isinstance(records, list) or len(records) < 1:
        rejected.append({"field": "records", "reason": "required list with >=1 element"})
    else:
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                rejected.append({"field": f"records[{i}]", "reason": "must be an object"})
                continue
            rejected += _reject_unknown_keys(rec, _INGEST_RECORD_KEYS, prefix=f"records[{i}].")
            for k in _INGEST_RECORD_REQUIRED:
                v = rec.get(k)
                if not isinstance(v, str) or not v.strip():
                    rejected.append({"field": f"records[{i}].{k}", "reason": "required non-empty string"})
                elif k in ("notice_summary", "actual_rule_summary", "gap_note", "correction",
                           "adjudication_rationale"):
                    rejected += _quality_reject(f"records[{i}].{k}", v, max_bad, max_chars)
            verdict = rec.get("verdict")
            if verdict not in _VERDICTS:
                rejected.append({"field": f"records[{i}].verdict", "reason": f"must be one of {_VERDICTS}"})
            clean_records.append(rec)

    validated_input = {
        "mode": "ingest",
        "records": clean_records,
        "tenant_id": obj.get("tenant_id", ""),
        "org_id": obj.get("org_id", ""),
    }
    return (len(rejected) == 0), rejected, validated_input


def validate_feedback(obj: dict, config: dict) -> tuple[bool, list[dict], dict]:
    """Validate a /feedback body (how a manager acted on a prior /invoke case)."""
    max_bad, max_chars, _ = _limits(config)
    rejected: list[dict] = _reject_unknown_keys(obj, _FEEDBACK_KEYS)

    case_id = obj.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        rejected.append({"field": "case_id", "reason": "required non-empty string"})

    action = obj.get("manager_action")
    if action not in _MANAGER_ACTIONS:
        rejected.append({"field": "manager_action", "reason": f"must be one of {_MANAGER_ACTIONS}"})

    final_verdict = obj.get("final_verdict")
    if final_verdict not in _VERDICTS:
        rejected.append({"field": "final_verdict", "reason": f"must be one of {_VERDICTS}"})

    note = obj.get("correction_note", "")
    if "correction_note" in obj:
        if not isinstance(note, str):
            rejected.append({"field": "correction_note", "reason": "must be a string"})
            note = ""
        elif note:
            rejected += _quality_reject("correction_note", note, max_bad, max_chars)

    validated_input = {
        "mode": "feedback",
        "case_id": case_id if isinstance(case_id, str) else "",
        "manager_action": action if action in _MANAGER_ACTIONS else "",
        "final_verdict": final_verdict if final_verdict in _VERDICTS else "",
        "correction_note": note if isinstance(note, str) else "",
        "tenant_id": obj.get("tenant_id", ""),
        "org_id": obj.get("org_id", ""),
    }
    return (len(rejected) == 0), rejected, validated_input
