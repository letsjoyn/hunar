"""
Pytest configuration for AP IGRS Assistant.
Ensures local unit test suite runs 100% offline and deterministic.
"""
import os
import pytest

# Tests must exercise the local demo mode regardless of a developer's shell/.env.
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = "sqlite:///./igrs_ap_test.db"
os.environ["ALLOW_EXTERNAL_LLM"] = "false"
os.environ["PRODUCTION_RATE_DATA_APPROVED"] = "false"

@pytest.fixture(autouse=True)
def offline_tests(monkeypatch):
    """Isolate tests from external internet calls for maximum speed and determinism."""
    monkeypatch.setattr("backend.app.engine.gemini_agent.get_gemini_client", lambda: None)
