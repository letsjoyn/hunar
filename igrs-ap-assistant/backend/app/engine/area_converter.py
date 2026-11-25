"""
Andhra Pradesh Land Measurement Unit Converter.

Implements statutory conversion factors recognized by IGRS AP:
Source: REVISIED DOCUMENT CLASSIFICATION AND TYPE.xlsx (Sheet 2) and AP Survey Manual.

Base standard unit in AP Registration: SQUARE YARDS (Sq.Yds).

Conversion Table to Sq.Yards:
- 1 Ankanam = 8 Sq.Yards (Used predominantly in Nellore, Chittoor, Kadapa, Rayalaseema)
- 1 Cent = 48.4 Sq.Yards = 0.01 Acres (Used throughout Coastal Andhra & Rayalaseema)
- 1 Gunta = 121 Sq.Yards = 0.025 Acres (Used in Telangana & erstwhile border districts)
- 1 Ground = 266.2 Sq.Yards = 0.055 Acres (Used in urban Chittoor / border areas, 2400 sq.ft)
- 1 Acre = 4,840 Sq.Yards = 100 Cents = 40 Guntas = 605 Ankanams
- 1 Hectare = 10,000 Sq.Meters = 2.47105 Acres = 11,959.88 Sq.Yards
- 1 Sq.Meter = 1.19599 Sq.Yards
- 1 Sq.Foot = 1/9 Sq.Yard (0.111111... Sq.Yards)
- 1 Sq.Yard = 9 Sq.Feet
"""
import re
from decimal import Decimal, ROUND_HALF_UP
from backend.app.engine.formatting import strip_numeric_commas
from pydantic import BaseModel, Field


# Conversion factors to Sq.Yards (Base unit)
CONVERSION_TO_SQ_YARDS: dict[str, Decimal] = {
    "SQ_YARDS": Decimal("1"),
    "ANKANAM": Decimal("8"),
    "CENT": Decimal("48.4"),
    "GUNTA": Decimal("121"),
    "GROUND": Decimal("266.2"),
    "ACRE": Decimal("4840"),
    "HECTARE": Decimal("11959.88"),  # 10,000 * 1.195988
    "SQ_METERS": Decimal("1.19599"),
    "SQ_FEET": Decimal("1") / Decimal("9"),
}

UNIT_ALIASES: dict[str, str] = {
    # Square Yards
    "sq.yds": "SQ_YARDS",
    "sq.yd": "SQ_YARDS",
    "sq yds": "SQ_YARDS",
    "sqyd": "SQ_YARDS",
    "sqyds": "SQ_YARDS",
    "square yards": "SQ_YARDS",
    "square yard": "SQ_YARDS",
    "yards": "SQ_YARDS",
    "gajalu": "SQ_YARDS",
    "gajam": "SQ_YARDS",
    
    # Ankanam
    "ankanam": "ANKANAM",
    "ankanams": "ANKANAM",
    "ank": "ANKANAM",
    
    # Cent
    "cent": "CENT",
    "cents": "CENT",
    "ct": "CENT",
    "cts": "CENT",
    
    # Gunta
    "gunta": "GUNTA",
    "guntas": "GUNTA",
    "kunta": "GUNTA",
    "kuntas": "GUNTA",
    
    # Ground
    "ground": "GROUND",
    "grounds": "GROUND",
    
    # Acre
    "acre": "ACRE",
    "acres": "ACRE",
    "ac": "ACRE",
    
    # Hectare
    "hectare": "HECTARE",
    "hectares": "HECTARE",
    "ha": "HECTARE",
    
    # Square Meters
    "sq.mtrs": "SQ_METERS",
    "sq.mtr": "SQ_METERS",
    "sq.m": "SQ_METERS",
    "sqm": "SQ_METERS",
    "square meters": "SQ_METERS",
    "square meter": "SQ_METERS",
    
    # Square Feet
    "sq.ft": "SQ_FEET",
    "sqft": "SQ_FEET",
    "sq ft": "SQ_FEET",
    "square feet": "SQ_FEET",
    "square foot": "SQ_FEET",
    "sft": "SQ_FEET",
}


class AreaConversionResult(BaseModel):
    input_value: Decimal
    input_unit: str
    sq_yards: Decimal
    sq_meters: Decimal
    sq_feet: Decimal
    cents: Decimal
    ankanams: Decimal
    guntas: Decimal
    acres: Decimal
    hectares: Decimal
    formatted_summary: str


def normalize_unit_name(unit: str) -> str:
    """Normalize user or API input unit to standard key."""
    u = unit.strip().lower()
    if u in UNIT_ALIASES:
        return UNIT_ALIASES[u]
    u_upper = unit.strip().upper().replace(" ", "_")
    if u_upper in CONVERSION_TO_SQ_YARDS:
        return u_upper
    raise ValueError(f"Unknown area unit: '{unit}'. Supported: Ankanam, Cent, Gunta, Ground, Acre, Hectare, Sq.Yds, Sq.Mtrs, Sq.Ft")


def to_sq_yards(value: Decimal, unit: str) -> Decimal:
    """Convert an amount in any unit to Square Yards."""
    norm_unit = normalize_unit_name(unit)
    factor = CONVERSION_TO_SQ_YARDS[norm_unit]
    return value * factor


def from_sq_yards(sq_yds: Decimal, target_unit: str) -> Decimal:
    """Convert Square Yards to target unit."""
    norm_unit = normalize_unit_name(target_unit)
    factor = CONVERSION_TO_SQ_YARDS[norm_unit]
    return (sq_yds / factor).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def convert_area(value: Decimal, from_unit: str) -> AreaConversionResult:
    """Convert an area value into all standard AP registration measurement units."""
    sq_yds = to_sq_yards(value, from_unit)
    
    sq_mtrs = (sq_yds / CONVERSION_TO_SQ_YARDS["SQ_METERS"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    sq_ft = (sq_yds * Decimal("9")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    cents = (sq_yds / CONVERSION_TO_SQ_YARDS["CENT"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    ankanams = (sq_yds / CONVERSION_TO_SQ_YARDS["ANKANAM"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    guntas = (sq_yds / CONVERSION_TO_SQ_YARDS["GUNTA"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    acres = (sq_yds / CONVERSION_TO_SQ_YARDS["ACRE"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    hectares = (sq_yds / CONVERSION_TO_SQ_YARDS["HECTARE"]).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    
    # Formatted summary for check-slips
    norm_unit = normalize_unit_name(from_unit)
    summary = f"{value} {norm_unit} = {sq_yds.quantize(Decimal('0.01'))} Sq.Yds ({acres.quantize(Decimal('0.0001'))} Acres / {cents.quantize(Decimal('0.01'))} Cents / {ankanams.quantize(Decimal('0.01'))} Ankanams)"
    
    return AreaConversionResult(
        input_value=value,
        input_unit=norm_unit,
        sq_yards=sq_yds.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        sq_meters=sq_mtrs,
        sq_feet=sq_ft,
        cents=cents,
        ankanams=ankanams,
        guntas=guntas,
        acres=acres,
        hectares=hectares,
        formatted_summary=summary,
    )


def parse_composite_land_string(text: str) -> Decimal:
    """
    Parse complex land strings commonly found in AP registered deeds, e.g.:
      - "2 Acres 35 Cents"
      - "1 Acre 15 Guntas"
      - "100 Ankanams 4 Sq.Yds"
      - "1 Hectare 20 Cents"
    Returns total area in Square Yards.
    """
    total_sq_yds = Decimal("0")
    cleaned = strip_numeric_commas(text)
    # Matches pairs of number + unit
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z\.]+(?:\s+[a-zA-Z\.]+)?)", re.IGNORECASE)
    matches = pattern.findall(cleaned)
    
    if not matches:
        raise ValueError(f"Could not parse land area description: '{text}'")
        
    for val_str, unit_str in matches:
        unit_str_clean = unit_str.strip().lower()
        # Find matching alias
        found_unit = None
        for alias, norm in UNIT_ALIASES.items():
            if unit_str_clean == alias or unit_str_clean.startswith(alias):
                found_unit = norm
                break
        if found_unit:
            val = Decimal(val_str)
            total_sq_yds += val * CONVERSION_TO_SQ_YARDS[found_unit]
            
    if total_sq_yds <= Decimal("0"):
        raise ValueError(f"Could not parse valid land area description: '{text}'")
        
    return total_sq_yds.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
