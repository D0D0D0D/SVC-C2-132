"""Read-only event-config export store seam (layer-1 payload_ref resolution).

A large read-only `event_config` export is passed by the caller as an opaque
`event_config_ref` handle rather than inline, so it is never checkpointed into
State (docs/02_design.md §2-1 — the two-layer data-governance split).
The runtime injects the real export store (S3 / object storage) via `set_export_store`;
absent one, an in-process reference store is used (tests / degradation).

Scope binding is mandatory and fail-closed: `resolve(ref, tenant_id, org_id)` returns
`None` unless the handle was staged for exactly that `(tenant_id, org_id)` — a caller
cannot dereference another tenant's export by guessing a handle.
"""

from __future__ import annotations

from typing import Optional


class ExportStore:
    """Reference in-process export store (tenant/org scoped, read-only)."""

    def __init__(self) -> None:
        # (tenant_id, org_id, ref) -> event_config dict
        self._exports: dict[tuple[str, str, str], dict] = {}

    def stage(self, ref: str, tenant_id: str, org_id: str, event_config: dict) -> None:
        """Pre-stage a read-only export under a scoped handle (test / gateway helper)."""
        self._exports[(tenant_id, org_id, ref)] = event_config

    def resolve(self, ref: str, tenant_id: str, org_id: str) -> Optional[dict]:
        """Return the staged export for this scope, or None (fail-closed) — never
        falls back to another tenant/org."""
        if not (isinstance(ref, str) and ref.strip()):
            return None
        return self._exports.get((tenant_id, org_id, ref))


_STORE: Optional[ExportStore] = None


def set_export_store(store: Optional[ExportStore]) -> None:
    """Inject the export store (runtime / tests). None resets to a fresh reference store."""
    global _STORE
    _STORE = store


def get_export_store() -> ExportStore:
    """Return the injected export store, or a lazily-created reference store."""
    global _STORE
    if _STORE is None:
        _STORE = ExportStore()
    return _STORE
