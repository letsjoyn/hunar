"""
Delayed Presentation / Appearance Fine Calculator.

Implements Registration Act Sections 23, 25, 34 & Rule 38:
- Normal window: 4 months from execution date
- DR condonation: up to 8 months for presentation
- Appearance condonation: up to 12 months
- Beyond 8 months presentation: VOID / REFUSE

Fine tiers (on proper registration fee):
  <= 1 week late    : 1x RF
  > 1 week to 1 mo  : 2x RF
  > 1 to 2 months   : 5x RF
  > 2 to 4 months   : 10x RF
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class FineResult:
    status: str  # "OK", "NO_FINE", "VOID"
    days_late: int = 0
    fine_multiplier: int = 0
    proper_registration_fee: Decimal = Decimal("0")
    fine_amount: Decimal = Decimal("0")
    message: str = ""
    legal_basis: str = ""


def _round(val: Decimal) -> Decimal:
    return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


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
        is_appearance: If True, the 12-month limit applies instead of 8
    
    Returns:
        FineResult with the exact fine calculation
    """
    result = FineResult(status="OK")
    result.proper_registration_fee = proper_registration_fee

    # Normal window: 4 months
    normal_deadline = execution_date + timedelta(days=4 * 30)  # ~4 months
    
    # Max allowed: 8 months for presentation, 12 for appearance
    max_months = 12 if is_appearance else 8
    max_deadline = execution_date + timedelta(days=max_months * 30)

    # Check if within normal window
    if presentation_date <= normal_deadline:
        result.status = "NO_FINE"
        result.message = "Presented within 4-month normal window. No fine applicable."
        result.legal_basis = "Section 23, Registration Act 1908"
        return result

    # Check if beyond maximum allowed
    if presentation_date > max_deadline:
        result.status = "VOID"
        result.days_late = (presentation_date - normal_deadline).days
        result.message = (
            f"Presented {result.days_late} days after the 4-month window, "
            f"exceeding the maximum {max_months}-month limit. "
            f"Registration MUST BE REFUSED."
        )
        result.legal_basis = (
            "Section 25(1) & Section 34, Registration Act 1908 — "
            "document cannot be accepted after 8 months (presentation) / 12 months (appearance)"
        )
        return result

    # Calculate delay from normal deadline
    days_late = (presentation_date - normal_deadline).days
    result.days_late = days_late

    # Determine fine tier
    if days_late <= 7:
        multiplier = 1
        tier = "≤ 1 week"
    elif days_late <= 30:
        multiplier = 2
        tier = "> 1 week to 1 month"
    elif days_late <= 60:
        multiplier = 5
        tier = "> 1 to 2 months"
    elif days_late <= 120:
        multiplier = 10
        tier = "> 2 to 4 months"
    else:
        multiplier = 10
        tier = "> 4 months (at maximum tier)"

    result.fine_multiplier = multiplier
    result.fine_amount = _round(proper_registration_fee * Decimal(str(multiplier)))
    result.message = (
        f"Delay: {days_late} days past 4-month window ({tier}). "
        f"Fine = {multiplier}x registration fee = Rs.{result.fine_amount}"
    )
    result.legal_basis = "Section 25 & Rule 38, Registration Act 1908"

    return result
