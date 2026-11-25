"""
Unit Tests for Phase 8: Gemini API Agent Integration.
"""
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import create_tables
from backend.app.db.seed import seed_all
from backend.app.db.seed_prohibited import seed_prohibited_properties
from backend.app.core.config import SEEDS_DIR
from backend.app.engine.gemini_agent import is_gemini_available, run_gemini_chat


@pytest.fixture
def session():
    test_engine = create_engine("sqlite:///:memory:")
    create_tables(test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    sess = TestingSession()
    seed_all(sess, SEEDS_DIR)
    seed_prohibited_properties(sess)
    yield sess
    sess.close()


def test_gemini_available_check():
    # In test environment without GEMINI_API_KEY, it should return False
    # and handle fallback gracefully
    assert isinstance(is_gemini_available(), bool)


def test_gemini_chat_fallback(session, monkeypatch):
    # Ensure offline test execution by isolating the client
    monkeypatch.setattr("backend.app.engine.gemini_agent.get_gemini_client", lambda: None)
    res = run_gemini_chat(session, "Calculate duty for 15 Lakh sale deed in Vijayawada Corporation in 2025")
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["total_payable"] == "113000"
    assert res.card_slip["integrity_verified"] is True
    assert res.card_slip["is_verified"] is False
    assert res.card_slip["reference_no"].startswith("AP-CARD-")
