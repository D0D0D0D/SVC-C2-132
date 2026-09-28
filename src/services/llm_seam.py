"""LLM injection seam — no model IDs, no bespoke client.

The template holds NO concrete LLM client — the framework already ships the
provider clients, and pinning a model id here would outlive the deployment that
chose it. The runtime (STG / platform / test)
injects a `BaseLLM` implementation via `set_llm()`; nodes call `get_llm()` and
degrade to a deterministic fallback when it is `None`.

Nodes must be no-arg constructible (PB-6 discovers and instantiates every node
under `src/nodes/` with `NodeClass()`), so the client cannot be a constructor
argument — it is a module-level seam.
"""

from __future__ import annotations

from typing import Optional

from shared.services.llm.base_llm import BaseLLM

_LLM: Optional[BaseLLM] = None


def set_llm(llm: Optional[BaseLLM]) -> None:
    """Inject the LLM client (called by the server at startup or by tests)."""
    global _LLM
    _LLM = llm


def get_llm() -> Optional[BaseLLM]:
    """Return the injected LLM client, or None (nodes then run deterministically)."""
    return _LLM
