"""Marketplace adapter boundary and wiring tests."""

from __future__ import annotations

import importlib
import json
from datetime import datetime

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret
from src.graph.graph import PlayerPromiseImpactReviewGraph
from src.services.llm_seam import get_llm, set_llm
from tests.conftest import FEEDBACK_BODY, INGEST_BODY, INVOKE_BODY, FakeLLM, load_config


def _set_marketplace_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "AZURE_OPENAI_API_KEY": "api-key",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "deployment",
        "ASTAR_TENANT_ID": "marketplace-tenant",
        "ASTAR_ORG_ID": "marketplace-org",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _capture_parent_invoke(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "captured"}

    monkeypatch.setattr(PlayerPromiseImpactReviewGraph, "invoke", fake_invoke)
    return captured


def test_module_import_wires_marketplace_llm_when_seam_is_empty():
    set_llm(None)
    reloaded = importlib.reload(cli)

    llm = get_llm()
    assert llm is not None
    assert isinstance(llm, reloaded._AzureMarketplaceLLM)


def test_module_import_preserves_an_injected_llm():
    injected = FakeLLM()
    set_llm(injected)
    try:
        reloaded = importlib.reload(cli)
        assert get_llm() is injected
    finally:
        set_llm(reloaded._AzureMarketplaceLLM())


def test_toolless_marketplace_llm_retains_no_connection_values():
    assert vars(cli._AzureMarketplaceLLM()) == {}


@pytest.mark.parametrize("body", [INGEST_BODY, FEEDBACK_BODY])
def test_write_modes_become_marketplace_rejection_sentinel(monkeypatch, body):
    _set_marketplace_environment(monkeypatch)
    captured = _capture_parent_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config=load_config())

    agent.invoke(json.dumps(body))

    transformed = json.loads(captured["user_input"])
    assert transformed == {"mode": "marketplace_mode_not_available"}


def test_invoke_mode_survives_with_payload_and_adapter_scope(monkeypatch):
    _set_marketplace_environment(monkeypatch)
    captured = _capture_parent_invoke(monkeypatch)
    body = dict(INVOKE_BODY)
    body["tenant_id"] = "foreign-tenant"
    body["org_id"] = "foreign-org"
    agent = cli.MarketplaceGraph(config=load_config())

    agent.invoke(json.dumps(body))

    transformed = json.loads(captured["user_input"])
    assert transformed["mode"] == "invoke"
    assert transformed["notice_text"] == INVOKE_BODY["notice_text"]
    assert transformed["event_config"] == INVOKE_BODY["event_config"]
    assert transformed["tenant_id"] == "marketplace-tenant"
    assert transformed["org_id"] == "marketplace-org"


def test_invoke_rebuilds_context_without_runner_history(monkeypatch):
    _set_marketplace_environment(monkeypatch)
    captured = _capture_parent_invoke(monkeypatch)
    ctx = InvocationContext(hitl_allowed=True)
    agent = cli.MarketplaceGraph(config=load_config())

    result = agent.invoke(
        json.dumps(INVOKE_BODY),
        session_id="session-1",
        ctx=ctx,
        input_context={"conversation_history": [{"content": "CANARY"}]},
    )

    assert result == {"status": "captured"}
    assert "CANARY" not in json.dumps(captured, default=str)
    assert captured["session_id"] == "session-1"
    assert captured["ctx"].hitl_allowed is False
    assert captured["input_context"]["authorized_scopes"] == [
        {
            "tenant_id": "marketplace-tenant",
            "org_id": "marketplace-org",
            "permissions": ["read"],
        }
    ]
    request_ts = datetime.fromisoformat(captured["input_context"]["request_ts"])
    assert request_ts.tzinfo is not None


def test_ingest_is_denied_end_to_end_with_read_only_scope(monkeypatch, fresh_stores):
    _set_marketplace_environment(monkeypatch)
    store, _ = fresh_stores
    agent = cli.MarketplaceGraph(config=load_config())
    agent.compile()
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    out = agent.invoke(json.dumps(INGEST_BODY), ctx=ctx)

    assert out["status"] == "error"
    assert not store._records


@pytest.mark.parametrize("key", cli._REQUIRED_SECRET_KEYS)
@pytest.mark.parametrize(
    ("bad_value", "expected_error"),
    [(None, MissingSecret), ("contains whitespace", ValueError)],
)
def test_provision_secrets_rejects_missing_or_non_clean_values(monkeypatch, key, bad_value, expected_error):
    _set_marketplace_environment(monkeypatch)
    if bad_value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, bad_value)
    agent = cli.MarketplaceGraph(config=load_config())

    with pytest.raises(expected_error):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_marketplace_environment(monkeypatch)
    agent = cli.MarketplaceGraph(config=load_config())
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
