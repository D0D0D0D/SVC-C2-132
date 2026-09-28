"""Config validation — fail-closed (docs/02_design.md §(a)(i) retention contract)."""

import pytest

from src.services.config_spec import ConfigError, validate_config


def test_valid_config_passes(config):
    validate_config(config)  # config/config.yaml must be valid


def test_retention_zero_rejected():
    with pytest.raises(ConfigError):
        validate_config({"kb_raw_retention_days": 0})


def test_retention_negative_rejected():
    with pytest.raises(ConfigError):
        validate_config({"kb_raw_retention_days": -30})


def test_retention_non_int_rejected():
    with pytest.raises(ConfigError):
        validate_config({"kb_raw_retention_days": 3.5})
    with pytest.raises(ConfigError):
        validate_config({"kb_raw_retention_days": "365"})


def test_retention_bool_rejected():
    # isinstance(True, int) is True in Python — must be explicitly rejected.
    with pytest.raises(ConfigError):
        validate_config({"kb_raw_retention_days": True})


def test_temperature_range():
    with pytest.raises(ConfigError):
        validate_config({"temperature": 2.0})


def test_max_input_bytes_positive():
    with pytest.raises(ConfigError):
        validate_config({"max_input_bytes": 0})


def test_unknown_keys_ignored():
    validate_config({"totally_unknown": "x", "kb_raw_retention_days": 365})
