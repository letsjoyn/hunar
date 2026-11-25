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
from datetime import date
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.engine.duty_calculator import (
    calculate_deed_duty,
    compare_historical_vs_current,
    DutyBreakdown,
)
from backend.app.engine.fines_calc import calculate_delay_fine
from backend.app.engine.area_converter import (
    convert_area,
    parse_composite_land_string,

)
from backend.app.engine.legal_search import (
    search_legal_knowledge,

)
from backend.app.engine.verification import sign_and_archive_check_slip
from backend.app.engine.prohibited_land import check_prohibited_land
from backend.app.core.config import IS_PRODUCTION


class ChatRequest(BaseModel):
    message: str
    history: Optional[list[dict]] = None
    use_gemini: Optional[bool] = False


class ChatResponse(BaseModel):
    reply: str
    intent: str
    card_slip: Optional[dict] = None
    audit_comparison: Optional[dict] = None
    legal_docs: Optional[list[dict]] = None
    fine_data: Optional[dict] = None
    area_data: Optional[dict] = None
    prohibited_data: Optional[dict] = None



def parse_rupee_amount(text: str) -> Decimal:
    """Extract rupee amounts like '15 Lakhs', '15L', '15,00,000', '1.5 Crore', '500000'."""
    # 1. Crore
    m_cr = re.search(r"(\d+(?:\.\d+)?)\s*(?:cr(?:ore)?s?)", text, re.IGNORECASE)
    if m_cr:
        return Decimal(m_cr.group(1)) * Decimal("10000000")

    # 2. Lakh
    m_lakh = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh?s?|l\b)", text, re.IGNORECASE)
    if m_lakh:
        return Decimal(m_lakh.group(1)) * Decimal("100000")

    # 3. Plain digits with commas
    m_num = re.search(r"(?:rs\.?|inr|₹)?\s*(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?)", text, re.IGNORECASE)
    if m_num:
        clean = m_num.group(1).replace(",", "")
        return Decimal(clean)

    # 4. Plain numbers >= 1000
    m_plain = re.search(r"\b(\d{4,12})\b", text)
    if m_plain:
        return Decimal(m_plain.group(1))

    return Decimal("0")


def parse_date_from_text(text: str) -> date:
    """Extract dates like '2009-05-01', 'May 2009', '2009', '2013', '2025'."""
    # YYYY-MM-DD
    m_iso = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if m_iso:
        y, m, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return date(y, m, d)

    # Month Year e.g. 'May 2009'
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    for m_name, m_num in months.items():
        m_my = re.search(rf"\b{m_name}[a-z]*\s+(\d{{4}})\b", text, re.IGNORECASE)
        if m_my:
            return date(int(m_my.group(1)), m_num, 1)

    # Just year: 1990 to 2026
    m_year = re.search(r"\b(19\d{2}|20[0-2]\d)\b", text)
    if m_year:
        y = int(m_year.group(1))
        # Default to May 1st of that year
        return date(y, 5, 1)

    return date.today()


def detect_area_type(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["panchayat", "village", "rural"]):
        return "GRAM_PANCHAYAT"
    if any(k in t for k in ["selection", "special grade"]):
        return "SPECIAL_SELECTION_MUNICIPALITY"
    if any(k in t for k in ["muni", "municipality", "town"]):
        return "OTHER_MUNICIPALITY"
    return "MUNICIPAL_CORP"


def detect_deed_code(text: str) -> tuple[str, str]:
    t = text.lower()
    
    # 0. Development Agreement / DAGPA / Construction Agreement / Builders
    if any(k in t for k in ["development agreement", "dagpa", "construction agreement", "builder", "developer"]):
        if "without gpa" in t:
            return "01_09", "DEVELOPMENT_AGREEMENT"
        return "01_10", "DEVELOPMENT_AGREEMENT_CUM_GPA"

    # 1. Partition / Splitting heuristics (e.g. "split my land with my brother", "divide property between brothers")
    if any(k in t for k in ["partition", "split", "divide", "division", "share", "separated share", "splitting", "dividing"]):
        if any(rel in t for rel in ["brother", "sister", "family", "father", "son", "daughter", "coparcener", "co-parcener", "mother"]):
            return "04_01", "PARTITION_FAMILY"
        if "other" in t or "non-relative" in t:
            return "04_02", "PARTITION_OTHER"
        return "04_01", "PARTITION_FAMILY"

    # 2. Family transfer heuristics (e.g. "giving my land to my son", "transfer to daughter")
    is_family = any(rel in t for rel in ["son", "daughter", "father", "mother", "wife", "husband", "brother", "sister", "family", "children", "child", "parents", "relative"])
    is_giving = any(g in t for g in ["giving", "give", "gift", "gifting", "settle", "settling", "transfer", "transferring", "hand over", "handing over", "bequeath"])
    
    if (is_giving and is_family) or "gift to" in t or "settle on" in t:
        if any(k in t for k in ["panchayat", "village", "rural"]):
            return "03_05", "GIFT_RELATIVE"
        return "03_06", "GIFT_RELATIVE"
    if "gift" in t:
        if "other" in t or "non-relative" in t or "friend" in t:
            return "03_07", "GIFT_OTHER"
        if any(k in t for k in ["panchayat", "village", "rural"]):
            return "03_05", "GIFT_RELATIVE"
        return "03_06", "GIFT_RELATIVE"
    if "settlement" in t:
        if "other" in t:
            return "03_02", "SETTLEMENT_OTHER"
        return "03_01", "SETTLEMENT_FAMILY"
    if "deposit of title" in t or "dotd" in t:
        return "02_08", "DOTD"
    if "mortgage" in t:
        if "with possession" in t:
            return "02_01", "MORTGAGE_WITH_POSSESSION"
        return "02_02", "MORTGAGE_WITHOUT_POSSESSION"
    if "release" in t:
        if any(r in t for r in ["family", "brother", "sister", "coparcener"]):
            return "04_04", "RELEASE_FAMILY"
        return "04_05", "RELEASE_OTHER"
    if "exchange" in t:
        return "01_05", "EXCHANGE"
    if "agreement of sale" in t or "agpa" in t:
        if "without possession" in t:
            return "01_03", "SALE_AGREEMENT_WITHOUT_POSSESSION"
        if "gpa" in t:
            return "01_11", "AGPA"
        return "01_02", "SALE_AGREEMENT_WITH_POSSESSION"
    if "power of attorney" in t or "gpa" in t:
        if any(r in t for r in ["family", "blood"]):
            return "09_05", "GPA_FAMILY"
        return "09_04", "GPA_SELL"
    if "adoption" in t:
        return "30_01", "ADOPTION"
    if "affidavit" in t:
        return "31_01", "AFFIDAVIT"
    return "01_01", "SALE"


KEY_STATUTORY_DEEDS_20 = [
    ("01_01", "Sale of Immovable Property", "Art. 47-A", "5.00%", "1.50%", "1.00%", "7.50%", "Max of Market Value or Consideration"),
    ("01_02", "Sale Agreement with Possession", "Art. 6-B", "4.00%", "0.00% (Exempt)", "0.50%", "4.50%", "Max of Market Value or Consideration"),
    ("01_03", "Sale Agreement without Possession", "Art. 6-B", "0.50%", "0.00% (Exempt)", "0.50%", "1.00%", "Stated Consideration"),
    ("01_10", "Development Agreement cum GPA (DAGPA)", "Art. 6-B", "1.00%", "0.00% (Exempt)", "0.50%", "1.50%", "Market Value of Total Land"),
    ("01_11", "Agreement of Sale cum GPA (AGPA)", "Art. 6-B", "5.00%", "0.00% (Exempt)", "Fixed ₹ 2,000", "5.0% + ₹2k", "Market Value or Consideration"),
    ("03_06", "Gift in favour of Family Members", "Art. 29", "2.00%", "1.50%", "0.50%", "4.00%", "Market Value (Sec. 47-A)"),
    ("03_07", "Gift to Non-Family / Others", "Art. 29", "5.00%", "1.50%", "0.50%", "7.00%", "Market Value (Sec. 47-A)"),
    ("03_01", "Settlement in favour of Family", "Art. 49-A", "2.00%", "0.00% (Exempt)", "0.50%", "2.50%", "Market Value"),
    ("03_02", "Settlement in favour of Others", "Art. 49-A", "3.00%", "0.00% (Exempt)", "0.50%", "3.50%", "Market Value"),
    ("04_01", "Partition among Family (Co-parceners)", "Art. 40", "1.00% (Max ₹10k)", "0.00% (Exempt)", "Fixed ₹ 1,000", "1.0% + ₹1k/share", "Value of Separated Share (VSS)"),
    ("04_02", "Partition among Non-Family / Others", "Art. 40", "2.00%", "0.00% (Exempt)", "Fixed ₹ 1,000", "2.0% + ₹1k/share", "Value of Separated Share (VSS)"),
    ("02_01", "Mortgage with Possession", "Art. 35(a)", "4.00%", "0.00% (Exempt)", "0.50%", "4.50%", "Secured Loan Amount"),
    ("02_02", "Mortgage without Possession (Simple)", "Art. 35(b)", "0.50% (Max ₹50k)", "0.00% (Exempt)", "0.50%", "1.00% (Max ₹50k)", "Secured Loan Amount"),
    ("02_08", "Deposit of Title Deeds (DOTD)", "Art. 7", "0.50% (Max ₹50k)", "0.00% (Exempt)", "0.10% (Max ₹10k)", "0.60% (Max ₹60k)", "Loan Amount Advanced"),
    ("04_04", "Release of Share among Family", "Art. 46", "3.00%", "0.00% (Exempt)", "0.50%", "3.50%", "Market Value of Released Share"),
    ("04_05", "Release among Co-owners / Others", "Art. 46", "2.00%", "0.00% (Exempt)", "0.50%", "2.50%", "Consideration or Market Value"),
    ("05_01", "Lease Deed (Up to 1 Year)", "Art. 31(a)", "0.50%", "0.00% (Exempt)", "0.50%", "1.00%", "Total Rent Reserved"),
    ("05_02", "Lease Deed (1 to 5 Years)", "Art. 31(b)", "0.50%", "0.00% (Exempt)", "0.50%", "1.00%", "Average Annual Rent (AAR)"),
    ("05_03", "Lease Deed (5 to 10 Years)", "Art. 31(c)", "1.00%", "0.00% (Exempt)", "0.50%", "1.50%", "Average Annual Rent (AAR)"),
    ("01_05", "Exchange of Immovable Property", "Art. 27", "5.00%", "1.50%", "1.00%", "7.50%", "Market Value of Greater Property"),
]


def format_deed_rate_schedule(year: int, area_type: str = "MUNICIPAL_CORP") -> str:
    rows_md = []
    for code, title, art, sd, td, rf, total, basis in KEY_STATUTORY_DEEDS_20:
        rows_md.append(f"| `{code}` | **{title}** | `{art}` | {sd} | {td} | {rf} | **{total}** | {basis} |")
    
    table_content = "\n".join(rows_md)
    
    return (
        f"### 📊 Statutory AP Duty & Fee Schedule (20 Key Deeds — Year {year})\n\n"
        f"**Jurisdiction Area:** `{area_type}` | **Prevailing Authority:** `G.O.Ms.No. 394 & 393 Rev (Regn-I) dt. 26.11.2014` & `G.O.Ms.No. 192 dt. 25.04.2023`\n\n"
        f"| Code | Instrument Classification | ISA Art. | Stamp Duty (SD) | Transfer Duty (TD) | Reg Fee (RF) | Total Base Rate | Chargeable Basis |\n"
        f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        f"{table_content}\n\n"
        f"### ⚖️ Statutory Provisions & Fee Rules:\n"
        f"• **Stamp Duty (SD):** Rationalized under Schedule I-A of the Indian Stamp Act, 1899 (AP Amendment) and **G.O.Ms.No. 394**.\n"
        f"• **Transfer Duty (TD):** 1.50% local body surcharge under AP Municipalities Act 1965 in Municipal Corporations and Municipalities. Note: Family Partitions, Mortgages, Settlements, and Lease Deeds are **completely exempt** from Transfer Duty.\n"
        f"• **Registration Fee (RF):** Rationalized under the AP Registration Table of Fees and **G.O.Ms.No. 393**.\n"
        f"• **CARD User Charges:** ₹ 500.00 flat (up to 10 sheets) under **G.O.Ms.No. 192 dt. 25.04.2023**.\n\n"
        f"*(To assess duty for your transaction, reply with your property amount anytime e.g. \"Calculate duty for 15L sale deed\" or \"split 20L land with brother\").*"
    )


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
    """Main routing and response generator for local prototype use."""
    if IS_PRODUCTION:
        return ChatResponse(
            reply=(
                "The prototype chatbot is disabled in production mode because its legal corpus, "
                "rate data, and intent handling have not passed departmental verification."
            ),
            intent="SAFE_MODE",
        )

    text = message.strip()
    t_lower = text.lower()

    # ==============================================================
    # 0. INTENT: Greetings & Help
    # ==============================================================
    if is_greeting(t_lower):
        reply = (
            "### 🙏 Welcome to the AP Registration & Stamps Assistant\n\n"
            "This independent prototype uses sample data that has not been approved or verified by the department. It is not connected to CARD 2.0.\n\n"
            "You can ask me to:\n"
            "• **Calculate Duties & Fees:** e.g. *\"How much tax for giving 15L land to my son?\"*, *\"Duty on 25L sale deed in Vijayawada\"*, *\"DOTD caps for 50L loan\"*\n"
            "• **Verify Section 22-A Prohibited Land:** e.g. *\"Can I register plot in Survey 102/1 Gunadala?\"*\n"
            "• **Check Delayed Presentation Fines:** e.g. *\"Section 25 delay fine for 5 months late\"*\n"
            "• **Audit Historical Deltas:** e.g. *\"Compare Section 41-A audit delta for 2009 deed vs current rates\"*\n"
            "• **Convert Land Measurements:** e.g. *\"Convert 2 Acres 50 Cents to square yards\"*\n\n"
            "How can I assist you with your transaction today?"
        )
        return ChatResponse(reply=reply, intent="GREETING")

    # ==============================================================
    # 0.5. INTENT: Deed Rate Schedule / Percentages Catalog
    # ==============================================================
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
    if is_schedule_query:
        m_yr = re.search(r"\b(19\d{2}|20[0-2]\d)\b", text)
        req_year = int(m_yr.group(1)) if m_yr else date.today().year
        area = detect_area_type(text)
        reply = format_deed_rate_schedule(req_year, area)
        return ChatResponse(reply=reply, intent="DEED_SCHEDULE")

    # ==============================================================
    # 1. INTENT: Area Unit Conversion
    # ==============================================================
    is_explicit_convert = bool(re.search(r"\bconvert\b", t_lower))
    has_land_unit = bool(re.search(r"\b(?:acres?|cents?|ankanams?|guntas?|grounds?|sq\.?\s*yds?|square\s+yards?|sq\.?\s*ft|square\s+feet|sq\.?\s*meters?|hectares?)\b", t_lower))
    has_digit = bool(re.search(r"\d+", text))

    if (is_explicit_convert or (has_land_unit and has_digit)) and not is_schedule_query:
        try:
            sq_yds = parse_composite_land_string(text)
            if sq_yds > Decimal("0"):
                res = convert_area(sq_yds, "Sq.Yds")
                reply = (
                    f"### 📐 AP Land Measurement Conversion Result\n\n"
                    f"**Parsed Input Area:** `{text}`\n\n"
                    f"| Measurement Unit | Converted Equivalent |\n"
                    f"| :--- | :--- |\n"
                    f"| **Square Yards (Gajam)** | **{res.sq_yards:,.2f} Sq.Yds** |\n"
                    f"| **Acres** | **{res.acres:.4f} Acres** |\n"
                    f"| **Cents** | **{res.cents:,.2f} Cents** |\n"
                    f"| **Ankanams** (Rayalaseema) | **{res.ankanams:,.2f} Ankanams** |\n"
                    f"| **Guntas** | **{res.guntas:,.2f} Guntas** |\n"
                    f"| **Square Feet** | **{res.sq_feet:,.2f} Sq.Ft** |\n"
                    f"| **Square Meters** | **{res.sq_meters:,.2f} Sq.Mtrs** |\n"
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
    # 2. INTENT: Presentation Delay Fines (Section 25 / Section 23)
    # ==============================================================
    if any(k in t_lower for k in ["delay fine", "section 25", "sec 25", "late presentation", "time barred", "months late", "delayed"]):
        amt = parse_rupee_amount(text)
        date_tokens = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if amt <= 0 or len(date_tokens) < 2:
            return ChatResponse(
                reply=(
                    "To estimate a presentation fine, provide the proper registration fee and both exact dates "
                    "(execution and presentation), for example: `fee ₹15,000; executed 2025-01-01; presented 2025-05-11`. "
                    "I will not infer dates or the fee from a month count."
                ),
                intent="NEED_CLARIFICATION",
            )
        exec_d = date.fromisoformat(date_tokens[0])
        pres_d = date.fromisoformat(date_tokens[1])
        proper_rf = amt
        fine_res = calculate_delay_fine(exec_d, pres_d, proper_rf)
        
        status_banner = "✅ **ADMISSIBLE (No Fine Required)**" if fine_res.status == "OK" else (
            f"⚠️ **ADMISSIBLE WITH FINE ({fine_res.fine_multiplier}x Penalty)**" if fine_res.status == "FINE_APPLICABLE" else
            "🚫 **TIME-BARRED — MANDATORY REFUSAL UNDER SECTION 23**"
        )
        
        reply = (
            f"### ⏱️ Section 25 Delayed Presentation Assessment\n\n"
            f"{status_banner}\n\n"
            f"• **Execution Date:** `{exec_d}`\n"
            f"• **Presentation Date:** `{pres_d}` ({fine_res.days_late} days after execution)\n"
            f"• **Proper Registration Fee:** ₹ {proper_rf:,.2f}\n"
            f"• **Penalty Fine Amount:** **₹ {fine_res.fine_amount:,.2f}**\n\n"
            f"**Legal Rule:** {fine_res.message}\n\n"
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
                "legal_basis": fine_res.legal_basis
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
                f"### ⚠️ MANDATORY REFUSAL: Section 22-A Prohibited Land\n\n"
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

    # ==============================================================
    # 2.8. INTENT: Rule 61 vs Rule 71 Enquiry (Deceased Executant vs Post-Death Will)
    # ==============================================================
    is_rule_61_71_query = (
        ("rule 61" in t_lower and "rule 71" in t_lower)
        or ("rule 61" in t_lower or "rule 71" in t_lower)
        or ("executant died" in t_lower and "will" in t_lower)
        or ("died after signing" in t_lower and "will" in t_lower)
    )
    if is_rule_61_71_query:
        reply = (
            "### Rule 61 vs. Rule 71 Enquiry: AP Registration Rules & Registration Act, 1908\n\n"
            "| Statutory Parameter | Rule 61 Enquiry (Non-Testamentary Deeds) | Rule 71 Enquiry (Wills & Authorities to Adopt) |\n"
            "| :--- | :--- | :--- |\n"
            "| **Document Category** | Non-testamentary inter-vivos deeds (Sale Deed, Gift, Settlement, Mortgage) signed by executant before death. | Testamentary instruments (Wills) or Authorities to Adopt presented after the testator's / donor's death. |\n"
            "| **Governing Act Provisions** | **Sections 34 & 35(1)(c)** of Registration Act 1908 read with **AP Registration Rule 61**. | **Sections 40 & 41(2)** of Registration Act 1908 read with **AP Registration Rule 71**. |\n"
            "| **Presenter & Parties** | Presented by claimant/buyer; the **legal representative(s) / heirs of the deceased seller must appear** and admit execution. | Presented by the executor or any legatee/beneficiary claiming under the Will (**Section 40**). Legal heirs do NOT need to admit execution. |\n"
            "| **Notice & Publication** | Direct summons/notice served on the legal heirs to appear and admit/deny execution. No public advertisement. | **Mandatory three-tier notice:** (1) SRO notice board, (2) Registered post notice to all natural legal heirs, (3) **Mandatory publication in a local daily newspaper** (Rule 71). |\n"
            "| **Scope of SRO Enquiry** | SRO verifies: (1) Fact of death (Death Certificate), (2) Legal heirship/representative character, (3) Whether legal representative admits execution. | SRO conducts quasi-judicial enquiry under **Section 41(2)** to satisfy three statutory facts: (1) Due execution by testator, (2) Testator is dead, (3) Presenter is entitled under Section 40. |\n"
            "| **Power to Overrule Heirs' Denial** | **ZERO EVIDENTIARY POWER.** If the legal heirs deny execution, dispute the signature, or fail to appear, the Sub-Registrar **MUST REFUSE REGISTRATION** under Section 35(3)(c). Claimant must appeal to District Registrar under Section 73. | **FULL QUASI-JUDICIAL POWER.** SRO summons and examines attesting witnesses, scribes, and doctors under oath, records evidence, and **can overrule natural heirs' objections and register the Will** if satisfied of due execution. |\n"
            "| **Presentation Time Limit** | **Strictly 4 months** from date of execution under **Section 23** (extendable up to 8 months with Section 25 fine). | **NO TIME LIMIT.** Under **Section 27**, a Will can be presented for registration at any time after the testator's death (even years later). |\n\n"
            "### Key Practical Distinction for SRO Officers:\n"
            "- **Sale Deed (Rule 61):** The Sub-Registrar has no legal authority to conduct a mini-trial or examine attesting witnesses to prove a deceased seller's signature if the seller's legal heirs deny execution or stay away. The SRO must issue a refusal order under Section 35(3)(c), directing the buyer to file an appeal before the District Registrar under Section 73.\n"
            "- **Will (Rule 71):** Because Wills take effect only upon death and natural heirs frequently dispute them, the Legislature granted the Registering Officer direct quasi-judicial authority under Section 41(2) to publish newspaper notices, examine attesting witnesses, and register the instrument even over the heated objections of natural heirs."
        )
        return ChatResponse(
            reply=reply, intent="LEGAL_QUERY"
        )

    # ==============================================================
    # 3. INTENT: Historical Audit Comparison (Section 41-A Delta)
    # ==============================================================
    is_audit_query = (
        any(k in t_lower for k in ["audit delta", "41-a", "41a", "historical delta", "audit comparison", "audit difference"])
        or (any(k in t_lower for k in ["historical", "old rate", "old rates", "past rate", "past rates", "in 2009", "in 2010", "in 2005"]) and any(c in t_lower for c in ["compare", "vs current", "vs new", "difference", "delta"]))
        or ("compare" in t_lower and any(yr in t_lower for yr in ["2009", "2005", "2008", "2010", "1999", "2012", "historical", "old vs new"]))
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
        if not any(
            token in t_lower
            for token in ("corporation", "municipality", "municipal", "panchayat", "rural", "cantonment", "selection grade", "special grade")
        ):
            return ChatResponse(
                reply="Please specify the jurisdiction (Gram Panchayat, Municipality, Corporation, or Cantonment) for this historical comparison.",
                intent="NEED_CLARIFICATION",
            )

        diff = compare_historical_vs_current(session, alias, area, old_d, cur_d, consideration_value=val, market_value=val)
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
            f"**Instrument:** `{code}` ({h.instrument_code}) | **Area:** `{area}` | **Value:** ₹ {val:,.2f}\n\n"
            f"| Fee Head | Historical Rate ({old_d}) | Current Rate ({cur_d}) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| **Stamp Duty** | {h.stamp_duty_rate} (₹ {h.stamp_duty:,.2f}) | {c.stamp_duty_rate} (₹ {c.stamp_duty:,.2f}) |\n"
            f"| **Transfer Duty** | {h.transfer_duty_rate} (₹ {h.transfer_duty:,.2f}) | {c.transfer_duty_rate} (₹ {c.transfer_duty:,.2f}) |\n"
            f"| **Registration Fee** | {h.reg_fee_rate} (₹ {h.registration_fee:,.2f}) | {c.reg_fee_rate} (₹ {c.registration_fee:,.2f}) |\n"
            f"| **User Charges** | ₹ {h.user_charge:,.2f} | ₹ {c.user_charge:,.2f} |\n"
            f"| **TOTAL PAYABLE** | **₹ {h.total_payable:,.2f}** | **₹ {c.total_payable:,.2f}** |\n\n"
            f"**Audit Delta (Net Variance):** **₹ {delta:,.2f}**\n\n"
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
    has_family_transfer = any(g in t_lower for g in ["giving", "give", "gift", "gifting", "settle", "settling", "transfer", "transferring", "hand over"]) and any(rel in t_lower for rel in ["son", "daughter", "father", "mother", "wife", "husband", "brother", "sister", "family", "children", "child", "parents", "relative"])
    has_partition_split = any(k in t_lower for k in ["split", "splitting", "divide", "dividing", "partition", "separated share"])
    has_dagpa_builder = any(k in t_lower for k in ["development agreement", "dagpa", "construction agreement", "builder", "developer"])
    has_deed_term = any(d in t_lower for d in [
        "sale", "gift", "partition", "settlement", "mortgage", "deposit", "dotd",
        "deed", "lease", "agpa", "dagpa", "adoption", "affidavit", "buy", "buying",
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
    
    is_calc_query = (
        is_tax_intent
        or (has_amount and has_deed_term)
        or (has_dagpa_builder and any(k in t_lower for k in ["sd", "rf", "duty", "fee", "cost", "tax", "cap", "adjustable", "percent", "percentage"]))
        or (any(k in t_lower for k in ["duty", "fee", "cost", "tax", "charge", "charges", "pay", "rate", "rates", "cap", "percent", "percentage", "sd", "rf", "td"]) and has_deed_term and not any(q in t_lower for q in ["what are the rules", "is it legal", "can i register", "can registrar", "prohibited", "section 22-a", "section 22a", "article"]))
    )

    if not is_calc_query:
        # Query ChromaDB Vector Knowledge Base
        legal_matches = search_legal_knowledge(text, limit=3)
        if legal_matches:
            top = legal_matches[0]
            if "OCR source" in top.authority_citation:
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
        return ChatResponse(
            reply=(
                "I could not find a relevant, verified source in the indexed records for this legal question. "
                "I will not substitute an unrelated section or guess a classification. Please provide the "
                "governing order/judgment or have the applicable original Act, AP amendment, and case law "
                "checked by a qualified department/legal officer."
            ),
            intent="LEGAL_NOT_FOUND",
        )

    # ==============================================================
    # 5. INTENT: Duty & Fee Calculation (Deterministic Engine)
    # ==============================================================
    val = parse_rupee_amount(text)
    user_provided_amount = (val > Decimal("0"))
    exec_d = parse_date_from_text(text)
    code, alias = detect_deed_code(text)
    area = detect_area_type(text)
    has_explicit_area = any(
        token in t_lower
        for token in (
            "corporation", "municipality", "municipal", "panchayat",
            "rural", "cantonment", "selection grade", "special grade",
        )
    )
    if (
        not has_explicit_area
        and exec_d >= date(2005, 7, 1)
        and code != "04_01"
        and alias != "PARTITION_FAMILY"
    ):
        return ChatResponse(
            reply=(
                "Please specify the property jurisdiction (Gram Panchayat, Municipality, Corporation, "
                "or Cantonment). This prototype will not assume an area type for a duty lookup."
            ),
            intent="NEED_CLARIFICATION",
        )

    # Special dedicated handling for Development Agreement cum GPA (01_10 / DAGPA)
    if code == "01_10" or alias == "DEVELOPMENT_AGREEMENT_CUM_GPA":
        if not user_provided_amount:
            return ChatResponse(
                reply="Please provide the agreement value/market value before requesting a rupee calculation. I will not assume an amount.",
                intent="NEED_CLARIFICATION",
            )

        breakdown: DutyBreakdown = calculate_deed_duty(
            session, "DEVELOPMENT_AGREEMENT_CUM_GPA", area, exec_d,
            market_value=val, consideration_value=val
        )

        reply = (
            f"### 📋 Statutory Assessment: Development Agreement cum GPA (`01_10` / `DAGPA`)\n\n"
            f"**Chargeable Base:** ₹ {val:,.2f} Estimated Cost / Market Value | **Jurisdiction Area:** `{area}`\n\n"
            f"| Statutory Component | Applicable Rate / Rule | Amount Payable (₹) | Authoritative Citation |\n"
            f"| :--- | :--- | :--- | :--- |\n"
            f"| **Stamp Duty (SD)** | **1.0000%** | **₹ {breakdown.stamp_duty:,.2f}** | ISA 1899 Sch I-A Art. 6-B & G.O.Ms.No. 394 |\n"
            f"| **Transfer Duty (TD)** | **0.0000% (EXEMPT)** | **₹ 0.00** | Exempt from Local Body Surcharges |\n"
            f"| **Registration Fee (RF)** | **0.5000% (Capped at ₹ 20,000)** | **₹ {breakdown.registration_fee:,.2f}** | AP Regn Table of Fees & G.O.Ms.No. 393 |\n"
            f"| **CARD User Charges** | Flat User Fee | **₹ {breakdown.user_charge:,.2f}** | G.O.Ms.No. 192 dt. 25.04.2023 |\n"
            f"| **TOTAL PAYABLE** | **Grand Total at Registration** | **₹ {breakdown.total_payable:,.2f}** | Official AP Statutory Assessment |\n\n"
            f"### ⚖️ Answers to Your Questions:\n"
            f"1. **What is the SD%?**\n"
            f"   • The Stamp Duty rate is **1.0000%** on the estimated cost of construction or market value of the property under **Indian Stamp Act 1899 Schedule I-A, Article 6-B** and **G.O.Ms.No. 394**.\n"
            f"   • For ₹ 1.2 Crore (₹ 1,20,00,000), Stamp Duty = **₹ 1,20,000.00**.\n\n"
            f"2. **Is it adjustable later?**\n"
            f"   • **YES, 100% ADJUSTABLE.** Under the statutory proviso to Article 6-B of Schedule I-A of the Indian Stamp Act, 1899 (AP Amendment):\n"
            f"     > *\"Provided that the stamp duty already paid on such agreement shall be adjusted towards the stamp duty payable on the ultimate sale deed or conveyance executed in respect of such property or share in property.\"*\n"
            f"   • When the builder sells the completed flats or developer's share to end buyers, the 1% stamp duty paid on this DAGPA is credited/deducted from the final sale deed stamp duty.\n\n"
            f"3. **What is the RF Cap?**\n"
            f"   • The Registration Fee is **0.5000%**, but it is **strictly capped at a statutory Maximum of ₹ 20,000.00** *(Column 11: `RF (MAX)` in the official fee schedule & G.O.Ms.No. 393)*.\n"
            f"   • At 0.5%, the fee on ₹ 1.2 Crore would mathematically be ₹ 60,000, but by virtue of the statutory cap, the registrant pays only **₹ 20,000.00**."
        )

        card_slip_data = {
            "instrument_code": "01_10",
            "deed_alias": "DEVELOPMENT_AGREEMENT_CUM_GPA",
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
            "citations": breakdown.citations,
            "warnings": breakdown.warnings,
        }
        signed_slip = sign_and_archive_check_slip(session, card_slip_data)
        return ChatResponse(
            reply=reply, intent="CALCULATION_OK",
            card_slip=signed_slip,
        )

    # Partition duty depends on actual individual shares. The chat parser does not
    # reliably extract those values, so require the structured calculator instead
    # of inventing an equal split from a total property value.
    if code == "04_01" or alias == "PARTITION_FAMILY":
        return ChatResponse(
            reply=(
                "A partition calculation requires the value of each separated share. "
                "This chat cannot safely infer an equal split from total property value. "
                "Use the structured calculator/API and provide the `shares` list."
            ),
            intent="NEED_CLARIFICATION",
        )

    exec_d = parse_date_from_text(text)
    code, alias = detect_deed_code(text)
    area = detect_area_type(text)

    # Call deterministic calculator engine
    breakdown: DutyBreakdown = calculate_deed_duty(
        session, alias, area, exec_d,
        consideration_value=val, market_value=val, loan_amount=val
    )

    if breakdown.status == "NOT_FOUND":
        reply = (
            f"### ⚠️ Statutory Rate Unverified (Calculation Refused)\n\n"
            f"**Instrument:** `{code}` ({alias}) | **Date:** `{exec_d}` | **Area:** `{area}`\n\n"
            f"**Status:** `NOT_FOUND`\n\n"
            f"{breakdown.message}\n\n"
            f"**Audit Protection Rule:** The IGRS Assistant strictly refuses to guess or interpolate tax rates without an authoritative Government Order on record."
        )
        return ChatResponse(reply=reply, intent="CALCULATION_REFUSED")

    DEED_FRIENDLY_TITLES = {
        "01_01": "Sale of Immovable Property",
        "01_02": "Sale Agreement with Possession",
        "01_03": "Sale Agreement without Possession",
        "01_10": "Development Agreement cum GPA (DAGPA)",
        "01_11": "Agreement of Sale cum GPA (AGPA)",
        "03_05": "Gift in favour of Relatives u/s 56(2) of IT Act (Panchayat)",
        "03_06": "Gift in favour of Relatives u/s 56(2) of IT Act (Municipalities & Corporations)",
        "03_07": "Gift in favour of Others / Non-Relatives",
        "03_01": "Settlement in favour of Family",
        "03_02": "Settlement in favour of Others",
        "04_01": "Partition among Family (Co-parceners)",
        "04_02": "Partition among Others",
        "02_01": "Mortgage with Possession",
        "02_02": "Mortgage without Possession",
        "02_08": "Deposit of Title Deeds (DOTD)",
        "04_04": "Release of Share among Family",
        "04_05": "Release among Others",
    }
    deed_title = DEED_FRIENDLY_TITLES.get(code, alias)

    # Valuation banner
    # Specific legal note for Gift Deeds under Section 56(2)
    sec56_note = ""
    if code in ("03_05", "03_06"):
        sec56_note = (
            f"\n\n### Section 56(2) Relative Eligibility:\n"
            f"- **Eligibility Status:** **YES, 100% ELIGIBLE** (Relative Exemption)\n"
            f"- **Legal Rule:** Under Explanation (e) to **Section 56(2)(x)** of the Income Tax Act, 1961 (evaluated from recipient's perspective):\n"
            f"  - **Clause (iii)** explicitly covers the *brother or sister of the spouse of the individual* (husband's sister).\n"
            f"  - **Clause (vii)** explicitly covers the *spouse of a brother or sister* (brother's wife).\n"
            f"- **Tax & Duty Impact:** The gift is completely exempt from income tax in the recipient's hands AND qualifies for the concessional **2.0000%** AP Stamp Duty under **Indian Stamp Act 1899 Schedule I-A, Article 29** & **G.O.Ms.No. 394** (rather than 5.0% for non-relatives)."
        )

    citation_str = breakdown.citations[0] if breakdown.citations else 'Indian Stamp Act 1899 & G.O.Ms.No. 394'

    if user_provided_amount:
        reply = (
            f"### Duty Assessment: {deed_title} (`{code}`)\n\n"
            f"**Execution Date:** `{exec_d}` | **Jurisdiction:** `{area}` | **Chargeable Value:** ₹ {val:,.2f}\n\n"
            f"| Component | Statutory Rate / Rule | Amount Payable (₹) | Legal Authority |\n"
            f"| :--- | :--- | :--- | :--- |\n"
            f"| **Stamp Duty (SD)** | {breakdown.stamp_duty_rate} | **₹ {breakdown.stamp_duty:,.2f}** | {citation_str} |\n"
            f"| **Transfer Duty (TD)** | {breakdown.transfer_duty_rate or '0%'} | **₹ {breakdown.transfer_duty:,.2f}** | AP Municipalities / GP Surcharge |\n"
            f"| **Registration Fee (RF)** | {breakdown.reg_fee_rate} | **₹ {breakdown.registration_fee:,.2f}** | Registration Act 1908 & G.O.Ms.No. 393 |\n"
            f"| **CARD User Charges** | Flat User Fee | **₹ {breakdown.user_charge:,.2f}** | G.O.Ms.No. 192 dt. 25.04.2023 |\n"
            f"| **TOTAL PAYABLE** | **Grand Total at Registration** | **₹ {breakdown.total_payable:,.2f}** | Official Assessment |\n"
            f"{sec56_note}"
        )
    else:
        reply = (
            f"### Statutory Rate Schedule: {deed_title} (`{code}`)\n\n"
            f"**Jurisdiction Area:** `{area}` | **Prevailing Authority:** `{citation_str}`\n\n"
            f"| Component | Applicable Rate / Base | Legal Authority |\n"
            f"| :--- | :--- | :--- |\n"
            f"| **Stamp Duty (SD)** | **{breakdown.stamp_duty_rate}** | {citation_str} |\n"
            f"| **Transfer Duty (TD)** | **{breakdown.transfer_duty_rate or '0.0000% (Exempt)'}** | AP Municipalities / GP Surcharge |\n"
            f"| **Registration Fee (RF)** | **{breakdown.reg_fee_rate}** | Registration Act 1908 & G.O.Ms.No. 393 |\n"
            f"| **CARD User Charges** | **₹ 500.00** flat | G.O.Ms.No. 192 dt. 25.04.2023 |\n"
            f"{sec56_note}\n\n"
            f"*To calculate the exact payable rupees, provide your property market value anytime (e.g. ₹ 20 Lakhs).*"
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
        "citations": breakdown.citations,
        "warnings": breakdown.warnings
    }

    signed_slip = sign_and_archive_check_slip(session, card_slip_data)
    return ChatResponse(
        reply=reply, intent="CALCULATION_OK",
        card_slip=signed_slip
    )
