import pytest

from cipherfx_clean.configuration import load_settings


def test_clean_configuration_requires_explicit_values():
    settings = load_settings({"environment": "research", "symbols": ["XAUUSD"], "timeframes": ["M5"], "execution_enabled": False})
    assert settings.symbols == ("XAUUSD",)
    assert settings.execution_enabled is False


def test_clean_configuration_does_not_invent_defaults():
    with pytest.raises(ValueError, match="missing explicit settings"):
        load_settings({"environment": "research"})
