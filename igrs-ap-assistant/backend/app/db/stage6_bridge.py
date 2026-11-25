"""Bridge between Stage 6 staged_deed_rates (source_staging.db) and runtime DutyRate."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.app.core.config import DATA_DIR
from backend.app.db.models import DutyRate

STAGING_DB_PATH = DATA_DIR / "source_staging.db"
DEFAULT_EFFECTIVE_FROM = date(2014, 11, 26)
SALE_AUTHORITY_CITATION = (
    "G.O.Ms.No. 394 Rev (Regn-I) dt. 26.11.2014 & "
    "G.O.Ms.No. 393 Rev (Regn-I) dt. 26.11.2014"
)
DEFAULT_AUTHORITY_CITATION = (
    "Indian Stamp Act 1899 (AP Sch. I-A) & "
    "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013"
)


def _default_authorities_for_code(norm_code: str, art_ref: str) -> tuple[str, str, str, str]:
    """Return (combined, sd_authority, td_authority, rf_authority) by instrument."""
    if norm_code in {"01_01", "01_04", "01_05", "01_06", "01_07", "01_08", "01_14", "01_15", "01_16", "01_17", "01_18", "01_19", "01_20", "01_21", "01_22"}:
        return (
            SALE_AUTHORITY_CITATION,
            f"{art_ref} · G.O.Ms.No. 394 Rev (Regn-I) dt. 26.11.2014",
            "AP Municipalities / GP Transfer Duty Surcharge (1.5%)",
            "G.O.Ms.No. 393 Rev (Regn-I) dt. 26.11.2014 (1.0%)",
        )
    if norm_code in {"01_09", "01_10", "01_02", "01_03", "01_11"}:
        rf_note = (
            "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (Fixed ₹2,000)"
            if norm_code == "01_11"
            else "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (0.5%, max ₹20,000)"
        )
        return (
            f"{art_ref} · G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013 & G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
            f"{art_ref} · G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013",
            "Exempt (Not Applicable)",
            rf_note,
        )
    if norm_code in {"03_02", "03_03", "03_04", "03_12", "03_14"} or "49" in art_ref:
        return (
            "Art. 49-A · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014 & G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
            "Art. 49-A · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014",
            "Exempt (Not Applicable)",
            "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (0.5%, min ₹1,000 / max ₹10,000)",
        )
    if norm_code in {"03_01", "03_05", "03_06", "03_07"}:
        td_auth = (
            "G.O.Ms.No. 463, PR&RD (PTS.I), dt. 19.12.2013 (0.5% GP Transfer Duty)"
            if norm_code == "03_05"
            else "AP Municipalities / GP Transfer Duty Surcharge (1.5%)"
        )
        return (
            f"{art_ref} · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014 & G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
            f"{art_ref} · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014",
            td_auth,
            "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (0.5%, min ₹1,000 / max ₹10,000)",
        )
    if norm_code == "04_02":
        return (
            "Art. 40 · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014 & G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
            "Art. 40 · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014",
            "Exempt (Not Applicable)",
            "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (Fixed ₹1,000)",
        )
    if norm_code == "04_01":
        return (
            "Art. 40 · Indian Stamp Act 1899 (AP Sch. I-A) & G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
            "Art. 40 · Indian Stamp Act 1899 (AP Sch. I-A)",
            "Exempt (Not Applicable)",
            "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (Fixed ₹1,000)",
        )
    return (
        f"{art_ref} · Indian Stamp Act 1899 (AP Sch. I-A) & G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
        f"{art_ref} · Indian Stamp Act 1899 (AP Sch. I-A)",
        "Exempt (Not Applicable)",
        "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013",
    )


def _normalize_code(deed_code: str) -> str:
    return (deed_code or "").strip().replace("-", "_").upper()


def _infer_calc_basis(deed_code: str, classification: str, deed_type: str, raw_basis: Optional[str]) -> str:
    if raw_basis and raw_basis.strip() not in ("", "UNSPECIFIED"):
        return raw_basis.strip()
    upper = f"{classification} {deed_type}".upper()
    if deed_code.startswith("04_") or "PARTITION" in upper:
        return "VSS"
    if deed_code.startswith("02_") or "MORTGAGE" in upper or "FURTHER CHARGE" in upper:
        return "LOAN_AMOUNT"
    if deed_code.startswith("06_") or "EXCHANGE" in upper:
        return "HIGHER_EXCHANGE_MV"
    if deed_code.startswith("07_") or "LEASE" in upper:
        return "AAR"
    if deed_code.startswith(("03_", "05_", "08_", "09_", "20_", "21_", "30_", "31_", "32_", "33_", "34_", "35_", "36_", "37_", "38_", "39_", "40_", "41_", "42_", "43_", "44_")):
        return "MARKET_VALUE"
    if deed_code in {"01_09", "01_10"} or "DEVELOPMENT AGREEMENT" in upper:
        return "MARKET_VALUE"
    return "MAX_MV_CONSID"


def _to_decimal(val: Any) -> Decimal:
    if val is None or str(val).strip() == "":
        return Decimal("0")
    return Decimal(str(val).strip())


def list_stage6_queue(
    session: Session,
    staging_db: Path = STAGING_DB_PATH,
    only_pending: bool = False,
) -> list[dict[str, Any]]:
    """Return Stage 6 staged_deed_rates paired with runtime DutyRate status."""
    if not staging_db.exists():
        return []
    conn = sqlite3.connect(f"file:{staging_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, source_row_locator, deed_code, classification, deed_type,
                      article_ref, book_number, stamp_duty_percent, transfer_duty_percent,
                      registration_fee_percent, registration_fee_min, registration_fee_max,
                      calculation_basis, area_type, property_subtype, effective_from,
                      effective_to, source_status, review_status
               FROM staged_deed_rates
               ORDER BY deed_code"""
        ).fetchall()
    finally:
        conn.close()

    runtime_rows = session.query(DutyRate).filter(DutyRate.effective_to.is_(None)).all()
    runtime_by_code = {r.instrument_code: r for r in runtime_rows}

    queue: list[dict[str, Any]] = []
    for row in rows:
        norm_code = _normalize_code(row["deed_code"])
        existing = runtime_by_code.get(norm_code)
        if only_pending and row["review_status"] == "APPROVED" and existing and existing.verification_status == "VERIFIED":
            continue
        queue.append({
            "staged_id": row["id"],
            "staged_code": row["deed_code"],
            "runtime_code": norm_code,
            "classification": row["classification"],
            "deed_type": row["deed_type"],
            "article_ref": f"Art. {row['article_ref']}" if row["article_ref"] and not str(row["article_ref"]).startswith("Art") else (row["article_ref"] or "Schedule I-A"),
            "book_number": row["book_number"],
            "stamp_duty_percent": row["stamp_duty_percent"],
            "transfer_duty_percent": row["transfer_duty_percent"],
            "registration_fee_percent": row["registration_fee_percent"],
            "registration_fee_min": row["registration_fee_min"],
            "registration_fee_max": row["registration_fee_max"],
            "staged_review_status": row["review_status"],
            "runtime_rate_id": existing.id if existing else None,
            "runtime_verification_status": existing.verification_status if existing else "MISSING",
        })
    return queue


def import_approve_stage6_rates(
    session: Session,
    reviewed_by: str,
    attestation: str,
    codes: Optional[list[str]] = None,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    authority_citation: str = DEFAULT_AUTHORITY_CITATION,
    staging_db: Path = STAGING_DB_PATH,
) -> dict[str, Any]:
    """Promote Stage 6 staged_deed_rates into verified runtime DutyRate records."""
    if not staging_db.exists():
        raise FileNotFoundError(f"Staging database not found: {staging_db}")

    normalized_filter = {_normalize_code(c) for c in codes} if codes else None
    conn = sqlite3.connect(staging_db)
    conn.row_factory = sqlite3.Row
    updated_count = 0
    inserted_count = 0
    promoted_codes: list[str] = []

    try:
        staged_rows = conn.execute(
            "SELECT * FROM staged_deed_rates ORDER BY deed_code"
        ).fetchall()

        for row in staged_rows:
            norm_code = _normalize_code(row["deed_code"])
            if normalized_filter is not None and norm_code not in normalized_filter:
                continue

            eff_from = (
                date.fromisoformat(row["effective_from"])
                if row["effective_from"]
                else effective_from
            )
            eff_to = date.fromisoformat(row["effective_to"]) if row["effective_to"] else None
            area_type = row["area_type"] if row["area_type"] and row["area_type"] != "UNSPECIFIED" else "ALL"
            if area_type == "ALL" and norm_code == "03_05":
                area_type = "GRAM_PANCHAYAT"
            elif area_type == "ALL" and norm_code == "03_06":
                area_type = "MUNICIPAL_CORP"
            prop_sub = row["property_subtype"] if row["property_subtype"] and row["property_subtype"] != "UNSPECIFIED" else "ALL"
            calc_basis = _infer_calc_basis(norm_code, row["classification"], row["deed_type"], row["calculation_basis"])

            sd_raw = _to_decimal(row["stamp_duty_percent"])
            sd_type = "FIXED" if sd_raw > Decimal("15") else "PERCENT"
            td_pct = _to_decimal(row["transfer_duty_percent"])
            rf_raw = _to_decimal(row["registration_fee_percent"])
            rf_type = "FIXED" if rf_raw > Decimal("10") else "PERCENT"
            rf_min = _to_decimal(row["registration_fee_min"]) if row["registration_fee_min"] is not None else Decimal("0")
            rf_max = _to_decimal(row["registration_fee_max"]) if row["registration_fee_max"] is not None else Decimal("0")
            raw_art = str(row["article_ref"] or "").strip()
            art_ref = raw_art if raw_art.startswith("Art") else (f"Art. {raw_art}" if raw_art else "Schedule I-A")
            if raw_art == "31" and norm_code.startswith("07_"):
                calc_basis = "AAR"

            existing = (
                session.query(DutyRate)
                .filter(
                    DutyRate.instrument_code == norm_code,
                    DutyRate.area_type == area_type,
                    DutyRate.property_subtype == prop_sub,
                    DutyRate.effective_to.is_(None),
                )
                .first()
            )
            if not existing and area_type == "ALL":
                existing = (
                    session.query(DutyRate)
                    .filter(
                        DutyRate.instrument_code == norm_code,
                        DutyRate.property_subtype == prop_sub,
                        DutyRate.effective_to.is_(None),
                    )
                    .first()
                )
            audit_note = (
                f"[{norm_code}] {row['classification']} - {row['deed_type']} (Book {row['book_number']}) | "
                f"Stage 6 promoted by {reviewed_by} on {date.today().isoformat()}"
            )
            if existing:
                existing.verification_status = "VERIFIED"
                if not existing.notes:
                    existing.notes = audit_note
                updated_count += 1
            else:
                combined_cit, sd_auth, td_auth, rf_auth = _default_authorities_for_code(norm_code, art_ref)
                adj_pct = Decimal("4") if norm_code in {"01_02", "01_11"} else Decimal("0")
                rate = DutyRate(
                    instrument_code=norm_code,
                    article_ref=art_ref,
                    area_type=area_type,
                    property_subtype=prop_sub,
                    calc_basis=calc_basis,
                    stamp_duty_type=sd_type,
                    stamp_duty_value=sd_raw,
                    transfer_duty_pct=td_pct,
                    reg_fee_type=rf_type,
                    reg_fee_value=rf_raw,
                    reg_fee_min=rf_min,
                    reg_fee_max=rf_max,
                    adjustable_sd_pct=adj_pct,
                    effective_from=eff_from,
                    effective_to=eff_to,
                    authority_citation=combined_cit if authority_citation == DEFAULT_AUTHORITY_CITATION else authority_citation,
                    sd_authority=sd_auth,
                    td_authority=td_auth,
                    rf_authority=rf_auth,
                    verification_status="VERIFIED",
                    notes=audit_note,
                )
                session.add(rate)
                inserted_count += 1

            meta = {}
            try:
                meta = json.loads(row["row_metadata"] or "{}")
            except (TypeError, json.JSONDecodeError):
                meta = {}
            meta["approved_by"] = reviewed_by
            meta["approved_on"] = date.today().isoformat()
            meta["attestation"] = attestation
            meta["not_for_calculation"] = False
            conn.execute(
                "UPDATE staged_deed_rates SET review_status = 'APPROVED', row_metadata = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), row["id"]),
            )
            promoted_codes.append(norm_code)

        session.commit()
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "APPROVED",
        "reviewed_by": reviewed_by,
        "promoted_count": len(promoted_codes),
        "updated_runtime_rows": updated_count,
        "inserted_runtime_rows": inserted_count,
        "codes": promoted_codes,
    }
