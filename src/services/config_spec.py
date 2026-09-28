"""Runtime config validation (fail-closed at graph compile).

Every accepted key has a type/range and a hardcoded default read at use-site.
`validate_config` raises `ConfigError` for any out-of-range / wrong-type value;
unknown keys are ignored (docs/02_design.md §Config contract). The config layout
is the FLAT key set committed in `config/config.yaml`.

The retention contract (docs/02_design.md §(a)(i)) is enforced here: an invalid
`kb_raw_retention_days` (<=0 or non-int) is a config error — the agent MUST NOT
silently fall back to "keep raw text forever".
"""

from __future__ import annotations

try:  # framework ConfigError if available, else a local fallback
    from framework.errors import ConfigError  # type: ignore
except Exception:  # pragma: no cover - environment-dependent
    class ConfigError(Exception):  # type: ignore
        pass


def _fail(msg: str) -> None:
    raise ConfigError(f"SVC-C2-132 config invalid: {msg}")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _require_mapping(cfg: dict, key: str) -> dict:
    """A present section MUST be a mapping (fail-closed — a list/scalar section is a
    config error, not silently coerced to {} which would skip its range checks)."""
    if key not in cfg:
        return {}
    val = cfg[key]
    if not isinstance(val, dict):
        _fail(f"{key} must be a mapping, got {type(val).__name__}")
    return val


def _pos_int(cfg: dict, key: str, lo: int = 1, hi: int | None = None) -> None:
    """A present key must be an int in [lo, hi] (hi=None means unbounded above)."""
    if key not in cfg:
        return
    v = cfg[key]
    if not (_is_int(v) and v >= lo and (hi is None or v <= hi)):
        rng = f"[{lo}, {hi}]" if hi is not None else f">= {lo}"
        _fail(f"{key} must be an int {rng}")


def _num_in(cfg: dict, key: str, lo: float, hi: float) -> None:
    """A present key must be a number in [lo, hi]."""
    if key in cfg and not (_is_num(cfg[key]) and lo <= cfg[key] <= hi):
        _fail(f"{key} must be a number in [{lo}, {hi}]")


def validate_config(config: dict) -> None:
    """Validate the runtime config; raise ConfigError on any out-of-range value."""
    cfg = config or {}

    # ── backbone runtime ─────────────────────────────────────────────────────
    _pos_int(cfg, "max_retry", lo=0, hi=3)   # framework backbone ceiling
    _pos_int(cfg, "timeout_s")
    if "memory_enabled" in cfg and not isinstance(cfg["memory_enabled"], bool):
        _fail("memory_enabled must be a bool")
    hitl = _require_mapping(cfg, "hitl")
    if "enabled" in hitl and not isinstance(hitl["enabled"], bool):
        _fail("hitl.enabled must be a bool")

    # ── LLM (flat keys — endpoint/model are runtime params) ──────────────────
    if "llm_model" in cfg and not (isinstance(cfg["llm_model"], str) and cfg["llm_model"].strip()):
        _fail("llm_model must be a non-empty string")
    _num_in(cfg, "temperature", 0.0, 1.0)
    _pos_int(cfg, "max_tokens")
    if "budget_usd" in cfg and cfg["budget_usd"] is not None \
            and not (_is_num(cfg["budget_usd"]) and cfg["budget_usd"] >= 0):
        _fail("budget_usd must be null or a number >= 0")

    # ── provenance / gap review / KB recall ──────────────────────────────────
    _pos_int(cfg, "provenance_min_quote_len")
    _pos_int(cfg, "kb_recall_top_k")

    # ── KB raw-text retention (executable contract §(a)(i)) ──────────────────
    # <=0 or non-int is rejected — no silent "keep raw text forever" (fail-closed).
    if "kb_raw_retention_days" in cfg and not (_is_int(cfg["kb_raw_retention_days"])
                                               and cfg["kb_raw_retention_days"] >= 1):
        _fail("kb_raw_retention_days must be a positive integer (days); "
              "<=0 or non-int would silently keep raw text forever (fail-closed)")

    # ── input allowlist + quality gate ───────────────────────────────────────
    _pos_int(cfg, "max_input_bytes")
    _pos_int(cfg, "max_field_chars")
    _num_in(cfg, "max_bad_char_ratio", 0.0, 1.0)
