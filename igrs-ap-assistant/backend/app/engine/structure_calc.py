"""
Building Structure Value Calculator.

Handles:
- 2008 (`Procgs. No. MV6/10440/2008`), 2023 (`Procgs. MV1/752/2022 dt. 31.05.2023`),
  and Feb 2026+ (`REV08-25025/16/2022-MV-IGRS-Part(1)`) temporal structure rate eras
- Unfinished structure percentages (Foundation 25%, Slab 65%, Finishing 85%)
- Depreciation: Non-industrial (0% 1-10 yrs, 1%/yr above 10 yrs, max 70%)
  and Industrial (5%/yr from year 1, max 50%)
- Composite apartment rate formula: (site_rate_per_sqyd / 20) + base_rcc_rate (+ 3rd+ floor adder)
- Single-floor, multi-floor split, and multi-floor unsplit unit-rate breakdowns
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.core.config import IS_PRODUCTION, PRODUCTION_RATE_DATA_APPROVED
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


@dataclass
class FloorStructureBreakdown:
    floor_number: int
    floor_label: str
    plinth_area_sqft: Optional[Decimal]
    base_rate_per_sqft: Decimal
    floor_adder: Decimal
    composite_site_adder: Decimal
    gross_unit_rate: Decimal
    depreciated_unit_rate: Decimal
    depreciated_structure_unit_rate: Decimal = Decimal("0")
    net_value: Optional[Decimal] = None


@dataclass
class StructureValuationSummary:
    status: str  # "OK" or "NOT_FOUND"
    structure_category: str = ""
    area_tier: str = ""
    execution_date: Optional[date] = None
    total_plinth_area_sqft: Decimal = Decimal("0")
    age_years: int = 0
    is_industrial: bool = False
    is_apartment: bool = False
    completion_stage: str = "COMPLETE"
    completion_factor: Decimal = Decimal("1")
    depreciation_pct: Decimal = Decimal("0")
    site_rate_per_sqyd: Optional[Decimal] = None
    composite_site_adder: Decimal = Decimal("0")
    base_rate_per_sqft: Decimal = Decimal("0")
    needs_floor_split: bool = False
    assumption_note: str = ""
    floor_breakdowns: list[FloorStructureBreakdown] = field(default_factory=list)
    gross_value: Decimal = Decimal("0")
    depreciation_amount: Decimal = Decimal("0")
    net_building_value: Decimal = Decimal("0")
    citation: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)


def _round(val: Decimal) -> Decimal:
    return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _floor_label(floor_number: int) -> str:
    if floor_number <= 0:
        return "Ground Floor"
    if floor_number == 1:
        return "1st Floor"
    if floor_number == 2:
        return "2nd Floor"
    if floor_number == 3:
        return "3rd Floor"
    return f"{floor_number}th Floor"


def _compute_depreciation_pct(age_years: int, is_industrial: bool) -> Decimal:
    safe_age = max(0, int(age_years))
    if is_industrial:
        return min(Decimal(str(safe_age)) * Decimal("5"), Decimal("50"))
    if safe_age <= 10:
        return Decimal("0")
    return min(Decimal(str(safe_age - 10)) * Decimal("1"), Decimal("70"))


def _find_structure_rate(
    session: Session,
    structure_category: str,
    area_tier: str,
    execution_date: date,
) -> Optional[StructureRate]:
    tiers_to_try = [area_tier]
    if area_tier == "MAJOR_GRAM_PANCHAYAT":
        tiers_to_try.append("GRAM_PANCHAYAT")
    elif area_tier == "MINOR_GRAM_PANCHAYAT":
        tiers_to_try.append("GRAM_PANCHAYAT")

    for tier in tiers_to_try:
        hit = session.query(StructureRate).filter(
            StructureRate.structure_category == structure_category,
            StructureRate.area_tier == tier,
            StructureRate.effective_from <= execution_date,
            or_(
                StructureRate.effective_to.is_(None),
                StructureRate.effective_to >= execution_date,
            ),
        ).first()
        if hit is not None:
            return hit
    return None


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
    """Calculate the market value of a building structure for a single floor."""
    result = BuildingValueResult(status="NOT_FOUND")

    if IS_PRODUCTION and not PRODUCTION_RATE_DATA_APPROVED:
        result.message = "No production-approved structure-rate dataset is configured."
        return result

    rate = _find_structure_rate(session, structure_category, area_tier, execution_date)
    if rate is None:
        result.message = (
            f"No structure rate for '{structure_category}' in '{area_tier}' "
            f"on {execution_date}."
        )
        return result

    result.status = "OK"
    result.citation = rate.authority_citation
    base_rate = rate.rate_per_sqft
    result.base_rate_per_sqft = base_rate

    adder_per_floor = rate.extra_floor_adder or Decimal("0")
    if adder_per_floor == 0 and floor_number >= 3:
        fallback_3f = _find_structure_rate(session, "RCC_RESIDENTIAL_3F_PLUS", area_tier, execution_date)
        if fallback_3f and fallback_3f.extra_floor_adder:
            adder_per_floor = fallback_3f.extra_floor_adder

    adder = Decimal("0")
    if adder_per_floor > 0 and floor_number >= 3:
        adder = adder_per_floor * Decimal(str(floor_number - 2))
    result.floor_adder = adder

    site_adder = Decimal("0")
    if is_apartment and site_rate_per_sqyd is not None and site_rate_per_sqyd > 0:
        site_adder = site_rate_per_sqyd / Decimal("20")
        result.composite_site_adder = _round(site_adder)

    effective_rate = base_rate + adder + site_adder
    result.effective_rate = _round(effective_rate)

    structure_gross = (base_rate + adder) * plinth_area_sqft
    land_site_total = site_adder * plinth_area_sqft
    gross = structure_gross + land_site_total
    result.gross_value = _round(gross)

    completion_factors = {
        "FOUNDATION": Decimal("0.25"),
        "SLAB": Decimal("0.65"),
        "FINISHING": Decimal("0.85"),
        "COMPLETE": Decimal("1"),
    }
    comp_factor = completion_factors.get(completion_stage.upper(), Decimal("1"))
    result.completion_factor = comp_factor
    after_comp_structure = structure_gross * comp_factor
    after_comp = after_comp_structure + land_site_total
    result.after_completion = _round(after_comp)

    dep_pct = _compute_depreciation_pct(age_years, is_industrial)
    result.depreciation_pct = dep_pct
    # Land/site component NEVER depreciates; depreciation applies only to structure value.
    dep_amount = after_comp_structure * dep_pct / Decimal("100")
    result.depreciation_amount = _round(dep_amount)
    result.net_building_value = _round(after_comp - dep_amount)

    return result


def calculate_building_structure(
    session: Session,
    structure_category: str = "RCC_RESIDENTIAL_GF_2F",
    area_tier: str = "CORP_MUNI",
    execution_date: Optional[date] = None,
    plinth_area_sqft: Decimal = Decimal("0"),
    floors: Optional[list[int]] = None,
    floor_areas: Optional[list[tuple[int, Decimal]]] = None,
    age_years: int = 0,
    is_industrial: bool = False,
    completion_stage: str = "COMPLETE",
    site_rate_per_sqyd: Optional[Decimal] = None,
    is_apartment: bool = False,
) -> StructureValuationSummary:
    """
    Temporal structure valuation supporting single floors, floor-wise splits,
    composite apartment rates, depreciation, and unsplit multi-floor guidance.
    """
    exec_d = execution_date or date.today()
    summary = StructureValuationSummary(
        status="NOT_FOUND",
        structure_category=structure_category,
        area_tier=area_tier,
        execution_date=exec_d,
        total_plinth_area_sqft=plinth_area_sqft,
        age_years=age_years,
        is_industrial=is_industrial,
        is_apartment=is_apartment or (site_rate_per_sqyd is not None and site_rate_per_sqyd > 0),
        completion_stage=completion_stage,
        site_rate_per_sqyd=site_rate_per_sqyd,
    )

    if IS_PRODUCTION and not PRODUCTION_RATE_DATA_APPROVED:
        summary.message = "No production-approved structure-rate dataset is configured."
        return summary

    # Normalize category when 3rd+ floor is present
    req_floors = sorted(set(floors or ([0] if not floor_areas else [f for f, _ in floor_areas])))
    has_3f_plus = any(f >= 3 for f in req_floors)
    lookup_cat = structure_category
    if has_3f_plus and lookup_cat == "RCC_RESIDENTIAL_GF_2F":
        lookup_cat = "RCC_RESIDENTIAL_3F_PLUS"

    rate = _find_structure_rate(session, lookup_cat, area_tier, exec_d)
    if rate is None:
        rate = _find_structure_rate(session, structure_category, area_tier, exec_d)
    if rate is None:
        summary.message = f"No structure rate for '{structure_category}' in '{area_tier}' on {exec_d}."
        return summary

    summary.status = "OK"
    summary.citation = rate.authority_citation
    summary.base_rate_per_sqft = rate.rate_per_sqft

    completion_factors = {
        "FOUNDATION": Decimal("0.25"),
        "SLAB": Decimal("0.65"),
        "FINISHING": Decimal("0.85"),
        "COMPLETE": Decimal("1"),
    }
    comp_factor = completion_factors.get(completion_stage.upper(), Decimal("1"))
    summary.completion_factor = comp_factor
    dep_pct = _compute_depreciation_pct(age_years, is_industrial)
    summary.depreciation_pct = dep_pct

    site_adder = Decimal("0")
    if summary.is_apartment and site_rate_per_sqyd is not None and site_rate_per_sqyd > 0:
        site_adder = site_rate_per_sqyd / Decimal("20")
        summary.composite_site_adder = site_adder.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    adder_per_floor = rate.extra_floor_adder or Decimal("0")
    if adder_per_floor == 0 and has_3f_plus:
        fallback_3f = _find_structure_rate(session, "RCC_RESIDENTIAL_3F_PLUS", area_tier, exec_d)
        if fallback_3f and fallback_3f.extra_floor_adder:
            adder_per_floor = fallback_3f.extra_floor_adder

    def _build_floor_row(f_num: int, area_val: Optional[Decimal]) -> FloorStructureBreakdown:
        f_adder = (
            adder_per_floor * Decimal(str(f_num - 2))
            if adder_per_floor > 0 and f_num >= 3
            else Decimal("0")
        )
        structure_gross_unit = rate.rate_per_sqft + f_adder
        dep_structure_unit = (
            structure_gross_unit * comp_factor * (Decimal("100") - dep_pct) / Decimal("100")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross_unit = structure_gross_unit + site_adder
        # Composite apartment formula: site_adder (site_rate/20) is land component and NEVER depreciates
        dep_unit = (dep_structure_unit + site_adder).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        net_val = _round(dep_unit * area_val) if area_val is not None and area_val > 0 else None
        return FloorStructureBreakdown(
            floor_number=f_num,
            floor_label=_floor_label(f_num),
            plinth_area_sqft=area_val,
            base_rate_per_sqft=rate.rate_per_sqft,
            floor_adder=f_adder,
            composite_site_adder=site_adder.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            gross_unit_rate=gross_unit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            depreciated_unit_rate=dep_unit,
            depreciated_structure_unit_rate=dep_structure_unit,
            net_value=net_val,
        )

    def _accumulate_totals(rows: list[FloorStructureBreakdown]) -> tuple[Decimal, Decimal, Decimal]:
        gross_total = Decimal("0")
        after_comp_total = Decimal("0")
        net_total = Decimal("0")
        for fb in rows:
            area = fb.plinth_area_sqft or Decimal("0")
            struct_unit = fb.base_rate_per_sqft + fb.floor_adder
            gross_total += (struct_unit + fb.composite_site_adder) * area
            after_comp_total += (struct_unit * comp_factor + fb.composite_site_adder) * area
            net_total += fb.net_value or Decimal("0")
        return _round(gross_total), max(Decimal("0"), _round(after_comp_total) - net_total), net_total

    if floor_areas:
        summary.floor_breakdowns = [_build_floor_row(f, a) for f, a in floor_areas]
        summary.total_plinth_area_sqft = sum((a for _, a in floor_areas), Decimal("0"))
        summary.gross_value, summary.depreciation_amount, summary.net_building_value = _accumulate_totals(
            summary.floor_breakdowns
        )
    elif len(req_floors) >= 2:
        summary.needs_floor_split = True
        equal_area = (
            (plinth_area_sqft / Decimal(str(len(req_floors)))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if plinth_area_sqft > 0
            else None
        )
        summary.floor_breakdowns = [_build_floor_row(f, equal_area) for f in req_floors]
        if plinth_area_sqft > 0 and equal_area is not None:
            summary.gross_value, summary.depreciation_amount, summary.net_building_value = _accumulate_totals(
                summary.floor_breakdowns
            )
            summary.assumption_note = (
                f"Equal floor split assumed ({equal_area} sq.ft per floor across {len(req_floors)} floors). "
                "Provide exact floor-wise sq.ft areas for a final floor-weighted figure."
            )
        else:
            summary.assumption_note = (
                "Per-floor depreciated unit rates are listed above. Provide the plinth area (sq.ft) per floor to compute total value."
            )
    else:
        f_single = req_floors[0]
        row = _build_floor_row(f_single, plinth_area_sqft if plinth_area_sqft > 0 else None)
        summary.floor_breakdowns = [row]
        if plinth_area_sqft > 0:
            summary.gross_value, summary.depreciation_amount, summary.net_building_value = _accumulate_totals(
                summary.floor_breakdowns
            )

    return summary
