"""
Delayed Presentation / Appearance Fine Calculator.

Implements Registration Act Sections 23, 25, 34, S.O. 240 & Rule 38:
- Normal presentation window: 4 months from execution date (Sec. 23)
- DR condonation window: up to 8 months total from execution (4m normal + 4m delay, Sec. 25)
- Appearance condonation: up to 12 months total (only if presented within 8 months, Sec. 34)
- Beyond 8 months presentation: VOID / MANDATORY REFUSAL (not curable u/s 87; S.O. 240)

Fine tiers (on proper registration fee under Rule 38):
  <= 1 week late    : 1x RF
  > 1 week to 1 mo  : 2x RF
  > 1 to 2 months   : 5x RF
  > 2 to 4 months   : 10x RF
"""
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class FineResult:
    status: str  # "OK", "NO_FINE", "VOID"
    days_late: int = 0
    fine_multiplier: int = 0
    proper_registration_fee: Decimal = Decimal("0")
    fine_amount: Decimal = Decimal("0")
    condoning_authority: str = ""
    message: str = ""
    legal_basis: str = ""


def _round(val: Decimal) -> Decimal:
    return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    """Add exact calendar months, clamping to month-end when needed."""
    total_months = (d.year * 12 + (d.month - 1)) + months
    year, month0 = divmod(total_months, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def calculate_delay_fine(
    execution_date: date,
    presentation_date: date,
    proper_registration_fee: Decimal,
    is_appearance: bool = False,
) -> FineResult:
    """
    Calculate the fine for delayed presentation or appearance.

    Args:
        execution_date: Date the instrument was executed
        presentation_date: Date actually presented for registration
        proper_registration_fee: The registration fee that was due
        is_appearance: If True, the 12-month appearance limit (Sec. 34) applies instead of 8

    Returns:
        FineResult with the exact fine calculation
    """
    result = FineResult(status="OK")
    result.proper_registration_fee = proper_registration_fee

    # Normal window: exact 4 calendar months (Sec. 23)
    normal_deadline = add_months(execution_date, 4)

    # Max allowed: 8 calendar months for initial presentation (4+4 u/s 25),
    # 12 calendar months for appearance (u/s 34, only if validly presented within 8 months)
    max_months = 12 if is_appearance else 8
    max_deadline = add_months(execution_date, max_months)

    if presentation_date <= normal_deadline:
        result.status = "NO_FINE"
        result.condoning_authority = "Sub-Registrar (Routine Admission u/s 23)"
        result.message = "Presented within 4-month normal window. No fine applicable."
        result.legal_basis = "Section 23, Registration Act 1908"
        return result

    days_late = (presentation_date - normal_deadline).days
    result.days_late = days_late

    if presentation_date > max_deadline:
        result.status = "VOID"
        result.condoning_authority = "None — District Registrar CANNOT condone"
        if is_appearance:
            result.message = (
                f"Appearance delayed {days_late} days past the 4-month window, exceeding the "
                "maximum 12-month appearance ceiling u/s 34. Acceptance is INVALID/VOID u/s 87 (S.O. 240)."
            )
            result.legal_basis = (
                "Section 34 & Section 87, Registration Act 1908; Standing Order 240 — "
                "appearance cannot be condoned beyond 12 months"
            )
        else:
            result.message = (
                f"Presented {days_late} days past the 4-month normal period, exceeding the "
                "maximum 8-month presentation limit (4 months u/s 23 + max 4 months u/s 25). "
                "District Registrar CANNOT condone initial presentation beyond 8 months; acceptance is "
                "VOID and incurable under Section 87 (Standing Order 240)."
            )
            result.legal_basis = (
                "Sections 23, 25 & 87, Registration Act 1908; Standing Order 240 "
                "(12-month ceiling u/s 34 applies only to appearance if already presented within 8 months)"
            )
        return result

    one_month_cutoff = add_months(normal_deadline, 1)
    two_month_cutoff = add_months(normal_deadline, 2)
    if days_late <= 7:
        multiplier = 1
        tier = "≤ 7 days (up to 1 week)"
    elif presentation_date <= one_month_cutoff:
        multiplier = 2
        tier = "> 7 days to 1 calendar month"
    elif presentation_date <= two_month_cutoff:
        multiplier = 5
        tier = "> 1 month to 2 calendar months"
    else:
        multiplier = 10
        tier = "> 2 months to 4 calendar months"

    sec_ref = "Section 34" if is_appearance else "Section 25(1)"
    result.fine_multiplier = multiplier
    result.fine_amount = _round(proper_registration_fee * Decimal(str(multiplier)))
    result.condoning_authority = f"District Registrar (Registrar) under {sec_ref} & Rule 38"
    result.message = (
        f"Delay: {days_late} days past the 4-month normal window ({tier}). "
        f"Fine = {multiplier}× proper registration fee = ₹{result.fine_amount:,}. "
        f"Condonation approved by District Registrar under {sec_ref} read with Rule 38."
    )
    result.legal_basis = f"{sec_ref}, Registration Act 1908 read with AP Registration Rule 38"

    return result
