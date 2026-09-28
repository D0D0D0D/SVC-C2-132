# SVC-C2-132 — Integration: STG provisional scope fallback (staging evidence path).
#
# 🔴 なぜこの経路が要るか: `deploy-stg` の provisional mode は AuthMiddleware を配線せずに
#    `uvicorn src.api.server:app` を起動し、`scripts/stg_invoke_evidence.py` は
#    `Authorization: Bearer` しか送らない。B-6（レビュー HIGH）対応で生ヘッダを信頼しなく
#    なったため `_trusted_scopes()` は必ず [] を返し、RBAC が全拒否して evidence が
#    構造的に FAIL する。非HITL の fail-closed エージェントの既存前例（Shooting Plan Risk Advisory & Incident History Agent 型）。
#
# 🔴 テストの立て方: fail-closed の主張は「通る条件」だけでは証明できない。
#    対照実験を対で置く — env なし=拒否 / **部分設定=拒否**（部分設定を救済しない）/
#    全設定=200。加えて B-6 の本丸（生ヘッダは依然として信頼されない）と、
#    **署名済みの空リスト（= 明示的な deny）を既定 scope に昇格させない**ことを直接検査する。

from __future__ import annotations

import hashlib
import hmac as _hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.api.server import app, assert_scope_env_is_sane
from tests.conftest import INVOKE_BODY

_TOKEN = "stg-smoke-token"
_TENANT = "stg-provisional-tenant-reserved"
_ORG = "stg-provisional-org-reserved"


def _body(**over):
    b = dict(INVOKE_BODY)
    b["tenant_id"] = _TENANT
    b["org_id"] = _ORG
    b.update(over)
    return {"input": json.dumps(b, ensure_ascii=False), "session_id": "stg-test"}


def _post_bearer_only(client, headers=None):
    """CI の evidence スクリプトと同じ呼び方 — Authorization ヘッダ **だけ**を送る。"""
    h = {"Authorization": f"Bearer {_TOKEN}"}
    h.update(headers or {})
    return client.post("/invoke", json=_body(), headers=h)


def _signed(payload: str, key: str) -> dict:
    sig = _hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {"x-authorized-scopes": payload, "x-authorized-scopes-signature": sig}


def _clear_stg_env(monkeypatch):
    for name in ("STG_PROVISIONAL_MODE", "STG_DEFAULT_TENANT_ID",
                 "STG_DEFAULT_ORG_ID", "STG_DEFAULT_PERMISSIONS", "SCOPE_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)


def _set_full_stg_env(monkeypatch):
    monkeypatch.setenv("STG_PROVISIONAL_MODE", "true")
    monkeypatch.setenv("STG_DEFAULT_TENANT_ID", _TENANT)
    monkeypatch.setenv("STG_DEFAULT_ORG_ID", _ORG)
    monkeypatch.setenv("STG_DEFAULT_PERMISSIONS", "read,write")


class _FakeRequest:
    """`_trusted_scopes` に渡すための最小 Request 相当（state と headers だけ持つ）。"""

    def __init__(self, headers=None, state_scopes=None):
        self.headers = headers or {}
        self.state = type("S", (), {})()
        if state_scopes is not None:
            self.state.authorized_scopes = state_scopes


class TestStgProvisionalScope:
    def test_bearer_only_without_stg_env_is_denied(self, monkeypatch):
        """対照 (1): STG env が無ければ、entry-token を持っていても scope は空のまま。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        with TestClient(app) as client:
            r = _post_bearer_only(client)
        assert r.status_code == 200
        assert r.json()["status"] != "success"      # RBAC が fail-closed で拒否

    @pytest.mark.parametrize(
        "present",
        [
            {"STG_DEFAULT_TENANT_ID": _TENANT},
            {"STG_DEFAULT_ORG_ID": _ORG},
            {"STG_DEFAULT_PERMISSIONS": "read,write"},
            {"STG_DEFAULT_TENANT_ID": _TENANT, "STG_DEFAULT_ORG_ID": _ORG},
        ],
    )
    def test_partial_stg_env_is_not_rescued(self, monkeypatch, present):
        """対照 (2): 部分設定は救済しない。欠けたキーがあれば RBAC まで届いて拒否される。

        🔴 ヘルパーの戻り値だけを見ると、`_trusted_scopes` がそのヘルパーを無視していても
        緑になる（レビュー Low）。エンドポイント経路で結果を見る。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        monkeypatch.setenv("STG_PROVISIONAL_MODE", "true")
        for k, v in present.items():
            monkeypatch.setenv(k, v)
        with TestClient(app) as client:
            r = _post_bearer_only(client)
        assert r.status_code == 200
        assert r.json()["status"] != "success"

    def test_values_are_inert_without_the_mode_declaration(self, monkeypatch):
        """二重ゲート (1): 値が揃っていても、モード宣言が無ければ inert。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", _TENANT)
        monkeypatch.setenv("STG_DEFAULT_ORG_ID", _ORG)
        monkeypatch.setenv("STG_DEFAULT_PERMISSIONS", "read,write")
        from src.api import server

        assert server._stg_default_scopes() == []
        assert server._trusted_scopes(_FakeRequest(), token_authenticated=True) == []

    def test_full_stg_env_supplies_scope_and_invoke_succeeds(self, monkeypatch):
        """対照 (3): 宣言 + 全キーが揃ったときだけ scope が供給され success になる。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        with TestClient(app) as client:
            r = _post_bearer_only(client)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "success", r.text

    def test_unauthenticated_caller_never_gets_the_stg_scope(self, monkeypatch):
        """entry-token を出さない呼び出しは、provisional でも既定 scope に到達しない。

        INVOKE_AUTH_TOKEN が設定されていれば 401 で止まる。設定が無い（=token 認証が
        成立しえない）配備では `token_authenticated` が False のままなので scope は空。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        _set_full_stg_env(monkeypatch)
        with TestClient(app) as client:
            r = client.post("/invoke", json=_body())      # Authorization ヘッダ無し
        assert r.status_code == 200
        assert r.json()["status"] != "success"            # scope 空 → RBAC 拒否


class TestSignedChannelIsNotDowngraded:
    """🔴 敵対的レビュー High: 署名済みの空リストを既定 scope に昇格させないこと。

    ゲートウェイは「この呼び出し元に scope は無い」を **署名付きの空リスト**で伝えられる。
    それを「検証失敗」と同じ `[]` に潰すと、STG フォールバックが *明示的な deny を
    既定 scope の read/write に変える*。
    """

    def test_signed_empty_scope_list_stays_empty(self, monkeypatch):
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        monkeypatch.setenv("SCOPE_SIGNING_KEY", "gateway-key")
        from src.api import server

        req = _FakeRequest(headers=_signed("[]", "gateway-key"))
        assert server._trusted_scopes(req, token_authenticated=True) == []

    def test_signed_empty_scope_list_is_denied_end_to_end(self, monkeypatch):
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        monkeypatch.setenv("SCOPE_SIGNING_KEY", "gateway-key")
        with TestClient(app) as client:
            r = _post_bearer_only(client, headers=_signed("[]", "gateway-key"))
        assert r.status_code == 200
        assert r.json()["status"] != "success"

    def test_signed_non_empty_scope_list_is_unchanged(self, monkeypatch):
        """既存挙動の非退行: 署名済みの非空リストはそのまま採用される。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        monkeypatch.setenv("SCOPE_SIGNING_KEY", "gateway-key")
        from src.api import server

        payload = json.dumps([{"tenant_id": "real-t", "org_id": "real-o",
                               "permissions": ["read"]}])
        req = _FakeRequest(headers=_signed(payload, "gateway-key"))
        assert server._trusted_scopes(req, token_authenticated=True) == [
            {"tenant_id": "real-t", "org_id": "real-o", "permissions": ["read"]}
        ]

    def test_raw_unsigned_scope_header_is_still_not_trusted(self, monkeypatch):
        """B-6 の本丸: 生の（署名なし）scope ヘッダは依然として無効。

        provisional では STG default が返るため、**ヘッダで指定した別テナントが通らない**
        ことをもって「生ヘッダは読まれていない」と示す。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        from src.api import server

        forged = json.dumps([{"tenant_id": "victim-tenant", "org_id": "victim-org",
                              "permissions": ["read", "write"]}])
        req = _FakeRequest(headers={"x-authorized-scopes": forged})
        scopes = server._trusted_scopes(req, token_authenticated=True)
        assert all(s.get("tenant_id") != "victim-tenant" for s in scopes)
        assert scopes == [{"tenant_id": _TENANT, "org_id": _ORG,
                           "permissions": ["read", "write"]}]

    def test_bad_signature_falls_back_and_never_honors_the_header(self, monkeypatch):
        """署名が検証できないヘッダは「チャネル未提示」扱い — 内容は決して採用しない。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        monkeypatch.setenv("SCOPE_SIGNING_KEY", "gateway-key")
        from src.api import server

        req = _FakeRequest(headers={
            "x-authorized-scopes": json.dumps([{"tenant_id": "victim"}]),
            "x-authorized-scopes-signature": "deadbeef"})
        scopes = server._trusted_scopes(req, token_authenticated=True)
        assert all(s.get("tenant_id") != "victim" for s in scopes)
        assert scopes == [{"tenant_id": _TENANT, "org_id": _ORG,
                           "permissions": ["read", "write"]}]

    def test_injected_empty_state_scopes_still_win(self, monkeypatch):
        """middleware が空リストを注入した場合も、それが答え（従来どおり早期 return）。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        _set_full_stg_env(monkeypatch)
        from src.api import server

        assert server._trusted_scopes(
            _FakeRequest(state_scopes=[]), token_authenticated=True) == []


class TestStartupGuard:
    def test_app_startup_actually_runs_the_guard(self, monkeypatch):
        """🔴 guard が **app に配線されている**ことを検査する（レビュー Low）。

        直接呼ぶテストだけだと `@app.on_event("startup")` の配線を消しても緑のまま。
        """
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", _TENANT)
        with pytest.raises(RuntimeError, match="STG_PROVISIONAL_MODE"):
            with TestClient(app):
                pass

    def test_startup_refuses_stg_env_without_the_mode_declaration(self, monkeypatch):
        """本番同居の認可バイパスを起動時に止める（宣言なし + STG_DEFAULT_* → 起動拒否）。"""
        _clear_stg_env(monkeypatch)
        monkeypatch.setenv("STG_DEFAULT_TENANT_ID", _TENANT)
        with pytest.raises(RuntimeError, match="STG_PROVISIONAL_MODE"):
            assert_scope_env_is_sane()

    def test_startup_allows_the_declared_provisional_deployment(self, monkeypatch):
        _clear_stg_env(monkeypatch)
        _set_full_stg_env(monkeypatch)
        assert_scope_env_is_sane()        # raises しなければ合格

    def test_startup_allows_a_clean_production_process(self, monkeypatch):
        _clear_stg_env(monkeypatch)
        assert_scope_env_is_sane()
