"""B-6 RBAC trust boundary (review finding) — the standalone HTTP entry point must
NOT let a client forge authorization scopes.

`X-Authorized-Scopes` is trusted ONLY when it is HMAC-signed by the gateway
(`SCOPE_SIGNING_KEY`) or injected in-process by the platform AuthMiddleware
(`request.state`); a raw client header is never trusted → the request fails closed.

The E2E tests drive the REAL `invoke()` endpoint through a constructed Starlette
`Request` (no httpx dependency). Every E2E case gives the caller the same
`INVOKE_AUTH_TOKEN` trust (so S-1 passes identically) and varies ONLY the scope
channel — so a denial in the forged case can come only from RBAC, not from the trust
gate. This is the negative test the review required: same trusted caller, forged scope ⇒
denied; valid signed / injected scope ⇒ authorized.
"""

import asyncio
import hashlib
import hmac
import json

from starlette.requests import Request

import src.api.server as srv
from tests.conftest import INVOKE_BODY, scopes

_TOKEN = "invoke-token-for-tests"
_KEY = "gateway-signing-key"


def _request(headers: dict | None = None, state: dict | None = None) -> Request:
    raw = [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "POST", "path": "/invoke", "headers": raw, "query_string": b""}
    req = Request(scope)
    for k, v in (state or {}).items():
        setattr(req.state, k, v)
    return req


def _invoke(body: dict, headers: dict | None = None, state: dict | None = None) -> dict:
    hdrs = {"authorization": f"Bearer {_TOKEN}"}  # caller is trusted to invoke (S-1 passes)
    hdrs.update(headers or {})
    req = _request(headers=hdrs, state=state)
    return asyncio.run(
        srv.invoke(srv.InvokeRequest(input=json.dumps(body, ensure_ascii=False), session_id="b6"), req)
    )


def _sign(header_value: str, key: str) -> str:
    return hmac.new(key.encode(), header_value.encode(), hashlib.sha256).hexdigest()


# ── the negative tests the review required — forged scope cannot authorize ─────
def test_forged_unsigned_scope_header_cannot_authorize(fake_llm, fresh_stores, monkeypatch):
    """A trusted caller (INVOKE_AUTH_TOKEN) supplying a full-permission
    X-Authorized-Scopes header WITHOUT a valid signature must NOT be authorized."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    monkeypatch.delenv("SCOPE_SIGNING_KEY", raising=False)
    forged = json.dumps(scopes(perms=("read", "write")))  # [{t1/o1, read+write}]
    out = _invoke(INVOKE_BODY, headers={"x-authorized-scopes": forged})
    assert out["status"] == "error"  # RBAC denied — forged scope did not authorize


def test_wrong_signature_scope_header_cannot_authorize(fake_llm, fresh_stores, monkeypatch):
    """A signing key is configured, but the caller supplies a bogus signature → denied."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("SCOPE_SIGNING_KEY", _KEY)
    forged = json.dumps(scopes(perms=("read", "write")))
    out = _invoke(INVOKE_BODY, headers={"x-authorized-scopes": forged, "x-authorized-scopes-signature": "deadbeef"})
    assert out["status"] == "error"


# ── the two verifiable channels DO authorize (same caller/body as the negatives) ──
def test_gateway_signed_scope_header_authorizes(fake_llm, fresh_stores, monkeypatch):
    """Identical caller + body as the forged case; only difference is a VALID gateway
    signature → authorized. Proves the denial above is the forged scope, not the caller."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("SCOPE_SIGNING_KEY", _KEY)
    header = json.dumps(scopes(perms=("read", "write")))
    out = _invoke(INVOKE_BODY, headers={"x-authorized-scopes": header, "x-authorized-scopes-signature": _sign(header, _KEY)})
    assert out["status"] == "success"


def test_middleware_injected_scopes_authorize(fake_llm, fresh_stores, monkeypatch):
    """Scopes injected in-process via request.state (platform AuthMiddleware) are trusted
    even with no signing key configured — a remote client cannot set request.state."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    monkeypatch.delenv("SCOPE_SIGNING_KEY", raising=False)
    out = _invoke(INVOKE_BODY, state={"authorized_scopes": scopes(perms=("read", "write"))})
    assert out["status"] == "success"


# ── unit-level boundary function ──────────────────────────────────────────────
def test_trusted_scopes_rejects_unsigned_and_bad_signature(monkeypatch):
    monkeypatch.setenv("SCOPE_SIGNING_KEY", _KEY)
    header = json.dumps(scopes())
    assert srv._trusted_scopes(_request(headers={"x-authorized-scopes": header})) == []
    assert srv._trusted_scopes(
        _request(headers={"x-authorized-scopes": header, "x-authorized-scopes-signature": "bad"})
    ) == []


def test_trusted_scopes_accepts_signed_and_injected(monkeypatch):
    monkeypatch.setenv("SCOPE_SIGNING_KEY", _KEY)
    header = json.dumps(scopes())
    signed = srv._trusted_scopes(
        _request(headers={"x-authorized-scopes": header, "x-authorized-scopes-signature": _sign(header, _KEY)})
    )
    assert signed == scopes()
    monkeypatch.delenv("SCOPE_SIGNING_KEY", raising=False)
    injected = scopes()
    assert srv._trusted_scopes(_request(state={"authorized_scopes": injected})) == injected


def test_non_ascii_signature_fails_closed_without_crash(monkeypatch):
    """A raw non-ASCII signature header (Latin-1 decoded by the ASGI layer) must not
    crash constant-time compare — it fails closed to [] (review R2 M-1)."""
    monkeypatch.setenv("SCOPE_SIGNING_KEY", _KEY)
    header = json.dumps(scopes())
    assert srv._trusted_scopes(
        _request(headers={"x-authorized-scopes": header, "x-authorized-scopes-signature": "\xff\xfe\x80"})
    ) == []


def test_no_signing_key_fails_closed_even_with_signature(monkeypatch):
    """Without a configured SCOPE_SIGNING_KEY the deployment cannot verify the channel,
    so even a (locally) 'signed' header yields no scopes (fail-closed)."""
    monkeypatch.delenv("SCOPE_SIGNING_KEY", raising=False)
    header = json.dumps(scopes())
    assert srv._trusted_scopes(
        _request(headers={"x-authorized-scopes": header, "x-authorized-scopes-signature": _sign(header, _KEY)})
    ) == []
