"""Regression tests for 111-deed coverage, DOTD/numeric user charges, and Stage 6 bridge."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import DATA_DIR, SEEDS_DIR
from backend.app.db.models import Base, DutyRate, create_tables
from backend.app.db.seed import seed_all
from backend.app.db.stage6_bridge import list_stage6_queue
from backend.app.engine.duty_calculator import calculate_deed_duty
from backend.app.main import app


def test_all_111_deeds_resolve_verified_with_non_zero_user_charge():
    engine = create_engine("sqlite:///:memory:")
    create_tables(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        seed_all(session, SEEDS_DIR)
        deeds = json.loads((DATA_DIR / "deeds_111.json").read_text(encoding="utf-8"))
        assert len(deeds) == 111

        for deed in deeds:
            code = deed["code"]
            res = calculate_deed_duty(
                session=session,
                instrument_code=code,
                area_type="MUNICIPAL_CORP",
                execution_date=date(2026, 5, 1),
                consideration_value=Decimal("1000000"),
                market_value=Decimal("1000000"),
                loan_amount=Decimal("1000000"),
                shares=[Decimal("500000"), Decimal("500000")],
                annual_rent=Decimal("100000"),
                total_rent=Decimal("100000"),
                lease_months=24,
            )
            assert res.status == "OK", f"Deed {code} returned {res.status}: {res.message}"
            assert res.user_charge > Decimal("0"), f"Deed {code} had 0 user charge"

        dotd = calculate_deed_duty(
            session=session,
            instrument_code="02_08",
            area_type="MUNICIPAL_CORP",
            execution_date=date(2026, 5, 1),
            loan_amount=Decimal("5000000"),
        )
        assert dotd.status == "OK"
        assert dotd.user_charge == Decimal("100")
    finally:
        session.close()


def test_stage6_queue_pairs_staged_rates_with_runtime():
    engine = create_engine("sqlite:///:memory:")
    create_tables(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        seed_all(session, SEEDS_DIR)
        queue = list_stage6_queue(session)
        assert len(queue) == 111
        sale = next(item for item in queue if item["runtime_code"] == "01_01")
        assert sale["runtime_verification_status"] == "VERIFIED"
    finally:
        session.close()


def test_admin_stage6_queue_endpoint():
    with TestClient(app) as client:
        res = client.get("/api/admin/stage6/queue")
        assert res.status_code == 200
        rows = res.json()
        assert len(rows) == 111
