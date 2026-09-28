"""Startup exercise — importing the server compiles the graph and proves
config/config.yaml actually reaches the nodes (the
the startup-never-exercised defect class: run-tests only runs pytest, so
a real startup blocker — TrustLevel lowercase, config not loaded — would otherwise
slip through every gate)."""

from framework.schemas.trust_level import TrustLevel


def test_server_import_compiles_and_propagates_config():
    import src.api.server as srv

    assert srv.agent is not None
    # config.yaml values must have reached the nodes (not defaulted away).
    pre = srv.agent._nodes["pre_process"]
    assert pre._config.get("max_field_chars") == 20000
    post = srv.agent._nodes["post_process"]
    assert post._config.get("provenance_min_quote_len") == 8
    main = srv.agent._nodes["main"]
    assert main._config.get("kb_raw_retention_days") == 365


def test_trust_level_enum_not_lowercase_string():
    import src.api.server as srv  # noqa: F401
    assert TrustLevel.VERIFIED_EXTERNAL.value == "VERIFIED_EXTERNAL"


def test_authorized_scopes_header_parse():
    import src.api.server as srv
    assert srv._parse_authorized_scopes('[{"tenant_id":"t1","org_id":"o1"}]') == [
        {"tenant_id": "t1", "org_id": "o1"}]
    assert srv._parse_authorized_scopes("not-json") == []  # fail-closed
    assert srv._parse_authorized_scopes("") == []
