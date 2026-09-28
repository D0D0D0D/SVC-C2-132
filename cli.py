"""AGENTIC STAR Marketplace entry adapter for SVC-C2-132."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0/1.0.1 compatibility.
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # pragma: no cover - local wheel has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("The installed AgentCore wheel does not include Marketplace support")


from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import PlayerPromiseImpactReviewGraph
from src.services.llm_seam import get_llm, set_llm

_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-132"
_PROVIDER_AGENT_NAME = "svc-c2-132"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_KEYS = (
    "ASTAR_TENANT_ID",
    "ASTAR_ORG_ID",
)
_REQUIRED_SECRET_KEYS = _AZURE_OPENAI_SECRET_KEYS + _SCOPE_KEYS
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)
_MARKETPLACE_REJECTION_MODE = "marketplace_mode_not_available"


class _AllowlistedEnvironmentProvider(SecretProvider):
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        self._namespace = _NAMESPACE
        self._agent_name = _PROVIDER_AGENT_NAME

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = os.environ.get(key)
        return value if value else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        # Connection values are checked before the optional lazy import so a
        # deployment error is reported as such in this repository's old wheel.
        provider = current_secrets()
        connection = {
            "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
            "azure_endpoint": _require_azure_endpoint(provider),
            "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
        }
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        client: BaseLLM = AzureOpenAIClient(connection)
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _marketplace_user_input(user_input: str) -> str:
    """Force JSON packets to the adapter-owned read-only scope and invoke mode."""

    try:
        packet = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        return user_input
    if not isinstance(packet, dict):
        return user_input

    requested_mode = packet.get("mode", "invoke")
    if requested_mode != "invoke":
        return json.dumps({"mode": _MARKETPLACE_REJECTION_MODE})

    packet["mode"] = "invoke"
    packet["tenant_id"] = os.environ.get("ASTAR_TENANT_ID")
    packet["org_id"] = os.environ.get("ASTAR_ORG_ID")
    return json.dumps(packet, ensure_ascii=False)


class MarketplaceGraph(PlayerPromiseImpactReviewGraph):
    """Marketplace-only read boundary and Azure LLM seam wiring."""

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        tenant_id = os.environ.get("ASTAR_TENANT_ID")
        org_id = os.environ.get("ASTAR_ORG_ID")
        marketplace_input_context = {
            "authorized_scopes": [
                {
                    "tenant_id": tenant_id,
                    "org_id": org_id,
                    "permissions": ["read"],
                }
            ],
            "request_ts": datetime.now(timezone.utc).isoformat(),
        }
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input),
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=marketplace_input_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


if get_llm() is None:
    set_llm(_AzureMarketplaceLLM())


def main() -> None:
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
