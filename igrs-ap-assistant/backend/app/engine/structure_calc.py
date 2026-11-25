"""
Building Structure Value Calculator.

Handles:
- 2008 and 2023 structure rate eras
- Unfinished structure percentages (Foundation 25%, Slab 65%, Finishing 85%)
- Depreciation: Non-industrial (1%/yr after 10 yrs, max 70%) and Industrial (5%/yr, max 50%)
- Composite apartment rate formula: base_rcc_rate + (1/20 * site_rate_per_sqyd)
- Floor adders from 3rd floor onward (2023 rates)
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.core.config import APP_ENV, IS_PRODUCTION, PRODUCTION_RATE_DATA_APPROVED
from backend.app.db.models import StructureRate


@dataclass
class BuildingValueResult:
    status: str  # "OK" or "NOT_FOUND"
    base_rate_per_sqft: Decimal = Decimal("0")
    floor_adder: Decimal = Decimal("0")
    effective_rate: Decimal = Decimal("0")
    gross_value: Decimal = Decimal("0")
    completion_factor: Decimal = Decimal("1")
    after_completion: Decimal = Decimal("0")
    depreciation_pct: Decimal = Decimal("0")
    depreciation_amount: Decimal = Decimal("0")
    net_building_value: Decimal = Decimal("0")
    composite_site_adder: Decimal = Decimal("0")
    citation: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)


def _round(val: Decimal) -> Decimal:
    return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def calculate_building_value(
    session: Session,
    structure_category: str,
    area_tier: str,
    plinth_area_sqft: Decimal,
    floor_number: int,
    age_years: int,
    is_industrial: bool,
    execution_date: date,
    completion_stage: str = "COMPLETE",  # FOUNDATION, SLAB, FINISHING, COMPLETE
    site_rate_per_sqyd: Optional[Decimal] = None,
    is_apartment: bool = False,
) -> BuildingValueResult:
    """Calculate the market value of a building structure."""
    result = BuildingValueResult(status="NOT_FOUND")

    if IS_PRODUCTION and not PRODUCTION_RATE_DATA_APPROVED:
        result.message = "No production-approved structure-rate dataset is configured."
        return result

    # Find structure rate active on execution_date
    rate = session.query(StructureRate).filter(
        StructureRate.structure_category == structure_category,
        StructureRate.area_tier == area_tier,
        StructureRate.effective_from <= execution_date,
        or_(
            StructureRate.effective_to.is_(None),
            StructureRate.effective_to >= execution_date,
        ),
    ).first()

    if rate is None:
        result.message = (
            f"No structure rate for '{structure_category}' in '{area_tier}' "
            f"on {execution_date}."
        )
        return result

    result.status = "OK"
    if APP_ENV != "production":
        result.warnings.append(
            "DEMO ONLY: structure-rate data has not been independently verified. "
            "Do not use this result for an official transaction or valuation."
        )
    result.citation = rate.authority_citation
    base_rate = rate.rate_per_sqft
    result.base_rate_per_sqft = base_rate

    # Floor adder (from 3rd floor onward in 2023 rates)
    adder = Decimal("0")
    if rate.extra_floor_adder and rate.extra_floor_adder > 0 and floor_number >= 3:
        floors_above_2 = floor_number - 2
        adder = rate.extra_floor_adder * Decimal(str(floors_above_2))
    result.floor_adder = adder

    # Composite apartment rate
    site_adder = Decimal("0")
    if is_apartment and site_rate_per_sqyd is not None and site_rate_per_sqyd > 0:
        # 1 sq.yd = 9 sq.ft, so site_rate per sq.ft = site_rate_per_sqyd / 9
        # But the formula is: base_rcc + (1/20 * site_rate_per_sqyd)
        # The 1/20 is applied to site_rate_per_sqyd (per sq.yd), result added per sq.ft
        site_adder = site_rate_per_sqyd / Decimal("20")
        result.composite_site_adder = _round(site_adder)

    effective_rate = base_rate + adder + site_adder
    result.effective_rate = _round(effective_rate)

    # Gross value
    gross = effective_rate * plinth_area_sqft
    result.gross_value = _round(gross)

    # Completion factor
    completion_factors = {
        "FOUNDATION": Decimal("0.25"),
        "SLAB": Decimal("0.65"),
        "FINISHING": Decimal("0.85"),
        "COMPLETE": Decimal("1"),
    }
    comp_factor = completion_factors.get(completion_stage.upper(), Decimal("1"))
    result.completion_factor = comp_factor
    after_comp = gross * comp_factor
    result.after_completion = _round(after_comp)

    # Depreciation
    if is_industrial:
        # Industrial: 5% per year from year 1, max 50%
        dep_pct = min(Decimal(str(age_years)) * Decimal("5"), Decimal("50"))
    else:
        # Non-industrial: 0% for 1-10 years, 1% per year over 10, max 70%
        if age_years <= 10:
            dep_pct = Decimal("0")
        else:
            dep_pct = min(Decimal(str(age_years - 10)) * Decimal("1"), Decimal("70"))

    result.depreciation_pct = dep_pct
    dep_amount = after_comp * dep_pct / Decimal("100")
    result.depreciation_amount = _round(dep_amount)

    # Net value
    result.net_building_value = _round(after_comp - dep_amount)

    return result
