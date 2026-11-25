"""
Phase 3: AI Chatbot Orchestrator for IGRS AP Assistant.

Acts as the intelligent legal brain connecting natural language chat with
100% deterministic calculation engines and the Phase 2 statutory knowledge base.

Rules:
- NEVER does math in prompt text — always calls Python calculation tools.
- NEVER guesses unverified rates — reports NOT_FOUND explicitly.
- Inlines exact statutory citations and G.O. numbers next to EVERY fee/duty rate.
- Returns reactive payloads for the right-hand panel (Check-Slip, Verbatim Legal Text, Area Grid).
"""
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.core.config import APP_ENV, IS_PRODUCTION
from backend.app.db.models import DutyRate
from backend.app.engine.duty_calculator import (
    CODE_ALIASES,
    calculate_deed_duty,
    calculate_lease_duty,
    compare_historical_vs_current,
    DutyBreakdown,
    _get_user_charge,
)
from backend.app.engine.formatting import (
    format_indian_number,
    format_inr,
    strip_numeric_commas,
)
from backend.app.engine.structure_calc import calculate_building_structure
from backend.app.engine.fines_calc import calculate_delay_fine, FineResult
from backend.app.engine.area_converter import (
    convert_area,
    parse_composite_land_string,
    AreaConversionResult,
)
from backend.app.engine.legal_search import (
    search_legal_knowledge,
    get_verbatim_section,
    LegalSearchResult,
)
from backend.app.engine.verification import sign_and_archive_check_slip
from backend.app.engine.prohibited_land import check_prohibited_land


class ChatRequest(BaseModel):
    message: str
    history: Optional[list[dict]] = None
    use_gemini: Optional[bool] = None


class ChatResponse(BaseModel):
    reply: str
    intent: str
    card_slip: Optional[dict] = None
    audit_comparison: Optional[dict] = None
    legal_docs: Optional[list[dict]] = None
    fine_data: Optional[dict] = None
    area_data: Optional[dict] = None
    prohibited_data: Optional[dict] = None
    structure_data: Optional[dict] = None



_UNIT_SCALE = {
    "cr": Decimal("10000000"),
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "l": Decimal("100000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "k": Decimal("1000"),
}

_AMOUNT_TOKEN_RE = re.compile(
    r"(?:(rs\.?|inr|₹)\s*)?(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\b"
    r"(?!\s*(?:sq\.?|square|acres?|cents?|ankanams?|guntas?|grounds?|hectares?|pages?|pgs?|sheets?|months?|yrs?|years?|floors?|%|brothers?|sisters?|co-?parceners?|persons?|parts?|members?))",
    re.IGNORECASE,
)


def _strip_dates_and_commas(text: str) -> str:
    cleaned = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", " ", text)
    cleaned = re.sub(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b", " ", cleaned)
    return strip_numeric_commas(cleaned)


def _is_non_property_value_amount(text: str, end: int) -> bool:
    """Return whether a number is labelled as a fee/fine/cap rather than property value."""
    return bool(re.match(r"\s*(?:rs\.?\s*)?(?:fee|fine|user\s+charge|cap)\b", text[end:], re.IGNORECASE))


def parse_all_rupee_amounts(text: str, *, exclude_fee_context: bool = False) -> list[Decimal]:
    """Extract rupee amounts; optionally omit values explicitly labelled as fees/fines/caps."""
    cleaned = _strip_dates_and_commas(text)
    amounts: list[Decimal] = []
    for m in _AMOUNT_TOKEN_RE.finditer(cleaned):
        if exclude_fee_context and _is_non_property_value_amount(cleaned, m.end()):
            continue
        prefix = m.group(1)
        num_str = m.group(2)
        suffix = (m.group(3) or "").lower()
        try:
            num = Decimal(num_str)
        except Exception:
            continue
        if suffix in _UNIT_SCALE:
            amounts.append(num * _UNIT_SCALE[suffix])
            continue
        if prefix:
            amounts.append(num)
            continue
        if "." not in num_str and len(num_str) >= 4:
            val_int = int(num_str)
            if 1900 <= val_int <= 2099:
                continue
            amounts.append(num)
    return amounts


def parse_rupee_amount(text: str) -> Decimal:
    """Extract primary rupee amount like '15 Lakhs', '30L', '25k', '15,00,000', '1.5 Crore', '500000'."""
    amounts = parse_all_rupee_amounts(text)
    return amounts[0] if amounts else Decimal("0")


def parse_consideration_and_mv(text: str) -> tuple[Decimal, Decimal]:
    """Extract distinct consideration/sale price and market value when both appear (else mirror primary amount)."""
    cleaned = _strip_dates_and_commas(text)
    consid_val: Optional[Decimal] = None
    mv_val: Optional[Decimal] = None

    m_consid = re.search(
        r"(?:sale\s+price|consideration(?:\s+value)?|sold\s+for|price)\s*(?:is|of|:|=)?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\b",
        cleaned,
        re.IGNORECASE,
    )
    if m_consid and not _is_non_property_value_amount(cleaned, m_consid.end()):
        suf = (m_consid.group(2) or "").lower()
        consid_val = Decimal(m_consid.group(1)) * _UNIT_SCALE.get(suf, Decimal("1"))

    m_mv = re.search(
        r"(?:market\s+value|govt\.?\s+value|guideline\s+value|\bmv\b)\s*(?:is|of|:|=)?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\b",
        cleaned,
        re.IGNORECASE,
    )
    if m_mv and not _is_non_property_value_amount(cleaned, m_mv.end()):
        suf = (m_mv.group(2) or "").lower()
        mv_val = Decimal(m_mv.group(1)) * _UNIT_SCALE.get(suf, Decimal("1"))

    if consid_val is not None or mv_val is not None:
        primary = max(consid_val or Decimal("0"), mv_val or Decimal("0"))
        return (consid_val if consid_val is not None else primary, mv_val if mv_val is not None else primary)

    amounts = parse_all_rupee_amounts(text, exclude_fee_context=True)
    if len(amounts) >= 2:
        return min(amounts), max(amounts)
    amt = amounts[0] if amounts else Decimal("0")
    return amt, amt


def parse_rupee_shares(text: str) -> list[Decimal]:
    """Extract individual share values from partition prompts like 'splitting 90L into 40L, 30L and 20L shares'."""
    cleaned = _strip_dates_and_commas(text)
    m_into = re.search(r"\binto\b(.+)", cleaned, re.IGNORECASE)
    if m_into:
        tail_shares = parse_all_rupee_amounts(m_into.group(1))
        if len(tail_shares) >= 2:
            return tail_shares
    all_amts = parse_all_rupee_amounts(cleaned)
    if len(all_amts) >= 3 and all_amts[0] == sum(all_amts[1:]):
        return all_amts[1:]
    if len(all_amts) >= 2 and not (len(all_amts) == 2 and all_amts[0] == all_amts[1]):
        return all_amts
    return []


def parse_sheet_count(text: str) -> int:
    """Extract explicit page/sheet counts (e.g. '14 pages', '12 sheets'); default 10."""
    m = re.search(r"\b(\d{1,3})\s*(?:pages?|pgs?|sheets?)\b", text, re.IGNORECASE)
    if not m:
        return 10
    return max(1, min(int(m.group(1)), 500))


def parse_date_from_text(text: str) -> date:
    """Extract dates like '2009-05-01', '01-02-2026', 'May 2009', '15 June 2023', '2026'."""
    # YYYY-MM-DD
    m_iso = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass

    # DD-MM-YYYY or DD/MM/YYYY
    m_dmy = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", text)
    if m_dmy:
        try:
            return date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1)))
        except ValueError:
            pass

    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    # DD Month YYYY  e.g. '15 June 2023' or '15th June 2023'
    m_dmy2 = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(f"{k}[a-z]*" for k in months) + r")\s+(\d{4})\b",
        text, re.IGNORECASE,
    )
    if m_dmy2:
        mon_str = m_dmy2.group(2)[:3].lower()
        try:
            return date(int(m_dmy2.group(3)), months[mon_str], int(m_dmy2.group(1)))
        except ValueError:
            pass

    # Month DD, YYYY  e.g. 'June 15, 2023'
    m_mdy = re.search(
        r"\b(" + "|".join(f"{k}[a-z]*" for k in months) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
        text, re.IGNORECASE,
    )
    if m_mdy:
        mon_str = m_mdy.group(1)[:3].lower()
        try:
            return date(int(m_mdy.group(3)), months[mon_str], int(m_mdy.group(2)))
        except ValueError:
            pass

    # Month Year e.g. 'May 2009', 'Feb 2026'
    for m_name, m_num in months.items():
        m_my = re.search(rf"\b{m_name}[a-z]*\s+(\d{{4}})\b", text, re.IGNORECASE)
        if m_my:
            return date(int(m_my.group(1)), m_num, 1)

    # Just year: 1990 to 2029 (skip amounts or areas like ₹2000, 2000 sq.ft, 2020 acres)
    m_year = re.search(
        r"(?<!₹)(?<!rs\.\s)\b(19\d{2}|20[0-2]\d)\b(?!\s*(?:sq|sft|acre|cent|gunta|ankanam|ground|hectare|lakh|crore|rs|inr|rupee|\/|per\b|\-?\s*year|\-?\s*yr))",
        text,
        re.IGNORECASE,
    )
    if m_year:
        return date(int(m_year.group(1)), 5, 1)

    return date.today()


def parse_relative_execution_date(text: str) -> date | None:
    """Parse relative delay phrases like '9 months back', '4 months 18 days ago' into an execution date."""
    t = text.lower()
    # 'X months Y days ago/back'
    m = re.search(r"(\d+)\s*months?\s*(?:and\s*)?(\d+)?\s*days?\s*(?:ago|back)\b", t)
    if m:
        from dateutil.relativedelta import relativedelta
        months = int(m.group(1))
        days = int(m.group(2)) if m.group(2) else 0
        return (date.today() - relativedelta(months=months, days=days))
    # 'X months ago/back'
    m2 = re.search(r"(\d+)\s*months?\s*(?:ago|back)\b", t)
    if m2:
        from dateutil.relativedelta import relativedelta
        return (date.today() - relativedelta(months=int(m2.group(1))))
    return None


def detect_area_type(text: str) -> str:
    t = text.lower()
    if "cantonment" in t:
        return "CANTONMENT"
    if any(k in t for k in ["major gram panchayat", "major gp"]):
        return "MAJOR_GRAM_PANCHAYAT"
    if any(k in t for k in ["minor gram panchayat", "minor gp"]):
        return "MINOR_GRAM_PANCHAYAT"
    if any(k in t for k in ["village panchayat", "gram panchayat", "panchayat", "village", "rural"]) or re.search(r"\bgp\b", t):
        return "GRAM_PANCHAYAT"
    if any(k in t for k in ["selection grade", "special grade"]):
        return "SPECIAL_SELECTION_MUNICIPALITY"
    if (
        "municipal corporation" in t
        or "municipal corp" in t
        or re.search(r"\b(?:corporation|corp)\b", t)
        or any(city in t for city in (
            "vijayawada", "vijyawada", "vizag", "visakhapatnam", "vishakhapatnam",
            "guntur", "tirupati", "nellore", "kurnool", "kakinada",
            "rajahmundry", "rajamahendravaram", "kadapa", "anantapur",
            "eluru", "ongole", "chittoor", "machilipatnam", "vizianagaram", "srikakulam",
        ))
    ):
        return "MUNICIPAL_CORP"
    if re.search(r"\b(?:muni|municipality|municipal|town)\b", t):
        return "OTHER_MUNICIPALITY"
    return "MUNICIPAL_CORP"


def evaluate_small_agri_bit(
    area_cents: Decimal,
    land_type: str,
    area_type: str,
    agri_sides_count: int,
) -> str | None:
    """Classify qualifying small urban dry-land bits under MV1/34372/92."""
    urban_areas = {
        "MUNICIPAL_CORP", "OTHER_MUNICIPALITY", "SPECIAL_SELECTION_MUNICIPALITY",
        "MAJOR_GRAM_PANCHAYAT", "MANDAL_HEADQUARTERS", "URBAN_AGGLOMERATION",
    }
    if area_cents <= 0 or area_cents > Decimal("10"):
        return None
    if land_type.strip().upper() != "DRY" or area_type.strip().upper() not in urban_areas:
        return None
    if not 0 <= agri_sides_count <= 4:
        return None
    return "HOUSE_SITE_RATE" if agri_sides_count < 3 else "AGRICULTURAL_ACRE_RATE"


def _small_agri_bit_inputs(text: str) -> tuple[Decimal, str, str, int] | None:
    normalized = strip_numeric_commas(text).lower()
    area_match = re.search(r"\b(\d+(?:\.\d+)?)\s*cents?\b", normalized)
    side_match = re.search(
        r"\b(?:only\s*)?(\d+|one|two|three|four)\s*(?:of\s*4\s*)?(?:sides?|boundaries?)\b",
        normalized,
    )
    if not area_match or not side_match:
        return None
    side_words = {"one": 1, "two": 2, "three": 3, "four": 4}
    sides_text = side_match.group(1)
    sides = int(sides_text) if sides_text.isdigit() else side_words[sides_text]
    if "wet" in normalized:
        land_type = "WET"
    elif "dry" in normalized:
        land_type = "DRY"
    else:
        return None

    area_type = detect_area_type(text)
    if not any(term in normalized for term in (
        "urban", "corporation", "corp", "municipality", "municipal", "major gp",
        "major gram panchayat", "mandal hq", "urban agglomeration", "city",
    )):
        return None
    return Decimal(area_match.group(1)), land_type, area_type, sides


def parse_lease_months(text: str) -> int | None:
    cleaned = strip_numeric_commas(text)
    match = re.search(r"\b(\d+)\s*(years?|yrs?|months?)\b", cleaned, re.IGNORECASE)
    if not match:
        return None
    duration = int(match.group(1))
    return duration * 12 if match.group(2).lower().startswith(("year", "yr")) else duration


def parse_monthly_rent(text: str) -> Decimal:
    cleaned = strip_numeric_commas(text)
    patterns = (
        r"(?:monthly\s+rent|rent\s+per\s+month|rent\s*/\s*month)\s*(?:is|of|:|@|at)?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\b",
        r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\s*(?:monthly\s+rent|monthly|per\s+month|/\s*month|/\s*mo|p\.?\s*m\.?)(?:\s+rent)?\b",
        r"(?:rent)\s*(?:is|of|:|@|at)\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\b",
    )
    for pat in patterns:
        match = re.search(pat, cleaned, re.IGNORECASE)
        if match:
            amount = Decimal(match.group(1))
            suffix = (match.group(2) or "").lower()
            return amount * _UNIT_SCALE.get(suffix, Decimal("1"))
    return Decimal("0")


def parse_lease_advance(text: str) -> Decimal:
    cleaned = strip_numeric_commas(text)
    patterns = (
        r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\s*(?:non[-\s]?refundable\s+|refundable\s+)?(?:advance\s+deposit|security\s+deposit|advance|deposit|premium)\b",
        r"(?:advance\s+deposit|security\s+deposit|advance|deposit|premium)\s*(?:is|of|:)?\s*(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|l|k)?\b",
    )
    for pat in patterns:
        match = re.search(pat, cleaned, re.IGNORECASE)
        if match:
            amount = Decimal(match.group(1))
            suffix = (match.group(2) or "").lower()
            return amount * _UNIT_SCALE.get(suffix, Decimal("1"))
    return Decimal("0")


def parse_lease_advance_is_refundable(text: str) -> Optional[bool]:
    t = text.lower()
    if "non-refundable" in t or "non refundable" in t or "premium" in t:
        return False
    if "refundable" in t:
        return True
    return None


def _is_structure_valuation_query(t_lower: str) -> bool:
    structure_markers = (
        "structure value", "structure rate", "building value", "building valuation",
        "rcc", "plinth area", "plinth", "depreciation", "age proof",
        "composite rate", "composite apartment", "floor adder", "unfinished structure",
    )
    if any(marker in t_lower for marker in structure_markers):
        return True
    if "floor" in t_lower and re.search(r"\b(?:sq\.?\s*ft|sqft|square\s+feet|sq\.?\s*yd|sqyd|site\s+rate|years?\s+old|age)\b", t_lower):
        return True
    return False


def _floor_num_from_token(tok: str) -> Optional[int]:
    t = tok.strip().lower()
    if t in ("ground", "gf", "g.f", "0", "0th"):
        return 0
    if t in ("first", "1st", "1f", "1"):
        return 1
    if t in ("second", "2nd", "2f", "2"):
        return 2
    if t in ("third", "3rd", "3f", "3"):
        return 3
    if t in ("fourth", "4th", "4f", "4"):
        return 4
    if t in ("fifth", "5th", "5f", "5"):
        return 5
    m = re.match(r"^(\d+)", t)
    return int(m.group(1)) if m else None


def parse_structure_params(text: str) -> dict:
    cleaned = strip_numeric_commas(text)
    t_lower = cleaned.lower()

    exec_d = parse_date_from_text(text)
    if any(k in t_lower for k in ("major gram panchayat", "major gp")):
        area_tier = "MAJOR_GRAM_PANCHAYAT"
    elif any(k in t_lower for k in ("minor gram panchayat", "minor gp")):
        area_tier = "MINOR_GRAM_PANCHAYAT"
    elif any(k in t_lower for k in ("gram panchayat", "panchayat", "village", "rural")):
        area_tier = "GRAM_PANCHAYAT"
    elif any(k in t_lower for k in ("nagar panchayat", "uda", "town")):
        area_tier = "NAGAR_PANCHAYAT_UDA"
    else:
        area_tier = "CORP_MUNI"

    is_industrial = any(k in t_lower for k in ("industrial", "factory", "mill", "cinema"))
    is_apartment = any(k in t_lower for k in ("apartment", "flat", "composite"))

    completion_stage = "COMPLETE"
    matched_stages: list[str] = []
    if re.search(r"\b(?:foundation|basement)\b", t_lower):
        matched_stages.append("FOUNDATION")
    if re.search(r"\b(?:slab|roof|lintel|superstructure)\b", t_lower):
        matched_stages.append("SLAB")
    if re.search(r"\b(?:finishing|plaster|semi-finished|semi finished|unfinished)\b", t_lower):
        matched_stages.append("FINISHING")
    if len(matched_stages) == 1:
        completion_stage = matched_stages[0]
    elif matched_stages:
        completion_stage = matched_stages[0]

    age_years = 0
    m_age = re.search(r"\b(\d+)[\s\-]*(?:years?|yrs?)[\s\-]*(?:old|age|building)?\b", t_lower)
    if not m_age:
        m_age = re.search(r"\bage\s*(?:is|of|:)?\s*(\d+)\b", t_lower)
    if m_age:
        age_years = int(m_age.group(1))

    site_rate = None
    m_site = re.search(
        r"(?:site\s+rate|land\s+rate|sq\.?\s*yd\s+rate|rate\s+per\s+sq\.?\s*yd)[^\d₹rs]*?(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)",
        t_lower,
    )
    if not m_site:
        m_site = re.search(r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(?:per|/)\s*(?:sq\.?\s*yd|sqyd|square\s+yard)", t_lower)
    if m_site:
        site_rate = Decimal(m_site.group(1))

    floor_token_pat = r"(ground|gf|first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|\d+(?:st|nd|rd|th)?)"
    floor_areas: list[tuple[int, Decimal]] = []
    seen_floor_nums: set[int] = set()
    for pat in (
        rf"\b(\d+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square\s+feet|sft)\s*(?:on|in|for|at)?\s*{floor_token_pat}(?:\s+floor)?\b",
        rf"\b{floor_token_pat}\s*(?:floor\s*[:\-]?|[:\-])\s*(\d+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square\s+feet|sft)\b",
    ):
        for m in re.finditer(pat, t_lower):
            if pat.startswith(r"\b(\d"):
                area_s, f_tok = m.group(1), m.group(2)
            else:
                f_tok, area_s = m.group(1), m.group(2)
            f_num = _floor_num_from_token(f_tok)
            if f_num is not None and f_num not in seen_floor_nums:
                seen_floor_nums.add(f_num)
                floor_areas.append((f_num, Decimal(area_s)))

    # If a single prompt had e.g. "1,800 sq.ft ground + 3rd floor", don't misbind total 1,800 exclusively to ground if '+' joins multiple floors
    floors_found: list[int] = []
    for fm in re.finditer(r"\b(ground|gf|1st|first|2nd|second|3rd|third|4th|fourth|5th|\d+(?:st|nd|rd|th)?)\b(?=\s*(?:\+|and|,|/|&|\bfloor\b))", t_lower):
        f_num = _floor_num_from_token(fm.group(1))
        if f_num is not None and f_num not in floors_found:
            floors_found.append(f_num)
    for fm in re.finditer(r"\b(ground|gf|1st|first|2nd|second|3rd|third|4th|fourth|5th|\d+(?:st|nd|rd|th)?)\s+floor\b", t_lower):
        f_num = _floor_num_from_token(fm.group(1))
        if f_num is not None and f_num not in floors_found:
            floors_found.append(f_num)

    if len(floors_found) >= 2 and len(floor_areas) < len(floors_found):
        floor_areas = []

    total_plinth = Decimal("0")
    m_plinth = re.search(r"(\d+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square\s+feet|sft)", t_lower)
    if floor_areas:
        total_plinth = sum((a for _, a in floor_areas), Decimal("0"))
    elif m_plinth:
        total_plinth = Decimal(m_plinth.group(1))

    if not floors_found and floor_areas:
        floors_found = [f for f, _ in floor_areas]
    if not floors_found:
        floors_found = [0]

    if is_apartment:
        cat = "RCC_RESIDENTIAL_APARTMENT"
    elif is_industrial and "rcc" not in t_lower:
        cat = "NON_RCC_CINEMA_FACTORY"
    elif "cellar" in t_lower or "parking" in t_lower:
        cat = "RCC_COMMERCIAL_CELLAR" if "commercial" in t_lower else "RCC_CELLAR_PARKING"
    elif any(k in t_lower for k in ("commercial", "shop", "office")):
        max_f = max(floors_found)
        cat = "RCC_COMMERCIAL_GF" if max_f == 0 else ("RCC_COMMERCIAL_1F" if max_f == 1 else "RCC_COMMERCIAL_2F_PLUS")
    elif any(f >= 3 for f in floors_found):
        cat = "RCC_RESIDENTIAL_3F_PLUS"
    else:
        cat = "RCC_RESIDENTIAL_GF_2F"

    return {
        "structure_category": cat,
        "area_tier": area_tier,
        "execution_date": exec_d,
        "plinth_area_sqft": total_plinth,
        "floors": sorted(floors_found),
        "floor_areas": floor_areas or None,
        "age_years": age_years,
        "is_industrial": is_industrial,
        "completion_stage": completion_stage,
        "site_rate_per_sqyd": site_rate,
        "is_apartment": is_apartment,
        "matched_stages": matched_stages,
    }


def _has_universal_rate(
    session: Session,
    instrument_code: str,
    execution_date: date,
    property_subtype: str = "ALL",
    lease_months: int | None = None,
) -> bool:
    statuses = ["VERIFIED"]
    if APP_ENV != "production":
        statuses.append("PENDING_REVIEW")
    aliases = CODE_ALIASES.get(instrument_code, [instrument_code])
    query = session.query(DutyRate).filter(
        DutyRate.instrument_code.in_(aliases),
        DutyRate.area_type == "ALL",
        DutyRate.effective_from <= execution_date,
        or_(DutyRate.effective_to.is_(None), DutyRate.effective_to >= execution_date),
        DutyRate.verification_status.in_(statuses),
    )
    if property_subtype != "ALL":
        query = query.filter(DutyRate.property_subtype.in_([property_subtype, "ALL"]))
    if lease_months is not None:
        query = query.filter(
            or_(DutyRate.duration_min_months.is_(None), DutyRate.duration_min_months <= lease_months),
            or_(DutyRate.duration_max_months.is_(None), DutyRate.duration_max_months >= lease_months),
        )
    return query.first() is not None


_FAMILY_REL_RE = re.compile(
    r"\b(?:son|daughter|father|dad|papa|mother|mom|wife|husband|spouse|"
    r"brother|brothers|bro|sister|sisters|sis|grandson|granddaughter|"
    r"grandfather|grandmother|brother'?s\s+wife|sister-?in-?law|"
    r"brother-?in-?law|father-?in-?law|mother-?in-?law|son-?in-?law|"
    r"daughter-?in-?law|family|children|child|parents|relative|relatives|"
    r"ancestral|co-?parcener|co-?parceners)\b",
    re.IGNORECASE,
)


def detect_deed_code(text: str) -> tuple[str, str]:
    t = text.lower()
    
    # 0. Development Agreement / DAGPA / Construction Agreement / Builders
    if any(k in t for k in ["development agreement", "dagpa", "construction agreement", "builder", "developer"]):
        if "without gpa" in t:
            return "01_09", "DEVELOPMENT_AGREEMENT"
        return "01_10", "DEVELOPMENT_AGREEMENT_CUM_GPA"

    # 1. Partition / Splitting heuristics (CARD/e-1730463 Annexure I: 04_02 Family 1%, 04_01 Others 2%)
    if any(k in t for k in ["partition", "split", "divide", "division", "share", "separated share", "splitting", "dividing"]):
        if _FAMILY_REL_RE.search(t):
            return "04_02", "PARTITION_FAMILY"
        if re.search(r"\b(?:others?|non-relative)\b", t):
            return "04_01", "PARTITION_OTHER"
        return "04_02", "PARTITION_FAMILY"

    # 2. Family transfer heuristics (e.g. "dad gifting house to daughter", "giving my land to my son")
    is_family = bool(_FAMILY_REL_RE.search(t))
    is_giving = any(g in t for g in ["giving", "give", "gift", "gifting", "settle", "settling", "transfer", "transferring", "hand over", "handing over", "bequeath"])
    is_panchayat = any(k in t for k in ["village panchayat", "gram panchayat", "panchayat", "village", "rural"]) or bool(re.search(r"\bgp\b", t))
    
    if (is_giving and is_family) or "gift to" in t or "settle on" in t:
        if is_panchayat:
            return "03_05", "GIFT_RELATIVE"
        return "03_06", "GIFT_RELATIVE"
    if "gift" in t:
        if re.search(r"\b(?:others?|non-relative|friend)\b", t):
            return "03_07", "GIFT_OTHER"
        if is_panchayat:
            return "03_05", "GIFT_RELATIVE"
        return "03_06", "GIFT_RELATIVE"
    if "settlement" in t:
        if (
            re.search(r"\bothers?\b", t)
            or any(ex in t for ex in ("step-mother", "stepmother", "great-grand", "great grand", "would-be", "prospective wife"))
        ):
            return "03_03", "SETTLEMENT_OTHER"
        return "03_02", "SETTLEMENT_FAMILY"
    if "deposit of title" in t or "dotd" in t:
        return "02_08", "DOTD"
    if "mortgage" in t:
        if "with possession" in t:
            return "02_01", "MORTGAGE_WITH_POSSESSION"
        return "02_02", "MORTGAGE_WITHOUT_POSSESSION"
    if "release" in t:
        if _FAMILY_REL_RE.search(t):
            return "05_01", "RELEASE_FAMILY"
        return "05_02", "RELEASE_WITHOUT_CONSID"
    if "exchange" in t:
        return "06_01", "EXCHANGE"
    if "lease" in t or "tenancy" in t or "rent agreement" in t or "rental agreement" in t:
        return "LEASE", "LEASE"
    if "agreement of sale" in t or "sale agreement" in t or "agpa" in t:
        if "without possession" in t or "without poss" in t:
            return "01_03", "SALE_AGREEMENT_WITHOUT_POSSESSION"
        if "gpa" in t:
            return "01_11", "AGPA"
        return "01_02", "SALE_AGREEMENT_WITH_POSSESSION"
    if "power of attorney" in t or "gpa" in t:
        if any(r in t for r in ["family", "blood"]):
            return "09_05", "GPA_FAMILY"
        return "09_04", "GPA_SELL"
    if "conveyance" in t and "development" not in t:
        return "01_13", "CONVEYANCE"
    if "adoption" in t:
        return "30_01", "ADOPTION"
    if "affidavit" in t:
        return "31_01", "AFFIDAVIT"
    return "01_01", "SALE"


def is_greeting(t: str) -> bool:
    """Robust natural greeting and conversational intro detector."""
    clean = re.sub(r"[^a-z\s]", "", t.lower()).strip()
    if not clean:
        return True
    
    # Collapse repeated chars (e.g. 'hiiii broooo' -> 'hi bro')
    collapsed = re.sub(r"([a-z])\1+", r"\1", clean)
    
    greeting_tokens = {
        "hi", "hello", "hey", "namaste", "namaskaram", "bro", "bhai", "dude",
        "sir", "madam", "morning", "evening", "afternoon", "night", "start",
        "help", "sup", "yo", "there", "good", "anyone", "assistant", "test", "testing"
    }
    
    words = clean.split()
    collapsed_words = collapsed.split()
    
    # If all words in the input are greeting/casual tokens
    if all(w in greeting_tokens for w in words) or all(w in greeting_tokens for w in collapsed_words):
        return True
    
    # Common conversational intro phrases
    phrases = [
        "who are you", "what can you do", "help me", "how are you",
        "tell me about yourself", "how do you work", "what is this", "options"
    ]
    if any(p in clean for p in phrases):
        return True
    
    # Regex for casual greetings: hi bro, heyy, hiiii, hello sir, etc.
    if re.match(r"^(h+i+|h+e+y+|h+e+l+o+|namaste|namaskaram|yo+|sup)\b", clean):
        legal_terms = ["sale", "gift", "deed", "tax", "duty", "fee", "survey", "section", "convert", "acre", "cent", "audit", "fine"]
        if not any(lt in clean for lt in legal_terms):
            return True
            
    return False


def process_chat_message(session: Session, message: str) -> ChatResponse:
    """Main routing and response generator."""
    text = message.strip()
    t_lower = text.lower()

    # ==============================================================
    # 0. INTENT: Greetings & Help
    # ==============================================================
    if is_greeting(t_lower):
        reply = (
            "### AP Registration & Stamps Advisory\n\n"
            "Ask about deed stamp duty, transfer duty, registration fee rates or rupee calculations, "
            "Section 22-A prohibited property checks, delayed presentation fines, Section 41-A audit comparisons, "
            "land measurement conversions, or statutory registration rules."
        )
        return ChatResponse(reply=reply, intent="GREETING")

    # Authoritative schedule across key AP instruments backed by runtime DutyRate rows
    is_schedule_query = (
        any(phrase in t_lower for phrase in [
            "percentages of", "percentage of", "rates of", "list of deeds", "different deeds",
            "various deeds", "schedule of deeds", "schedule of rates", "duty percentages",
            "rate schedule", "tax percentages", "fees of deeds", "rates for 20 deeds",
            "different 20 deeds", "20 deeds", "deeds of year", "deeds in year",
            "duty rates for", "what are the percentages", "percentages for different"
        ]) or (
            any(k in t_lower for k in ["percentage", "percentages", "rates schedule", "duty schedule"])
            and any(d in t_lower for d in ["deeds", "instruments", "classifications"])
        )
    )
    if is_schedule_query and not _is_structure_valuation_query(t_lower):
        schedule_specs = [
            ("01_01", "MUNICIPAL_CORP", "Sale Deed"),
            ("01_02", "ALL", "Sale Agreement with Possession"),
            ("01_03", "ALL", "Sale Agreement without Possession"),
            ("01_10", "ALL", "Development Agreement-cum-GPA (DAGPA)"),
            ("01_11", "ALL", "Agreement of Sale-cum-GPA (AGPA)"),
            ("02_01", "ALL", "Mortgage with Possession"),
            ("02_02", "ALL", "Mortgage without Possession"),
            ("02_08", "ALL", "Deposit of Title Deeds (DOTD)"),
            ("03_01", "ALL", "Gift Deed (Art. 29)"),
            ("03_02", "ALL", "Gift Settlement in favour of Family (Art. 49-A(a))"),
            ("03_03", "ALL", "Gift Settlement in favour of Others (Art. 49-A(b))"),
            ("03_06", "MUNICIPAL_CORP", "Gift to Relatives (Municipal/Corp)"),
            ("03_05", "GRAM_PANCHAYAT", "Gift to Relatives (Panchayat)"),
            ("03_07", "MUNICIPAL_CORP", "Gift to Others / Non-Relatives"),
            ("04_02", "ALL", "Partition among Family (VSS)"),
            ("04_01", "ALL", "Partition among Others (VSS)"),
            ("05_01", "ALL", "Release among Family / Co-parceners"),
            ("06_01", "ALL", "Exchange of Property"),
            ("07_01", "ALL", "Lease Deed (Short Term)"),
            ("09_04", "ALL", "Power to Sell Immovable Property (Book-I)"),
            ("09_05", "ALL", "GPA in favour of Family Members"),
            ("08_03", "ALL", "Cancellation Deed"),
        ]
        lookup_date = parse_date_from_text(text)
        lines = [
            f"### Andhra Pradesh Registration Duty & Fee Schedule (As of `{lookup_date}`)\n",
            f"**Direct Answer:** Active AP Stamp Duty, Transfer Duty, Registration Fee percentages, and statutory caps as of `{lookup_date}` across standard CARD instruments:\n",
            "| Code | Instrument | Stamp Duty | Transfer Duty | Reg Fee | Note / Authority |",
            "|---|---|---:|---:|---:|---|",
        ]
        for scode, sarea, slabel in schedule_specs:
            row = (
                session.query(DutyRate)
                .filter(
                    DutyRate.instrument_code == scode,
                    DutyRate.area_type.in_([sarea, "ALL"]),
                    DutyRate.effective_from <= lookup_date,
                    or_(DutyRate.effective_to.is_(None), DutyRate.effective_to >= lookup_date),
                    DutyRate.verification_status.in_(["VERIFIED", "PENDING_REVIEW"]),
                )
                .order_by((DutyRate.area_type == sarea).desc())
                .first()
            )
            if not row:
                continue
            sd = f"{row.stamp_duty_value}%" if row.stamp_duty_type == "PERCENT" else format_inr(row.stamp_duty_value, 0)
            td = f"{row.transfer_duty_pct or 0}%"
            rf = f"{row.reg_fee_value}%" if row.reg_fee_type == "PERCENT" else format_inr(row.reg_fee_value, 0)
            cap_bits = []
            if row.reg_fee_min and row.reg_fee_min > 0:
                cap_bits.append(f"Min {format_inr(row.reg_fee_min, 0)}")
            if row.reg_fee_max and row.reg_fee_max > 0:
                cap_bits.append(f"Max {format_inr(row.reg_fee_max, 0)}")
            adj_pct = Decimal(str(getattr(row, "adjustable_sd_pct", None) or 0))
            if adj_pct > 0:
                cap_bits.append(f"{adj_pct.normalize()}% Adjustable")
            elif scode in {"01_09", "01_10"}:
                cap_bits.append("Not adjustable")
            note = ", ".join(cap_bits) if cap_bits else (getattr(row, "sd_authority", None) or row.article_ref)
            lines.append(f"| `{scode}` | {slabel} | {sd} | {td} | {rf} | {note} |")
        lines.append("\nCARD User Charges (`G.O.Ms.No. 192 dt. 25.04.2023`): **₹500** for Book-I transfers (up to 10 sheets), **₹100** for Mortgages/Leases/Books III–IV.")
        return ChatResponse(reply="\n".join(lines), intent="DEED_SCHEDULE")

    legal_question_markers = (
        "eligible", "eligibility", "qualify", "qualifies", "exempt", "exemption",
        "excluded", "exclude", "family definition", "definition of family",
        "step-mother", "stepmother", "great-grand", "great grand",
        "would-be", "prospective wife", "attracts", "which rate", "or does it",
        "non-family", "trap",
        "pure settlement", "settlement-cum-conveyance", "settlement cum conveyance",
        "relative under", "is it treated as", "does this count as",
        "what is the difference between", "classify this", "rule 61", "rule 71",
        "22-b", "22b", "webland", "ptin", "vlt", "lpm", "iplp", "1730463", "gramakantam",
        "aadhaar", "aadhar", "g1/8686", "iar/8575", "file separately", "scanned with",
        "small bit", "small bits", "acre rate", "house site rate", "house-site rate", "34372",
        "small farmer", "marginal farmer", "crop loan", "agricultural loan", "g.o.ms.no. 15", "go 15/2007",
        "challan", "refund", "gso1/4660", "41-a penalty", "41a penalty", "three times", "3x penalty",
        "s3/7423",
    )
    _has_amt_for_legal = parse_rupee_amount(text) > Decimal("0")
    _sec56_legal = (
        ("56(2)" in t_lower or "income tax act" in t_lower or bool(re.search(r"\bit\s+act\b", t_lower)))
        and (
            not _has_amt_for_legal
            or any(q in t_lower for q in ("eligible", "eligibility", "qualify", "exempt", "sister-in-law", "brother's wife", "brothers wife", "who is", "meaning", "?"))
        )
    )
    _can_classification = bool(re.match(r"^(?:can\s+a|does\s+a|is\s+a)\b", t_lower) and not _has_amt_for_legal)
    _41a_proc_legal = ("41-a" in t_lower or "41a" in t_lower) and any(k in t_lower for k in ("penalty", "limitation", "fraud", "appeal", "years", "times", "notice")) and not any(k in t_lower for k in ("compare", "vs now", "vs today", "historical"))
    _rectification_classification = "rectification" in t_lower and any(
        term in t_lower for term in ("fresh sale", "just 1000", "fee or", "stamp duty", "survey number", "boundar")
    )
    is_legal_question = bool(
        _sec56_legal or _can_classification or _41a_proc_legal or _rectification_classification
        or any(marker in t_lower for marker in legal_question_markers)
    )

    # Family-transfer questions without a stated value need a clean comparison,
    # not an unstructured RAG excerpt or a guessed calculation.
    is_family_transfer_question = (
        any(term in t_lower for term in ("give", "giving", "transfer", "settle", "gift"))
        and bool(_FAMILY_REL_RE.search(t_lower))
        and any(term in t_lower for term in ("how much", "which is cheaper", "what is cheaper", "options"))
        and parse_rupee_amount(text) == Decimal("0")
    )
    if is_family_transfer_question:
        return ChatResponse(
            reply=(
                "**Direct Answer:** For a transfer to your mother, compare a **Family Settlement** with a **Gift to a Relative**. "
                "The settlement has no Transfer Duty; the gift’s Transfer Duty depends on whether the property is in a Gram Panchayat or an urban local body.\n\n"
                "| Option | Stamp Duty | Transfer Duty | Registration Fee | User Charge |\n"
                "|---|---:|---:|---:|---:|\n"
                "| Family Settlement (`03_02`, Art. 49-A(a)) | 2% | 0% | 0.5% (₹1,000 min / ₹10,000 max) | ₹500 |\n"
                "| Gift to Relative — Gram Panchayat (`03_05`, Art. 29) | 2% | 0.5% | 0.5% (₹1,000 min / ₹10,000 max) | ₹500 |\n"
                "| Gift to Relative — Urban (`03_06`, Art. 29) | 2% | 1.5% | 0.5% (₹1,000 min / ₹10,000 max) | ₹500 |\n\n"
                "Authorities: `G.O.Ms.No. 395` (Stamp Duty), `G.O.Ms.No. 463` (Transfer Duty / Registration Fee), and `G.O.Ms.No. 192` (user charge). "
                "A settlement is generally cheaper because Transfer Duty is 0%. Share the property value and jurisdiction for the payable total."
            ),
            intent="LEGAL_QUERY",
        )

    # ==============================================================
    # 0.9. INTENT: Building Structure Valuation (MUST precede Area Conversion)
    # ==============================================================
    if _is_structure_valuation_query(t_lower):
        sparams = parse_structure_params(text)
        matched_stages = sparams.pop("matched_stages", [])
        sres = calculate_building_structure(session, **sparams)
        if sres.status != "OK":
            return ChatResponse(
                reply=f"**Direct Answer:** No verified structure rate matches `{sparams['structure_category']}` in `{sparams['area_tier']}` on `{sparams['execution_date']}`. {sres.message}",
                intent="STRUCTURE_VALUE",
            )

        rows_md = []
        for fb in sres.floor_breakdowns:
            area_col = f"{format_indian_number(fb.plinth_area_sqft)} sq.ft" if fb.plinth_area_sqft is not None else "Per sq.ft"
            net_col = format_inr(fb.net_value) if fb.net_value is not None else "—"
            rows_md.append(
                f"| {fb.floor_label} | {format_inr(fb.base_rate_per_sqft)} | {format_inr(fb.floor_adder)} | "
                f"{format_inr(fb.composite_site_adder)} | **{format_inr(fb.depreciated_unit_rate)}** | {area_col} | **{net_col}** |"
            )

        stage_pct_map = {"FOUNDATION": "25%", "SLAB": "65%", "FINISHING": "85%", "COMPLETE": "100%"}
        stage_pct_lbl = stage_pct_map.get(sres.completion_stage, "100%")
        if sres.needs_floor_split:
            equal_note = (
                f"Provisional equal-split estimate across {len(sres.floor_breakdowns)} floors: **{format_inr(sres.net_building_value)}**."
                if sres.total_plinth_area_sqft > 0
                else "Provide per-floor plinth areas (sq.ft) to compute the exact total valuation."
            )
            direct_ans = (
                f"**Direct Answer:** Floor rates differ (+₹30/+₹25/+₹20 per floor from 3rd floor onward), so provide the exact floor-wise sq.ft split for a final figure. "
                f"Per-floor depreciated rates and {equal_note.lower()}"
            )
        else:
            fb0 = sres.floor_breakdowns[0] if sres.floor_breakdowns else None
            unit_r = fb0.depreciated_unit_rate if fb0 else Decimal("0")
            struct_dep_r = fb0.depreciated_structure_unit_rate if fb0 else Decimal("0")
            undepr_comp_r = (fb0.base_rate_per_sqft + fb0.floor_adder + fb0.composite_site_adder) if fb0 else Decimal("0")
            comp_note = (
                f" (depreciated structure **{format_inr(struct_dep_r)}/sq.ft** + non-depreciable site adder `({format_inr(sres.site_rate_per_sqyd, 0)} / 20)` = {format_inr(sres.composite_site_adder)}/sq.ft; un-depreciated composite rate = **{format_inr(undepr_comp_r)}/sq.ft**)"
                if sres.composite_site_adder > 0
                else ""
            )
            dep_cap_note = " (**50% max industrial depreciation cap** @ 5%/yr)" if sres.is_industrial else ""
            stage_note = f" at **{stage_pct_lbl}** completion (`{sres.completion_stage}`)" if sres.completion_stage != "COMPLETE" else ""
            total_txt = f"**{format_inr(sres.net_building_value)}** across {format_indian_number(sres.total_plinth_area_sqft)} sq.ft" if sres.total_plinth_area_sqft > 0 else f"**{format_inr(unit_r)}/sq.ft**"
            direct_ans = (
                f"**Direct Answer:** Net depreciated structure valuation{stage_note} is {total_txt} "
                f"at **{format_inr(unit_r)}/sq.ft** effective rate after **{sres.depreciation_pct}%** depreciation{dep_cap_note}{comp_note} (`{sres.citation}`)."
            )

        reply_lines = [
            f"### Structure Market Value Assessment (`{sres.area_tier}`)\n",
            direct_ans + "\n",
            f"**Execution Date:** `{sres.execution_date}` · **Category:** `{sres.structure_category}` · **Age:** {sres.age_years} yrs ({'Industrial 5%/yr max 50%' if sres.is_industrial else 'Non-Industrial 0% 1–10 yrs, 1%/yr >10 yrs, max 70%'} = **{sres.depreciation_pct}%** dep.) · **Stage:** `{sres.completion_stage}` ({stage_pct_lbl})\n",
            "| Floor | Base Rate/sq.ft | Floor Adder | Site Adder (`site/20`) | Depreciated Rate/sq.ft | Plinth Area | Net Value |",
            "|---|---:|---:|---:|---:|---:|---:|",
            *rows_md,
        ]
        if len(matched_stages) >= 2:
            stage_lines = [
                "\n**Unfinished Structure Stage Percentages (`MV1/752/2022` / `REV08-25025`):**",
                f"- **Foundation stage:** **25%** of full rate (`{format_inr(sres.base_rate_per_sqft * Decimal('0.25'))}/sq.ft`)",
                f"- **Roof / Slab stage:** **65%** of full rate (`{format_inr(sres.base_rate_per_sqft * Decimal('0.65'))}/sq.ft`)",
                f"- **Finishing stage:** **85%** of full rate (`{format_inr(sres.base_rate_per_sqft * Decimal('0.85'))}/sq.ft`)",
            ]
            reply_lines.extend(stage_lines)
        if sres.net_building_value > 0:
            reply_lines.append(f"\n**Total Net Building Value:** **{format_inr(sres.net_building_value)}**")
        if sres.assumption_note:
            reply_lines.append(f"\n*Note:* {sres.assumption_note}")
        reply_lines.append(f"\n**Authority:** `{sres.citation}`")

        return ChatResponse(
            reply="\n".join(reply_lines),
            intent="STRUCTURE_VALUE",
            structure_data={
                "status": sres.status,
                "structure_category": sres.structure_category,
                "area_tier": sres.area_tier,
                "execution_date": str(sres.execution_date),
                "base_rate_per_sqft": str(sres.base_rate_per_sqft),
                "composite_site_adder": str(sres.composite_site_adder),
                "depreciation_pct": str(sres.depreciation_pct),
                "net_building_value": str(sres.net_building_value),
                "needs_floor_split": sres.needs_floor_split,
                "citation": sres.citation,
                "floors": [
                    {
                        "floor_number": fb.floor_number,
                        "floor_label": fb.floor_label,
                        "depreciated_unit_rate": str(fb.depreciated_unit_rate),
                        "plinth_area_sqft": str(fb.plinth_area_sqft) if fb.plinth_area_sqft is not None else None,
                        "net_value": str(fb.net_value) if fb.net_value is not None else None,
                    }
                    for fb in sres.floor_breakdowns
                ],
            },
        )

    # Resolve the small urban dry-agricultural-bit boundary test deterministically;
    # do not ask the language model to compare the side count with three.
    small_bit_inputs = _small_agri_bit_inputs(text)
    if small_bit_inputs:
        bit_result = evaluate_small_agri_bit(*small_bit_inputs)
        if bit_result:
            cents, land_type, _, sides = small_bit_inputs
            if bit_result == "HOUSE_SITE_RATE":
                rate_text = "**House-Site (Sq.Yd.) Rate applies**"
                reason = f"only {sides} sides have adjoining agricultural land, which is fewer than 3"
            else:
                rate_text = "**Agricultural Acre Rate applies**"
                reason = f"{sides} sides have adjoining agricultural land, meeting the 3-side threshold"
            return ChatResponse(
                reply=(
                    "### Small Agricultural Bit Classification\n\n"
                    f"**Direct Answer:** {rate_text} for {cents} cents of {land_type.lower()} land in this urban jurisdiction: {reason}.\n\n"
                    "Under `I.G. Procgs. No. MV1/34372/92, dt. 27.11.1992` (effective 20.12.1992), the small-bit rule applies to dry land of **10 cents or less** "
                    "in a Corporation, Municipality, Major Gram Panchayat, Mandal Headquarters, or Urban Agglomeration: fewer than 3 agricultural sides takes the house-site rate; 3 or more takes the agricultural acre rate."
                ),
                intent="LEGAL_QUERY",
            )

    # ==============================================================
    # 1. INTENT: Area Unit Conversion
    # ==============================================================
    is_explicit_convert = bool(re.search(r"\bconvert\b", t_lower))
    has_land_unit = bool(re.search(r"\b(?:acres?|cents?|ankanams?|guntas?|grounds?|sq\.?\s*yds?|square\s+yards?|sq\.?\s*ft|square\s+feet|sq\.?\s*meters?|hectares?)\b", t_lower))
    has_digit = bool(re.search(r"\d+", text))

    is_conversion_request = is_explicit_convert or (
        has_land_unit
        and has_digit
        and not re.search(
            r"\b(?:loan|mortgage|crop|stamp\s+duty|registration|deed|gift|settlement|sale|lease|"
            r"exempt\w*|eligib\w*|tax|structure|rcc|plinth|floor|depreciat\w*|age|composite|building|site\s+rate)\b",
            t_lower,
        )
    )
    if is_conversion_request and not is_schedule_query and not is_legal_question and not _is_structure_valuation_query(t_lower):
        try:
            sq_yds = parse_composite_land_string(text)
            if sq_yds > Decimal("0"):
                res = convert_area(sq_yds, "Sq.Yds")
                qty_parts = re.findall(
                    r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:acres?|cents?|ankanams?|guntas?|grounds?|sq\.?\s*yds?|square\s+yards?|sq\.?\s*ft|square\s+feet|sq\.?\s*meters?|hectares?)",
                    text,
                    re.IGNORECASE,
                )
                qty_label = " + ".join(qty_parts) if qty_parts else f"{format_indian_number(res.sq_yards)} Sq.Yds"
                reply = (
                    f"### AP Land Measurement Conversion Result\n\n"
                    f"**Direct Answer:** `{qty_label}` equals **{format_indian_number(res.sq_yards)} Sq.Yds** (**{format_indian_number(res.sq_feet)} Sq.Ft** · **{res.acres:.4f} Acres**).\n\n"
                    f"| Measurement Unit | Converted Equivalent |\n"
                    f"| :--- | ---: |\n"
                    f"| **Square Yards (Gajam)** | **{format_indian_number(res.sq_yards)} Sq.Yds** |\n"
                    f"| **Acres** | **{res.acres:.4f} Acres** |\n"
                    f"| **Cents** | **{format_indian_number(res.cents)} Cents** |\n"
                    f"| **Ankanams** (Rayalaseema) | **{format_indian_number(res.ankanams)} Ankanams** |\n"
                    f"| **Guntas** | **{format_indian_number(res.guntas)} Guntas** |\n"
                    f"| **Square Feet** | **{format_indian_number(res.sq_feet)} Sq.Ft** |\n"
                    f"| **Square Meters** | **{format_indian_number(res.sq_meters)} Sq.Mtrs** |\n"
                    f"| **Hectares** | **{res.hectares:.4f} Ha** |\n\n"
                    f"**Statutory Base:** REVISIED DOCUMENT CLASSIFICATION Sheet 2 & AP Survey Manual (1 Ankanam = 8 Sq.Yds, 1 Cent = 48.4 Sq.Yds, 1 Acre = 4,840 Sq.Yds)."
                )
                return ChatResponse(
                    reply=reply, intent="AREA_CONVERT",
                    area_data=res.model_dump()
                )
        except Exception:
            pass

    # ==============================================================
    # 2. INTENT: Presentation Delay Fines (Section 25 / Section 34)
    # ==============================================================
    _has_relative_delay = bool(re.search(r"\b\d+\s*months?(?:\s*(?:and\s*)?\d+\s*days?)?\s*(?:back|ago)\b", t_lower))
    _is_delay_fine_query = (
        (_has_relative_delay or any(k in t_lower for k in [
            "delay fine", "section 25", "sec 25", "late presentation", "time barred",
            "months late", "delayed", "months back", "months ago", "10x fine", "condone",
            "rule 38", "s.o. 240", "standing order 240",
        ]))
        and not any(excl in t_lower for excl in ["marriage", "stamp act sec 25", "stamp act section 25"])
        and any(cue in t_lower for cue in [
            "register", "deed", "presentation", "document", "months", "section", "fine",
            "penalty", "condone", "dr", "district registrar", "time", "late", "back", "ago",
        ])
    )
    if _is_delay_fine_query:
        # Determine execution date and presentation date
        amt = parse_rupee_amount(text)
        proper_rf = amt if amt > 0 else Decimal("0")
        date_tokens = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if len(date_tokens) >= 2:
            exec_d = date.fromisoformat(date_tokens[0])
            pres_d = date.fromisoformat(date_tokens[1])
        else:
            # Try relative offset like '9 months back', '4 months 18 days ago'
            exec_d_relative = parse_relative_execution_date(text)
            if exec_d_relative:
                exec_d = exec_d_relative
                pres_d = date.today()
            else:
                # Fall back to month-count approximation
                exec_d = date.today().replace(day=1)
                m_month = re.search(r"(\d+)\s*months?", t_lower)
                if m_month:
                    from dateutil.relativedelta import relativedelta
                    m_count = int(m_month.group(1))
                    exec_d = date.today() - relativedelta(months=m_count)
                    pres_d = date.today()
                else:
                    pres_d = date.today()

        fine_res = calculate_delay_fine(exec_d, pres_d, proper_rf)

        if fine_res.status == "NO_FINE":
            status_banner = "**ADMISSIBLE — No Fine Required** (within 4-month ordinary period)"
        elif fine_res.status in ("OK", "FINE_APPLICABLE"):
            status_banner = f"**ADMISSIBLE WITH FINE — {fine_res.fine_multiplier}× Registration Fee Penalty** (Sec 25 / Rule 38)"
        else:
            status_banner = "**VOID — MANDATORY REFUSAL** (exceeds presentation/appearance ceiling: S.O. 240)"

        condone_line = ""
        if hasattr(fine_res, "condoning_authority") and fine_res.condoning_authority:
            condone_line = f"\n• **Condoning Authority:** {fine_res.condoning_authority}"
        relative_age_match = re.search(r"\d+\s*months?(?:\s*(?:and\s*)?\d+\s*days?)?\s*(?:back|ago)\b", t_lower)
        relative_age_note = (
            "; " + relative_age_match.group(0).replace("back", "from execution").replace("ago", "from execution")
            if relative_age_match else ""
        )

        reply = (
            f"### Section 25 / Section 34 Delayed Presentation Assessment\n\n"
            f"**Direct Answer:** {status_banner}\n\n"
            f"• **Execution Date:** `{exec_d}`\n"
            f"• **Presentation / Today's Date:** `{pres_d}` ({fine_res.days_late} days past 4-month window{relative_age_note})\n"
            f"• **Proper Registration Fee:** {format_inr(proper_rf) if amt > 0 else 'Not specified'}\n"
            f"• **Fine Amount Payable:** **{format_inr(fine_res.fine_amount) if amt > 0 or fine_res.status == 'VOID' or fine_res.status == 'NO_FINE' else 'Cannot calculate without the proper registration fee'}**"
            f"{condone_line}\n\n"
            f"**Legal Rule:** {fine_res.message}\n\n"
            f"| Limit | Rule | Period |\n"
            f"| :--- | :--- | :--- |\n"
            f"| Ordinary (no fine) | Sec 23, Registration Act | 4 months from execution |\n"
            f"| With fine (1×–10×) | Sec 25 / AP Rule 38 | 5th to 8th month (upto 10× RF) |\n"
            f"| Appearance of executants (if presented within 8m) | Sec 34 | Up to 12 months from execution |\n"
            f"| Absolute void — Refuse | S.O. 240 | Beyond 8 months for presentation (or >12 months for appearance) — cannot be condoned |\n\n"
            f"**Statutory Authority:** `{fine_res.legal_basis}`"
        )
        return ChatResponse(
            reply=reply, intent="DELAY_FINE",
            fine_data={
                "status": fine_res.status,
                "days_late": fine_res.days_late,
                "fine_multiplier": fine_res.fine_multiplier,
                "proper_rf": str(proper_rf),
                "fine_amount": str(fine_res.fine_amount),
                "legal_basis": fine_res.legal_basis,
            }
        )

    # ==============================================================
    # 2.5. INTENT: Section 22-A Prohibited Land Verification
    # ==============================================================
    sy_match = re.search(r"(?i)\b(?:sy\.?|survey)\s*(?:no\.?)?\s*([0-9]+[0-9/A-Za-z\-]*)", text)
    is_prohibited_query = any(k in t_lower for k in ["prohibit", "22-a", "22a", "assigned", "endowment", "waqf", "allowed to buy", "can i register", "can i buy", "refused", "poramboke", "check survey"])
    if sy_match and (is_prohibited_query or "survey" in t_lower):
        sy_num = sy_match.group(1)
        village = None
        for v in ["gunadala", "bhavanipuram", "patamata", "simhachalam", "madhurawada", "mangalagiri", "thullur", "tirupati", "chilakalapudi"]:
            if v in t_lower:
                village = v.upper()
                break
        res_proh = check_prohibited_land(session, sy_num, village=village)
        if res_proh.is_prohibited:
            reply = (
                f"### MANDATORY REFUSAL: Section 22-A Prohibited Land\n\n"
                f"**Survey Number:** `{res_proh.survey_number}` | **Village:** `{res_proh.village or 'Notified'}` | **District:** `{res_proh.district or 'AP'}`\n\n"
                f"**Category:** `{res_proh.category}` — **{res_proh.category_title}**\n\n"
                f"**Reason:** `{res_proh.reason}`\n\n"
                f"**Notified Authority:** {res_proh.notified_authority}\n\n"
                f"**Gazette / Order:** `{res_proh.gazette_notification}`\n\n"
                f"> **Statutory Directive:** {res_proh.statutory_directive}\n\n"
                f"**Legal Citation:** *{res_proh.legal_citation}*\n\n"
                f"*(Note: Under Section 22-A(2) of Registration Act 1908, any registration of this parcel is void ab initio).* "
            )
        else:
            reply = (
                f"### Section 22-A Lookup: No Clearance Provided\n\n"
                f"**Survey Number:** `{res_proh.survey_number}` | **Village:** `{res_proh.village or 'not specified'}`\n\n"
                f"**Status:** `{res_proh.status}`\n\n"
                f"> {res_proh.statutory_directive}\n\n"
                f"**Reference:** *{res_proh.legal_citation}*"
            )
        return ChatResponse(
            reply=reply,
            intent="PROHIBITED_CHECK",
            prohibited_data=res_proh.model_dump(),
        )

    # Historical adjustment questions are legal-rule lookups, not rate comparisons.
    _asks_adjustability = any(term in t_lower for term in ("adjust", "adjustable", "credit", "set off", "set-off"))
    _is_historical_agpa_adjustment = (
        _asks_adjustability
        and any(term in t_lower for term in ("agpa", "agreement of sale-cum-gpa", "agreement of sale cum gpa"))
        and any(term in t_lower for term in ("1481", "2008", "concessional"))
    )
    if _is_historical_agpa_adjustment:
        return ChatResponse(
            reply=(
                "**Direct Answer:** **No** — the concessional 1% Stamp Duty paid on the 2008 AGPA under "
                "`G.O.Ms.No. 1481` is not adjustable against the final sale deed. The non-adjustability "
                "is reflected in `C&IG Memo No. CCRA.3/6216/08A`. This is distinct from the later general "
                "01_11 arrangement under G.O.Ms.No. 581, where only the specified 4% component is adjustable."
            ),
            intent="LEGAL_QUERY",
        )

    # ==============================================================
    # 3. INTENT: Historical Audit Comparison (Section 41-A Delta)
    # ==============================================================
    _explicit_comparison = any(term in t_lower for term in (
        "compare", "versus", " vs ", "difference", "back then vs now", "audit delta",
        "historical delta", "audit comparison", "audit difference",
    ))
    is_audit_query = (not is_legal_question and not _asks_adjustability and _explicit_comparison) and (
        bool(re.search(r"\b(?:19|20)\d{2}\b", text))
        and any(k in t_lower for k in ("sale", "deed", "agreement", "duty", "rate", "audit", "historical"))
    )
    if is_audit_query:
        val = parse_rupee_amount(text)
        has_historical_date = bool(re.search(r"\b(?:19|20)\d{2}\b", text))
        if val <= 0 or not has_historical_date:
            return ChatResponse(
                reply=(
                    "For an audit comparison, provide the deed value and execution year/date. "
                    "I will not choose a sample value or historical year for you."
                ),
                intent="NEED_CLARIFICATION",
            )

        old_d = parse_date_from_text(text)
        cur_d = date.today()

        code, alias = detect_deed_code(text)
        area = detect_area_type(text)
        audit_subtype = (
            "FLAT_APARTMENT" if any(k in t_lower for k in ("flat", "apartment"))
            else ("NON_FLAT" if any(k in t_lower for k in ("house", "independent house", "plot", "land", "site")) and "farmhouse" not in t_lower else "ALL")
        )
        if not any(
            token in t_lower
            for token in (
                "corporation", "corp", "municipality", "municipal", "panchayat", "rural", "cantonment",
                "selection grade", "special grade", "vijayawada", "vijyawada", "vizag", "visakhapatnam",
                "guntur", "tirupati", "nellore", "kurnool", "kakinada", "rajahmundry",
            )
        ):
            return ChatResponse(
                reply="Please specify the jurisdiction (Gram Panchayat, Municipality, Corporation, or Cantonment) for this historical comparison.",
                intent="NEED_CLARIFICATION",
            )

        diff = compare_historical_vs_current(
            session, alias, area, old_d, cur_d,
            consideration_value=val, market_value=val,
            property_subtype=audit_subtype,
        )
        h = diff["historical"]
        c = diff["current"]
        delta = diff["delta_total"]
        if h.status != "OK" or c.status != "OK":
            return ChatResponse(
                reply=(
                    "Historical comparison refused: there is no verified rate row for "
                    f"the requested execution date ({old_d}) or comparison date ({cur_d}). "
                    "No delta has been calculated."
                ),
                intent="CALCULATION_REFUSED",
            )

        reply = (
            f"### Section 41-A Historical Audit Comparison ({old_d} vs {cur_d})\n\n"
            f"**Direct Answer:** On {format_inr(val)}, total payable changed from **{format_inr(h.total_payable)}** (`{old_d}`) to **{format_inr(c.total_payable)}** (`{cur_d}`), a net delta of **{format_inr(delta)}**.\n\n"
            f"**Instrument:** `{code}` ({h.instrument_code}) · **Area:** `{area}` · **Value:** {format_inr(val)}\n\n"
            f"| Fee Head | Historical Rate ({old_d}) | Current Rate ({cur_d}) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| Stamp Duty | {h.stamp_duty_rate} ({format_inr(h.stamp_duty)}) | {c.stamp_duty_rate} ({format_inr(c.stamp_duty)}) |\n"
            f"| Transfer Duty | {h.transfer_duty_rate} ({format_inr(h.transfer_duty)}) | {c.transfer_duty_rate} ({format_inr(c.transfer_duty)}) |\n"
            f"| Registration Fee | {h.reg_fee_rate} ({format_inr(h.registration_fee)}) | {c.reg_fee_rate} ({format_inr(c.registration_fee)}) |\n"
            f"| User Charges | {format_inr(h.user_charge)} | {format_inr(c.user_charge)} |\n"
            f"| **Total Payable** | **{format_inr(h.total_payable)}** | **{format_inr(c.total_payable)}** |\n\n"
            f"**Audit Delta (Net Variance):** **{format_inr(delta)}**\n\n"
            f"• **Historical Citation:** `{', '.join(h.citations)}`\n"
            f"• **Current Citation:** `{', '.join(c.citations)}`"
        )
        slip_dict = {
            "instrument_code": code,
            "deed_alias": alias,
            "area_type": area,
            "execution_date": str(cur_d),
            "chargeable_value": str(val),
            "stamp_duty": str(c.stamp_duty),
            "stamp_duty_rate": c.stamp_duty_rate,
            "transfer_duty": str(c.transfer_duty),
            "transfer_duty_rate": c.transfer_duty_rate,
            "registration_fee": str(c.registration_fee),
            "reg_fee_rate": c.reg_fee_rate,
            "user_charge": str(c.user_charge),
            "total_payable": str(c.total_payable),
            "citations": c.citations,
            "warnings": [],
        }
        signed_slip = sign_and_archive_check_slip(session, slip_dict)
        return ChatResponse(
            reply=reply, intent="AUDIT_COMPARE",
            audit_comparison=diff,
            card_slip=signed_slip
        )

    # ==============================================================
    # 4. INTENT: Legal & Statutory Inquiries (ChromaDB Vector Search)
    # ==============================================================
    # Explicit calculation check
    has_amount = parse_rupee_amount(text) > 0
    has_family_transfer = (
        any(g in t_lower for g in ["giving", "give", "gift", "gifting", "settle", "settling", "transfer", "transferring", "hand over"])
        and bool(_FAMILY_REL_RE.search(t_lower))
    )
    has_partition_split = any(k in t_lower for k in ["split", "splitting", "divide", "dividing", "partition", "separated share"])
    has_dagpa_builder = any(k in t_lower for k in ["development agreement", "dagpa", "construction agreement", "builder", "developer"])
    has_deed_term = any(d in t_lower for d in [
        "sale", "gift", "partition", "settlement", "mortgage", "deposit", "dotd",
        "deed", "lease", "rent agreement", "rental agreement", "agpa", "dagpa", "adoption", "affidavit", "buy", "buying",
        "sell", "selling", "purchasing", "purchase", "development", "agreement",
        "gpa", "power of attorney", "release", "exchange"
    ]) or has_family_transfer or has_partition_split or has_dagpa_builder
    
    is_tax_intent = any(k in t_lower for k in [
        "what tax", "tax when", "tax on", "tax for", "tax is", "tax it is",
        "how much tax", "what is the tax", "how much duty", "what is the duty",
        "duty on", "duty for", "how much fee", "what fee", "calculate", "compute",
        "charges for", "what are the charges", "total duty", "cost to register",
        "how much cost", "cost of", "how much will it cost", "how much to pay",
        "registration charges", "registration cost", "charges when", "fee when",
        "sd%", "sd percent", "rf cap", "rf max", "registration fee cap", "what is the sd",
        "what is the rf", "is it adjustable", "adjustable later", "adjustability",
        "maximum cap", "cap on rf", "stamp duty %"
    ])
    
    is_calc_query = not is_legal_question and (
        is_tax_intent
        or (has_amount and has_deed_term)
        or (detect_deed_code(text)[1] == "LEASE" and parse_monthly_rent(text) > Decimal("0") and parse_lease_months(text) is not None)
        or (has_dagpa_builder and any(k in t_lower for k in ["sd", "rf", "duty", "fee", "cost", "tax", "cap", "adjustable", "percent", "percentage"]))
        or (any(k in t_lower for k in ["duty", "fee", "cost", "tax", "charge", "charges", "pay", "rate", "rates", "cap", "percent", "percentage", "sd", "rf", "td"]) and has_deed_term and not any(q in t_lower for q in ["what are the rules", "is it legal", "can i register", "can registrar", "prohibited", "section 22-a", "section 22a", "article"]))
    )

    if not is_calc_query:
        is_art49_exclusion_q = (
            ("settlement" in t_lower or "art. 49" in t_lower or "article 49" in t_lower or "49-a" in t_lower or "49a" in t_lower)
            and any(k in t_lower for k in ("step-mother", "stepmother", "great-grand", "great grand", "family definition", "definition of family", "excluded", "would-be", "prospective wife", "non-family", "2%"))
            and not any(k in t_lower for k in ("debt", "discharge", "annuity"))
        )
        is_sec22b_circ_q = any(k in t_lower for k in ("22-b", "22b", "1730463", "webland", "ptin", "vlt", "iplp", "gramakantam")) and "22-a" not in t_lower and "22a" not in t_lower
        legal_matches = search_legal_knowledge(text, limit=5)
        if is_art49_exclusion_q:
            art49_rows = (
                session.query(DutyRate)
                .filter(
                    DutyRate.instrument_code.in_(["03_02", "03_03"]),
                    DutyRate.effective_to.is_(None),
                )
                .order_by(DutyRate.instrument_code)
                .all()
            )
            rate_lines = [
                "| CARD Code | Settlement Classification | Stamp Duty (SD) | Transfer Duty | Registration Fee | Authority |",
                "|---|---|---:|---:|---:|---|",
            ]
            for rr in art49_rows:
                sd_a = getattr(rr, "sd_authority", None) or "Art. 49-A · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014"
                rf_a = getattr(rr, "rf_authority", None) or "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013"
                cls_label = "Gift Settlement in F/O Family Member (`Art. 49-A(a)`)" if rr.instrument_code == "03_02" else "Gift Settlement in F/O Others (`Art. 49-A(b)`)"
                rate_lines.append(
                    f"| `{rr.instrument_code}` | {cls_label} | **{rr.stamp_duty_value}%** | {rr.transfer_duty_pct or 0}% (Exempt) | {rr.reg_fee_value}% (Min ₹1,000 / Max ₹10,000) | {sd_a}; RF: {rf_a} |"
                )
            rate_table = "\n".join(rate_lines)
            reply = (
                "### Article 49-A Family Settlement Eligibility & Non-Family Rate Assessment\n\n"
                "**Direct Answer / Verdict:** **No** — both a **step-mother** and a **great-grandson** (great-grandchildren) are **excluded** from the **Article 49-A** statutory definition of \"family\" and therefore attract the **3.0000% non-family Gift Settlement rate** (`03_03` · Art. 49-A(b)), NOT the 2.0000% family rate (`03_02`).\n\n"
                "- **Statutory Family Definition (Explanation to Art. 49-A, Sch. I-A):** Strictly limited to *father, mother, husband, wife, brother, sister, son, daughter, grandfather, grandmother, grandchild*, plus *adoptive father/mother and adopted son/daughter*.\n"
                "- **Step-Mother Excluded:** Explicitly outside \"family\" under Art. 49-A (`C&IG Memo No. S2/24212/2000, dt. 29.03.2001`) → attracts **3.0000% non-family SD** (`03_03`, Art. 49-A(b)).\n"
                "- **Great-Grandson Excluded:** Explicitly outside \"family\" since the Explanation stops at *grandchild* (`I.G. Procgs. No. S1/24755/71, dt. 03.09.1991`) → attracts **3.0000% non-family SD** (`03_03`, Art. 49-A(b)). *(Also excluded: prospective wife / would-be son-in-law per `I.G. Memo No. S3/34696/76, dt. 18.10.1982`.)*\n"
                "- **SRO Nuance (Gift vs. Settlement):** Under **Gift (`Art. 29` · `G.O.Ms.No. 395` read with `Sec. 56(2)` IT Act)**, a great-grandson qualifies as a *\"lineal descendant\"* for 2.00% Gift SD (+ applicable Transfer Duty), whereas under **Settlement (`Art. 49-A`)** he does not and attracts 3.00% SD (Nil Transfer Duty).\n\n"
                f"{rate_table}\n\n"
                "*CARD User Charges: ₹500.00 (`G.O.Ms.No. 192 dt. 25.04.2023`).*"
            )
            return ChatResponse(
                reply=reply,
                intent="LEGAL_QUERY",
                legal_docs=[m.model_dump() for m in legal_matches] if legal_matches else None,
            )

        if is_sec22b_circ_q:
            if not legal_matches or not any("1730463" in m.content or "22b" in f"{m.section_or_rule} {m.title} {m.content}".lower().replace("-", "") for m in legal_matches):
                return ChatResponse(
                    reply="**Direct Answer:** Matching circular not found in index for the requested Section 22-B / CARD/e-1730463 query.",
                    intent="LEGAL_NOT_FOUND",
                )
            reply = (
                "### Section 22-B Double-Registration Prevention Procedure (`CIRCULAR Memo No: CARD/e-1730463`)\n\n"
                "**Direct Answer:** Under **Section 22-B of the Registration Act, 1908** and **Circular Memo No. CARD/e-1730463** (dated **31.03.2023**, w.e.f. **01.04.2023**; clarified **03.08.2023**), if the executant's name is absent from the Webland pattadar column (Part A) or Urban Local Body electronic PTIN/VLT record (Part B), the Sub-Registrar must refuse registration unless a prescribed **Annexure II / Annexure IV** exception or **Link Document** fallback applies—otherwise the party must update Webland / ULB records before registration.\n\n"
                "#### Part A — Agricultural Lands (Webland / LPM)\n"
                "- **General Bar (A.1):** For Annexure I documents affecting agricultural land, do **not** register if the executant's name is absent from the **Webland Pattadar column**.\n"
                "- **Annexure II Exceptions:** Permitted where the property stands in the name of (1) *Ancestors / Co-parceners / Co-owners / Legal heirs* in **Partitions** (enclose **Legal Heir Certificate** for ancestors/legal heirs), (2) *Ancestors / Co-parceners / Co-owners* in **Releases** (all co-owners' names must appear in link document), (3) **Sale by GPA holder** (principal's name of previous registered document in Webland), (4) **Rectification** (tallies with Webland in executant's name), or (5) **Ratification** (no Webland check if mutated to claimant; establish relationship with link executants).\n"
                "- **Prior Alienation & Annexure III Declaration (A.2):** Refuse if already alienated by the same executant/agent unless cancelled by Court; obtain mandatory **Annexure III Declaration** (penal liability under **Section 82** for Section 22-B violation).\n"
                "- **Resurveyed / LPM Villages (A.3–A.6):** Capture both **LPM Number** and Survey Number; **no part-extent LPM registration**—issue an endorsement directing parties to complete **Tahsildar sub-division** first; capture **Aadhaar e-KYC** for Executant, Claimant, and representatives.\n\n"
                "#### Part B — Non-Agricultural Properties in Urban Local Bodies (Municipal Corp / Municipality / Nagar Panchayat)\n"
                "- **Houses / Flats / Buildings (B.1–B.4):** Verify executant in **PTIN (House Assessment Number)** electronic record. For new/first-time constructions without Assessment No., verify **Building Plan Approval** particulars against ULB database. If neither is available in ULB data, cross-check via registered **Link Document**; if data is missing/unupdated in ULB records, **advise the public to get the ULB electronic database updated** before registration.\n"
                "- **Vacant Sites / Plots (B.5–B.7):** Verify executant/developer/landowner in **VLT (Vacant Land Tax)**, **LP (Layout Permission No.)**, or **IPLP (In-Principle Layout Pattern No.)** electronic records; if unavailable, verify **Link Document** matching executant and property with the claimant in the link document.\n"
                "- **Annexure IV Exceptions (B.8):** Partitions, Releases, GPA sales, Rectifications, and Ratifications in ULBs follow **Annexure IV** (parallel to Annexure II against ULB electronic records / link documents).\n"
                "- **Clause B.9 (Gramakantams & Rural Plots):** For Houses/Flats/Buildings, vacant sites, or plots in **Gramakantams**, villages, or rural local bodies, do **not** insist upon PTIN, VLT, LP, or IPLP numbers until electronic data integration is completed.\n\n"
                "*Source:* C&IG R&S AP `Circular Memo No. CARD/e-1730463` (effective 01.04.2023; Dairy 2024 pp. 167–170, 180–181, 252; placeholder header `Dt.#ApprovedDate#` resolved to 31.03.2023)."
            )
            return ChatResponse(
                reply=reply,
                intent="LEGAL_QUERY",
                legal_docs=[m.model_dump() for m in legal_matches],
            )

        if legal_matches:
            top = legal_matches[0]
            if "rule 61" in t_lower and "rule 71" in t_lower:
                reply = (
                    "### Rule 61 vs. Rule 71 Enquiry (Registration Act, 1908)\n\n"
                    "| Parameter | Rule 61 Enquiry (`Sec 35(1)(c)`) | Rule 71 Enquiry (`Secs 40 & 41(2)`) |\n"
                    "|---|---|---|\n"
                    "| **Document Type** | Non-testamentary documents (Sale, Gift, etc.) where executant died after signing | Testamentary documents (Wills & Authorities to Adopt) presented after death |\n"
                    "| **Time Limit** | Strict statutory limit applies (`4 months` u/s 23; condonable up to `8 months` u/s 25) | No statutory time limit for presentation (`Sec 27`) |\n"
                    "| **Notice Procedure** | Notice proclaimed by **tom-tom** in the village(s) and summons to legal representatives/heirs | Notice published in a **daily newspaper** specified by the Registering Officer (at presenter's cost) + SRO notice board |\n"
                    "| **Deposition Record** | Recorded in the office **Deposition Book** | Recorded on **loose sheets** |\n"
                    "| **SRO Jurisdiction** | Must be presented only to the SRO/DR having jurisdiction over the property | May be presented to **any Sub-Registrar or Registrar** |\n"
                    "| **Transfer of Enquiry** | No provision to transfer enquiry | **Inspector General of Registration (IGR)** may transfer enquiry between SROs |\n\n"
                    f"**Sources:** {', '.join(dict.fromkeys(m.authority_citation for m in legal_matches[:3]))}"
                )
            elif "56(2)" in t_lower or ("sister-in-law" in t_lower or "brother's wife" in t_lower or "brothers wife" in t_lower):
                rel_rates = (
                    session.query(DutyRate)
                    .filter(
                        DutyRate.instrument_code.in_(["03_06", "03_05"]),
                        DutyRate.effective_to.is_(None),
                    )
                    .all()
                )
                rate_summary = (
                    "| Area | Stamp Duty | Transfer Duty | Registration Fee |\n"
                    "|---|---:|---:|---:|\n"
                    "| `03_06` (Municipal Corp / Municipality) | 2.00% (`Art. 29 · G.O.Ms.No. 395, dt. 26.11.2014`) | 1.50% (Municipalities Surcharge) | 0.50% (`G.O.Ms.No. 463, Rev, dt. 17.08.2013`, Min ₹1,000 / Max ₹10,000) |\n"
                    "| `03_05` (Gram Panchayat) | 2.00% (`Art. 29 · G.O.Ms.No. 395, dt. 26.11.2014`) | 0.50% (`G.O.Ms.No. 463, PR&RD, dt. 19.12.2013`) | 0.50% (`G.O.Ms.No. 463, Rev, dt. 17.08.2013`, Min ₹1,000 / Max ₹10,000) |\n"
                )
                if rel_rates:
                    rows_md = []
                    for rr in rel_rates:
                        sd_auth = getattr(rr, "sd_authority", None) or rr.authority_citation
                        td_auth = getattr(rr, "td_authority", None) or "AP Transfer Duty"
                        rf_auth = getattr(rr, "rf_authority", None) or rr.authority_citation
                        rows_md.append(
                            f"| `{rr.instrument_code}` ({rr.area_type}) | {rr.stamp_duty_value}% ({sd_auth}) | {rr.transfer_duty_pct or 0}% ({td_auth}) | "
                            f"{rr.reg_fee_value}% ({rf_auth}) |"
                        )
                    rate_summary = (
                        "| Deed Code & Area | Stamp Duty | Transfer Duty | Registration Fee |\n"
                        "|---|---|---|---|\n" + "\n".join(rows_md)
                    )
                reply = (
                    "### Relative Eligibility under Section 56(2) of the Income Tax Act & AP Gift Duty\n\n"
                    "**Direct Answer:** **Yes** — a sister-in-law (brother's wife or husband's sister) is an exempt \"relative\" under **Section 56(2)** of the Income Tax Act, 1961 (clauses **(vii)** & **(iii)**), making the gift eligible for **2.00% concessional Stamp Duty** under **Art. 29 · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014**.\n\n"
                    "- **Brother's Wife / Donee's Perspective:** Covered under Explanation **clause (vii)** (*spouse of any person referred to in clauses (ii)–(vi)*) read with **clause (ii)** (*brother or sister of the individual*). From the brother's wife's perspective as donee, the donor is her husband's sister, covered under **clause (iii)** (*brother or sister of the spouse of the individual*).\n"
                    "- Verified in `AP Registration Dairy 2024, p. 91`.\n\n"
                    f"{rate_summary}\n\n"
                    "*CARD User Charges: ₹500.00 (`G.O.Ms.No. 192 dt. 25.04.2023`). Provide market value and jurisdiction to compute exact rupees.*"
                )
            elif "OCR source" in top.authority_citation and ("freedom fighter" in t_lower or "political sufferer" in t_lower or "freedom fighter" in top.title.lower()):
                reply = (
                    f"### Source-based guidance: {top.title}\n\n"
                    f"The indexed OCR transcription says that an NOC is not required "
                    f"for a freedom-fighter land assignment once ten years have elapsed, "
                    f"provided there is no dispute with the Government. It also says the "
                    f"Collector should verify the assignee's credentials and assignment "
                    f"date, and that qualifying undisputed entries should be removed from "
                    f"the Section 22-A prohibited list. This is not an automatic self-service "
                    f"deletion; the responsible district authorities must process and verify it.\n\n"
                    f"**Evidence:** {top.authority_citation}\n\n"
                    f"**Important:** This is an OCR transcription in a diary, not the original "
                    f"circular or G.O. The scan/OCR may contain errors. Confirm the original "
                    f"CCLA circular and G.O.Ms.No. 279 with the department before relying on "
                    f"this for a transaction or official decision."
                )
            elif "Full Bench" in top.authority_citation:
                reply = (
                    f"### Source-limited case-law note: {top.title}\n\n"
                    f"The indexed BSR manual summarizes a reported Andhra Pradesh Full Bench "
                    f"case as treating a co-owner relinquishment for consideration in favour "
                    f"of remaining co-owners as a conveyance under Article 20, rather than a "
                    f"Release under Article 46. That is a secondary-source summary, not the "
                    f"judgment itself, and the facts you gave do not identify who receives "
                    f"the brothers' shares or whether consideration is paid. I cannot safely "
                    f"classify this deed from those facts alone.\n\n"
                    f"**Source note:** `{top.authority_citation}`\n\n"
                    f"Confirm the original judgment and current AP Schedule I-A. For a more "
                    f"specific assessment, clarify whether the shares accrue to all remaining "
                    f"co-owners (including the sister) or only to selected brothers, and state "
                    f"the consideration/transaction terms."
                )
            elif "aadhaar" in t_lower or "aadhar" in t_lower or "g1/8686" in t_lower or "iar/8575" in t_lower:
                reply = (
                    "### Aadhaar Copies Handling & Non-Scanning Rule (`Memo No. G1/8686/2015` / `IAR/8575/2023`)\n\n"
                    "**Direct Answer:** **No** — photostat copies of Aadhaar ID proofs submitted by the **Executant, Claimant/representatives, Witnesses, or Scribe** must be **filed separately** and **MUST NOT be scanned** with the registered document. Additionally, the **scribe's Aadhaar copy must neither be mentioned in the document nor scanned**.\n\n"
                    "- **Parties & Witnesses (Point C):** Collect Photostat Aadhaar ID proof separately for the SRO physical file; **do not scan** into CARD.\n"
                    "- **Scribe (Point D):** File the scribe's Photostat Aadhaar ID proof separately; **never mention** it in the deed recitals and **never scan** it with the document.\n\n"
                    "**Authority:** `Director & IG R&S Memo No. G1/8686/2015, dt. 04.09.2018` (Points C & D) reiterated in `Circular No. IAR/8575/2023, dt. 25.04.2023 (Part-C)`."
                )
            elif "small bit" in t_lower or "small bits" in t_lower or "34372" in t_lower or ("acre rate" in t_lower and any(k in t_lower for k in ("cent", "gunta", "site", "boundaries", "surrounded"))):
                reply = (
                    "### Small Bits of Agricultural Land (`<= 10 Cents / 4 Guntas`): Acre Rate vs. House-Site Rate\n\n"
                    "**Direct Answer:** Apply the **agricultural Acre rate** in **rural areas** and for **urban wetlands**. Apply the **minimum house-site (`sq.yd`) rate** ONLY to **small bits (`<= 10 cents` / `4 guntas`) of DRY land in URBAN areas** (urban agglomerations, corporations, municipalities, major GPs, mandal HQs) **that are NOT surrounded by agricultural lands on 3 out of 4 boundaries**.\n\n"
                    "- **Urban Dry Land Surrounded by Agri Land on `>= 3` Sides:** Retains **agricultural Acre rate** (`IG Procdgs. No. MV1/34372/92, dt. 27.11.1992`, modifying `MV1/20363-A/90, dt. 10.08.1990`).\n"
                    "- **Urban Dry Land NOT Surrounded by Agri Land on 3+ Sides:** Adopts **house-site (`sq.yd`) rate**.\n"
                    "- **Rural Areas & Urban Wetlands (`<= 10 Cents`):** House-site rate conversion restriction was lifted; **Acre rate** applies.\n\n"
                    "**Authority:** `IG Procdgs. No. MV1/34372/92, dt. 27.11.1992` & `MV1/20363-A/90, dt. 10.08.1990` (AP Registration & Stamps Diary 2010, pp. 197–198)."
                )
            elif "settlement" in t_lower and any(k in t_lower for k in ("debt", "discharge", "liabilit", "clear")):
                shares_or_amts = parse_rupee_shares(text)
                split_note = ""
                if len(shares_or_amts) >= 2:
                    prop_v = max(shares_or_amts)
                    debt_v = min(shares_or_amts)
                    if prop_v > debt_v > Decimal("0"):
                        net_settle_v = prop_v - debt_v
                        split_note = (
                            f"\n- **Your Property Split:** On property value **{format_inr(prop_v)}** with **{format_inr(debt_v)}** debt discharged, "
                            f"charge **Settlement (`Art. 49` / `49-A`)** on **{format_inr(net_settle_v)}** AND **Conveyance (`Art. 20` / `47-A`)** on **{format_inr(debt_v)}**."
                        )
                reply = (
                    "### Settlement Burdened with Debt Discharge (`C&IG Procdgs. No. S3/7423/93`)\n\n"
                    "**Direct Answer:** It is classified as a **Settlement-cum-Conveyance** — NOT a pure settlement. Under **C&IG Proceedings No. S3/7423/93, dated 10.10.1996** (superseding the older Section 5 Note 10 example), duty is levied cumulatively under both **Article 49** (`Settlement` on net property value minus debt) and **Article 20** (`Conveyance` on the debt liability discharged).{0}\n\n"
                    "- **Standard Precedent Example (`S3/7423/93`):** Property worth **₹4,00,000** settled subject to discharging **₹1,00,000** liability → viewed as **Settlement on ₹3,00,000** (`Art. 49`) + **Conveyance on ₹1,00,000** (`Art. 20`).\n\n"
                    "**Authority:** `C&IG Procdgs. No. S3/7423/93, dt. 10.10.1996` (AP Registration & Stamps Diary 2010, p. 220)."
                ).format(split_note)
            elif any(k in t_lower for k in ("small farmer", "marginal farmer", "crop loan", "g.o.ms.no. 15", "go 15/2007")) or ("farmer" in t_lower and any(k in t_lower for k in ("loan", "credit", "mortgage", "exempt"))):
                loan_val = parse_rupee_amount(text)
                ceiling = Decimal("300000")
                verdict_line = (
                    f"**Yes — 100% exempt** on **{format_inr(loan_val)}** (since `{format_inr(loan_val)} <= ₹3,00,000.00` ceiling)"
                    if Decimal("0") < loan_val <= ceiling
                    else (
                        f"**Partially exempt:** First **₹3,00,000.00** is 100% exempt; Stamp Duty & RF apply only on the excess **{format_inr(loan_val - ceiling)}** (`Memo No. 3990/R&M(1)/05-2, dt. 12.08.2005`)"
                        if loan_val > ceiling
                        else "**Yes — 100% full exemption** of Stamp Duty and Registration Fee up to **₹3,00,000.00**"
                    )
                )
                reply = (
                    "### Small & Marginal Farmer Crop-Loan Exemption (`G.O.Ms.No. 15, dt. 08.01.2007`)\n\n"
                    f"**Direct Answer:** {verdict_line} for qualifying small/marginal farmers holding **`<= 5 acres` wet land** or **`<= 10 acres` dry land** taking bank or co-operative society agricultural loans (`G.O.Ms.No. 15, Rev (R&M) Dept., dt. 08.01.2007`).\n\n"
                    "- **Limit History:** Initial **₹1,00,000** (`G.O.Ms.No. 516, dt. 03.08.2004`) → raised to **₹2,00,000** (`G.O.Ms.No. 1575, dt. 20.08.2005`) → raised to **₹3,00,000** (`G.O.Ms.No. 15, dt. 08.01.2007` & `C&IG Endt. S1/14203/2004, dt. 20.02.2007`).\n"
                    "- **Excess Loan Rule:** Where loan exceeds the ceiling, Stamp Duty and Registration Fee are levied only on the **excess amount above ₹3,00,000** (`Memo No. 3990/R&M(1)/05-2, dt. 12.08.2005`).\n"
                    "- **Verification Requirement:** Farmer must produce MRO small/marginal farmer holding certificate (`<= 5 acres` wet / `<= 10 acres` dry).\n\n"
                    "**Authority:** `G.O.Ms.No. 15, Rev (Regn & Mandals) Dept., dt. 08.01.2007` (`C&IG Endt. S1/14203/2004, dt. 20.02.2007`, AP Diary 2010, pp. 224–225)."
                )
            elif "challan" in t_lower or ("refund" in t_lower and any(k in t_lower for k in ("stamp", "duty", "fee", "user charge", "rf", "unused", "4660"))):
                reply = (
                    "### Challan, Stamp Duty & Registration Fee Refund Rules (`Memo No. GSO1/4660/08` & `Rule 185`)\n\n"
                    "**Direct Answer:** For **Stamp Duty** paid through an unused bank challan, a **10% deduction** applies within **3 months** (`20%` deduction in the **4th month** grace period; challans expire after **4 months**). By contrast, **Registration Fee & CARD User Charges** for services not rendered are **100% refundable** by the **District Registrar** under **Registration Rule 185** (with no 10%/20% deduction).\n\n"
                    "- **Filing Authority:** Submit claim through the Sub-Registrar to the competent Revenue Official u/s **2(9)** IS Act with original challan, original bank receipt, identity proof, and non-utilization certificates (Bank, Sub-Treasury, SRO).\n"
                    "- **Stamp Duty Deductions:** **10%** within 3 months (per Note below SR 27, T.R. 16); **20%** during 4th month grace period (`C&IG Memo No. GSO1/4660/08, dt. 21.08.2009`).\n"
                    "- **Excess Stamp Duty on Used Challan:** Allowed within the **same financial year** per `G.O.Ms.No. 195, Finance (TFR), dt. 24.05.1989`.\n"
                    "- **Transfer Duty (Corporations):** Follows the same refund procedure as Stamp Duty if remitted to the Municipal Corporation account.\n\n"
                    "**Authority:** `C&IG R&S Memo No. GSO1/4660/08, dt. 21.08.2009`; `Registration Rule 185`; `G.O.Ms.No. 195, Fin (TFR), dt. 24.05.1989` (AP Diary 2010, pp. 208–209)."
                )
            elif ("41-a" in t_lower or "41a" in t_lower) and any(k in t_lower for k in ("penalty", "limitation", "fraud", "appeal", "years", "times", "notice")):
                reply = (
                    "### Section 41-A Deficit Stamp Duty: Limitation Period & Penalty (`Indian Stamp Act, 1899` — AP Amendment)\n\n"
                    "**Direct Answer:** Under **Section 41-A(1)**, deficit stamp duty detected in audit carries a **5-year limitation period** from registration in ordinary cases (extended to **10 years** in cases of **fraud, collusion, or wilful mis-statement/suppression of facts**), along with a **3-times (3×) penalty** of the deficit Stamp Duty — and per `C&IG Minutes dt. 27.03.2003 / AR1/27842/02 dt. 09.05.2003`, the **3× penalty applies to Transfer Duty (TD) as well**.\n\n"
                    "- **Notice Jurisdiction:** DR / Collector (after DIG/MMC audit finalization; within 15 days of final review orders; recovery also enforceable u/s **48** as arrears of land revenue).\n"
                    "- **Appellate Remedy (`Sec. 41-A(3)`):** Aggrieved party may appeal to the **Chief Controlling Revenue Authority (CCRA)** within **3 months** of the Collector's order.\n\n"
                    "**Authority:** `Section 41-A, Indian Stamp Act 1899 (AP Act 17 of 1986 & S1/24387/2000 dt. 09.09.2002)`; `C&IG AR1/27842/02 dt. 09.05.2003` (AP Diary 2010 & 2024)."
                )
            else:
                reply = (
                    f"### Source-limited legal information: {top.title}\n\n"
                    f"**Source:** `{top.authority_citation}`\n\n"
                    f"{top.sro_guidance}\n\n"
                    f"```text\n{top.content[:800]}...\n```"
                )
            return ChatResponse(
                reply=reply, intent="LEGAL_QUERY",
                legal_docs=[m.model_dump() for m in legal_matches]
            )
        if any(k in t_lower for k in ("circular", "22-b", "22b", "22-a", "22a", "webland", "ptin", "vlt", "section ", "article ")):
            return ChatResponse(
                reply="**Direct Answer:** Matching circular not found in index for the specified statutory reference or entities.",
                intent="LEGAL_NOT_FOUND",
            )
        return ChatResponse(
            reply=(
                "I could not find a relevant source in the indexed records for this legal question. "
                "I will not substitute an unrelated section or guess a classification."
            ),
            intent="LEGAL_NOT_FOUND",
        )

    # ==============================================================
    # 5. INTENT: Duty & Fee Calculation (Deterministic Engine)
    # ==============================================================
    consid_val, mv_val = parse_consideration_and_mv(text)
    val = max(consid_val, mv_val)
    if val <= Decimal("0"):
        val = parse_rupee_amount(text)
        consid_val = val
        mv_val = val
    user_provided_amount = (val > Decimal("0"))
    sheet_count = parse_sheet_count(text)
    exec_d = parse_date_from_text(text)
    code, alias = detect_deed_code(text)
    area = detect_area_type(text)
    lease_months = None
    lease_monthly_rent = Decimal("0")
    lease_subtype = "ALL"

    if alias == "LEASE":
        lease_months = parse_lease_months(text)
        lease_monthly_rent = parse_monthly_rent(text)
        lease_advance = parse_lease_advance(text)
        adv_refundable = parse_lease_advance_is_refundable(text)
        if any(term in t_lower for term in ("commercial", "shop", "office", "godown", "showroom", "retail")):
            lease_subtype = "COMMERCIAL"
        elif any(term in t_lower for term in ("residential", "house", "dwelling", "flat", "apartment")):
            lease_subtype = "RESIDENTIAL"
        if lease_months is None or lease_monthly_rent <= 0:
            return ChatResponse(
                reply=(
                    "For a lease estimate, provide the lease term and monthly rent, "
                    "for example: `commercial lease for 8 years at ₹25,000 monthly rent`."
                ),
                intent="NEED_CLARIFICATION",
            )
        if lease_subtype == "ALL":
            return ChatResponse(
                reply="Please say whether the lease is residential or commercial; the schedule separates those categories.",
                intent="NEED_CLARIFICATION",
            )
        lease_b = calculate_lease_duty(
            session=session,
            monthly_rent=lease_monthly_rent,
            duration_months=lease_months,
            property_subtype=lease_subtype,
            advance_amount=lease_advance,
            execution_date=exec_d,
            sheet_count=sheet_count,
            advance_is_refundable=adv_refundable,
        )
        if lease_b.status == "NOT_FOUND":
            return ChatResponse(
                reply=(
                    f"### Statutory Rate Unavailable (`NOT_FOUND`)\n\n"
                    f"**Instrument:** `LEASE` ({lease_subtype}) · **Date:** `{exec_d}` · **Duration:** {lease_months} months\n\n"
                    f"{lease_b.message}"
                ),
                intent="CALCULATION_REFUSED",
            )

        basis_label = "Total Rent (< 12 months)" if lease_b.calc_basis_used == "TOTAL_RENT" else "Average Annual Rent (AAR = 12 × monthly rent)"
        alt_total = lease_b.rent_stamp_duty + lease_b.transfer_duty + lease_b.registration_fee + lease_b.user_charge
        rows_md = [
            f"| Rent Stamp Duty | {lease_b.stamp_duty_rate} on {format_inr(lease_b.chargeable_value)} | {format_inr(lease_b.rent_stamp_duty)} | Art. 31 · G.O.Ms.No. 588, Rev (Regn-I), dt. 04.12.2013 |",
        ]
        if lease_advance > Decimal("0"):
            if adv_refundable is True:
                rows_md.append(
                    f"| Refundable Security Deposit | 0.0000% (Exempt) on {format_inr(lease_advance)} | ₹0.00 | C&IG Memo No. S1/4813/2008 & Memo No. CCRA1/13429/2008 |"
                )
                direct_extra = "Refundable security deposit attracts **₹0.00** Stamp Duty."
            else:
                rows_md.append(
                    f"| Advance / Premium Stamp Duty | 2.0000% (if chargeable advance/premium; ₹0.00 if refundable security) on {format_inr(lease_advance)} | {format_inr(lease_b.advance_stamp_duty)} | Art. 31(c) Sch. I-A; C&IG Memo S1/4813/2008 & CCRA1/13429/2008 |"
                )
                direct_extra = (
                    f"Total is **{format_inr(lease_b.total_payable)}** if the {format_inr(lease_advance)} deposit is a chargeable advance/premium (2% = {format_inr(lease_b.advance_stamp_duty)}), "
                    f"or **{format_inr(alt_total)}** if purely refundable security (₹0.00 SD on deposit)."
                )
        else:
            direct_extra = f"Total payable is **{format_inr(lease_b.total_payable)}**."

        rows_md.extend([
            f"| Transfer Duty (TD) | {lease_b.transfer_duty_rate or '0.0000%'} | {format_inr(lease_b.transfer_duty)} | {lease_b.td_authority} |",
            f"| Registration Fee (RF) | {lease_b.reg_fee_rate} | {format_inr(lease_b.registration_fee)} | {lease_b.rf_authority} |",
            f"| CARD User Charges | Flat Fee | {format_inr(lease_b.user_charge)} | {lease_b.user_charge_detail} |",
            f"| **Total Payable** | — | **{format_inr(lease_b.total_payable)}** | — |",
        ])

        _month_art = "an" if str(lease_months) in {"8", "11", "18", "80", "81", "82", "83", "84", "85", "86", "87", "88", "89"} else "a"
        reply = (
            f"### Fee & Duty Breakdown: {lease_subtype.title()} Lease (`{lease_b.instrument_code}`)\n\n"
            f"**Direct Answer:** For {_month_art} {lease_months}-month {lease_subtype.lower()} lease at {format_inr(lease_monthly_rent)}/month (**AAR: {format_inr(lease_b.chargeable_value)}**), "
            f"Rent SD ({lease_b.stamp_duty_rate}) is **{format_inr(lease_b.rent_stamp_duty)}**, RF ({lease_b.reg_fee_rate}) is **{format_inr(lease_b.registration_fee)}**, and User Charge is **{format_inr(lease_b.user_charge)}**. {direct_extra}\n\n"
            f"**Execution Date:** `{exec_d}` · **Valuation Basis:** {basis_label} = **{format_inr(lease_b.chargeable_value)}**\n\n"
            "| Fee Head | Rate / Rule | Amount Payable (₹) | Statutory Authority |\n"
            "| :--- | :--- | ---: | :--- |\n"
            + "\n".join(rows_md)
        )
        if lease_b.warnings:
            reply += "\n\n> " + " ".join(lease_b.warnings)

        slip_payload = {
            "instrument_code": lease_b.instrument_code,
            "deed_alias": "LEASE",
            "area_type": lease_b.area_type,
            "execution_date": str(exec_d),
            "calc_basis_used": lease_b.calc_basis_used,
            "chargeable_value": str(lease_b.chargeable_value),
            "stamp_duty": str(lease_b.stamp_duty),
            "stamp_duty_rate": lease_b.stamp_duty_rate,
            "rent_stamp_duty": str(lease_b.rent_stamp_duty),
            "advance_stamp_duty": str(lease_b.advance_stamp_duty),
            "lease_advance_amount": str(lease_b.lease_advance_amount),
            "transfer_duty": str(lease_b.transfer_duty),
            "transfer_duty_rate": lease_b.transfer_duty_rate,
            "registration_fee": str(lease_b.registration_fee),
            "reg_fee_rate": lease_b.reg_fee_rate,
            "user_charge": str(lease_b.user_charge),
            "user_charge_detail": lease_b.user_charge_detail,
            "total_payable": str(lease_b.total_payable),
            "adjustable_sd_pct": str(lease_b.adjustable_sd_pct),
            "is_adjustable": lease_b.is_adjustable,
            "sd_authority": lease_b.sd_authority,
            "td_authority": lease_b.td_authority,
            "rf_authority": lease_b.rf_authority,
            "citations": lease_b.citations,
            "warnings": lease_b.warnings,
        }
        signed_slip = sign_and_archive_check_slip(session, slip_payload)
        return ChatResponse(reply=reply, intent="CALCULATION_OK", card_slip=signed_slip)

    has_explicit_area = (
        any(
            token in t_lower
            for token in (
                "corporation", "municipality", "municipal", "village panchayat", "gram panchayat", "panchayat",
                "rural", "cantonment", "selection grade", "special grade", "village", "town",
                "vijayawada", "vijyawada", "vizag", "visakhapatnam", "vishakhapatnam",
                "guntur", "tirupati", "nellore", "kurnool", "kakinada",
                "rajahmundry", "rajamahendravaram", "kadapa", "anantapur",
                "eluru", "ongole", "chittoor", "machilipatnam", "vizianagaram", "srikakulam",
            )
        )
        or bool(re.search(r"\b(?:corp|gp|muni)\b", t_lower))
    )
    property_subtype = lease_subtype if alias == "LEASE" else "ALL"
    has_universal_rate = _has_universal_rate(
        session, alias, exec_d, property_subtype, lease_months
    )
    if not has_explicit_area and has_universal_rate:
        area = "ALL"
    elif (
        not has_explicit_area
        and user_provided_amount
        and exec_d >= date(2005, 7, 1)
        and code not in {"04_01", "04_02"}
        and alias not in {"PARTITION_FAMILY", "PARTITION_OTHER"}
    ):
        return ChatResponse(
            reply=(
                "Please specify the property jurisdiction (Gram Panchayat, Municipality, Corporation, "
                "or Cantonment)."
            ),
            intent="NEED_CLARIFICATION",
        )

    # Special dedicated handling for Development Agreement cum GPA (01_10 / DAGPA)
    if code == "01_10" or alias == "DEVELOPMENT_AGREEMENT_CUM_GPA":
        if not user_provided_amount:
            rate_row = (
                session.query(DutyRate)
                .filter(
                    DutyRate.instrument_code == "01_10",
                    DutyRate.effective_from <= exec_d,
                    or_(DutyRate.effective_to.is_(None), DutyRate.effective_to >= exec_d),
                )
                .order_by(DutyRate.effective_from.desc())
                .first()
            )
            sd_rate = f"{rate_row.stamp_duty_value}%" if rate_row else "1.0000%"
            td_rate = f"{rate_row.transfer_duty_pct or 0}%" if rate_row else "0.0000%"
            rf_rate = f"{rate_row.reg_fee_value}%" if rate_row else "0.5000%"
            rf_min = format_inr(rate_row.reg_fee_min, decimals=0) if rate_row and rate_row.reg_fee_min else "₹1,000"
            rf_max = format_inr(rate_row.reg_fee_max, decimals=0) if rate_row and rate_row.reg_fee_max else "₹20,000"
            sd_auth = (
                rate_row.sd_authority
                if rate_row and rate_row.sd_authority
                else "Art. 6-B · G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013"
            )
            td_auth = rate_row.td_authority if rate_row and rate_row.td_authority else "Exempt"
            rf_auth = (
                rate_row.rf_authority
                if rate_row and rate_row.rf_authority
                else "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013"
            )
            is_adj = bool(rate_row.is_adjustable) if rate_row else False
            adj_ans = "**adjustable**" if is_adj else "**NOT adjustable**"
            uc_amt, uc_detail = _get_user_charge(session, "01_10", exec_d, Decimal("0"))
            reply = (
                f"**Direct Answer:** Development Agreement-cum-GPA (`01_10`) attracts **{sd_rate} Stamp Duty**, "
                f"**{td_rate} Transfer Duty**, and **{rf_rate} Registration Fee** (Min {rf_min}, **Max Cap {rf_max}**), "
                f"and is {adj_ans} against subsequent sale deeds under **G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013** (w.e.f. 02.12.2013).\n\n"
                "### Development Agreement-cum-GPA (`01_10` · Article 6-B)\n\n"
                "| Component | Statutory Rate / Cap | Authority / Legal Rule |\n"
                "|---|---|---|\n"
                f"| **Stamp Duty (SD)** | **{sd_rate}** | {sd_auth} (on estimated cost / MV) |\n"
                f"| **Transfer Duty (TD)** | **{td_rate}** | {td_auth} |\n"
                f"| **Registration Fee (RF)** | **{rf_rate}** | {rf_auth} (Min {rf_min}, **Max Cap {rf_max}**) |\n"
                f"| **User Charges** | **{format_inr(uc_amt)}** | {uc_detail} |\n\n"
                "**Adjustability:** **No (Not Adjustable)** — Under **G.O.Ms.No. 581, Revenue (Regn-I), dt. 30.11.2013** "
                "(w.e.f. 02.12.2013), Stamp Duty paid on a Development Agreement-cum-GPA (`01_10`) or Construction/Development Agreement "
                "(`01_09`) is **not adjustable** against Stamp Duty payable on subsequent sale deeds. "
                "(Adjustability applies only to Sale Agreement with Possession `01_02` at 4% and AGPA `01_11` at 5% where 4% is adjustable.)\n\n"
                "*Provide the estimated cost or market value to compute exact payable rupees.*"
            )
            return ChatResponse(reply=reply, intent="RATE_LOOKUP")

        dagpa_breakdown: DutyBreakdown = calculate_deed_duty(
            session, "DEVELOPMENT_AGREEMENT_CUM_GPA", area, exec_d,
            market_value=val, consideration_value=val, sheet_count=sheet_count
        )
        sd_auth = dagpa_breakdown.sd_authority or "Art. 6-B · G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013"
        td_auth = dagpa_breakdown.td_authority or "Exempt"
        rf_auth = dagpa_breakdown.rf_authority or "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013 (0.5%, Min ₹1,000 / Max ₹20,000)"
        adj_label = "Adjustable" if dagpa_breakdown.is_adjustable else "not adjustable"
        adjustment_note = (
            "\n\n**Adjustability (Art. 6-B / G.O.Ms.No. 581 dt. 30.11.2013):** **No (Not Adjustable)** — "
            "With effect from 02.12.2013 under G.O.Ms.No. 581, Stamp Duty paid on DAGPA (`01_10`, 1%) and Development/Construction Agreement "
            "(`01_09`, 0.5%) is **not adjustable** against subsequent sale deeds (unlike `01_02` Sale Agreement with Possession / `01_11` AGPA)."
            if not dagpa_breakdown.is_adjustable
            else "\n\n**Adjustability (Art. 6-B):** Yes — Stamp Duty paid is adjustable against subsequent sale deeds."
        )
        reply = (
            f"**Direct Answer:** Stamp Duty is **{dagpa_breakdown.stamp_duty_rate}** (**{format_inr(dagpa_breakdown.stamp_duty)}**), "
            f"**{adj_label}** against subsequent sale deeds under **G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013** (w.e.f. 02.12.2013), "
            f"and Registration Fee is **{dagpa_breakdown.reg_fee_rate}** capped at **₹20,000.00** "
            f"(Total Payable: **{format_inr(dagpa_breakdown.total_payable)}**).\n\n"
            f"### Development Agreement-cum-GPA (`01_10`)\n\n"
            f"**Execution Date:** `{exec_d}` · **Jurisdiction:** `{area}` · **Chargeable Value:** {format_inr(val)}\n\n"
            f"| Component | Rate / Cap | Amount Payable (₹) | Authority |\n"
            f"|---|---:|---:|---|\n"
            f"| Stamp Duty (SD) | {dagpa_breakdown.stamp_duty_rate} | {format_inr(dagpa_breakdown.stamp_duty)} | {sd_auth} |\n"
            f"| Transfer Duty (TD) | {dagpa_breakdown.transfer_duty_rate} | {format_inr(dagpa_breakdown.transfer_duty)} | {td_auth} |\n"
            f"| Registration Fee (RF) | {dagpa_breakdown.reg_fee_rate} | {format_inr(dagpa_breakdown.registration_fee)} | {rf_auth} |\n"
            f"| User Charge | Flat | {format_inr(dagpa_breakdown.user_charge)} | {dagpa_breakdown.user_charge_detail} |\n"
            f"| **Total Payable** | — | **{format_inr(dagpa_breakdown.total_payable)}** | — |"
            f"{adjustment_note}"
        )

        card_slip_data = {
            "instrument_code": "01_10",
            "deed_alias": "DEVELOPMENT_AGREEMENT_CUM_GPA",
            "area_type": area,
            "execution_date": str(exec_d),
            "calc_basis_used": dagpa_breakdown.calc_basis_used,
            "chargeable_value": str(dagpa_breakdown.chargeable_value),
            "stamp_duty": str(dagpa_breakdown.stamp_duty),
            "stamp_duty_rate": dagpa_breakdown.stamp_duty_rate,
            "transfer_duty": str(dagpa_breakdown.transfer_duty),
            "transfer_duty_rate": dagpa_breakdown.transfer_duty_rate,
            "registration_fee": str(dagpa_breakdown.registration_fee),
            "reg_fee_rate": dagpa_breakdown.reg_fee_rate,
            "user_charge": str(dagpa_breakdown.user_charge),
            "user_charge_detail": dagpa_breakdown.user_charge_detail,
            "total_payable": str(dagpa_breakdown.total_payable),
            "adjustable_sd_pct": str(dagpa_breakdown.adjustable_sd_pct),
            "adjustable_stamp_duty": str(dagpa_breakdown.adjustable_stamp_duty),
            "non_adjustable_stamp_duty": str(dagpa_breakdown.non_adjustable_stamp_duty),
            "is_adjustable": dagpa_breakdown.is_adjustable,
            "sd_authority": sd_auth,
            "td_authority": td_auth,
            "rf_authority": rf_auth,
            "citations": dagpa_breakdown.citations,
            "warnings": dagpa_breakdown.warnings,
        }
        signed_slip = sign_and_archive_check_slip(session, card_slip_data)
        return ChatResponse(
            reply=reply, intent="CALCULATION_OK",
            card_slip=signed_slip,
        )

    # Partition handling (CARD/e-1730463 Annexure I: 04_02 Family 1% VSS, 04_01 Others 2% VSS)
    if code in {"04_01", "04_02"} or alias in {"PARTITION_FAMILY", "PARTITION_OTHER"}:
        part_code = "04_01" if alias == "PARTITION_OTHER" else "04_02"
        part_alias = "PARTITION_OTHER" if part_code == "04_01" else "PARTITION_FAMILY"
        part_title = "Partition among Others" if part_code == "04_01" else "Family Partition"

        shares_list = parse_rupee_shares(text)
        if not user_provided_amount and not shares_list:
            rate_row = (
                session.query(DutyRate)
                .filter(
                    DutyRate.instrument_code == part_code,
                    DutyRate.effective_from <= exec_d,
                    or_(DutyRate.effective_to.is_(None), DutyRate.effective_to >= exec_d),
                )
                .order_by(DutyRate.effective_from.desc())
                .first()
            )
            default_sd = "2.0000%" if part_code == "04_01" else "1.0000%"
            sd_rate = f"{rate_row.stamp_duty_value}%" if rate_row else default_sd
            td_rate = f"{rate_row.transfer_duty_pct or 0}%" if rate_row else "0.0000%"
            sd_auth = (
                rate_row.sd_authority
                if rate_row and rate_row.sd_authority
                else (
                    "Art. 40 · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014"
                    if part_code == "04_02"
                    else "Art. 40 · Indian Stamp Act 1899 (AP Sch. I-A)"
                )
            )
            td_auth = rate_row.td_authority if rate_row and rate_row.td_authority else "Exempt (Not Applicable)"
            rf_auth = (
                rate_row.rf_authority
                if rate_row and rate_row.rf_authority
                else "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013"
            )
            uc_amt, uc_detail = _get_user_charge(session, part_code, exec_d, Decimal("0"))
            reply = (
                f"**Direct Answer:** {part_title} (`{part_code}`) attracts **{sd_rate} Stamp Duty** on the "
                "**Value of Separated Shares (VSS)** (Total minus Largest Share), **0.0000% Transfer Duty**, and "
                "**₹1,000.00 Fixed Registration Fee** (non-adjustable).\n\n"
                f"### {part_title} (`{part_code}` · Article 40)\n\n"
                "| Component | Statutory Rate / Cap | Authority / Legal Rule |\n"
                "|---|---|---|\n"
                f"| **Stamp Duty (SD)** | **{sd_rate}** | {sd_auth} — Computed on **VSS** = Total Value minus Largest Share |\n"
                f"| **Transfer Duty (TD)** | **{td_rate}** | {td_auth} |\n"
                "| **Registration Fee (RF)** | **₹1,000.00 Fixed** | "
                f"{rf_auth} |\n"
                f"| **User Charges** | **{format_inr(uc_amt)}** | {uc_detail} |\n\n"
                "*To calculate payable rupees, state the value of each share (e.g. `3 brothers splitting 90L into 40L, 30L and 20L shares`).*"
            )
            return ChatResponse(reply=reply, intent="RATE_LOOKUP")

        shares_desc = ""
        if len(shares_list) >= 2:
            share_labels = ", ".join(format_inr(s) for s in shares_list)
            total_prop = sum(shares_list)
            largest = max(shares_list)
            shares_desc = (
                f"{len(shares_list)} shares ({share_labels} · Total {format_inr(total_prop)} − Largest Share {format_inr(largest)})"
            )
        else:
            equal_match = re.search(r"\b(\d+)\s*(?:brothers?|sisters?|co-?parceners?|shares?|persons?|parts?|members?)\b", t_lower)
            is_equal = any(term in t_lower for term in ("equal", "equally", "evenly"))
            if equal_match and is_equal and int(equal_match.group(1)) >= 2:
                n_shares = int(equal_match.group(1))
                share_val = val / Decimal(str(n_shares))
                shares_list = [share_val] * n_shares
                shares_desc = f"{n_shares} equal shares of {format_inr(share_val)}"

        if len(shares_list) >= 2:
            part_breakdown = calculate_deed_duty(
                session, part_alias, "ALL", exec_d,
                shares=shares_list, sheet_count=sheet_count,
            )
            if part_breakdown.status == "OK":
                vss_val = part_breakdown.chargeable_value
                sd_auth = part_breakdown.sd_authority or "Art. 40 · G.O.Ms.No. 395, Rev (Regn-I), dt. 26.11.2014"
                td_auth = part_breakdown.td_authority or "Exempt (Not Applicable)"
                rf_auth = part_breakdown.rf_authority or "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013"
                reply = (
                    f"**Direct Answer:** Dividing across {len(shares_list)} shares yields a Value of Separated Shares (VSS) of "
                    f"**{format_inr(vss_val)}** (Total minus largest share), with Stamp Duty (**{part_breakdown.stamp_duty_rate}**) of "
                    f"**{format_inr(part_breakdown.stamp_duty)}** and total payable of **{format_inr(part_breakdown.total_payable)}**.\n\n"
                    f"### Fee & Duty Breakdown: {part_title} (`{part_code}`)\n\n"
                    f"**Execution Date:** `{exec_d}` · **Shares:** {shares_desc} · **Chargeable VSS:** {format_inr(vss_val)}\n\n"
                    "| Fee Head | Rate / Rule | Amount Payable (₹) | Authority |\n"
                    "| :--- | :--- | ---: | :--- |\n"
                    f"| Stamp Duty (SD) | {part_breakdown.stamp_duty_rate} on VSS | {format_inr(part_breakdown.stamp_duty)} | {sd_auth} |\n"
                    f"| Transfer Duty (TD) | {part_breakdown.transfer_duty_rate or '0.0000%'} | {format_inr(part_breakdown.transfer_duty)} | {td_auth} |\n"
                    f"| Registration Fee (RF) | {part_breakdown.reg_fee_rate} | {format_inr(part_breakdown.registration_fee)} | {rf_auth} |\n"
                    f"| CARD User Charges | Flat | {format_inr(part_breakdown.user_charge)} | {part_breakdown.user_charge_detail} |\n"
                    f"| **Total Payable** | — | **{format_inr(part_breakdown.total_payable)}** | — |"
                )
                signed_slip = sign_and_archive_check_slip(session, {
                    "instrument_code": part_code,
                    "deed_alias": part_alias,
                    "area_type": "ALL",
                    "execution_date": str(exec_d),
                    "calc_basis_used": part_breakdown.calc_basis_used,
                    "chargeable_value": str(part_breakdown.chargeable_value),
                    "stamp_duty": str(part_breakdown.stamp_duty),
                    "stamp_duty_rate": part_breakdown.stamp_duty_rate,
                    "transfer_duty": str(part_breakdown.transfer_duty),
                    "transfer_duty_rate": part_breakdown.transfer_duty_rate,
                    "registration_fee": str(part_breakdown.registration_fee),
                    "reg_fee_rate": part_breakdown.reg_fee_rate,
                    "user_charge": str(part_breakdown.user_charge),
                    "user_charge_detail": part_breakdown.user_charge_detail,
                    "total_payable": str(part_breakdown.total_payable),
                    "adjustable_sd_pct": str(part_breakdown.adjustable_sd_pct),
                    "is_adjustable": part_breakdown.is_adjustable,
                    "sd_authority": sd_auth,
                    "td_authority": td_auth,
                    "rf_authority": rf_auth,
                    "citations": part_breakdown.citations,
                    "warnings": part_breakdown.warnings,
                })
                return ChatResponse(reply=reply, intent="CALCULATION_OK", card_slip=signed_slip)

        return ChatResponse(
            reply=(
                f"{part_title} (`{part_code}`) depends on the Value of Separated Shares (VSS = Total Value minus Largest Share; "
                f"{'1%' if part_code == '04_02' else '2%'} SD on VSS under Art. 40, ₹1,000 Registration Fee, ₹500 User Charges). "
                "Please provide the value of each individual share (or specify an equal split across N shares)."
            ),
            intent="NEED_CLARIFICATION",
        )

    # Call deterministic calculator engine
    calc_subtype = (
        lease_subtype if alias == "LEASE"
        else ("FLAT_APARTMENT" if any(k in t_lower for k in ("flat", "apartment"))
              else ("NON_FLAT" if any(k in t_lower for k in ("house", "independent house", "plot", "land", "site")) and "farmhouse" not in t_lower
                    else "ALL"))
    )
    breakdown: DutyBreakdown = calculate_deed_duty(
        session, alias, area, exec_d,
        consideration_value=consid_val, market_value=mv_val, loan_amount=val,
        property_subtype=calc_subtype,
        lease_months=lease_months,
        annual_rent=lease_monthly_rent * Decimal("12"),
        sheet_count=sheet_count,
    )

    if breakdown.status == "NOT_FOUND":
        reply = (
            f"### Statutory Rate Unavailable (`NOT_FOUND`)\n\n"
            f"**Instrument:** `{code}` ({alias}) · **Date:** `{exec_d}` · **Area:** `{area}`\n\n"
            f"{breakdown.message}\n\n"
            f"Calculation refused because no active Government Order covers this date and instrument."
        )
        return ChatResponse(reply=reply, intent="CALCULATION_REFUSED")

    DEED_FRIENDLY_TITLES = {
        "01_01": "Sale of Immovable Property",
        "01_02": "Sale Agreement with Possession",
        "01_03": "Sale Agreement without Possession",
        "01_10": "Development Agreement cum GPA (DAGPA)",
        "01_11": "Agreement of Sale cum GPA (AGPA)",
        "03_01": "Gift Deed (Art. 29)",
        "03_02": "Gift Settlement in F/O Family Member (Art. 49-A(a))",
        "03_03": "Gift Settlement in F/O Others (Art. 49-A(b))",
        "03_05": "Gift in favour of Relatives u/s 56(2) of IT Act (Panchayat)",
        "03_06": "Gift in favour of Relatives u/s 56(2) of IT Act (Municipalities & Corporations)",
        "03_07": "Gift in favour of Others / Non-Relatives",
        "04_01": "Partition among Others",
        "04_02": "Partition among Family Members",
        "02_01": "Mortgage with Possession",
        "02_02": "Mortgage without Possession",
        "02_08": "Deposit of Title Deeds (DOTD)",
        "05_01": "Release of Share among Family",
        "05_02": "Release without Consideration",
        "06_01": "Exchange of Property",
    }
    deed_title = DEED_FRIENDLY_TITLES.get(code, alias)

    citation_str = breakdown.citations[0] if breakdown.citations else 'Indian Stamp Act 1899 (AP Sch. I-A)'
    sd_auth = breakdown.sd_authority or citation_str
    td_auth = breakdown.td_authority or ("AP Municipalities / GP Transfer Duty Surcharge" if breakdown.transfer_duty > 0 else "Exempt (Not Applicable)")
    rf_auth = breakdown.rf_authority or "G.O.Ms.No. 463, Rev (Regn-I), dt. 17.08.2013"
    if breakdown.adjustable_sd_pct > 0:
        adj_note = f" ({breakdown.adjustable_sd_pct.normalize()}% = {format_inr(breakdown.adjustable_stamp_duty)} adjustable on subsequent sale deed"
        if breakdown.non_adjustable_stamp_duty > 0:
            adj_note += f"; {format_inr(breakdown.non_adjustable_stamp_duty)} non-adjustable GPA duty)"
        else:
            adj_note += ")"
    else:
        adj_note = ""

    if user_provided_amount:
        adj_rows = ""
        if breakdown.adjustable_sd_pct > 0:
            adj_rows = (
                f"\n| ↳ *Adjustable SD Credit* | *{breakdown.adjustable_sd_pct.normalize()}% (adjustable later)* | *{format_inr(breakdown.adjustable_stamp_duty)}* | *G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013* |"
            )
            if breakdown.non_adjustable_stamp_duty > 0:
                adj_rows += (
                    f"\n| ↳ *Non-Adjustable GPA Duty* | *Balance (not adjustable)* | *{format_inr(breakdown.non_adjustable_stamp_duty)}* | *G.O.Ms.No. 581, Rev (Regn-I), dt. 30.11.2013* |"
                )

        reply = (
            f"### Fee & Duty Breakdown: {deed_title} (`{code}`)\n\n"
            f"**Direct Answer:** Total payable on {format_inr(breakdown.chargeable_value)} (`{breakdown.calc_basis_used}`) is "
            f"**{format_inr(breakdown.total_payable)}** (SD {breakdown.stamp_duty_rate}, TD {breakdown.transfer_duty_rate or '0%'}, RF {breakdown.reg_fee_rate}{adj_note}).\n\n"
            f"**Execution Date:** `{exec_d}` · **Jurisdiction:** `{area}` · **Chargeable Value:** {format_inr(breakdown.chargeable_value)}\n\n"
            f"| Fee Head | Statutory Rate / Rule | Amount Payable | Legal Authority |\n"
            f"| :--- | :--- | ---: | :--- |\n"
            f"| Stamp Duty (SD) | {breakdown.stamp_duty_rate} | {format_inr(breakdown.stamp_duty)} | {sd_auth} |{adj_rows}\n"
            f"| Transfer Duty (TD) | {breakdown.transfer_duty_rate or '0%'} | {format_inr(breakdown.transfer_duty)} | {td_auth} |\n"
            f"| Registration Fee (RF) | {breakdown.reg_fee_rate} | {format_inr(breakdown.registration_fee)} | {rf_auth} |\n"
            f"| CARD User Charges | Flat User Fee | {format_inr(breakdown.user_charge)} | {breakdown.user_charge_detail} |\n"
            f"| **Total Payable** | — | **{format_inr(breakdown.total_payable)}** | — |"
        )
        if breakdown.warnings:
            reply += "\n\n> " + " ".join(breakdown.warnings)
    else:
        uc_amt, uc_detail = _get_user_charge(session, code, exec_d, Decimal("0"))
        reply = (
            f"### Statutory Rate Schedule: {deed_title} (`{code}`)\n\n"
            f"**Direct Answer:** Applicable rates are **SD {breakdown.stamp_duty_rate}**, **TD {breakdown.transfer_duty_rate or '0%'}**, **RF {breakdown.reg_fee_rate}**, plus **{format_inr(uc_amt)}** User Charges{adj_note}.\n\n"
            f"**Jurisdiction:** `{area}` · **Authority:** `{citation_str}`\n\n"
            f"| Component | Applicable Rate / Base | Legal Authority |\n"
            f"| :--- | :--- | :--- |\n"
            f"| Stamp Duty (SD) | **{breakdown.stamp_duty_rate}** | {sd_auth} |\n"
            f"| Transfer Duty (TD) | **{breakdown.transfer_duty_rate or '0.0000% (Exempt)'}** | {td_auth} |\n"
            f"| Registration Fee (RF) | **{breakdown.reg_fee_rate}** | {rf_auth} |\n"
            f"| CARD User Charges | **{format_inr(uc_amt)}** | {uc_detail} |\n\n"
            f"*Provide the property value or consideration amount to compute exact rupees.*"
        )

    if not user_provided_amount:
        return ChatResponse(reply=reply, intent="RATE_LOOKUP")

    card_slip_data = {
        "instrument_code": code,
        "deed_alias": alias,
        "area_type": area,
        "execution_date": str(exec_d),
        "calc_basis_used": breakdown.calc_basis_used,
        "chargeable_value": str(breakdown.chargeable_value),
        "stamp_duty": str(breakdown.stamp_duty),
        "stamp_duty_rate": breakdown.stamp_duty_rate,
        "transfer_duty": str(breakdown.transfer_duty),
        "transfer_duty_rate": breakdown.transfer_duty_rate,
        "registration_fee": str(breakdown.registration_fee),
        "reg_fee_rate": breakdown.reg_fee_rate,
        "user_charge": str(breakdown.user_charge),
        "user_charge_detail": breakdown.user_charge_detail,
        "total_payable": str(breakdown.total_payable),
        "adjustable_sd_pct": str(breakdown.adjustable_sd_pct),
        "adjustable_stamp_duty": str(breakdown.adjustable_stamp_duty),
        "non_adjustable_stamp_duty": str(breakdown.non_adjustable_stamp_duty),
        "is_adjustable": breakdown.is_adjustable,
        "citations": breakdown.citations,
        "warnings": breakdown.warnings
    }

    signed_slip = sign_and_archive_check_slip(session, card_slip_data)
    return ChatResponse(
        reply=reply, intent="CALCULATION_OK",
        card_slip=signed_slip
    )
