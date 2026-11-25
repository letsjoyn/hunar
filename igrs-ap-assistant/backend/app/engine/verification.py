"""
Cryptographic Tamper-Evident Verification for CARD 2.0 Check-Slips.

Generates immutable HMAC-SHA256 signatures for every generated calculation check-slip,
assigns a unique tracking reference number (e.g. AP-CARD-2026-XXXX),
archives the record in the database, and provides verification lookups.
"""
import hashlib
import hmac
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session
from backend.app.db.models import CardCheckSlipRecord
from backend.app.core.config import CARD_HMAC_SECRET, SRO_OFFICE_CODE

# This fallback is only for local demo signatures. Production startup requires a
# separately configured high-entropy CARD_HMAC_SECRET.
HMAC_SECRET = (CARD_HMAC_SECRET or "local-demo-only-not-a-secret").encode("utf-8")


def _canonicalize_value(val: Any) -> str:
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return f"{val:.2f}"
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val).strip()


def compute_slip_hmac(
    sro_code: str,
    instrument_code: str,
    chargeable_value: Any,
    total_payable: Any,
    execution_date: str,
) -> str:
    """Compute deterministic HMAC-SHA256 signature for core financial and legal parameters."""
    raw = (
        f"SRO={sro_code}|"
        f"DOC={instrument_code}|"
        f"VAL={_canonicalize_value(chargeable_value)}|"
        f"TOT={_canonicalize_value(total_payable)}|"
        f"DATE={execution_date}"
    )
    sig = hmac.new(HMAC_SECRET, raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return sig


def generate_reference_no(sig_hex: str) -> str:
    """Generate human-readable reference number: AP-CARD-YYYY-XXXXXXXX."""
    year = datetime.utcnow().year
    suffix = sig_hex[:10].upper()
    return f"AP-CARD-{year}-{suffix}"


def sign_and_archive_check_slip(
    session: Session,
    slip_dict: Dict[str, Any],
    client_ip: str = "127.0.0.1",
    sro_code: str = SRO_OFFICE_CODE,
) -> Dict[str, Any]:
    """
    Cryptographically signs and archives the check-slip in the database.
    Updates slip_dict with reference_no and verification_hash.
    """
    instrument_code = slip_dict.get("instrument_code", "01_01")
    deed_alias = slip_dict.get("deed_alias", "CONVEYANCE")
    area_type = slip_dict.get("area_type", "MUNICIPAL_CORP")
    execution_date_str = str(slip_dict.get("execution_date", date.today().isoformat()))
    calc_basis = slip_dict.get("calc_basis_used", "MAX_MV_CONSID")
    
    val = Decimal(str(slip_dict.get("chargeable_value", 0)))
    sd = Decimal(str(slip_dict.get("stamp_duty", 0)))
    td = Decimal(str(slip_dict.get("transfer_duty", 0)))
    rf = Decimal(str(slip_dict.get("registration_fee", 0)))
    uc = Decimal(str(slip_dict.get("user_charge", 0)))
    tot = Decimal(str(slip_dict.get("total_payable", 0)))

    sig = compute_slip_hmac(sro_code, instrument_code, val, tot, execution_date_str)
    ref_no = generate_reference_no(sig)

    # Check if this exact record already exists to avoid unique constraint duplication
    existing = session.query(CardCheckSlipRecord).filter_by(reference_no=ref_no).first()
    if not existing:
        record = CardCheckSlipRecord(
            reference_no=ref_no,
            verification_hash=sig,
            sro_office_code=sro_code,
            instrument_code=instrument_code,
            deed_alias=deed_alias,
            area_type=area_type,
            execution_date=datetime.strptime(execution_date_str, "%Y-%m-%d").date(),
            calc_basis_used=calc_basis,
            chargeable_value=val,
            stamp_duty=sd,
            transfer_duty=td,
            registration_fee=rf,
            user_charge=uc,
            total_payable=tot,
            citations_json=json.dumps(slip_dict.get("citations", [])),
            breakdown_json=json.dumps(slip_dict, default=str),
            client_ip=client_ip,
        )
        session.add(record)
        session.commit()

    # Enrich returned dict
    slip_dict["reference_no"] = ref_no
    slip_dict["verification_hash"] = sig
    slip_dict["verification_url"] = f"/api/verify-slip/{ref_no}"
    slip_dict["integrity_verified"] = True
    slip_dict["is_verified"] = False
    slip_dict["verification_scope"] = (
        "HMAC integrity only; this does not verify the legality or accuracy of the rates."
    )
    slip_dict["sro_office_code"] = sro_code
    return slip_dict


def verify_check_slip(session: Session, reference_no: str) -> Optional[Dict[str, Any]]:
    """
    Looks up archived slip by reference number, validates HMAC integrity,
    and returns authoritative verification status.
    """
    record = session.query(CardCheckSlipRecord).filter_by(reference_no=reference_no.strip()).first()
    if not record:
        return None

    # Re-verify HMAC to detect database tampering
    recomputed = compute_slip_hmac(
        record.sro_office_code,
        record.instrument_code,
        record.chargeable_value,
        record.total_payable,
        record.execution_date.isoformat(),
    )
    tamper_detected = (recomputed != record.verification_hash)

    return {
        "reference_no": record.reference_no,
        "is_authentic": not tamper_detected,
        "integrity_verified": not tamper_detected,
        "legal_data_verified": False,
        "tamper_detected": tamper_detected,
        "sro_office_code": record.sro_office_code,
        "instrument_code": record.instrument_code,
        "deed_alias": record.deed_alias,
        "area_type": record.area_type,
        "execution_date": record.execution_date.isoformat(),
        "chargeable_value": float(record.chargeable_value),
        "stamp_duty": float(record.stamp_duty),
        "transfer_duty": float(record.transfer_duty),
        "registration_fee": float(record.registration_fee),
        "user_charge": float(record.user_charge),
        "total_payable": float(record.total_payable),
        "citations": json.loads(record.citations_json) if record.citations_json else [],
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
