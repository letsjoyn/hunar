"""
Unit Tests for Phase 7: Admin Government Order (G.O.) Rate Management & Ingestion.
"""
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.db.models import Base, create_tables, DutyRate
from backend.app.db.seed import seed_all
from backend.app.db.seed_prohibited import seed_prohibited_properties
from backend.app.core.config import SEEDS_DIR
from sqlalchemy.pool import StaticPool
from backend.app.api.calculator import get_session
from backend.app.api import admin as admin_api

# Test DB setup with StaticPool so all connections share the same in-memory database
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=test_engine)
create_tables(test_engine)
init_sess = TestingSession()
seed_all(init_sess, SEEDS_DIR)
seed_prohibited_properties(init_sess)
init_sess.close()


def override_get_session():
    sess = TestingSession()
    try:
        yield sess
    finally:
        sess.close()


app.dependency_overrides[get_session] = override_get_session
client = TestClient(app)


def test_admin_list_rates():
    res = client.get("/api/admin/rates?instrument_code=01_01")
    assert res.status_code == 200
    data = res.json()
    assert len(data) > 0
    assert data[0]["instrument_code"] == "01_01"


def test_admin_add_new_rate_schedule():
    # Publish new G.O. for deed 01_01 starting 2027-01-01
    payload = {
        "instrument_code": "01_01",
        "article_ref": "Article 47-A",
        "area_type": "MUNICIPAL_CORP",
        "property_subtype": "ALL",
        "calc_basis": "MAX_MV_CONSID",
        "stamp_duty_type": "PERCENT",
        "stamp_duty_value": 4.5000,
        "transfer_duty_type": "PERCENT",
        "transfer_duty_value": 1.5000,
        "reg_fee_type": "PERCENT",
        "reg_fee_value": 1.0000,
        "effective_from": "2027-01-01",
        "effective_to": "9999-12-31",
        "gov_order_ref": "G.O.Ms.No. 888 Rev dt. 15.12.2026",
        "source_citation": "G.O.Ms.No. 888",
    }
    res = client.post("/api/admin/rates", json=payload)
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["status"] == "PENDING_REVIEW"
    assert res_data["sunsetting_count"] == 0

    approval = client.post(
        f"/api/admin/rates/{res_data['rate_id']}/approve",
        json={
            "reviewed_by": "Test reviewer",
            "source_document": "test official G.O. PDF",
            "page_reference": "page 1",
        },
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "APPROVED"


def test_admin_bulk_rate_approval_requires_configured_token(monkeypatch):
    monkeypatch.setattr(admin_api, "ADMIN_API_TOKEN", "")
    response = client.post(
        "/api/admin/rates/bulk-approval-preview",
        json={"rate_ids": [1]},
    )
    assert response.status_code == 503
    assert "ADMIN_API_TOKEN" in response.json()["detail"]


def test_admin_bulk_rate_approval_preview_and_atomic_approval(monkeypatch):
    monkeypatch.setattr(admin_api, "ADMIN_API_TOKEN", "review-token-for-test")
    session = TestingSession()
    rate = DutyRate(
        instrument_code="BULK_TEST_DEED",
        article_ref="Article 99",
        area_type="ALL",
        property_subtype="ALL",
        calc_basis="MARKET_VALUE",
        stamp_duty_type="PERCENT",
        stamp_duty_value=Decimal("2"),
        transfer_duty_pct=Decimal("0"),
        reg_fee_type="PERCENT",
        reg_fee_value=Decimal("0.5"),
        effective_from=date(2027, 1, 1),
        effective_to=date(2027, 12, 31),
        authority_citation="Test official order citation",
        verification_status="PENDING_REVIEW",
    )
    session.add(rate)
    session.commit()
    session.refresh(rate)
    rate_id = rate.id
    session.close()

    headers = {"x-admin-token": "review-token-for-test"}
    preview = client.post(
        "/api/admin/rates/bulk-approval-preview",
        json={"rate_ids": [rate_id]},
        headers=headers,
    )
    assert preview.status_code == 200
    preview_row = preview.json()["rows"][0]
    assert preview_row["blockers"] == []

    approval = client.post(
        "/api/admin/rates/bulk-approve",
        json={
            "reviewed_by": "Authorized test reviewer",
            "attestation": (
                "I reviewed every listed rate against its cited authoritative source and approve its rate, dates, scope, calculation basis, and caps."
            ),
            "items": [{
                "rate_id": rate_id,
                "source_document": "Test official order.pdf",
                "page_reference": "page 2, item 3",
                "reviewed_fingerprint": preview_row["reviewed_fingerprint"],
            }],
        },
        headers=headers,
    )
    assert approval.status_code == 200
    assert approval.json()["approved_count"] == 1

    check = TestingSession()
    approved = check.query(DutyRate).filter(DutyRate.id == rate_id).one()
    assert approved.verification_status == "VERIFIED"
    assert "Authorized test reviewer" in approved.notes
    check.delete(approved)
    check.commit()
    check.close()


def test_admin_bulk_rate_approval_rejects_incomplete_candidate(monkeypatch):
    monkeypatch.setattr(admin_api, "ADMIN_API_TOKEN", "review-token-for-test")
    session = TestingSession()
    rate = DutyRate(
        instrument_code="BULK_BLOCKED_TEST",
        article_ref="Article 99",
        area_type="ALL",
        property_subtype="ALL",
        calc_basis="UNSPECIFIED",
        stamp_duty_type="PERCENT",
        stamp_duty_value=Decimal("2"),
        transfer_duty_pct=Decimal("0"),
        reg_fee_type="PERCENT",
        reg_fee_value=Decimal("0.5"),
        effective_from=date(2027, 1, 1),
        effective_to=date(2027, 12, 31),
        authority_citation="Test candidate citation",
        verification_status="PENDING_REVIEW",
    )
    session.add(rate)
    session.commit()
    session.refresh(rate)
    rate_id = rate.id
    session.close()

    headers = {"x-admin-token": "review-token-for-test"}
    preview = client.post(
        "/api/admin/rates/bulk-approval-preview",
        json={"rate_ids": [rate_id]},
        headers=headers,
    )
    assert preview.status_code == 200
    row = preview.json()["rows"][0]
    assert "missing or unsupported calculation basis" in row["blockers"]

    approval = client.post(
        "/api/admin/rates/bulk-approve",
        json={
            "reviewed_by": "Authorized test reviewer",
            "attestation": (
                "I reviewed every listed rate against its cited authoritative source and approve its rate, dates, scope, calculation basis, and caps."
            ),
            "items": [{
                "rate_id": rate_id,
                "source_document": "Test official order.pdf",
                "page_reference": "page 2, item 3",
                "reviewed_fingerprint": row["reviewed_fingerprint"],
            }],
        },
        headers=headers,
    )
    assert approval.status_code == 409
    check = TestingSession()
    blocked = check.query(DutyRate).filter(DutyRate.id == rate_id).one()
    assert blocked.verification_status == "PENDING_REVIEW"
    check.delete(blocked)
    check.commit()
    check.close()


def test_admin_add_prohibited_survey():
    payload = {
        "district": "KRISHNA",
        "mandal": "VIJAYAWADA",
        "village": "KANURU",
        "survey_number": "777/2",
        "subdivision": "2",
        "prohibition_category": "22-A(1)(a)",
        "prohibition_reason": "ASSIGNED_LAND",
        "notified_authority": "Revenue Divisional Officer",
        "remarks": "Assigned land",
    }
    res = client.post("/api/admin/prohibited", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] == "REGISTERED"

    # Verify check endpoint flags it immediately
    chk = client.post("/api/check-prohibited", json={"survey_number": "777/2", "village": "KANURU"})
    assert chk.status_code == 200
    assert chk.json()["is_prohibited"] is True


def test_admin_get_audit_logs():
    res = client.get("/api/admin/audit-logs")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_system_status_endpoint():
    res = client.get("/api/system-status")
    assert res.status_code == 200
    status = res.json()
    assert status["status"] == "OPERATIONAL"
    assert status["production_ready"] is True
    assert status["chroma_vectors"] >= 0
    assert status["verified_rates"] > 0
    assert "VERIFIED" in status["data_notice"]
