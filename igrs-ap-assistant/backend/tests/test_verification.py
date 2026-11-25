"""
Unit Tests for Phase 5: Cryptographic Check-Slip Tamper Verification.
"""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import create_tables, CardCheckSlipRecord
from backend.app.engine.verification import (
    compute_slip_hmac,
    generate_reference_no,
    sign_and_archive_check_slip,
    verify_check_slip,
)


@pytest.fixture
def session():
    test_engine = create_engine("sqlite:///:memory:")
    create_tables(test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    sess = TestingSession()
    yield sess
    sess.close()


def test_hmac_deterministic():
    sig1 = compute_slip_hmac("SRO-01", "01_01", Decimal("1500000"), Decimal("113000"), "2025-05-01")
    sig2 = compute_slip_hmac("SRO-01", "01_01", Decimal("1500000"), Decimal("113000"), "2025-05-01")
    assert sig1 == sig2
    assert len(sig1) == 64  # SHA-256 hex length


def test_sign_and_archive_and_verify(session):
    slip_dict = {
        "instrument_code": "01_01",
        "deed_alias": "SALE",
        "area_type": "MUNICIPAL_CORP",
        "execution_date": "2025-05-01",
        "calc_basis_used": "MAX_MV_CONSID",
        "chargeable_value": "1500000",
        "stamp_duty": "75000",
        "transfer_duty": "22500",
        "registration_fee": "15000",
        "user_charge": "500",
        "total_payable": "113000",
        "citations": ["G.O.Ms.No. 394"],
    }

    signed = sign_and_archive_check_slip(session, slip_dict, client_ip="192.168.1.10")
    assert "reference_no" in signed
    assert signed["reference_no"].startswith("AP-CARD-")
    assert signed["integrity_verified"] is True
    assert signed["is_verified"] is False
    assert "does not verify" in signed["verification_scope"]

    # Verify via registry
    verification = verify_check_slip(session, signed["reference_no"])
    assert verification is not None
    assert verification["is_authentic"] is True
    assert verification["integrity_verified"] is True
    assert verification["legal_data_verified"] is False
    assert verification["tamper_detected"] is False
    assert verification["total_payable"] == 113000.0


def test_tamper_detection(session):
    slip_dict = {
        "instrument_code": "01_01",
        "deed_alias": "SALE",
        "area_type": "MUNICIPAL_CORP",
        "execution_date": "2025-05-01",
        "calc_basis_used": "MAX_MV_CONSID",
        "chargeable_value": "1500000",
        "stamp_duty": "75000",
        "transfer_duty": "22500",
        "registration_fee": "15000",
        "user_charge": "500",
        "total_payable": "113000",
        "citations": ["G.O.Ms.No. 394"],
    }
    signed = sign_and_archive_check_slip(session, slip_dict)
    ref = signed["reference_no"]

    # Malicious direct modification in database (tampering)
    rec = session.query(CardCheckSlipRecord).filter_by(reference_no=ref).first()
    rec.total_payable = Decimal("50000")  # Fraudulently reduced amount
    session.commit()

    # System must detect tampering!
    verification = verify_check_slip(session, ref)
    assert verification["is_authentic"] is False
    assert verification["tamper_detected"] is True
