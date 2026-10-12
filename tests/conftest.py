"""Force deterministic rule-based agent in tests (never hit a paid LLM)."""
import pytest


@pytest.fixture(autouse=True)
def _force_rule_mode(monkeypatch):
    # Empty (not deleted): agent's .env loader uses setdefault, so an
    # existing empty value blocks the real key from being resurrected.
    monkeypatch.setenv("LLM_API_KEY", "")


@pytest.fixture(autouse=True)
def _force_demo_governance(monkeypatch):
    # Tests must never depend on a live WARDEN engine. Empty (not deleted):
    # agent's .env loader uses setdefault at import time, so existing empty
    # values block the real config from being resurrected mid-test.
    monkeypatch.setenv("WARDEN_BASE_URL", "")
    monkeypatch.setenv("WARDEN_ENDPOINT", "")
    monkeypatch.setenv("WARDEN_SIMULATE_OUTAGE", "")
