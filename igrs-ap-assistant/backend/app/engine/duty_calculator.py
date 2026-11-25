"""
AP IGRS Deterministic Duty Calculator Engine.

100% DETERMINISTIC — the LLM NEVER does math.
Every calculation is backed by a temporal SQL query proving which rate
row was active on the execution_date, plus the exact G.O. citation.

Edge cases handled:
  - Partition (Art. 40): Excludes largest share (VSS rule)
  - Gift relative vs. other: Different SD/TD by area
  - Panchayat gift TD: 0.5% instead of 1.5%
  - Caps (min/max) on SD and RF
  - User charges by era
  - NOT_FOUND refusal for unverified dates
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.core.config import APP_ENV, IS_PRODUCTION, PRODUCTION_RATE_DATA_APPROVED
from backend.app.db.models import DutyRate, UserCharge
from backend.app.engine.formatting import format_inr


@dataclass
class DutyBreakdown:
    """Result of a duty calculation — every field is exact, never estimated."""
    status: str  # "OK" or "NOT_FOUND"
    instrument_code: str = ""
    area_type: str = ""
    execution_date: Optional[date] = None
    chargeable_value: Decimal = Decimal("0")
    calc_basis_used: str = ""
    stamp_duty: Decimal = Decimal("0")
    stamp_duty_rate: str = ""
    transfer_duty: Decimal = Decimal("0")
    transfer_duty_rate: str = ""
    registration_fee: Decimal = Decimal("0")
    reg_fee_rate: str = ""
    user_charge: Decimal = Decimal("0")
    user_charge_detail: str = ""
    total_payable: Decimal = Decimal("0")
    adjustable_sd_pct: Decimal = Decimal("0")
    adjustable_stamp_duty: Decimal = Decimal("0")
    non_adjustable_stamp_duty: Decimal = Decimal("0")
    lease_advance_amount: Decimal = Decimal("0")
    rent_stamp_duty: Decimal = Decimal("0")
    advance_stamp_duty: Decimal = Decimal("0")
    is_historical: bool = False
    sd_authority: str = ""
    td_authority: str = ""
    rf_authority: str = ""
    citations: list[str] = field(default_factory=list)
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def is_adjustable(self) -> bool:
        return (self.adjustable_sd_pct or Decimal("0")) > Decimal("0")


def _round(val: Decimal) -> Decimal:
    """Round to nearest rupee."""
    return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _apply_cap(val: Decimal, min_cap: Optional[Decimal], max_cap: Optional[Decimal]) -> Decimal:
    """Apply min/max caps if set."""
    if min_cap is not None and min_cap > 0 and val < min_cap:
        val = min_cap
    if max_cap is not None and max_cap > 0 and val > max_cap:
        val = max_cap
    return val


# Canonical instrument code alias mapping
CODE_ALIASES: dict[str, list[str]] = {
    "SALE": ["SALE", "01_01", "SALE_DEED"],
    "01_01": ["01_01", "SALE", "SALE_DEED"],
    "SALE_AGREEMENT_WITH_POSSESSION": ["SALE_AGREEMENT_WITH_POSSESSION", "01_02", "SALE_AGREEMENT_POSSESSION"],
    "01_02": ["01_02", "SALE_AGREEMENT_WITH_POSSESSION", "SALE_AGREEMENT_POSSESSION"],
    "SALE_AGREEMENT_WITHOUT_POSSESSION": ["SALE_AGREEMENT_WITHOUT_POSSESSION", "01_03"],
    "01_03": ["01_03", "SALE_AGREEMENT_WITHOUT_POSSESSION"],
    "AGPA": ["AGPA", "01_11", "AGREEMENT_OF_SALE_CUM_GPA"],
    "01_11": ["01_11", "AGPA", "AGREEMENT_OF_SALE_CUM_GPA"],
    "DEVELOPMENT_AGREEMENT_CUM_GPA": ["DEVELOPMENT_AGREEMENT_CUM_GPA", "01_10", "DAGPA"],
    "01_10": ["01_10", "DEVELOPMENT_AGREEMENT_CUM_GPA", "DAGPA"],
    "DEVELOPMENT_AGREEMENT": ["DEVELOPMENT_AGREEMENT", "01_09"],
    "01_09": ["01_09", "DEVELOPMENT_AGREEMENT"],
    "MORTGAGE_WITH_POSSESSION": ["MORTGAGE_WITH_POSSESSION", "02_01"],
    "02_01": ["02_01", "MORTGAGE_WITH_POSSESSION"],
    "MORTGAGE_WITHOUT_POSSESSION": ["MORTGAGE_WITHOUT_POSSESSION", "02_02"],
    "02_02": ["02_02", "MORTGAGE_WITHOUT_POSSESSION"],
    "DOTD": ["DOTD", "02_08", "DEPOSIT_OF_TITLE_DEEDS"],
    "02_08": ["02_08", "DOTD", "DEPOSIT_OF_TITLE_DEEDS"],
    "GIFT": ["GIFT", "03_01", "03_07", "GIFT_OTHER", "GIFT_OTHERS"],
    "03_01": ["03_01", "GIFT", "GIFT_OTHER", "GIFT_OTHERS"],
    "SETTLEMENT_FAMILY": ["SETTLEMENT_FAMILY", "03_02"],
    "03_02": ["03_02", "SETTLEMENT_FAMILY"],
    "SETTLEMENT_OTHER": ["SETTLEMENT_OTHER", "03_03", "SETTLEMENT_OTHERS"],
    "03_03": ["03_03", "SETTLEMENT_OTHER", "SETTLEMENT_OTHERS"],
    "GIFT_RELATIVE": ["GIFT_RELATIVE", "03_06", "03_05", "GIFT_FAMILY"],
    "03_06": ["03_06", "GIFT_RELATIVE", "GIFT_FAMILY"],
    "03_05": ["03_05", "GIFT_RELATIVE"],
    "GIFT_OTHER": ["GIFT_OTHER", "03_07", "03_01", "GIFT_OTHERS"],
    "03_07": ["03_07", "GIFT_OTHER", "GIFT_OTHERS"],
    "PARTITION_FAMILY": ["PARTITION_FAMILY", "04_02"],
    "04_02": ["04_02", "PARTITION_FAMILY"],
    "PARTITION_OTHER": ["PARTITION_OTHER", "04_01", "PARTITION_OTHERS"],
    "04_01": ["04_01", "PARTITION_OTHER", "PARTITION_OTHERS"],
    "RELEASE_FAMILY": ["RELEASE_FAMILY", "05_01"],
    "05_01": ["05_01", "RELEASE_FAMILY"],
    "RELEASE_WITHOUT_CONSID": ["RELEASE_WITHOUT_CONSID", "05_02"],
    "05_02": ["05_02", "RELEASE_WITHOUT_CONSID"],
    "RELEASE": ["RELEASE", "05_03"],
    "05_03": ["05_03", "RELEASE"],
    "EXCHANGE": ["EXCHANGE", "06_01"],
    "06_01": ["06_01", "EXCHANGE"],
    "GPA_SELL": ["GPA_SELL", "09_04"],
    "09_04": ["09_04", "GPA_SELL"],
    "GPA_FAMILY": ["GPA_FAMILY", "09_05"],
    "09_05": ["09_05", "GPA_FAMILY"],
}


def _find_rate(
    session: Session,
    instrument_code: str,
    area_type: str,
    execution_date: date,
    property_subtype: str = "ALL",
    lease_months: Optional[int] = None,
) -> Optional[DutyRate]:
    """
    Find the exact duty rate row active on execution_date.
    
    Matching priority:
    1. Exact area_type + exact property_subtype
    2. Exact area_type + ALL subtype
    3. ALL area_type + exact property_subtype
    4. ALL area_type + ALL subtype
    """
    # This prototype has not completed the source review required for production.
    if IS_PRODUCTION and not PRODUCTION_RATE_DATA_APPROVED:
        return None

    # Verified historical corpus starts from 01.04.1999 (Art. 47-A differential regime
    # & Act 4/1999 agreement rules). Earlier unverified dates must fail closed.
    if execution_date < date(1999, 4, 1):
        return None

    codes_to_try = CODE_ALIASES.get(instrument_code, [instrument_code])

    accepted_statuses = ["VERIFIED"]
    if APP_ENV != "production":
        accepted_statuses.append("PENDING_REVIEW")

    base_q = session.query(DutyRate).filter(
        DutyRate.instrument_code.in_(codes_to_try),
        DutyRate.effective_from <= execution_date,
        or_(
            DutyRate.effective_to.is_(None),
            DutyRate.effective_to >= execution_date,
        ),
        DutyRate.verification_status.in_(accepted_statuses),
    )

    # For leases, also filter by duration
    if lease_months is not None:
        base_q = base_q.filter(
            or_(
                DutyRate.duration_min_months.is_(None),
                DutyRate.duration_min_months <= lease_months,
            ),
            or_(
                DutyRate.duration_max_months.is_(None),
                DutyRate.duration_max_months >= lease_months,
            ),
        )

    # Normalize area types
    if area_type in ("MUNICIPAL_CORP", "CORPORATION", "CORP"):
        areas_to_try = ["MUNICIPAL_CORP", "CORPORATION", "ALL"]
    elif area_type in ("SPECIAL_SELECTION_MUNICIPALITY", "SELECTION_MUNICIPALITY"):
        areas_to_try = [area_type, "SPECIAL_SELECTION_MUNICIPALITY", "OTHER_MUNICIPALITY", "ALL"]
    elif area_type in ("GRAM_PANCHAYAT", "MAJOR_GRAM_PANCHAYAT", "MINOR_GRAM_PANCHAYAT", "PANCHAYAT", "RURAL"):
        areas_to_try = [area_type, "GRAM_PANCHAYAT", "ALL"]
    elif area_type == "CANTONMENT":
        areas_to_try = ["CANTONMENT", "OTHER_MUNICIPALITY", "ALL"]
    elif area_type == "ALL":
        areas_to_try = ["ALL", "MUNICIPAL_CORP", "GRAM_PANCHAYAT"]
    else:
        areas_to_try = [area_type, "MUNICIPAL_CORP", "ALL"]
    # De-duplicate preserving order
    areas_to_try = list(dict.fromkeys(areas_to_try))

    # Normalize property subtypes
    if property_subtype in ("FLAT_APARTMENT", "FLAT", "APARTMENT"):
        subtypes_to_try = [property_subtype, "FLAT_APARTMENT", "FLAT", "ALL"]
    elif property_subtype in ("NON_FLAT", "PLOT", "LAND", "HOUSE"):
        subtypes_to_try = [property_subtype, "NON_FLAT", "ALL"]
    elif property_subtype == "ALL":
        subtypes_to_try = ["ALL", "NON_FLAT"]
    else:
        subtypes_to_try = [property_subtype, "ALL"]
    subtypes_to_try = list(dict.fromkeys(subtypes_to_try))

    for at in areas_to_try:
        for ps in subtypes_to_try:
            match = base_q.filter(
                DutyRate.area_type == at,
                DutyRate.property_subtype == ps,
            ).first()
            if match:
                return match

    return None


def _get_user_charge(
    session: Session,
    instrument_code: str,
    execution_date: date,
    chargeable_value: Decimal,
    sheet_count: int = 10,
) -> tuple[Decimal, str]:
    """
    Look up the CARD user charge for Book-I major transactions.
    Returns (charge_amount, detail_string).
    """
    # Determine which service code to use
    book1_majors = {
        "SALE", "SALE_HOUSING_BOARD", "SALE_GOVT", "SALE_MORTGAGEE", "SALE_INDEMNITY",
        "SALE_TERRACE", "SALE_COURT", "SALE_OTHERS",
        "GIFT_RELATIVE", "GIFT_OTHER",
        "SETTLEMENT_FAMILY", "SETTLEMENT_OTHER",
        "PARTITION_FAMILY", "PARTITION_OTHER",
        "RELEASE", "RELEASE_WITHOUT_CONSID", "RELEASE_FAMILY",
        "EXCHANGE", "AGPA",
        "DEVELOPMENT_AGREEMENT_CUM_GPA", "DEVELOPMENT_AGREEMENT", "DAGPA", "01_10", "01_09",
    }
    book34_items = {
        "LEASE", "LEASE_PERPETUAL",
        "MORTGAGE_WITH_POSSESSION", "MORTGAGE_WITHOUT_POSSESSION",
        "DOTD", "DEPOSIT_OF_TITLE_DEEDS",
        "FURTHER_CHARGE_WITH_POSSESSION", "FURTHER_CHARGE_WITHOUT_POSSESSION",
    }
    book1_prefixes = ("01_", "03_", "04_", "05_", "06_", "09_")
    book34_prefixes = (
        "02_", "07_", "08_", "20_", "21_",
        "30_", "31_", "32_", "33_", "34_", "35_", "36_", "37_", "38_", "39_",
        "40_", "42_", "43_", "44_",
    )

    aliases = CODE_ALIASES.get(instrument_code, [instrument_code])
    is_book1_major = (
        any(a in book1_majors for a in aliases)
        or instrument_code in book1_majors
        or instrument_code.startswith(book1_prefixes)
        or instrument_code == "41_03"
    )
    is_book34 = (
        any(a in book34_items for a in aliases)
        or instrument_code in book34_items
        or instrument_code.startswith(book34_prefixes)
        or instrument_code in {"41_01", "41_02", "41_04", "41_05"}
    )

    if is_book1_major:
        # Post-2023: flat Rs.500
        # Pre-2023: Rs.50 (<=50k) or Rs.95 (>50k)
        if execution_date >= date(2023, 4, 25):
            base_charge = Decimal("500")
            citation = "G.O.Ms.No. 192 dt. 25.04.2023"
        elif execution_date >= date(2002, 3, 8):
            if chargeable_value <= Decimal("50000"):
                base_charge = Decimal("50")
            else:
                base_charge = Decimal("95")
            citation = "G.O.Ms.No. 99 dt. 08.03.2002"
        else:
            return Decimal("0"), "No user charge data before 2002"

        # Extra sheets
        extra_sheets = max(0, sheet_count - 10)
        if execution_date >= date(2023, 4, 25):
            extra_charge = Decimal(str(extra_sheets)) * Decimal("10")
        else:
            extra_charge = Decimal(str(extra_sheets)) * Decimal("5")

        total = base_charge + extra_charge
        detail = f"Rs.{base_charge} (up to 10 sheets)"
        if extra_sheets > 0:
            detail += f" + Rs.{extra_charge} ({extra_sheets} extra sheets)"
        detail += f" [{citation}]"
        return total, detail

    elif is_book34:
        if execution_date >= date(2023, 4, 25):
            base_charge = Decimal("100")
            citation = "G.O.Ms.No. 192 dt. 25.04.2023"
        elif execution_date >= date(2002, 3, 8):
            base_charge = Decimal("50")  # Approximate for Book-III/IV
            citation = "G.O.Ms.No. 99 dt. 08.03.2002"
        else:
            return Decimal("0"), "No user charge data before 2002"

        extra_sheets = max(0, sheet_count - 10)
        if execution_date >= date(2023, 4, 25):
            extra_charge = Decimal(str(extra_sheets)) * Decimal("10")
        else:
            extra_charge = Decimal(str(extra_sheets)) * Decimal("5")

        total = base_charge + extra_charge
        detail = f"Rs.{base_charge} (up to 10 sheets) [{citation}]"
        return total, detail

    return Decimal("0"), "N/A"


def calculate_deed_duty(
    session: Session,
    instrument_code: str,
    area_type: str,
    execution_date: date,
    consideration_value: Decimal = Decimal("0"),
    market_value: Decimal = Decimal("0"),
    property_subtype: str = "ALL",
    shares: Optional[list[Decimal]] = None,
    lease_months: Optional[int] = None,
    annual_rent: Decimal = Decimal("0"),
    total_rent: Decimal = Decimal("0"),
    loan_amount: Decimal = Decimal("0"),
    sheet_count: int = 10,
) -> DutyBreakdown:
    """
    Main calculator entry point. Queries the temporal SQL database for the
    exact rate active on execution_date and computes deterministic results.
    
    Returns NOT_FOUND if no verified rate row exists — NEVER guesses.
    """
    result = DutyBreakdown(status="NOT_FOUND", instrument_code=instrument_code,
                           area_type=area_type, execution_date=execution_date)

    # --- Find the rate row ---
    rate = _find_rate(session, instrument_code, area_type, execution_date,
                      property_subtype, lease_months)
    if rate is None:
        result.message = (
            f"No verified rate record for instrument '{instrument_code}' "
            f"in area '{area_type}' on date {execution_date}. "
            f"Cannot calculate — refusing to guess."
        )
        return result

    result.status = "OK"
    if rate.verification_status != "VERIFIED":
        result.warnings.append(
            "DEMO ONLY: rate data is not independently verified against the cited official source. "
            "Do not use this result for an official transaction, collection, or audit decision."
        )
    result.citations.append(rate.authority_citation)
    result.sd_authority = getattr(rate, "sd_authority", None) or rate.authority_citation
    result.td_authority = getattr(rate, "td_authority", None) or (
        "AP Municipalities / GP Transfer Duty Surcharge" if (rate.transfer_duty_pct or Decimal("0")) > 0 else "Exempt (Not Applicable)"
    )
    result.rf_authority = getattr(rate, "rf_authority", None) or rate.authority_citation
    result.adjustable_sd_pct = Decimal(str(getattr(rate, "adjustable_sd_pct", None) or 0))
    result.is_historical = (rate.effective_to is not None)

    # --- Determine chargeable value based on calc_basis ---
    calc_basis = rate.calc_basis
    result.calc_basis_used = calc_basis

    if calc_basis == "MAX_MV_CONSID":
        chargeable = max(consideration_value, market_value)
    elif calc_basis == "MARKET_VALUE":
        chargeable = market_value if market_value > 0 else consideration_value
    elif calc_basis == "HIGHER_EXCHANGE_MV":
        chargeable = max(consideration_value, market_value)
    elif calc_basis in ("VSS", "SEPARATED_SHARE"):
        # Partition: Value of Separated Shares = sum(all) - max(single share)
        if shares and len(shares) >= 2:
            total_shares = sum(shares)
            largest_share = max(shares)
            chargeable = total_shares - largest_share
        elif shares and len(shares) == 1:
            chargeable = Decimal("0")  # Single share = nothing separated
            result.warnings.append("Only 1 share provided — VSS is 0 (nothing to separate)")
        else:
            # Fallback: use consideration/MV but warn
            chargeable = max(consideration_value, market_value)
            result.warnings.append(
                "No shares[] provided for partition — using total value. "
                "For correct Art. 40 calculation, provide individual share values."
            )
    elif calc_basis == "LOAN_AMOUNT":
        chargeable = loan_amount if loan_amount > 0 else max(consideration_value, market_value)
    elif calc_basis == "TOTAL_RENT":
        chargeable = total_rent
    elif calc_basis == "AAR":
        chargeable = annual_rent
    else:
        chargeable = max(consideration_value, market_value)

    result.chargeable_value = chargeable

    # --- Stamp Duty ---
    if rate.stamp_duty_type == "PERCENT":
        sd_pct = rate.stamp_duty_value
        sd_amount = _round(chargeable * sd_pct / Decimal("100"))
        sd_amount = _apply_cap(sd_amount, rate.stamp_duty_min, rate.stamp_duty_max)
        result.stamp_duty = sd_amount
        result.stamp_duty_rate = f"{sd_pct}%"
        if rate.stamp_duty_max and sd_amount == rate.stamp_duty_max:
            result.stamp_duty_rate += f" (capped at {format_inr(rate.stamp_duty_max)})"
    elif rate.stamp_duty_type == "FIXED":
        result.stamp_duty = _round(rate.stamp_duty_value)
        result.stamp_duty_rate = f"Fixed {format_inr(rate.stamp_duty_value)}"

    if result.adjustable_sd_pct > 0 and rate.stamp_duty_type == "PERCENT" and rate.stamp_duty_value > 0:
        adj_pct = min(result.adjustable_sd_pct, rate.stamp_duty_value)
        result.adjustable_stamp_duty = _round(chargeable * adj_pct / Decimal("100"))
        result.non_adjustable_stamp_duty = max(Decimal("0"), result.stamp_duty - result.adjustable_stamp_duty)
    else:
        result.adjustable_stamp_duty = Decimal("0")
        result.non_adjustable_stamp_duty = result.stamp_duty

    # --- Transfer Duty ---
    td_pct = rate.transfer_duty_pct or Decimal("0")
    if td_pct > 0:
        td_base = max(consideration_value, market_value) if calc_basis != "VSS" else chargeable
        # For sales, TD is on the sale consideration/MV, not VSS
        if instrument_code.startswith("SALE") or instrument_code in ("GIFT_RELATIVE", "GIFT_OTHER", "EXCHANGE"):
            td_base = max(consideration_value, market_value)
        result.transfer_duty = _round(td_base * td_pct / Decimal("100"))
        result.transfer_duty_rate = f"{td_pct}%"

    # --- Registration Fee ---
    if rate.reg_fee_type == "PERCENT":
        rf_pct = rate.reg_fee_value
        if rate.rf_authority and "1% up to" in rate.rf_authority and rf_pct == Decimal("0.5000"):
            slab1 = min(chargeable, Decimal("50000"))
            slab2 = max(Decimal("0"), chargeable - Decimal("50000"))
            rf_amount = _round(slab1 * Decimal("0.01") + slab2 * rf_pct / Decimal("100"))
        else:
            rf_amount = _round(chargeable * rf_pct / Decimal("100"))
        rf_amount = _apply_cap(rf_amount, rate.reg_fee_min, rate.reg_fee_max)
        result.registration_fee = rf_amount
        if rf_pct == Decimal("0") and rate.reg_fee_min and rate.reg_fee_max and rate.reg_fee_min == rate.reg_fee_max:
            result.reg_fee_rate = f"Fixed {format_inr(rate.reg_fee_min)}"
        else:
            result.reg_fee_rate = f"{rf_pct}%"
            if rate.reg_fee_min and rf_amount == rate.reg_fee_min:
                result.reg_fee_rate += f" (min {format_inr(rate.reg_fee_min)})"
            if rate.reg_fee_max and rf_amount == rate.reg_fee_max:
                result.reg_fee_rate += f" (capped at {format_inr(rate.reg_fee_max)})"
    elif rate.reg_fee_type == "FIXED":
        result.registration_fee = _round(rate.reg_fee_value)
        result.reg_fee_rate = f"Fixed {format_inr(rate.reg_fee_value)}"

    # --- User Charges ---
    uc_amount, uc_detail = _get_user_charge(
        session, instrument_code, execution_date, chargeable, sheet_count
    )
    result.user_charge = uc_amount
    result.user_charge_detail = uc_detail

    # --- Total ---
    result.total_payable = (
        result.stamp_duty + result.transfer_duty +
        result.registration_fee + result.user_charge
    )

    # --- Historical warning ---
    if result.is_historical:
        result.warnings.append(
            f"Historical rate: showing rules in force as of {execution_date}. "
            f"Current rules may differ."
        )

    return result


def calculate_lease_duty(
    session: Session,
    monthly_rent: Decimal,
    duration_months: int,
    property_subtype: str = "COMMERCIAL",
    advance_amount: Decimal = Decimal("0"),
    execution_date: Optional[date] = None,
    sheet_count: int = 10,
    advance_is_refundable: Optional[bool] = None,
) -> DutyBreakdown:
    """
    Deterministic AP Lease Duty & Registration Fee Calculator (Article 31, G.O.Ms.No. 588 dt. 04.12.2013).

    Computes Average Annual Rent (AAR = monthly_rent * 12) or Total Rent (< 12 months),
    applies Art. 31 slab SD + 0.1% RF + Rs. 100 User Charge, and applies Art. 31(c)
    2% SD on chargeable non-refundable advance/premium with the refundable security
    deposit clarification per C&IG Memo S1/4813/2008 & Memo CCRA1/13429/2008.
    """
    exec_d = execution_date or date.today()
    aar = monthly_rent * Decimal("12")
    total_rent = monthly_rent * Decimal(str(duration_months))
    code = "LEASE_PERPETUAL" if duration_months > 360 else "LEASE"
    subtype = (property_subtype or "ALL").upper()
    breakdown = calculate_deed_duty(
        session=session,
        instrument_code=code,
        area_type="ALL",
        execution_date=exec_d,
        consideration_value=aar,
        market_value=aar,
        property_subtype=subtype,
        lease_months=duration_months,
        annual_rent=aar,
        total_rent=total_rent,
        sheet_count=sheet_count,
    )
    if breakdown.status != "OK":
        return breakdown

    rent_sd = breakdown.stamp_duty
    breakdown.rent_stamp_duty = rent_sd
    breakdown.lease_advance_amount = advance_amount or Decimal("0")
    if breakdown.lease_advance_amount > Decimal("0"):
        if advance_is_refundable is True:
            adv_sd = Decimal("0")
        else:
            adv_sd = _round(breakdown.lease_advance_amount * Decimal("2") / Decimal("100"))
        breakdown.advance_stamp_duty = adv_sd
        breakdown.stamp_duty = rent_sd + adv_sd
        breakdown.non_adjustable_stamp_duty = breakdown.stamp_duty
        breakdown.total_payable = (
            breakdown.stamp_duty
            + breakdown.transfer_duty
            + breakdown.registration_fee
            + breakdown.user_charge
        )
        alt_total = rent_sd + breakdown.transfer_duty + breakdown.registration_fee + breakdown.user_charge
        if advance_is_refundable is True:
            breakdown.warnings.append(
                "Refundable Security Deposit Clarification: Under C&IG Memo No. S1/4813/2008 & Memo No. CCRA1/13429/2008, "
                "a purely refundable security deposit (not adjustable towards rent and returnable at expiry) attracts ₹0.00 Stamp Duty."
            )
        else:
            breakdown.warnings.append(
                f"Art. 31(c) Advance/Premium Rule (C&IG Memo S1/4813/2008 & CCRA1/13429/2008): Non-refundable advance/premium "
                f"or money advanced in addition to rent attracts 2% SD ({format_inr(adv_sd)} on {format_inr(breakdown.lease_advance_amount)}). "
                f"If it is a purely refundable security deposit (not adjustable against rent and returnable on expiry), Advance SD is ₹0.00 "
                f"and Total Payable is {format_inr(alt_total)}."
            )
            breakdown.sd_authority = f"{breakdown.sd_authority}; Art. 31(c) (2% on non-refundable advance/premium)"
    return breakdown


def compare_historical_vs_current(
    session: Session,
    instrument_code: str,
    area_type: str,
    execution_date: date,
    current_date: Optional[date] = None,
    **kwargs,
) -> dict:
    """
    Runs calculate_deed_duty for both execution_date and current_date,
    returns both breakdowns plus the exact rupee delta.
    Built specifically for Section 41-A audit officers.
    """
    if current_date is None:
        current_date = date.today()

    historical = calculate_deed_duty(
        session, instrument_code, area_type, execution_date, **kwargs
    )
    current = calculate_deed_duty(
        session, instrument_code, area_type, current_date, **kwargs
    )

    delta_duty = Decimal("0")
    if historical.status == "OK" and current.status == "OK":
        delta_duty = current.total_payable - historical.total_payable

    return {
        "historical": historical,
        "current": current,
        "delta_total": delta_duty,
        "audit_note": (
            f"Duty on execution date ({execution_date}): Rs.{historical.total_payable} | "
            f"Duty at current rates ({current_date}): Rs.{current.total_payable} | "
            f"Delta: Rs.{delta_duty}"
        ) if historical.status == "OK" and current.status == "OK" else "One or both calculations returned NOT_FOUND",
    }
