"""
Unit Tests for Phase 6: Section 22-A Prohibited Land Verification.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import create_tables
from backend.app.db.seed_prohibited import seed_prohibited_properties
from backend.app.engine.prohibited_land import check_prohibited_land


@pytest.fixture
def session():
    test_engine = create_engine("sqlite:///:memory:")
    create_tables(test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    sess = TestingSession()
    seed_prohibited_properties(sess)
    yield sess
    sess.close()


def test_assigned_land_refusal(session):
    res = check_prohibited_land(session, "102/1", village="GUNADALA")
    assert res.is_prohibited is True
    assert res.status == "MANDATORY_REFUSAL"
    assert res.category == "22-A(1)(a)"
    assert res.reason == "ASSIGNED_LAND"
    assert "POTD Act" in res.gazette_notification or "POTD Act" in res.remarks


def test_endowments_temple_land_refusal(session):
    res = check_prohibited_land(session, "210", village="SIMHACHALAM")
    assert res.is_prohibited is True
    assert res.category == "22-A(1)(b)"
    assert "Devasthanam" in res.remarks


def test_waqf_board_land_refusal(session):
    res = check_prohibited_land(session, "144", village="CHILAKALAPUDI")
    assert res.is_prohibited is True
    assert res.category == "22-A(1)(c)"
    assert res.reason == "WAQF_PROPERTY"


def test_missing_land_record_is_not_a_clearance(session):
    res = check_prohibited_land(session, "999/8A", village="GUNADALA")
    assert res.is_prohibited is False
    assert res.status == "NOT_FOUND"
    assert "not proof" in res.statutory_directive
