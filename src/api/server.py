"""AgentCore Platform v1.0 — SVC-C2-132 standalone HTTP entry point.

Adapter only — no business logic. Loads the runtime config (`config/config.yaml`)
and passes it to the graph so the manifest's runtime knobs actually reach the nodes
(a server that ignores config is a defect class CI never catches, because
run-tests only runs pytest and never boots this module). No LLM client / model id
is constructed here (the
platform injects a `BaseLLM` via `src.services.llm_seam.set_llm`; absent one the
graph degrades deterministically).

Request body: `input` is a JSON string carrying `mode` (invoke|ingest|feedback) plus
that mode's payload and `tenant_id`/`org_id`. The trusted entry point conveys the
caller's VERIFIED authorization via `input_context` — NOT the body:

* `authorized_scopes` — the caller's VERIFIED authorization scopes, resolved by
  `_trusted_scopes()` from a channel the client CANNOT forge, fail-closed (B-6 RBAC
  trust boundary — enforced here, not left to deployment documentation; it was a
  review finding). Two trusted sources, in priority:
    1. `request.state.authorized_scopes`, injected in-process by the platform
       AuthMiddleware (same unforgeable channel as `request.state.trust_level` /
       `caller_id`; a remote HTTP client cannot set `request.state`).
    2. A gateway-signed `X-Authorized-Scopes` header, HMAC-SHA256-verified against the
       `X-Authorized-Scopes-Signature` header under the shared `SCOPE_SIGNING_KEY`
       (forwarded-claims INTEGRITY — the gateway still owns JWT verification + scope
       derivation; we only verify the channel, not the JWT, so the platform trust model
       is not forked). `SCOPE_SIGNING_KEY` is a deployment-level channel credential read
       from the environment BEFORE any InvocationContext exists (the entry-point
       exception, same class as `INVOKE_AUTH_TOKEN`); it is not an agent secret and never
       enters `requires.secrets` or state.
  A raw, UNSIGNED `X-Authorized-Scopes` header from an unauthenticated client is NEVER
  trusted → `[]` ⇒ RBAC (pre_process) then denies everything. When no trusted source
  provides scopes (a standalone deployment with no gateway signature and no middleware),
  the request fails closed — no tenant/org is authorized.
* `request_ts` — a single adapter-stamped TZ-aware timestamp that fixes the retention
  date basis for the whole request, fixed once at the adapter and TZ-aware.
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import PlayerPromiseImpactReviewGraph

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _parse_authorized_scopes(header: str) -> list:
    """Parse the gateway-verified X-Authorized-Scopes header (JSON list). Malformed
    → [] (fail-closed: no scopes ⇒ RBAC denies everything)."""
    if not header:
        return []
    try:
        val = json.loads(header)
    except (ValueError, TypeError):
        return []
    return val if isinstance(val, list) else []


def _hmac_scope_channel(header: str, signature: str, signing_key: str | None) -> tuple[bool, list]:
    """Return `(signature_verified, scopes)` for the forwarded-claims channel.

    🔴 なぜ tri-state が要るか（敵対的レビュー High）: ゲートウェイは
    「この呼び出し元に scope は無い」を **署名付きの空リスト**で伝えられる。
    検証結果を list 1本で返すと、その明示的な deny が「検証失敗」と同じ `[]` に
    潰れる。STG フォールバックを足した今、その取り違えは *deny を既定 scope への
    昇格に変える* ので、チャネルが検証できたか否かを別に返す。

    Any failure — no key configured, no/empty signature, signature mismatch — →
    `(False, [])` so RBAC then denies everything (fail-closed). Malformed JSON under a
    VALID signature stays `(True, [])`: the channel was authentic, its content just
    carried no scope. This verifies the forwarded-claims CHANNEL, not the JWT: the
    gateway still owns JWT verification + scope derivation.
    """
    if not signing_key or not header or not signature:
        return False, []
    expected = hmac.new(signing_key.encode(), header.encode(), hashlib.sha256).hexdigest()
    # Compare as BYTES, constant-time. `signature` is a raw client header (Latin-1
    # decoded by the ASGI layer) that may contain non-ASCII chars; the str form of
    # compare_digest would raise TypeError on those, breaking the "any failure → []"
    # contract (a malformed signature would 500 instead of failing closed). Byte form
    # accepts any input and treats a non-matching/garbage signature as a mismatch.
    if not secrets.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
        return False, []
    return True, _parse_authorized_scopes(header)


def _hmac_verified_scopes(header: str, signature: str, signing_key: str | None) -> list:
    """Verified scope list, or [] on any verification failure (fail-closed).

    Thin wrapper over `_hmac_scope_channel` kept for callers/tests that only need the
    list. New code that must distinguish "verified deny" from "no verified channel"
    calls `_hmac_scope_channel` directly.
    """
    return _hmac_scope_channel(header, signature, signing_key)[1]


# ── STG provisional scope (staging evidence path only) ─────────────────────────
# 🔴 なぜ要るか: `deploy-stg` の provisional mode は AuthMiddleware を配線せずに
#    `uvicorn src.api.server:app` を起動し、`scripts/stg_invoke_evidence.py` は
#    `Authorization: Bearer` **しか**送らない。B-6 対応後は生ヘッダを信頼しないので
#    `_trusted_scopes()` は必ず [] を返し、RBAC が全部拒否して evidence が構造的に FAIL する。
#    非HITL の fail-closed エージェントに対する既存の前例（Shooting Plan Risk Advisory & Incident History Agent 型）。
#    HITL AG は正規パケットでも `awaiting_human` になるのでこの経路を持たない。
#
# 🔴 env の危険を env で守る循環を避けるため二重ゲートにする（Professional-Services Engagement Estimation-Phase Agent と同形）:
#    1. `STG_PROVISIONAL_MODE=true` の宣言が無ければ値は inert
#    2. startup で相互排他を強制 — 宣言が無いのに STG_DEFAULT_* を持つプロセスは起動拒否。
#       これが無いと、本番に INVOKE_AUTH_TOKEN と STG_DEFAULT_* が同居した瞬間に
#       「scope を持たない bearer 呼び出しが既定 scope の権限を得る」認可バイパスになる。
_STG_PROVISIONAL_ENV = "STG_PROVISIONAL_MODE"
_STG_DEFAULT_TENANT_ENV = "STG_DEFAULT_TENANT_ID"
_STG_DEFAULT_ORG_ENV = "STG_DEFAULT_ORG_ID"
_STG_DEFAULT_PERMS_ENV = "STG_DEFAULT_PERMISSIONS"
_STG_ENVS = (_STG_DEFAULT_TENANT_ENV, _STG_DEFAULT_ORG_ENV, _STG_DEFAULT_PERMS_ENV)


def _provisional_stg_mode() -> bool:
    return os.environ.get(_STG_PROVISIONAL_ENV, "").strip().casefold() == "true"


def assert_scope_env_is_sane() -> None:
    """Refuse to start a non-provisional deployment that carries STG scope env.

    The STG default scope is a provisional-deployment-only path. A production process
    that happens to define both INVOKE_AUTH_TOKEN and STG_DEFAULT_* would otherwise let
    any bearer caller act under the default scope without a verified claim.
    """
    if _provisional_stg_mode():
        return
    present = [n for n in _STG_ENVS if os.environ.get(n, "").strip()]
    if present:
        raise RuntimeError(
            f"{'/'.join(present)} is set without {_STG_PROVISIONAL_ENV}=true. "
            "The STG default scope is a provisional-deployment-only path and must "
            "never be reachable in production."
        )


def _stg_default_scopes() -> list:
    """STG default scope list, or [] when this deployment did not declare itself provisional.

    Partial configuration is NOT rescued — every key must be present, otherwise the caller
    stays scope-less and RBAC denies fail-closed.
    """
    if not _provisional_stg_mode():
        return []
    tenant = os.environ.get(_STG_DEFAULT_TENANT_ENV, "").strip()
    org = os.environ.get(_STG_DEFAULT_ORG_ENV, "").strip()
    raw_perms = os.environ.get(_STG_DEFAULT_PERMS_ENV, "").strip()
    perms = [p.strip() for p in raw_perms.split(",") if p.strip()]
    if not tenant or not org or not perms:
        return []
    return [{"tenant_id": tenant, "org_id": org, "permissions": perms}]


def _trusted_scopes(request: Request, token_authenticated: bool = False) -> list:
    """Resolve the caller's VERIFIED authorized scopes from a channel the client cannot
    forge, fail-closed (B-6 RBAC trust boundary — review finding). Priority:

    1. `request.state.authorized_scopes` — injected in-process by the platform
       AuthMiddleware (same unforgeable channel as `request.state.trust_level` /
       `caller_id`; a remote HTTP client cannot set `request.state`).
    2. A gateway-signed `X-Authorized-Scopes` header, HMAC-verified with `SCOPE_SIGNING_KEY`.

    A raw, UNSIGNED `X-Authorized-Scopes` header is NEVER trusted → []. The current
    `INVOKE_AUTH_TOKEN` only establishes caller trust; it does NOT bind the scope set to
    that caller, so scopes must come from one of the two verifiable channels above."""
    injected = getattr(request.state, "authorized_scopes", None)
    if isinstance(injected, list):
        return injected
    channel_verified, verified = _hmac_scope_channel(
        request.headers.get("x-authorized-scopes", ""),
        request.headers.get("x-authorized-scopes-signature", ""),
        os.environ.get("SCOPE_SIGNING_KEY"),
    )
    if channel_verified:
        # 署名済みチャネルが提示された以上、その内容が **空でも** それが答え。
        # 空リスト = ゲートウェイによる明示的な「scope 無し」であって、
        # STG 既定 scope へ昇格させてよい「未供給」ではない（レビュー High）。
        return verified
    # 3. STG provisional default — entry-token callers only, when no trusted scope
    #    channel was supplied at all, and only when this deployment declared itself
    #    provisional (see _stg_default_scopes).
    return _stg_default_scopes() if token_authenticated else []


app = FastAPI(title="SVC-C2-132 — Game Live-Ops Player-Promise Impact Review Agent")

agent = PlayerPromiseImpactReviewGraph(config=_load_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="svc", agent_name="svc-c2-132"))


@app.on_event("startup")
def _startup_scope_env_guard() -> None:
    # Refuse to start a production process that carries the STG scope values.
    assert_scope_env_is_sane()


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone/STG caller auth. Entry-point
    # caller credential, read before any InvocationContext exists — the one
    # "Entry-point exception"; not an agent secret, never stored in state.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    token_authenticated = False
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
        token_authenticated = True

    input_context = {
        # B-6: scopes come only from a channel the client cannot forge (in-process
        # middleware or a gateway-signed header). A raw client header is never trusted.
        "authorized_scopes": _trusted_scopes(request, token_authenticated=token_authenticated),
        "request_ts": datetime.now(timezone.utc).isoformat(),
    }
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "svc-c2-132"}
