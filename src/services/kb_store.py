"""Persistent adjudication KB — layer-2 governance (docs/02_design.md §(a)).

This is the accumulation moat and the sensitive-data surface that `payload_ref`
(layer 1) does NOT cover — see the two-layer data-governance split.
The store enforces, independently of the graph:

* RBAC scoping — every read/write is bound to `(tenant_id, org_id)`; a record
  written under one scope is invisible to any other (no cross-tenant fallback).
* Search-axis separation — semantic recall matches on `retrieval_text`
  (category_key + consultation summary + viewpoint_code) ONLY; `verdict` /
  `adjudication_rationale` are surfaced AFTER top-k so ranking is never dragged by
  the conclusion vocabulary (docs/02_design.md §(a) "検索の軸分離").
* Retention — raw-text and the raw-derived `retrieval_text`/index are removed
  after `kb_raw_retention_days`, enforced by BOTH a physical redaction job and a
  read-time barrier (expired raw is never returned even before the job sweeps).
  `structured-retained` fields (verdict / adjudication_rationale / category_key /
  viewpoint_code) are kept indefinitely.

The store never reads the wall clock: the caller passes the request's fixed
`now_iso` (adapter-stamped, TZ-aware; the date basis is fixed once at the adapter).
ISO timestamps are PARSED for comparison, never compared lexically
(lexicographic comparison of ISO strings inverts across UTC offsets).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

# Raw-text fields — deleted on expiry (retention).
RAW_TEXT_FIELDS = ("notice_summary", "actual_rule_summary", "gap_note", "correction")
# Structured-retained — kept indefinitely (the moat).
STRUCTURED_FIELDS = ("verdict", "adjudication_rationale", "category_key", "viewpoint_code")


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO8601 timestamp (TZ-aware) to a datetime, or None."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_retrieval_text(category_key: str, notice_summary: str, viewpoint_code: str) -> str:
    """Index text = classification + consultation summary + viewpoint (raw-derived)."""
    return " ".join(p for p in (category_key, notice_summary, viewpoint_code) if p).strip()


def _derive_retrieval_text_structured_only(category_key: str, viewpoint_code: str) -> str:
    """Post-expiry re-derivation: structured-retained only (no raw-derived content)."""
    return " ".join(p for p in (category_key, viewpoint_code) if p).strip()


def is_expired(created_at: str, now_iso: str, retention_days: int) -> bool:
    """True when `created_at` is older than `retention_days` relative to `now_iso`.

    Unparseable / missing timestamps are treated as expired (fail-closed — an
    undatable record must not retain raw text forever)."""
    created = _parse_iso(created_at)
    now = _parse_iso(now_iso)
    if created is None or now is None:
        return True
    return (now - created).total_seconds() > retention_days * 86400


class KbStore:
    """In-process reference KB store (tenant/org scoped, retention-aware).

    A real deployment injects a durable store (pgvector / Dynamo) with the same
    contract via `set_kb_store`; this reference impl backs tests and degradation.
    """

    def __init__(self) -> None:
        # (tenant_id, org_id) -> list[record dict]
        self._records: dict[tuple[str, str], list[dict]] = {}
        self._seq = 0

    # ── write (ingest / feedback) ────────────────────────────────────────────
    def write(self, tenant_id: str, org_id: str, records: list[dict]) -> dict:
        """Persist already-sanitised records. Assigns record_ids. Fail-closed
        scope binding: records are stored ONLY under (tenant_id, org_id)."""
        bucket = self._records.setdefault((tenant_id, org_id), [])
        written_ids: list[str] = []
        for rec in records:
            self._seq += 1
            rid = f"{tenant_id}:{org_id}:{self._seq}"
            stored = dict(rec)
            stored["record_id"] = rid
            stored["tenant_id"] = tenant_id
            stored["org_id"] = org_id
            bucket.append(stored)
            written_ids.append(rid)
        return {"written_count": len(written_ids), "record_ids": written_ids}

    # ── recall (invoke — read-time barrier applied) ──────────────────────────
    def recall(self, tenant_id: str, org_id: str, query_text: str, top_k: int,
               now_iso: str, retention_days: int) -> list[dict]:
        """Return up to top_k past adjudications for THIS scope, ranked by overlap
        on `retrieval_text` only. Expired records are excluded (read-time barrier)
        even if the redaction job has not run yet."""
        bucket = self._records.get((tenant_id, org_id), [])
        q_tokens = set(_tokenize(query_text))
        scored: list[tuple[int, str, dict]] = []
        for rec in bucket:
            if is_expired(rec.get("created_at", ""), now_iso, retention_days):
                continue  # read-time barrier: no raw-derived content for expired records
            r_tokens = set(_tokenize(rec.get("retrieval_text", "")))
            score = len(q_tokens & r_tokens)
            if score <= 0:
                continue
            scored.append((score, rec.get("record_id", ""), rec))
        # rank by score desc, tie-break by record_id asc (reproducible)
        scored.sort(key=lambda t: (-t[0], t[1]))
        out: list[dict] = []
        for _score, _rid, rec in scored[: max(0, int(top_k))]:
            out.append(self._safe_recall_item(rec))
        return out

    @staticmethod
    def _safe_recall_item(rec: dict) -> dict:
        """Model-visible reference item: structured-retained + sanitised summary.

        `history_ref` is a namespace DISJOINT from `source_id` so a recalled case
        can never be cited as provenance for the current case (docs/02 §(b))."""
        # Keys are namespaced `past_*` so a recorded PAST verdict is never confused
        # with a (prohibited) current-case `verdict` output key (docs/02 §(c) floor).
        return {
            "history_ref": f"hist:{rec.get('record_id', '')}",
            "category_key": rec.get("category_key", ""),
            "viewpoint_code": rec.get("viewpoint_code", ""),
            "past_verdict": rec.get("verdict", ""),
            "past_rationale": rec.get("adjudication_rationale", ""),
            "notice_summary": rec.get("notice_summary", ""),
        }

    # ── direct lookup (feedback case resolution) ─────────────────────────────
    def get_record(self, tenant_id: str, org_id: str, record_id: str,
                   now_iso: str, retention_days: int) -> Optional[dict]:
        """Return a record by id within scope, ALWAYS with the read-time barrier
        applied — `now_iso`/`retention_days` are required so there is no unbarriered
        read path that could surface expired raw text before the redaction job runs."""
        for rec in self._records.get((tenant_id, org_id), []):
            if rec.get("record_id") == record_id:
                if is_expired(rec.get("created_at", ""), now_iso, retention_days):
                    view = dict(rec)
                    for f in RAW_TEXT_FIELDS:
                        view[f] = ""
                    view["retrieval_text"] = _derive_retrieval_text_structured_only(
                        rec.get("category_key", ""), rec.get("viewpoint_code", ""))
                    return view
                return dict(rec)
        return None

    def case_exists(self, tenant_id: str, org_id: str, case_id: str) -> bool:
        """True if `case_id` resolves to a record in THIS scope (feedback RBAC)."""
        return any(r.get("record_id") == case_id for r in self._records.get((tenant_id, org_id), []))

    # ── retention job (physical redaction) ───────────────────────────────────
    def run_retention(self, now_iso: str, retention_days: int) -> dict:
        """Physically redact raw-text + raw-derived index of every expired record
        (across all scopes). Returns a report the caller emits as S-4 events.
        `structured-retained` fields are preserved."""
        redacted: list[dict] = []
        for (tenant_id, org_id), bucket in self._records.items():
            for rec in bucket:
                if rec.get("redacted"):
                    continue
                if not is_expired(rec.get("created_at", ""), now_iso, retention_days):
                    continue
                removed = [f for f in RAW_TEXT_FIELDS if rec.get(f)]
                for f in RAW_TEXT_FIELDS:
                    rec[f] = ""
                # re-derive index from structured-retained only (drop raw-derived content)
                rec["retrieval_text"] = _derive_retrieval_text_structured_only(
                    rec.get("category_key", ""), rec.get("viewpoint_code", ""))
                rec["redacted"] = True
                redacted.append({
                    "record_id": rec.get("record_id", ""),
                    "tenant_id": tenant_id,
                    "org_id": org_id,
                    "fields": removed + ["retrieval_text"],
                    "now_iso": now_iso,
                })
        return {"redacted": redacted, "redacted_count": len(redacted)}


def _tokenize(text: str) -> list[str]:
    import re
    return [t for t in re.split(r"\W+", (text or "").lower()) if t]


_STORE: Optional[KbStore] = None


def set_kb_store(store: Optional[KbStore]) -> None:
    """Inject the KB store (runtime / tests). None resets to a fresh reference store."""
    global _STORE
    _STORE = store


def get_kb_store() -> KbStore:
    """Return the injected KB store, or a lazily-created reference store."""
    global _STORE
    if _STORE is None:
        _STORE = KbStore()
    return _STORE
