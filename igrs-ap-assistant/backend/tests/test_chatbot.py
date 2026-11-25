"""
Unit Tests for Phase 3: AI Chatbot Backend Engine (/api/chat).

Verifies that:
- Conversational duty calculation routes to deterministic engine and returns CARD slip data.
- Conversational Section 41-A audit comparison routes properly.
- Delay fine queries route to Section 25 engine.
- Legal inquiries route to Phase 2 statutory index (Section 22-A etc.).
- Area conversion queries route to area converter.
- Unverified dates strictly return NOT_FOUND refusal.
"""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import create_tables, Base
from backend.app.db.seed import seed_all
from backend.app.core.config import SEEDS_DIR
from backend.app.engine.chatbot_orchestrator import process_chat_message


@pytest.fixture(scope="module")
def session():
    test_engine = create_engine("sqlite:///:memory:")
    create_tables(test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    sess = TestingSession()
    seed_all(sess, SEEDS_DIR)
    yield sess
    sess.close()


def test_chat_sale_deed_calculation(session):
    msg = "Calculate duty for 15 Lakh sale deed in Vijayawada Corporation in 2025"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["total_payable"] == "113000"
    assert "75,000" in res.reply
    assert "G.O.Ms.No. 394" in res.reply or "Indian Stamp Act" in res.reply


def test_chat_audit_comparison(session):
    msg = "Compare Section 41-A audit delta for 15 Lakhs sale deed executed in May 2009 in Corporation"
    res = process_chat_message(session, msg)
    assert res.intent == "AUDIT_COMPARE"
    assert res.audit_comparison is not None
    assert res.card_slip is not None
    assert "142,595" in res.reply or "1,42,595" in res.reply
    assert "-29,595" in str(res.audit_comparison["delta_total"]) or "-29595" in str(res.audit_comparison["delta_total"])


def test_chat_delay_fine_section_25(session):
    msg = "What is the Section 25 delay fine? Proper registration fee ₹15,000; executed 2025-01-01; presented 2025-05-11."
    res = process_chat_message(session, msg)
    assert res.intent == "DELAY_FINE"
    assert res.fine_data is not None
    assert res.fine_data["fine_multiplier"] == 2
    assert "30,000" in res.reply


def test_chat_section_22a_inquiry(session):
    msg = "What are the rules for Section 22-A prohibited properties?"
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs is not None
    assert len(res.legal_docs) > 0
    assert "22" in res.legal_docs[0]["section_or_rule"]
    assert "Assigned Lands" in res.reply or "MANDATORY REFUSAL" in res.reply


def test_chat_freedom_fighter_noc_question_uses_ocr_caveat(session):
    msg = (
        "A freedom fighter's assigned land is over 10 years old and has no dispute—"
        "do they still need an NOC to delete it from Section 22-A?"
    )
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    assert "not required" in res.reply.lower()
    assert "Collector" in res.reply
    assert "OCR transcription" in res.reply
    assert "confirm the original" in res.reply.lower()
    assert "Section 28" not in res.reply


def test_chat_release_vs_conveyance_article_query_is_source_limited(session):
    msg = (
        "In a Release deed between 4 brothers, their sister (a co-parcener) did not join "
        "the deed. Is it a Release u/Art 46 or Conveyance u/Art 20?"
    )
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    assert "Section 55A" not in res.reply
    assert "secondary-source summary" in res.reply
    assert "cannot safely classify" in res.reply
    assert "AIR 1986 AP 42" in res.reply


def test_chat_area_conversion(session):
    msg = "Convert 2 Acres 50 Cents to square yards"
    res = process_chat_message(session, msg)
    assert res.intent == "AREA_CONVERT"
    assert res.area_data is not None
    assert Decimal(str(res.area_data["sq_yards"])) == Decimal("12100")
    assert "12,100" in res.reply


def test_chat_unverified_refusal(session):
    msg = "Calculate duty for a sale deed executed in 1985"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_REFUSED"
    assert "NOT_FOUND" in res.reply
    assert "refusing to guess" in res.reply


def test_chat_greeting(session):
    for g in ["hello", "hii broo", "heyy", "namaste", "namaskaram", "yo bro", "who are you"]:
        res = process_chat_message(session, g)
        assert res.intent == "GREETING", f"Failed for {g}"
        assert "AP Registration & Stamps Advisory" in res.reply


def test_chat_colloquial_family_gift(session):
    msg = "i am giving my land to my son in Vijayawada Municipal Corporation in 2025; how much tax if it is worth 15 L?"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "03_06"
    assert res.card_slip["total_payable"] == "60500"
    assert "30,000" in res.reply  # Stamp duty 2% on 15L = 30k


def test_chat_partition_brother_split(session):
    msg = "what tax when i split my land with my brother"
    res = process_chat_message(session, msg)
    assert res.intent == "RATE_LOOKUP"
    assert res.card_slip is None
    assert "1.0000%" in res.reply
    assert "Separated Shares" in res.reply


def test_chat_deed_rate_schedule_returns_verified_rows(session):
    msg = "bro can you give me percentages of different 20 deeds of year 2023"
    res = process_chat_message(session, msg)
    assert res.intent == "DEED_SCHEDULE"
    assert "01_01" in res.reply
    assert "5.0000%" in res.reply


def test_chat_development_agreement_dagpa(session):
    msg = "Builder is registering a Development Agreement-cum-GPA in Vijayawada Corporation for ₹1.2 crore estimated cost—what is the SD%, is it adjustable later, and what is the RF cap"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "01_10"
    assert res.card_slip["is_adjustable"] is False
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("120000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("20000")  # Capped at 20,000!
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("140500")
    assert "1.0000%" in res.reply
    assert "₹1,20,000.00" in res.reply
    assert "₹20,000" in res.reply
    assert "not adjustable" in res.reply.lower()
    assert "581" in res.reply
    assert "463" in res.reply
    assert "G.O.Ms.No. 394" not in res.reply
    assert "G.O.Ms.No. 393" not in res.reply
    assert "Art. 6-B" in res.reply


def test_chat_dagpa_without_area_uses_universal_candidate_row(session):
    msg = "Builder is registering a Development Agreement-cum-GPA for ₹1.2 crore estimated cost—what is the SD%, is it adjustable later, and what is the RF cap?"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["area_type"] == "ALL"
    assert res.card_slip["is_adjustable"] is False
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("120000")
    assert "Please specify the property jurisdiction" not in res.reply
    assert "not adjustable" in res.reply.lower()


def test_chat_commercial_lease_with_ambiguous_deposit_computes_both_scenarios(session):
    msg = "Commercial shop lease for 8 years, monthly rent ₹25,000 plus ₹5 lakh advance/deposit. How much SD and RF?"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "LEASE"
    assert Decimal(str(res.card_slip["chargeable_value"])) == Decimal("300000")
    assert Decimal(str(res.card_slip["rent_stamp_duty"])) == Decimal("6000")
    assert Decimal(str(res.card_slip["advance_stamp_duty"])) == Decimal("10000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("300")
    assert Decimal(str(res.card_slip["user_charge"])) == Decimal("100")
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("16400")
    assert "₹3,00,000.00" in res.reply
    assert "₹6,000.00" in res.reply
    assert "₹10,000.00" in res.reply
    assert "₹6,400.00" in res.reply
    assert "S1/4813/2008" in res.reply
    assert "CCRA1/13429/2008" in res.reply
    assert "Please specify the property jurisdiction" not in res.reply


def test_chat_commercial_lease_without_deposit_uses_duration_and_annual_rent(session):
    msg = "Commercial shop lease for 8 years with monthly rent ₹25,000. How much SD and RF?"
    res = process_chat_message(session, msg)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "LEASE"
    assert res.card_slip["area_type"] == "ALL"
    assert res.card_slip["calc_basis_used"] == "AAR"
    assert Decimal(res.card_slip["chargeable_value"]) == Decimal("300000")
    assert Decimal(res.card_slip["stamp_duty"]) == Decimal("6000")
    assert Decimal(res.card_slip["registration_fee"]) == Decimal("300")
    assert "6,400.00" in res.reply


def test_chat_gift_sister_in_law_sec56_confirms_relative_eligibility(session):
    msg = "Sister is gifting property in a Municipal Corporation to her brother's wife—is sister-in-law eligible for 2% relative gift duty under Sec 56(2) of IT Act?"
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.card_slip is None  # No amount provided — never assumes an amount
    assert res.legal_docs
    assert "clause (vii)" in res.reply.lower()
    assert "clause (iii)" in res.reply.lower()
    assert "2.00%" in res.reply


def test_chat_crop_loan_exemption_with_acreage_routes_to_legal_search(session):
    """#24 regression: 2.5 lakh crop loan for a small farmer (4 acres wet) must be 100% exempt under G.O.Ms.No. 15/2007."""
    msg = "A small farmer with 4 acres wet land takes a ₹2.5 lakh crop loan—is he exempt from Stamp Duty and Registration Fee?"
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    assert "AREA_CONVERT" not in res.intent
    assert "land measurement conversion" not in res.reply.lower()
    # Must mention exemption or G.O.Ms.No. 15 (not old ₹2L ceiling)
    combined = res.reply.lower()
    assert "exempt" in combined or "3,00,000" in res.reply or "g.o.ms.no. 15" in combined, \
        f"Reply should mention exemption under ₹3L ceiling; got: {res.reply[:400]}"
    # Must NOT still say the loan exceeds the ceiling (old bug with ₹2,00,000 limit)
    assert "secondary-source retrieval note" not in res.reply.lower(), \
        "Must not use the old secondary-source bug wording"


def test_chat_settlement_debt_classification_routes_to_legal_search(session):
    """#22 regression: Settlement with debt discharge must be classified as Settlement-cum-Conveyance (S3/7423/93)."""
    msg = (
        "A person is settling ₹10 lakh property on his son with a condition that the son "
        "must clear ₹3 lakh of the father's debts. Is this pure Settlement or "
        "Settlement-cum-Conveyance?"
    )
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    assert "Settlement-cum-Conveyance" in res.reply
    assert "S3/7423/93" in res.reply
    # Must NOT retain the old "secondary-source retrieval note" / Note-10 wording
    assert "secondary-source retrieval note" not in res.reply.lower(), \
        "Old Note-10 secondary-source wording must not appear; must cite S3/7423/93"
    assert "Please specify the property jurisdiction" not in res.reply


def test_chat_rule_61_vs_rule_71_returns_structured_comparison(session):
    msg = "Executant died after signing a sale deed, before registration vs a Will presented after the testator's death—what is the exact difference between Rule 61 and Rule 71 enquiry?"
    res = process_chat_message(session, msg)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    titles = " ".join(f"{item['section_or_rule']} {item['title']}" for item in res.legal_docs)
    assert "Rule 61" in titles and "Rule 71" in titles
    assert "Rule 61 vs. Rule 71 Enquiry" in res.reply
    assert "Tom-tom" in res.reply or "tom-tom" in res.reply
    assert "daily newspaper" in res.reply.lower()






