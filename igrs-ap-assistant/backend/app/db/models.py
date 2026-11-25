"""
SQLAlchemy ORM Models for IGRS AP Temporal Rate Database.

Every rate row carries effective_from / effective_to for temporal queries.
The LLM never touches these tables directly — only the deterministic
calculator engine reads them.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Index, Integer, Numeric, String, Text,
    create_engine, event, func,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.core.config import DATABASE_URL


class Base(DeclarativeBase):
    pass


class DutyRate(Base):
    """Temporal duty and fee rates; new imported rows require source review."""
    __tablename__ = "duty_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instrument_code = Column(String(50), nullable=False)
    article_ref = Column(String(50), nullable=False)
    area_type = Column(String(50), nullable=False)  # ALL, MUNICIPAL_CORP, etc.
    property_subtype = Column(String(50), default="ALL")  # ALL, FLAT_APARTMENT, RESIDENTIAL, COMMERCIAL, NON_FLAT
    duration_min_months = Column(Integer, nullable=True)
    duration_max_months = Column(Integer, nullable=True)
    calc_basis = Column(String(50), nullable=False)  # MAX_MV_CONSID, MARKET_VALUE, VSS, LOAN_AMOUNT, etc.
    stamp_duty_type = Column(String(20), nullable=False)  # PERCENT, FIXED
    stamp_duty_value = Column(Numeric(12, 4), nullable=False)
    stamp_duty_min = Column(Numeric(12, 2), nullable=True)
    stamp_duty_max = Column(Numeric(12, 2), nullable=True)
    transfer_duty_pct = Column(Numeric(8, 4), default=0)
    reg_fee_type = Column(String(20), nullable=False)  # PERCENT, FIXED
    reg_fee_value = Column(Numeric(12, 4), nullable=False)
    reg_fee_min = Column(Numeric(12, 2), nullable=True)
    reg_fee_max = Column(Numeric(12, 2), nullable=True)
    adjustable_sd_pct = Column(Numeric(6, 4), nullable=False, default=Decimal("0"))
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)  # NULL = currently active
    authority_citation = Column(Text, nullable=False)
    sd_authority = Column(Text, nullable=True)
    td_authority = Column(Text, nullable=True)
    rf_authority = Column(Text, nullable=True)
    verification_status = Column(String(20), default="PENDING_REVIEW")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def is_adjustable(self) -> bool:
        return (self.adjustable_sd_pct or Decimal("0")) > Decimal("0")

    __table_args__ = (
        Index("idx_duty_lookup", "instrument_code", "area_type", "effective_from", "effective_to"),
        Index(
            "uq_duty_rates_identity",
            "instrument_code",
            "area_type",
            "property_subtype",
            func.coalesce(duration_min_months, -1),
            "effective_from",
            unique=True,
        ),
    )


class StructureRate(Base):
    """Temporal structure & depreciation rates for building value calculation."""
    __tablename__ = "structure_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    structure_category = Column(String(80), nullable=False)
    area_tier = Column(String(50), nullable=False)
    rate_per_sqft = Column(Numeric(10, 2), nullable=False)
    extra_floor_adder = Column(Numeric(10, 2), default=0)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    authority_citation = Column(Text, nullable=False)


class UserCharge(Base):
    """Temporal CARD user charges."""
    __tablename__ = "user_charges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_code = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    value_threshold_min = Column(Numeric(14, 2), default=0)
    value_threshold_max = Column(Numeric(14, 2), nullable=True)
    included_sheets = Column(Integer, default=10)
    charge_amount = Column(Numeric(10, 2), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    authority_citation = Column(Text, nullable=False)
    verification_status = Column(String(20), default="PENDING_REVIEW")
    notes = Column(Text, nullable=True)


class QueryAuditLog(Base):
    """Full audit trail of every query and answer."""
    __tablename__ = "query_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=True)
    user_role = Column(String(30), nullable=True)
    query_text = Column(Text, nullable=False)
    tools_called = Column(Text, nullable=True)  # JSON string
    response_text = Column(Text, nullable=False)
    citations = Column(Text, nullable=True)  # JSON string
    feedback_score = Column(Integer, nullable=True)
    feedback_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CardCheckSlipRecord(Base):
    """Archived official check-slip with cryptographic tamper-evident signature."""
    __tablename__ = "card_check_slip_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reference_no = Column(String(50), unique=True, index=True, nullable=False)  # e.g. AP-CARD-2026-A1B2C3
    verification_hash = Column(String(64), index=True, nullable=False)  # HMAC-SHA256 hex
    sro_office_code = Column(String(30), default="AP-SRO-VJA-01")
    instrument_code = Column(String(50), nullable=False)
    deed_alias = Column(String(100), nullable=False)
    area_type = Column(String(50), nullable=False)
    execution_date = Column(Date, nullable=False)
    calc_basis_used = Column(String(50), nullable=False)
    chargeable_value = Column(Numeric(15, 2), nullable=False)
    stamp_duty = Column(Numeric(12, 2), nullable=False)
    transfer_duty = Column(Numeric(12, 2), nullable=False)
    registration_fee = Column(Numeric(12, 2), nullable=False)
    user_charge = Column(Numeric(12, 2), nullable=False)
    total_payable = Column(Numeric(12, 2), nullable=False)
    citations_json = Column(Text, nullable=True)
    breakdown_json = Column(Text, nullable=True)
    client_ip = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProhibitedProperty(Base):
    """Section 22-A prohibited properties register (Registration Act 1908)."""
    __tablename__ = "prohibited_properties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    district = Column(String(100), nullable=False, index=True)
    mandal = Column(String(100), nullable=False, index=True)
    village = Column(String(100), nullable=False, index=True)
    survey_number = Column(String(50), nullable=False, index=True)
    subdivision = Column(String(50), nullable=True)
    prohibition_category = Column(String(50), nullable=False)  # 22-A(1)(a), 22-A(1)(b), 22-A(1)(c), etc.
    prohibition_reason = Column(String(200), nullable=False)   # ASSIGNED_LAND, ENDOWMENTS, WAQF, PORAMBOKE, etc.
    notified_authority = Column(String(150), nullable=True)
    gazette_notification = Column(String(100), nullable=True)
    status = Column(String(30), default="MANDATORY_REFUSAL")
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)



# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------
def get_engine(url: str = DATABASE_URL):
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, connect_args=connect_args, echo=False)
    return engine


def create_tables(engine):
    Base.metadata.create_all(engine)


def get_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine)
