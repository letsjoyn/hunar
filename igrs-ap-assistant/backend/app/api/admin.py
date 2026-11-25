"""
Admin & Regulatory Management Endpoints.

Allows SRO Administrators and Revenue Department Authorities to:
1. Update and schedule new Government Orders (G.O.) and duty rates with strict date continuity.
2. Ingest and manage Section 22-A prohibited survey numbers.
3. Review immutable audit logs of calculations and SRO operations.
"""
from datetime import date, timedelta
from decimal import Decimal
import hmac
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.db.models import CardCheckSlipRecord, DutyRate, ProhibitedProperty
from backend.app.api.calculator import get_session
from backend.app.core.config import ADMIN_API_TOKEN, APP_ENV, IS_PRODUCTION

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin_token(x_admin_token: Optional[str] = Header(default=None)) -> None:
    """Require a configured token outside local-only development."""
    if not ADMIN_API_TOKEN:
        if IS_PRODUCTION:
            raise HTTPException(status_code=503, detail="Admin API is disabled: ADMIN_API_TOKEN is not configured")
        return
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")


class NewRateRequest(BaseModel):
    instrument_code: str
    article_ref: str
    area_type: str = "ALL"  # ALL, MUNICIPAL_CORP, etc.
    property_subtype: str = "ALL"
    calc_basis: str = "MAX_MV_CONSID"
    stamp_duty_type: str = "PERCENT"
    stamp_duty_value: Decimal = Field(..., ge=0, le=15)
    stamp_duty_min: Optional[Decimal] = None
    stamp_duty_max: Optional[Decimal] = None
    transfer_duty_value: Optional[Decimal] = Field(default=Decimal("0.0"), ge=0, le=10)
    reg_fee_type: str = "PERCENT"
    reg_fee_value: Decimal = Field(..., ge=0, le=10)
    reg_fee_min: Optional[Decimal] = None
    reg_fee_max: Optional[Decimal] = None
    effective_from: date
    effective_to: Optional[date] = date(9999, 12, 31)
    gov_order_ref: str
    source_citation: str = Field(..., min_length=4)
    notes: Optional[str] = None


class RateApprovalRequest(BaseModel):
    reviewed_by: str = Field(..., min_length=2)
    source_document: str = Field(..., min_length=3)
    page_reference: str = Field(..., min_length=1)


class NewProhibitedRequest(BaseModel):
    district: str
    mandal: str
    village: str
    survey_number: str
    subdivision: Optional[str] = "ALL"
    prohibition_category: str  # e.g. 22-A(1)(a)
    prohibition_reason: str    # ASSIGNED_LAND, ENDOWMENTS, WAQF, etc.
    notified_authority: Optional[str] = None
    gazette_notification: Optional[str] = None
    remarks: Optional[str] = None


@router.post("/rates")
def admin_add_rate(
    req: NewRateRequest,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_token),
):
    """
    Publish a new Government Order rate schedule.
    Enforces strict temporal continuity: automatically sunsets preceding active rates
    by setting their effective_to to (new_rate.effective_from - 1 day).
    """
    if req.effective_to is not None and req.effective_from > req.effective_to:
        raise HTTPException(status_code=400, detail="effective_from cannot be after effective_to")


    new_rate = DutyRate(
        instrument_code=req.instrument_code,
        article_ref=req.article_ref,
        area_type=req.area_type,
        property_subtype=req.property_subtype,
        calc_basis=req.calc_basis,
        stamp_duty_type=req.stamp_duty_type,
        stamp_duty_value=req.stamp_duty_value,
        stamp_duty_min=req.stamp_duty_min,
        stamp_duty_max=req.stamp_duty_max,
        transfer_duty_pct=req.transfer_duty_value or Decimal("0"),
        reg_fee_type=req.reg_fee_type,
        reg_fee_value=req.reg_fee_value,
        reg_fee_min=req.reg_fee_min,
        reg_fee_max=req.reg_fee_max,
        effective_from=req.effective_from,
        effective_to=req.effective_to,
        authority_citation=req.source_citation,
        verification_status="PENDING_REVIEW",
        notes=req.notes,
    )
    session.add(new_rate)
    session.commit()
    session.refresh(new_rate)

    return {
        "status": "PENDING_REVIEW",
        "rate_id": new_rate.id,
        "instrument_code": new_rate.instrument_code,
        "effective_from": str(new_rate.effective_from),
        "effective_to": str(new_rate.effective_to),
        "sunsetting_count": 0,
        "citation": new_rate.authority_citation,
        "message": "Draft saved; it is not used for calculations until reviewed and approved.",
    }


@router.post("/rates/{rate_id}/approve")
def admin_approve_rate(
    rate_id: int,
    req: RateApprovalRequest,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_token),
):
    """Approve a draft only after a reviewer records its official source location."""
    rate = session.query(DutyRate).filter(DutyRate.id == rate_id).one_or_none()
    if rate is None:
        raise HTTPException(status_code=404, detail="Rate draft not found")
    if rate.verification_status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="Only pending rate drafts can be approved")

    active = session.query(DutyRate).filter(
        DutyRate.id != rate.id,
        DutyRate.instrument_code == rate.instrument_code,
        DutyRate.area_type == rate.area_type,
        DutyRate.property_subtype == rate.property_subtype,
        DutyRate.verification_status == "VERIFIED",
        or_(DutyRate.effective_to.is_(None), DutyRate.effective_to >= rate.effective_from),
        DutyRate.effective_from <= (rate.effective_to or date.max),
    ).all()
    later_overlaps = [r for r in active if r.effective_from >= rate.effective_from]
    if later_overlaps:
        raise HTTPException(
            status_code=409,
            detail="The draft overlaps a later approved rate; resolve the date range before approval.",
        )

    for previous in active:
        previous.effective_to = rate.effective_from - timedelta(days=1)
        previous.notes = (previous.notes or "") + (
            f" [Superseded by approved rate {rate.id} on {date.today().isoformat()}]"
        )
    rate.verification_status = "VERIFIED"
    rate.notes = (rate.notes or "") + (
        f" [Reviewed by {req.reviewed_by}; source {req.source_document}, page/reference {req.page_reference}]"
    )
    session.commit()
    return {
        "status": "APPROVED",
        "rate_id": rate.id,
        "reviewed_by": req.reviewed_by,
        "source_document": req.source_document,
        "page_reference": req.page_reference,
        "sunsetting_count": len(active),
    }


@router.get("/rates")
def admin_list_rates(
    instrument_code: Optional[str] = None,
    area_type: Optional[str] = None,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_token),
):
    """Query temporal rate schedules."""
    q = session.query(DutyRate)
    if instrument_code:
        q = q.filter(DutyRate.instrument_code == instrument_code)
    if area_type:
        q = q.filter(DutyRate.area_type == area_type)
    rates = q.order_by(DutyRate.instrument_code, DutyRate.effective_from.desc()).all()
    return [
        {
            "id": r.id,
            "instrument_code": r.instrument_code,
            "area_type": r.area_type,
            "property_subtype": r.property_subtype,
            "stamp_duty_value": float(r.stamp_duty_value),
            "transfer_duty_value": float(r.transfer_duty_pct or 0),
            "reg_fee_value": float(r.reg_fee_value),
            "effective_from": str(r.effective_from),
            "effective_to": str(r.effective_to),
            "authority_citation": r.authority_citation,
        }
        for r in rates
    ]


@router.post("/prohibited")
def admin_add_prohibited_property(
    req: NewProhibitedRequest,
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_token),
):
    """Add a survey number to the Section 22-A prohibited register."""
    prop = ProhibitedProperty(
        district=req.district.upper().strip(),
        mandal=req.mandal.upper().strip(),
        village=req.village.upper().strip(),
        survey_number=req.survey_number.strip(),
        subdivision=req.subdivision,
        prohibition_category=req.prohibition_category,
        prohibition_reason=req.prohibition_reason.upper(),
        notified_authority=req.notified_authority,
        gazette_notification=req.gazette_notification,
        remarks=req.remarks,
    )
    session.add(prop)
    session.commit()
    session.refresh(prop)
    return {"status": "REGISTERED", "id": prop.id, "survey_number": prop.survey_number, "category": prop.prohibition_category}


@router.get("/audit-logs")
def admin_get_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    _: None = Depends(require_admin_token),
):
    """Retrieve immutable SRO calculation check-slips and audit trail."""
    slips = session.query(CardCheckSlipRecord).order_by(CardCheckSlipRecord.created_at.desc()).limit(limit).all()
    return [
        {
            "id": s.id,
            "reference_no": s.reference_no,
            "verification_hash": s.verification_hash,
            "instrument_code": s.instrument_code,
            "area_type": s.area_type,
            "chargeable_value": float(s.chargeable_value),
            "total_payable": float(s.total_payable),
            "execution_date": str(s.execution_date),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "client_ip": s.client_ip,
        }
        for s in slips
    ]
