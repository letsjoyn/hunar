"""
Pytest configuration for AP IGRS Assistant.
Ensures local unit test suite runs 100% offline and deterministic.
"""
import atexit
import os
from pathlib import Path
import tempfile
import pytest

_TEST_DB_PATH = Path(tempfile.gettempdir()) / f"igrs_ap_test_{os.getpid()}.db"


def _cleanup_test_db() -> None:
    try:
        if _TEST_DB_PATH.exists():
            _TEST_DB_PATH.unlink()
    except OSError:
        pass


atexit.register(_cleanup_test_db)

# Tests must exercise the local demo mode regardless of a developer's shell/.env.
os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
os.environ["ALLOW_EXTERNAL_LLM"] = "false"
os.environ["PRODUCTION_RATE_DATA_APPROVED"] = "false"

@pytest.fixture(autouse=True)
def offline_tests(monkeypatch):
    """Isolate tests from external internet calls for maximum speed and determinism."""
    monkeypatch.setattr("backend.app.engine.gemini_agent.get_gemini_client", lambda: None)
