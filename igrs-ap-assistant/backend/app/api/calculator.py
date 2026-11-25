"""
Calculator API endpoints.

These are the endpoints the LLM orchestrator calls via tool-calling,
and that the direct calculator UI form hits.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.db.models import create_tables, get_engine, get_session_factory
from backend.app.core.config import APP_ENV, DATA_DIR, DATABASE_URL, GEMINI_MODEL, IS_PRODUCTION, SEEDS_DIR
from backend.app.engine.duty_calculator import calculate_deed_duty, compare_historical_vs_current, DutyBreakdown
from backend.app.engine.structure_calc import calculate_building_value, BuildingValueResult
from backend.app.engine.fines_calc import calculate_delay_fine, FineResult
from backend.app.engine.area_converter import (
    convert_area,
    to_sq_yards,
    from_sq_yards,
    parse_composite_land_string,
    AreaConversionResult,
)
from backend.app.engine.legal_search import search_legal_knowledge, LegalSearchResult
from backend.app.engine.chatbot_orchestrator import (
    process_chat_message,
    ChatRequest,
    ChatResponse,
)
from backend.app.engine.verification import verify_check_slip, sign_and_archive_check_slip
from backend.app.engine.prohibited_land import check_prohibited_land, ProhibitedCheckResult

from backend.app.engine.gemini_agent import is_gemini_available, run_gemini_chat
from backend.app.db.models import CardCheckSlipRecord, ProhibitedProperty
import json
from pathlib import Path

router = APIRouter()


def get_session():
    engine = get_engine(DATABASE_URL)
    create_tables(engine)
    Session = get_session_factory(engine)
    session = Session()
    # Auto-seed if empty
    from backend.app.db.models import DutyRate
    from backend.app.db.seed import seed_all
    if session.query(DutyRate).count() == 0:
        seed_all(session, SEEDS_DIR)

    try:
        yield session

    finally:
        session.close()


# ==================== Request/Response Models ====================

class DutyRequest(BaseModel):
    instrument_code: str = Field(..., description="e.g. SALE, GIFT_RELATIVE, PARTITION_FAMILY")
    area_type: str = Field(default="ALL", description="ALL, MUNICIPAL_CORP, SEL_SPL_MUNI, OTHER_MUNI, GRAM_PANCHAYAT, CANTONMENT")
    execution_date: date = Field(..., description="Date the instrument was executed (YYYY-MM-DD)")
    consideration_value: Decimal = Field(default=Decimal("0"), description="Declared consideration amount in rupees")
    market_value: Decimal = Field(default=Decimal("0"), description="Government market value in rupees")
    property_subtype: str = Field(default="ALL", description="ALL, FLAT_APARTMENT, RESIDENTIAL, COMMERCIAL, NON_FLAT")
    shares: Optional[list[Decimal]] = Field(default=None, description="For Partition: list of share values in rupees")
    lease_months: Optional[int] = Field(default=None, description="For Lease: duration in months")
    annual_rent: Decimal = Field(default=Decimal("0"), description="For Lease: Average Annual Rent")
    total_rent: Decimal = Field(default=Decimal("0"), description="For Lease <1yr: total rent")
    loan_amount: Decimal = Field(default=Decimal("0"), description="For DOTD/Mortgage: loan amount")
    sheet_count: int = Field(default=10, description="Number of document sheets for user charges")


class DutyResponse(BaseModel):
    status: str
    instrument_code: str
    area_type: str
    execution_date: Optional[date]
    chargeable_value: str
    calc_basis_used: str
    stamp_duty: str
    stamp_duty_rate: str
    transfer_duty: str
    transfer_duty_rate: str
    registration_fee: str
    reg_fee_rate: str
    user_charge: str
    user_charge_detail: str
    total_payable: str
    is_adjustable: bool
    is_historical: bool
    citations: list[str]
    message: str
    warnings: list[str]


class CompareRequest(DutyRequest):
    current_date: Optional[date] = None


class StructureRequest(BaseModel):
    structure_category: str
    area_tier: str
    plinth_area_sqft: Decimal
    floor_number: int = 0
    age_years: int = 0
    is_industrial: bool = False
    execution_date: date = Field(default_factory=date.today)
    completion_stage: str = "COMPLETE"
    site_rate_per_sqyd: Optional[Decimal] = None
    is_apartment: bool = False


class FineRequest(BaseModel):
    execution_date: date
    presentation_date: date
    proper_registration_fee: Decimal
    is_appearance: bool = False


# ==================== Endpoints ====================

def _breakdown_to_response(b: DutyBreakdown) -> DutyResponse:
    return DutyResponse(
        status=b.status,
        instrument_code=b.instrument_code,
        area_type=b.area_type,
        execution_date=b.execution_date,
        chargeable_value=str(b.chargeable_value),
        calc_basis_used=b.calc_basis_used,
        stamp_duty=str(b.stamp_duty),
        stamp_duty_rate=b.stamp_duty_rate,
        transfer_duty=str(b.transfer_duty),
        transfer_duty_rate=b.transfer_duty_rate,
        registration_fee=str(b.registration_fee),
        reg_fee_rate=b.reg_fee_rate,
        user_charge=str(b.user_charge),
        user_charge_detail=b.user_charge_detail,
        total_payable=str(b.total_payable),
        is_adjustable=b.is_adjustable,
        is_historical=b.is_historical,
        citations=b.citations,
        message=b.message,
        warnings=b.warnings,
    )


@router.post("/calculate", response_model=DutyResponse)
def api_calculate(req: DutyRequest, session=Depends(get_session)):
    """Calculate stamp duty, transfer duty, registration fee and user charges."""
    result = calculate_deed_duty(
        session=session,
        instrument_code=req.instrument_code,
        area_type=req.area_type,
        execution_date=req.execution_date,
        consideration_value=req.consideration_value,
        market_value=req.market_value,
        property_subtype=req.property_subtype,
        shares=req.shares,
        lease_months=req.lease_months,
        annual_rent=req.annual_rent,
        total_rent=req.total_rent,
        loan_amount=req.loan_amount,
        sheet_count=req.sheet_count,
    )
    return _breakdown_to_response(result)


@router.post("/compare")
def api_compare(req: CompareRequest, session=Depends(get_session)):
    """Compare historical duty vs current for Section 41-A audit."""
    kwargs = dict(
        consideration_value=req.consideration_value,
        market_value=req.market_value,
        property_subtype=req.property_subtype,
        shares=req.shares,
        lease_months=req.lease_months,
        annual_rent=req.annual_rent,
        total_rent=req.total_rent,
        loan_amount=req.loan_amount,
        sheet_count=req.sheet_count,
    )
    result = compare_historical_vs_current(
        session=session,
        instrument_code=req.instrument_code,
        area_type=req.area_type,
        execution_date=req.execution_date,
        current_date=req.current_date,
        **kwargs,
    )
    return {
        "historical": _breakdown_to_response(result["historical"]),
        "current": _breakdown_to_response(result["current"]),
        "delta_total": str(result["delta_total"]),
        "audit_note": result["audit_note"],
    }


@router.post("/structure-value")
def api_structure_value(req: StructureRequest, session=Depends(get_session)):
    """Calculate building structure market value."""
    result = calculate_building_value(
        session=session,
        structure_category=req.structure_category,
        area_tier=req.area_tier,
        plinth_area_sqft=req.plinth_area_sqft,
        floor_number=req.floor_number,
        age_years=req.age_years,
        is_industrial=req.is_industrial,
        execution_date=req.execution_date,
        completion_stage=req.completion_stage,
        site_rate_per_sqyd=req.site_rate_per_sqyd,
        is_apartment=req.is_apartment,
    )
    return {
        "status": result.status,
        "base_rate_per_sqft": str(result.base_rate_per_sqft),
        "floor_adder": str(result.floor_adder),
        "effective_rate": str(result.effective_rate),
        "gross_value": str(result.gross_value),
        "completion_factor": str(result.completion_factor),
        "after_completion": str(result.after_completion),
        "depreciation_pct": str(result.depreciation_pct),
        "depreciation_amount": str(result.depreciation_amount),
        "net_building_value": str(result.net_building_value),
        "composite_site_adder": str(result.composite_site_adder),
        "citation": result.citation,
        "message": result.message,
        "warnings": result.warnings,
    }


@router.post("/delay-fine")
def api_delay_fine(req: FineRequest):
    """Calculate a demo delayed-presentation fine; not an official determination."""
    if IS_PRODUCTION:
        raise HTTPException(
            status_code=503,
            detail="Fine calculation is disabled: this prototype has no production-approved legal ruleset.",
        )
    result = calculate_delay_fine(
        execution_date=req.execution_date,
        presentation_date=req.presentation_date,
        proper_registration_fee=req.proper_registration_fee,
        is_appearance=req.is_appearance,
    )
    return {
        "status": result.status,
        "days_late": result.days_late,
        "fine_multiplier": result.fine_multiplier,
        "proper_registration_fee": str(result.proper_registration_fee),
        "fine_amount": str(result.fine_amount),
        "message": result.message,
        "legal_basis": result.legal_basis,
        "warnings": [
            "DEMO ONLY: fine rules have not been independently verified; do not use for official decisions."
        ],
    }


class AreaConvertRequest(BaseModel):
    value: Optional[Decimal] = None
    from_unit: Optional[str] = None
    composite_text: Optional[str] = None


@router.post("/convert-area")
def api_convert_area(req: AreaConvertRequest):
    """Convert land measurement units recognized by AP Registration Department."""
    if req.composite_text:
        try:
            sq_yds = parse_composite_land_string(req.composite_text)
            res = convert_area(sq_yds, "Sq.Yds")
            return res.model_dump()
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.value is not None and req.from_unit:
        try:
            res = convert_area(req.value, req.from_unit)
            return res.model_dump()
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        raise HTTPException(status_code=400, detail="Provide either (value and from_unit) or composite_text")


@router.get("/deeds")
def api_get_deeds():
    """Return all 111 official deed classifications from the CARD system."""
    deeds_file = DATA_DIR / "deeds_111.json"
    if not deeds_file.exists():
        raise HTTPException(status_code=404, detail="deeds_111.json not found")
    with open(deeds_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@router.get("/legal-search")
def api_legal_search(q: str = "", limit: int = 5):
    """Full-text statutory and circular search with exact section routing."""
    if not q.strip():
        return []
    results = search_legal_knowledge(q, limit=limit)
    return [r.model_dump() for r in results]


class ProhibitedRequest(BaseModel):
    survey_number: str
    village: Optional[str] = None
    district: Optional[str] = None


@router.post("/check-prohibited", response_model=ProhibitedCheckResult)
def api_check_prohibited(req: ProhibitedRequest, session: Session = Depends(get_session)):
    """Check survey number against Section 22-A prohibited properties register."""
    return check_prohibited_land(session, req.survey_number, req.village, req.district)


@router.get("/verify-slip/{reference_no}")
def api_verify_slip(reference_no: str, session: Session = Depends(get_session)):
    """Cryptographic tamper verification for CARD 2.0 Check-Slips."""
    res = verify_check_slip(session, reference_no)
    if not res:
        raise HTTPException(status_code=404, detail=f"Check-Slip '{reference_no}' not found in registry")
    return res


@router.get("/system-status")
def api_system_status(session: Session = Depends(get_session)):
    """System diagnostic and readiness metrics."""
    gemini_on = is_gemini_available()
    prohibited_count = session.query(ProhibitedProperty).count()
    slip_count = session.query(CardCheckSlipRecord).count()
    from backend.app.engine.legal_search import get_chroma_collection

    collection = get_chroma_collection()
    try:
        chroma_count = collection.count() if collection is not None else 0
    except Exception:
        chroma_count = 0
    return {
        "status": "LOCAL_DEMO" if APP_ENV != "production" else "NOT_READY_FOR_PRODUCTION",
        "environment": APP_ENV,
        "production_ready": False,
        "data_notice": (
            "Demo data and legal interpretations have not been independently verified. "
            "Do not use for official registration, fee collection, or audit decisions."
        ),
        "gemini_api_active": gemini_on,
        "gemini_model": GEMINI_MODEL if gemini_on else "OFFLINE_DETERMINISTIC",
        "chroma_vectors": chroma_count,
        "prohibited_records": 0 if IS_PRODUCTION else prohibited_count,
        "archived_slips": slip_count,
        "engine": "Prototype calculator and legal search",
    }


@router.post("/chat", response_model=ChatResponse)
def api_chat(req: ChatRequest, session: Session = Depends(get_session)):
    """
    Conversational Legal & Calculation AI Assistant.
    Routes to Gemini when available (or when req.use_gemini is True),
    otherwise uses deterministic local orchestrator.
    """
    if req.use_gemini:
        return run_gemini_chat(session, req.message)
    return process_chat_message(session, req.message)


@router.post("/chat/gemini", response_model=ChatResponse)
def api_chat_gemini(req: ChatRequest, session: Session = Depends(get_session)):
    """Force Gemini-powered conversational turn."""
    return run_gemini_chat(session, req.message)




