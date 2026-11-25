"""Regression tests for the 5 live QA audit fixes."""
from datetime import date
from decimal import Decimal
import re
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import SEEDS_DIR
from backend.app.db.models import create_tables
from backend.app.db.seed import seed_all
from backend.app.engine.area_converter import parse_composite_land_string
from backend.app.engine.chatbot_orchestrator import process_chat_message
from backend.app.engine.duty_calculator import calculate_deed_duty
from backend.app.engine.formatting import format_inr, strip_numeric_commas
from backend.app.engine.structure_calc import calculate_building_structure


@pytest.fixture(scope="module")
def session():
    test_engine = create_engine("sqlite:///:memory:")
    create_tables(test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    sess = TestingSession()
    seed_all(sess, SEEDS_DIR)
    yield sess
    sess.close()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# 1. Comma parsing in unit converter & number extractors
# ---------------------------------------------------------------------------

def test_strip_numeric_commas_and_unit_converter(session):
    assert strip_numeric_commas("1,800 sq.ft for ₹1,20,00,000") == "1800 sq.ft for ₹12000000"

    parsed = parse_composite_land_string("1,800 sq.ft")
    assert parsed == Decimal("200.0000")

    res = process_chat_message(session, "Convert 1,800 sq.ft to sq yards")
    assert res.intent == "AREA_CONVERT"
    assert res.area_data is not None
    assert Decimal(str(res.area_data["sq_feet"])) == Decimal("1800.00")
    assert Decimal(str(res.area_data["sq_yards"])) == Decimal("200.0000")
    assert "200.00 Sq.Yds" in res.reply
    assert "1,800.00 Sq.Ft" in res.reply
    assert " 800.00 Sq.Ft" not in res.reply
    assert "88.8889" not in res.reply


def test_comma_separated_rupee_amount_parsed_intact(session):
    res = process_chat_message(
        session,
        "Calculate stamp duty for Sale Deed in Vijayawada Municipal Corporation for ₹1,20,00,000",
    )
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert Decimal(str(res.card_slip["chargeable_value"])) == Decimal("12000000")
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("600000")
    assert "₹1,20,00,000.00" in res.reply
    assert "₹6,00,000.00" in res.reply


# ---------------------------------------------------------------------------
# 2. Structure value routing vs land converter + temporal calculate_building_structure()
# ---------------------------------------------------------------------------

def test_structure_keywords_route_to_structure_never_area_converter(session):
    prompts = [
        "Calculate structure value for 1,800 sq.ft RCC building 12 years old on 2026-03-01",
        "1,800 sq.ft plinth area RCC ground + 3rd floor, age proof 15 years on 2026-03-01",
        "Find depreciation and composite rate for 1,000 sq.ft apartment where site rate is ₹20,000 per sq.yd on 2026-03-01",
    ]
    for prompt in prompts:
        res = process_chat_message(session, prompt)
        assert res.intent == "STRUCTURE_VALUE", f"Prompt misrouted: {prompt}"
        assert res.area_data is None
        assert res.structure_data is not None


def test_calculate_building_structure_temporal_brackets_and_depreciation(session):
    # Pre-Feb 2026 bracket: 2023-06-01 to 2026-01-31 (1400 / 1200 / 850)
    old_corp = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_GF_2F",
        area_tier="CORP_MUNI",
        execution_date=date(2025, 5, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=15,
    )
    assert old_corp.status == "OK"
    assert old_corp.base_rate_per_sqft == Decimal("1400.00")
    assert old_corp.depreciation_pct == Decimal("5")  # 1%/yr above 10 yrs
    assert old_corp.net_building_value == Decimal("1330000")
    assert "MV1/752/2022" in old_corp.citation

    old_gp = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_GF_2F",
        area_tier="GRAM_PANCHAYAT",
        execution_date=date(2025, 5, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=5,
    )
    assert old_gp.base_rate_per_sqft == Decimal("850.00")
    assert old_gp.depreciation_pct == Decimal("0")  # 1-10 yrs = 0%

    # Current bracket: 2026-02-01 onward (1580 / 1350 / 900)
    new_corp = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_GF_2F",
        area_tier="CORP_MUNI",
        execution_date=date(2026, 3, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=120,
    )
    assert new_corp.base_rate_per_sqft == Decimal("1580.00")
    assert new_corp.depreciation_pct == Decimal("70")  # Max 70% non-industrial
    assert "REV08-25025/16/2022-MV-IGRS-Part(1)" in new_corp.citation

    new_muni = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_GF_2F",
        area_tier="NAGAR_PANCHAYAT_UDA",
        execution_date=date(2026, 3, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=0,
    )
    assert new_muni.base_rate_per_sqft == Decimal("1350.00")

    new_gp = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_GF_2F",
        area_tier="GRAM_PANCHAYAT",
        execution_date=date(2026, 3, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=0,
    )
    assert new_gp.base_rate_per_sqft == Decimal("900.00")


def test_industrial_depreciation_and_composite_apartment_rate(session):
    ind = calculate_building_structure(
        session,
        structure_category="NON_RCC_CINEMA_FACTORY",
        area_tier="CORP_MUNI",
        execution_date=date(2026, 3, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=4,
        is_industrial=True,
    )
    assert ind.depreciation_pct == Decimal("20")  # 5%/yr from year 1

    ind_capped = calculate_building_structure(
        session,
        structure_category="NON_RCC_CINEMA_FACTORY",
        area_tier="CORP_MUNI",
        execution_date=date(2026, 3, 1),
        floor_areas=[(0, Decimal("1000"))],
        age_years=25,
        is_industrial=True,
    )
    assert ind_capped.depreciation_pct == Decimal("50")  # Max 50% industrial

    # Composite apartment rate: (site_rate / 20) + base_rcc + floor_adder (from 3F onward)
    comp = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_APARTMENT",
        area_tier="CORP_MUNI",
        execution_date=date(2026, 3, 1),
        floor_areas=[(3, Decimal("1000"))],
        age_years=0,
        site_rate_per_sqyd=Decimal("20000"),
        is_apartment=True,
    )
    assert comp.composite_site_adder == Decimal("1000.00")
    assert comp.base_rate_per_sqft == Decimal("1580.00")
    # 1580 + 30 (3F adder) + 1000 (site/20) = 2610.00
    assert comp.floor_breakdowns[0].depreciated_unit_rate == Decimal("2610.00")
    assert comp.net_building_value == Decimal("2610000")


def test_multifloor_unsplit_area_states_assumption_and_requests_split(session):
    res = process_chat_message(
        session,
        "Calculate structure value for 1,800 sq.ft RCC ground + 3rd floor, 15 years old, age proof attached on 2026-03-01",
    )
    assert res.intent == "STRUCTURE_VALUE"
    assert res.structure_data is not None
    assert res.structure_data["needs_floor_split"] is True
    floors = res.structure_data["floors"]
    assert len(floors) == 2
    assert Decimal(floors[0]["plinth_area_sqft"]) == Decimal("900.00")
    assert Decimal(floors[0]["depreciated_unit_rate"]) == Decimal("1501.00")
    assert Decimal(floors[1]["depreciated_unit_rate"]) == Decimal("1529.50")
    assert "equal-split" in res.reply.lower() or "split" in res.reply.lower()
    assert "₹27,27,450.00" in res.reply


def test_gemini_plan_normalization_routes_structure_to_local_tool(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    bad_plan = ChatPlan(intent="LEGAL_QUERY", search_query="1,800 sq.ft RCC structure")
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: bad_plan)

    res = run_gemini_chat(session, "1,800 sq.ft RCC structure value 12 years old on 2026-03-01")
    assert res.intent == "STRUCTURE_VALUE"
    assert res.structure_data is not None


# ---------------------------------------------------------------------------
# 3. DAGPA (01_10) & 01_09 non-adjustable vs 01_02/01_11 adjustable + Partition
# ---------------------------------------------------------------------------

def test_adjustability_flags_and_partition_brothers_routing(session):
    b_dagpa = calculate_deed_duty(
        session, "01_10", "ALL", date(2026, 3, 1),
        market_value=Decimal("12000000"), consideration_value=Decimal("12000000"),
    )
    b_dev = calculate_deed_duty(
        session, "01_09", "ALL", date(2026, 3, 1),
        market_value=Decimal("10000000"), consideration_value=Decimal("10000000"),
    )
    b_poss = calculate_deed_duty(
        session, "01_02", "ALL", date(2026, 3, 1),
        market_value=Decimal("1000000"), consideration_value=Decimal("1000000"),
    )
    b_agpa = calculate_deed_duty(
        session, "01_11", "ALL", date(2026, 3, 1),
        market_value=Decimal("1000000"), consideration_value=Decimal("1000000"),
    )

    assert b_dagpa.is_adjustable is False
    assert b_dagpa.adjustable_sd_pct == Decimal("0")
    assert b_dev.is_adjustable is False
    assert b_dev.adjustable_sd_pct == Decimal("0")
    assert b_poss.is_adjustable is True
    assert b_poss.adjustable_sd_pct == Decimal("4")
    assert b_agpa.is_adjustable is True
    assert b_agpa.adjustable_sd_pct == Decimal("4")

    # CARD/e-1730463 Annexure I: 04_02 is Family Partition (1% VSS) and brothers never triggers 04_01 Others (2%)
    part = process_chat_message(session, "Split 30 lakhs equally among 3 brothers")
    assert part.intent == "CALCULATION_OK"
    assert part.card_slip is not None
    assert part.card_slip["instrument_code"] == "04_02"
    assert part.card_slip["deed_alias"] == "PARTITION_FAMILY"
    assert Decimal(str(part.card_slip["chargeable_value"])) == Decimal("2000000")
    assert Decimal(str(part.card_slip["total_payable"])) == Decimal("21500.00")


# ---------------------------------------------------------------------------
# 4. Per-component DB-driven G.O. citations (No G.O. 394/393 on non-Sale deeds)
# ---------------------------------------------------------------------------

def test_per_component_citations_no_sale_go_leakage(session):
    b_dagpa = calculate_deed_duty(
        session, "01_10", "ALL", date(2026, 3, 1),
        market_value=Decimal("12000000"), consideration_value=Decimal("12000000"),
    )
    assert "581" in b_dagpa.sd_authority
    assert "463" in b_dagpa.rf_authority
    assert "394" not in b_dagpa.sd_authority and "393" not in b_dagpa.rf_authority

    b_gp_gift = calculate_deed_duty(
        session, "03_05", "GRAM_PANCHAYAT", date(2026, 3, 1),
        market_value=Decimal("1000000"), consideration_value=Decimal("1000000"),
    )
    assert "395" in b_gp_gift.sd_authority
    assert "463" in b_gp_gift.td_authority and "PR&RD" in b_gp_gift.td_authority
    assert "463" in b_gp_gift.rf_authority
    assert "394" not in b_gp_gift.sd_authority and "393" not in b_gp_gift.rf_authority

    gift_chat = process_chat_message(
        session,
        "Gift deed of ₹10 lakh property in Gram Panchayat to son u/s 56(2) of IT Act",
    )
    assert gift_chat.intent == "CALCULATION_OK"
    assert "G.O.Ms.No. 395" in gift_chat.reply
    assert "G.O.Ms.No. 463" in gift_chat.reply
    assert "G.O.Ms.No. 394" not in gift_chat.reply
    assert "G.O.Ms.No. 393" not in gift_chat.reply

    b_sale = calculate_deed_duty(
        session, "01_01", "MUNICIPAL_CORP", date(2026, 3, 1),
        market_value=Decimal("1500000"), consideration_value=Decimal("1500000"),
    )
    assert "394" in b_sale.sd_authority
    assert "393" in b_sale.rf_authority


# ---------------------------------------------------------------------------
# 5. Indian currency grouping & Direct Answer summary above tables
# ---------------------------------------------------------------------------

def test_indian_currency_formatting_and_direct_answer_position(session):
    assert format_inr(Decimal("12000000")) == "₹1,20,00,000.00"
    assert format_inr(Decimal("140500")) == "₹1,40,500.00"
    assert format_inr(Decimal("-1500.5")) == "-₹1,500.50"

    prompts = [
        "Builder is registering a Development Agreement-cum-GPA for ₹1.2 crore estimated cost—what is the SD%, is it adjustable later, and what is the RF cap",
        "Calculate stamp duty for Sale Deed in Municipal Corporation for ₹1,20,00,000",
        "Split 30 lakhs equally among 3 brothers",
        "Calculate structure value for 1,800 sq.ft RCC ground + 3rd floor, 15 years old on 2026-03-01",
    ]
    for prompt in prompts:
        res = process_chat_message(session, prompt)
        assert "**Direct Answer:**" in res.reply
        assert res.reply.index("**Direct Answer:**") < res.reply.index("|")


def test_gemini_synthesis_empty_fallback_handles_legal_search_result_cleanly(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat
    from backend.app.engine.legal_search import LegalSearchResult

    plan = ChatPlan(intent="LEGAL_QUERY", search_query="Rule 61 Rule 71")
    source = LegalSearchResult(
        id=1,
        act_name="Andhra Pradesh Registration Rules, 1960",
        section_or_rule="Rule 61 & Rule 71",
        title="Enquiry procedures",
        content="Statutory rule text.",
        sro_guidance="Rule 61 governs non-testamentary executant death; Rule 71 governs Wills.",
        authority_citation="AP Registration Rules",
        snippet="Rule 61 vs Rule 71",
    )
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: plan)
    monkeypatch.setattr(agent, "search_legal_knowledge", lambda *args, **kwargs: [source])
    monkeypatch.setattr(agent, "_synthesize", lambda *args: (_ for _ in ()).throw(RuntimeError("empty")))

    res = run_gemini_chat(
        session,
        "Executant died after signing a sale deed, before registration vs a Will presented after the testator's death—what is the exact difference between Rule 61 and Rule 71 enquiry?",
    )
    assert res.intent == "LEGAL_QUERY"
    assert "Rule 61" in res.reply and "Rule 71" in res.reply


# ---------------------------------------------------------------------------
# 6. Art. 49 Family Settlement Exclusions, Shifted 03_* Codes & Sec 22-B RAG
# ---------------------------------------------------------------------------

def test_art49_stepmother_greatgrandson_classification_plus_rates_and_codes(session):
    # Verify Annexure I CARD code mapping (03_01=Gift, 03_02=Family Settlement 2%, 03_03=Others Settlement 3%)
    b_gift = calculate_deed_duty(
        session, "03_01", "ALL", date(2026, 3, 1),
        market_value=Decimal("1000000"), consideration_value=Decimal("1000000"),
    )
    b_fam = calculate_deed_duty(
        session, "03_02", "ALL", date(2026, 3, 1),
        market_value=Decimal("1000000"), consideration_value=Decimal("1000000"),
    )
    b_oth = calculate_deed_duty(
        session, "03_03", "ALL", date(2026, 3, 1),
        market_value=Decimal("1000000"), consideration_value=Decimal("1000000"),
    )
    assert b_gift.stamp_duty == Decimal("50000")
    assert b_fam.stamp_duty == Decimal("20000") and b_fam.transfer_duty == Decimal("0")
    assert b_oth.stamp_duty == Decimal("30000") and b_oth.transfer_duty == Decimal("0")
    assert "395" in b_fam.sd_authority and "463" in b_fam.rf_authority
    assert "395" in b_oth.sd_authority and "463" in b_oth.rf_authority
    assert "394" not in b_fam.sd_authority and "393" not in b_fam.rf_authority
    assert "394" not in b_oth.sd_authority and "393" not in b_oth.rf_authority

    q = (
        "Can a man execute a family settlement at 2% in favour of his step-mother "
        "or his great-grandson, or does it attract non-family rate?"
    )
    res = process_chat_message(session, q)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    assert "**Direct Answer / Verdict:** **No**" in res.reply
    assert "S2/24212/2000" in res.reply
    assert "S1/24755/71" in res.reply
    assert "03_03" in res.reply and "3.0000%" in res.reply
    assert "G.O.Ms.No. 395" in res.reply and "G.O.Ms.No. 463" in res.reply
    assert "G.O.Ms.No. 394" not in res.reply and "G.O.Ms.No. 393" not in res.reply
    assert "56(2)" in res.reply  # Pro SRO Gift vs Settlement lineal descendant nuance


def test_gemini_hybrid_classification_never_collapses_to_bare_rate_table(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    q = (
        "Can a man execute a family settlement at 2% in favour of his step-mother "
        "or his great-grandson, or does it attract non-family rate?"
    )
    # Even if Gemini returns RATE_LOOKUP, normalizer must force LEGAL_QUERY + include_rates=True
    raw_plan = ChatPlan(intent="RATE_LOOKUP", instrument_codes=["03_02"])
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: raw_plan)
    monkeypatch.setattr(agent, "_synthesize", lambda *args: "Direct Verdict: No, step-mother (S2/24212/2000) and great-grandson (S1/24755/71) are excluded and attract 3% (03_03).")

    res = run_gemini_chat(session, q)
    assert res.intent == "LEGAL_QUERY"
    assert res.legal_docs
    assert "No, step-mother" in res.reply
    assert "03_03" in res.reply and "3.0000%" in res.reply
    assert "G.O.Ms.No. 395" in res.reply
    assert "G.O.Ms.No. 394" not in res.reply and "G.O.Ms.No. 393" not in res.reply


def test_section_22b_webland_ptin_vlt_retrieves_card_e1730463_never_freedom_fighter(session):
    from backend.app.engine.hybrid_rag import search_stage7
    from backend.app.engine.legal_search import search_legal_knowledge

    q = (
        "Executant's name is not in Webland pattadar column, or Urban Local Body PTIN/VLT "
        "record doesn't match—what is the exact procedure under the Section 22-B circular?"
    )
    # Hyphenated and unhyphenated variants must both resolve CARD/e-1730463 cleanly
    for variant in (q, q.replace("Section 22-B", "Section 22B")):
        hits = search_legal_knowledge(variant, limit=5)
        assert hits, f"Expected CARD/e-1730463 results for {variant}"
        joined = " ".join(f"{h.section_or_rule} {h.title} {h.authority_citation} {h.content}" for h in hits)
        assert "1730463" in joined
        assert "Assn.1(1)/351/2022" not in joined
        assert "Freedom Fighter" not in hits[0].title

    s7 = search_stage7("Section 22-B Webland PTIN VLT", limit=5)
    assert s7 and "1730463" in (s7[0]["title"] + s7[0]["content"])
    assert "Freedom Fighter" not in s7[0]["title"]

    res = process_chat_message(session, q)
    assert res.intent == "LEGAL_QUERY"
    assert "CARD/e-1730463" in res.reply
    assert "Webland" in res.reply and "Annexure II" in res.reply and "Annexure III" in res.reply
    assert "LPM" in res.reply and "PTIN" in res.reply and "VLT" in res.reply
    assert "Link Document" in res.reply and "Annexure IV" in res.reply and "Gramakantam" in res.reply
    assert "Assn.1(1)/351/2022" not in res.reply
    assert "Freedom Fighter" not in res.reply


def test_rag_guardrail_abstains_when_requested_section_circular_absent(session):
    from backend.app.engine.legal_search import search_legal_knowledge

    assert search_legal_knowledge("Section 99-Z procedure for Webland PTIN") == []
    res = process_chat_message(session, "Section 99-Z circular procedure for Webland PTIN")
    assert res.intent == "LEGAL_NOT_FOUND"
    assert "Matching circular not found in index" in res.reply


# ---------------------------------------------------------------------------
# 7. Batch 1 Live QA Regression Fixes (Adjustability, Dedup, Synonyms, Shares, Lease)
# ---------------------------------------------------------------------------

def test_batch1_sale_deed_max_mv_consideration_and_14_pages(session):
    q = "sale deed vijyawada corp, sale price 42 lakhs but mv is 50 lakhs, 14 pages. total how much with user charges?"
    res = process_chat_message(session, q)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "01_01"
    assert res.card_slip["area_type"] == "MUNICIPAL_CORP"
    assert Decimal(str(res.card_slip["chargeable_value"])) == Decimal("5000000")
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("250000")
    assert Decimal(str(res.card_slip["transfer_duty"])) == Decimal("75000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("50000")
    assert Decimal(str(res.card_slip["user_charge"])) == Decimal("540")
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("375540")


def test_batch1_dad_daughter_village_panchayat_gift_single_row_and_unique_index(session):
    from sqlalchemy.exc import IntegrityError
    from backend.app.db.models import DutyRate

    q = "dad gifting house to daughter in a village panchayat, value 30L. how much sd td rf total?"
    res = process_chat_message(session, q)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "03_05"
    assert res.card_slip["area_type"] == "GRAM_PANCHAYAT"
    assert res.card_slip["calc_basis_used"] == "MARKET_VALUE"
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("60000")
    assert Decimal(str(res.card_slip["transfer_duty"])) == Decimal("15000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("10000")
    assert Decimal(str(res.card_slip["user_charge"])) == Decimal("500")
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("85500")

    rows_03_05 = session.query(DutyRate).filter(DutyRate.instrument_code == "03_05", DutyRate.effective_to.is_(None)).all()
    assert len(rows_03_05) == 1
    assert rows_03_05[0].area_type == "GRAM_PANCHAYAT"

    dup = DutyRate(
        instrument_code="03_05",
        article_ref="Art. 29",
        area_type="GRAM_PANCHAYAT",
        property_subtype="ALL",
        calc_basis="MARKET_VALUE",
        stamp_duty_type="PERCENT",
        stamp_duty_value=Decimal("2"),
        transfer_duty_pct=Decimal("0.5"),
        reg_fee_type="PERCENT",
        reg_fee_value=Decimal("0.5"),
        reg_fee_min=Decimal("1000"),
        reg_fee_max=Decimal("10000"),
        effective_from=date(2014, 11, 26),
        authority_citation="Duplicate test",
        verification_status="VERIFIED",
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_batch1_partition_unequal_shares_uses_04_02_family_vss(session):
    q = "3 brothers splitting 90L ancestral property into 40L, 30L and 20L shares. what is stamp duty and rf?"
    res = process_chat_message(session, q)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "04_02"
    assert Decimal(str(res.card_slip["chargeable_value"])) == Decimal("5000000")
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("50000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("1000")
    assert Decimal(str(res.card_slip["user_charge"])) == Decimal("500")
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("51500")

    # 04_01 must be Partition among Others (2% on VSS)
    b_others = calculate_deed_duty(
        session, "04_01", "ALL", date(2026, 3, 1),
        shares=[Decimal("4000000"), Decimal("3000000"), Decimal("2000000")],
    )
    assert b_others.status == "OK"
    assert b_others.stamp_duty == Decimal("100000")


def test_batch1_lease_calculator_with_advance_deposit_and_refundable_caveat(session):
    from backend.app.engine.duty_calculator import calculate_lease_duty

    q = "shop rent agreement 8 years, 25k per month rent and 5 lakh advance deposit. how much sd and rf?"
    res = process_chat_message(session, q)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "LEASE"
    assert Decimal(str(res.card_slip["chargeable_value"])) == Decimal("300000")
    assert Decimal(str(res.card_slip["rent_stamp_duty"])) == Decimal("6000")
    assert Decimal(str(res.card_slip["advance_stamp_duty"])) == Decimal("10000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("300")
    assert Decimal(str(res.card_slip["user_charge"])) == Decimal("100")
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("16400")
    assert "₹6,400.00" in res.reply
    assert "S1/4813/2008" in res.reply and "CCRA1/13429/2008" in res.reply

    refundable = calculate_lease_duty(
        session,
        monthly_rent=Decimal("25000"),
        duration_months=96,
        property_subtype="COMMERCIAL",
        advance_amount=Decimal("500000"),
        advance_is_refundable=True,
    )
    assert refundable.advance_stamp_duty == Decimal("0")
    assert refundable.total_payable == Decimal("6400")


def test_batch1_agpa_guntur_partial_adjustability_4pct_and_1pct(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    q = "agpa for 40 lakhs in guntur. how much stamp duty is adjustable later?"
    res = process_chat_message(session, q)
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["instrument_code"] == "01_11"
    assert res.card_slip["area_type"] == "MUNICIPAL_CORP"
    assert Decimal(str(res.card_slip["stamp_duty"])) == Decimal("200000")
    assert Decimal(str(res.card_slip["adjustable_sd_pct"])) == Decimal("4")
    assert Decimal(str(res.card_slip["adjustable_stamp_duty"])) == Decimal("160000")
    assert Decimal(str(res.card_slip["non_adjustable_stamp_duty"])) == Decimal("40000")
    assert Decimal(str(res.card_slip["registration_fee"])) == Decimal("2000")
    assert Decimal(str(res.card_slip["total_payable"])) == Decimal("202500")
    assert "₹1,60,000.00" in res.reply
    assert "₹40,000.00" in res.reply

    # Also verify Gemini path keeps adjustability exact without appending Applicable rates
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(
        agent,
        "_plan_with_gemini",
        lambda *args: ChatPlan(intent="RATE_LOOKUP", instrument_codes=["01_11"], include_rates=True),
    )
    monkeypatch.setattr(agent, "_synthesize", lambda *args: "AGPA carries partial adjustability under G.O.Ms.No. 581.")
    g_res = run_gemini_chat(session, q)
    assert g_res.intent == "CALCULATION_OK"
    assert g_res.card_slip is not None
    assert Decimal(str(g_res.card_slip["adjustable_stamp_duty"])) == Decimal("160000")
    assert "### Applicable rates" not in g_res.reply
    assert "Deterministic calculation (local result)" not in g_res.reply
    assert "₹1,60,000.00" in g_res.reply
    assert "₹40,000.00" in g_res.reply


# ==============================================================================
# Batch 2 / 3 / 4 / 6 Live QA Audit Regression Tests (#9–#40)
# ==============================================================================


def test_batch2_industrial_50pct_cap_and_major_gp_rates(session):
    # #9: Industrial non-RCC factory building -> MV6/10440/2008 Major GP rate 380 in 2010; post-2023 GP fallback 650; max 50% dep
    r_2010 = calculate_building_structure(
        session,
        structure_category="NON_RCC_CINEMA_FACTORY",
        area_tier="MAJOR_GRAM_PANCHAYAT",
        execution_date=date(2010, 5, 1),
        plinth_area_sqft=Decimal("1000"),
        age_years=20,
        is_industrial=True,
    )
    assert r_2010.status == "OK"
    assert r_2010.base_rate_per_sqft == Decimal("380.00")
    assert r_2010.depreciation_pct == Decimal("50")
    assert r_2010.net_building_value == Decimal("190000")

    r_now = process_chat_message(
        session,
        "20-year-old industrial factory building in a Major Gram Panchayat with 2,000 sq.ft plinth area, depreciation and structure value?",
    )
    assert r_now.intent == "STRUCTURE_VALUE"
    assert r_now.structure_data is not None
    assert Decimal(r_now.structure_data["depreciation_pct"]) == Decimal("50")
    assert "50%" in r_now.reply


def test_batch2_composite_site_adder_does_not_depreciate(session):
    # #10 / #11: Site adder (site_rate / 20) MUST NOT depreciate when building age > 10 years
    res = calculate_building_structure(
        session,
        structure_category="RCC_RESIDENTIAL_APARTMENT",
        area_tier="CORP_MUNI",
        execution_date=date(2026, 3, 1),
        plinth_area_sqft=Decimal("1000"),
        floors=[0],
        age_years=18,  # 8% depreciation on structure ONLY
        is_industrial=False,
        site_rate_per_sqyd=Decimal("20000"),  # site_adder = 1000/sq.ft
        is_apartment=True,
    )
    assert res.status == "OK"
    assert res.depreciation_pct == Decimal("8")
    fb = res.floor_breakdowns[0]
    # 1580 * 0.92 = 1453.60; + 1000 = 2453.60 (NOT (1580+1000)*0.92 = 2373.60)
    assert fb.depreciated_structure_unit_rate == Decimal("1453.60")
    assert fb.composite_site_adder == Decimal("1000.00")
    assert fb.depreciated_unit_rate == Decimal("2453.60")

    chat_res = process_chat_message(
        session,
        "18-year-old apartment flat in Vijayawada Municipal Corp 1,000 sq.ft ground floor with composite site rate ₹20,000 per sq.yd?",
    )
    assert chat_res.intent == "STRUCTURE_VALUE"
    assert "2,453.60" in chat_res.reply
    assert "1,453.60" in chat_res.reply
    assert "2,580.00" in chat_res.reply


def test_batch2_unfinished_building_stage_percentages(session):
    # #12: Foundation 25%, Slab 65%, Finishing 85%
    for stage_word, pct in (("foundation", "25%"), ("slab", "65%"), ("finishing", "85%")):
        res = process_chat_message(
            session,
            f"Unfinished RCC building at {stage_word} stage in Vijayawada Corp, 1,000 sq.ft ground floor structure value",
        )
        assert res.intent == "STRUCTURE_VALUE"
        assert pct in res.reply

    multi = process_chat_message(
        session,
        "What percentage of the structure rate applies at foundation, roof slab, and finishing stages of an unfinished building in Municipal Corp?",
    )
    assert multi.intent == "STRUCTURE_VALUE"
    assert "25%" in multi.reply and "65%" in multi.reply and "85%" in multi.reply


def test_batch2_1999_to_2010_historical_rates_and_gp_flat_vs_house(session, monkeypatch):
    # #14, #16, #17: 1999-2005 verified rates, 2008 GP flat vs non-flat, pre-1999 NOT_FOUND
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    b_1999 = calculate_deed_duty(session, "01_01", "MUNICIPAL_CORP", date(2000, 6, 1), Decimal("1000000"), Decimal("1000000"))
    assert b_1999.status == "OK"
    assert "8.0000%" in b_1999.stamp_duty_rate
    assert "5.0000%" in b_1999.transfer_duty_rate
    assert b_1999.registration_fee == Decimal("5250")  # 1% up to 50k (500) + 0.5% above (4750)

    b_2003 = calculate_deed_duty(session, "01_01", "SPECIAL_SELECTION_MUNICIPALITY", date(2003, 5, 1), Decimal("1000000"), Decimal("1000000"))
    assert b_2003.status == "OK"
    assert "7.0000%" in b_2003.stamp_duty_rate
    assert "5.0000%" in b_2003.transfer_duty_rate
    assert "0.5000%" in b_2003.reg_fee_rate  # G.O.Ms.No. 757 dt. 18.12.2001

    b_1995 = calculate_deed_duty(session, "01_01", "MUNICIPAL_CORP", date(1995, 5, 1), Decimal("1000000"), Decimal("1000000"))
    assert b_1995.status == "NOT_FOUND"

    flat_2008 = calculate_deed_duty(
        session, "01_01", "GRAM_PANCHAYAT", date(2008, 7, 1),
        Decimal("1000000"), Decimal("1000000"), property_subtype="FLAT_APARTMENT",
    )
    house_2008 = calculate_deed_duty(
        session, "01_01", "GRAM_PANCHAYAT", date(2008, 7, 1),
        Decimal("1000000"), Decimal("1000000"), property_subtype="NON_FLAT",
    )
    assert "5.0000%" in flat_2008.stamp_duty_rate
    assert "2.0000%" in flat_2008.transfer_duty_rate
    assert "Notification-III" in (flat_2008.td_authority or "")
    assert "6.0000%" in house_2008.stamp_duty_rate
    assert "3.0000%" in house_2008.transfer_duty_rate
    other_muni_2008 = calculate_deed_duty(
        session, "01_01", "OTHER_MUNICIPALITY", date(2008, 7, 1),
        Decimal("1000000"), Decimal("1000000"), property_subtype="NON_FLAT",
    )
    assert "6.0000%" in other_muni_2008.stamp_duty_rate
    assert "3.0000%" in other_muni_2008.transfer_duty_rate

    # Gemini normalizer must keep 2008 year from text even if planner put today's date
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(
        agent, "_plan_with_gemini",
        lambda *args: ChatPlan(intent="CALCULATION", execution_date=date.today().isoformat(), amount_rupees="1000000"),
    )
    monkeypatch.setattr(agent, "_synthesize", lambda *args: "")
    g_flat = run_gemini_chat(session, "Flat sale deed in a Gram Panchayat executed in July 2008 for ₹10,00,000")
    assert g_flat.intent == "CALCULATION_OK"
    assert g_flat.card_slip["execution_date"] == "2008-07-01"
    assert Decimal(g_flat.card_slip["stamp_duty"]) == Decimal("50000")


def test_batch3_03_06_single_row_and_month_date_and_audit_comparison(session, monkeypatch):
    # #13, #15, #19
    from sqlalchemy import func
    from backend.app.db.models import DutyRate
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    corp_03_06_cnt = session.query(func.count(DutyRate.id)).filter(
        DutyRate.instrument_code == "03_06",
        DutyRate.area_type.in_(["MUNICIPAL_CORP", "CORPORATION"]),
        DutyRate.effective_to.is_(None),
    ).scalar()
    assert corp_03_06_cnt == 1

    # #13: Gemini HISTORICAL_COMPARISON on 'May 2009 vs now' must not return clarification & must render Section 41-A table
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(
        agent, "_plan_with_gemini",
        lambda *args: ChatPlan(intent="CALCULATION", amount_rupees="1500000"),
    )
    monkeypatch.setattr(agent, "_synthesize", lambda *args: "Audit comparison prepared.")
    cmp_res = run_gemini_chat(session, "Compare May 2009 vs now for ₹15,00,000 sale deed in Vijayawada Municipal Corp")
    assert cmp_res.intent == "AUDIT_COMPARE"
    assert "2009-05-01" in cmp_res.reply
    assert "Section 41-A Historical Audit Comparison" in cmp_res.reply
    assert "Audit Delta (Net Variance)" in cmp_res.reply

    # #15: '15 June 2023' exact date parsing -> 1400/sq.ft vs 2026 -> 1580/sq.ft
    d_parsed = process_chat_message(
        session,
        "RCC ground floor structure value on 15 June 2023 in Vijayawada Corp 1,000 sq.ft",
    )
    assert d_parsed.structure_data["execution_date"] == "2023-06-15"
    assert Decimal(d_parsed.structure_data["base_rate_per_sqft"]) == Decimal("1400.00")


def test_rectification_classification_does_not_use_fee_as_property_value(session):
    from backend.app.engine.chatbot_orchestrator import parse_consideration_and_mv, parse_all_rupee_amounts

    query = (
        "party wants rectification deed to change survey number and 3 boundaries of old sale deed. "
        "just 1000 rs fee or fresh sale stamp duty?"
    )
    assert parse_all_rupee_amounts(query, exclude_fee_context=True) == []
    assert parse_consideration_and_mv(query) == (Decimal("0"), Decimal("0"))
    result = process_chat_message(session, query)
    assert result.intent not in ("CALCULATION_OK", "RATE_LOOKUP")
    assert result.card_slip is None


def test_small_agricultural_bit_boundary_rule_uses_side_count(session):
    query = "8 cents dry agri land in municipality, only 2 sides have agri land. acre rate or sq yd house site rate?"
    result = process_chat_message(session, query)
    assert result.intent == "LEGAL_QUERY"
    assert "House-Site (Sq.Yd.) Rate applies" in result.reply
    assert "fewer than 3" in result.reply
    assert "MV1/34372/92" in result.reply


def test_historical_agpa_adjustment_is_legal_not_comparison(session):
    query = (
        "party paid 1% sd on agpa back in 2008 under go 1481. "
        "now coming for final sale deed, can we adjust that 1%?"
    )
    result = process_chat_message(session, query)
    assert result.intent == "LEGAL_QUERY"
    assert "**No**" in result.reply
    assert "G.O.Ms.No. 1481" in result.reply
    assert "CCRA.3/6216/08A" in result.reply
    assert result.audit_comparison is None


def test_family_transfer_without_value_shows_clean_options(session):
    result = process_chat_message(session, "when i have to give this land to my mother then? how much")
    assert result.intent == "LEGAL_QUERY"
    assert "03_02" in result.reply and "Art. 49-A(a)" in result.reply
    assert "03_05" in result.reply and "03_06" in result.reply
    assert "0%" in result.reply and "0.5%" in result.reply and "1.5%" in result.reply
    assert "Source-limited" not in result.reply
    assert "PENDING_REVIEW" not in result.reply
    assert "authority=" not in result.reply


def test_day_precision_and_delay_fine_wording(session):
    from backend.app.engine.chatbot_orchestrator import parse_date_from_text
    from backend.app.engine.gemini_agent import ChatPlan, _normalize_plan_from_message, _supported_codes

    assert parse_date_from_text("10 march 2009") == date(2009, 3, 10)
    plan = _normalize_plan_from_message(
        ChatPlan(intent="RATE_LOOKUP"), "Sale deed rate on 10 march 2009", _supported_codes(session)
    )
    assert plan.rate_dates[0] == "2009-03-10"

    delay = process_chat_message(
        session, "sale deed executed 4 months 18 days ago, reg fee is 15000"
    )
    assert delay.intent == "DELAY_FINE"
    assert "18 days past 4-month window" in delay.reply
    assert "₹30,000.00" in delay.reply

    no_fee = process_chat_message(
        session, "deed was signed 9 months back, party begging to register with 10x fine. can DR condone?"
    )
    assert "Proper Registration Fee:** Not specified" in no_fee.reply
    assert re.search(r"\d+ days past 4-month window; 9 months from execution", no_fee.reply)
    assert "Appearance of executants (if presented within 8m)" in no_fee.reply
    assert "Beyond 8 months for presentation (or >12 months for appearance) — cannot be condoned" in no_fee.reply


def test_gemini_normalizer_protects_legal_routes_and_day_precision(session):
    from backend.app.engine.gemini_agent import ChatPlan, _normalize_plan_from_message, _supported_codes

    supported = _supported_codes(session)
    rectification = _normalize_plan_from_message(
        ChatPlan(intent="CALCULATION", amount_rupees="1000"),
        "party wants rectification deed, just 1000 rs fee or fresh sale stamp duty?", supported,
    )
    assert rectification.intent == "LOCAL_TOOL"
    assert rectification.instrument_codes == []

    agpa = _normalize_plan_from_message(
        ChatPlan(intent="HISTORICAL_COMPARISON"),
        "party paid 1% sd on agpa back in 2008 under go 1481. now coming for final sale deed, can we adjust that 1%?",
        supported,
    )
    assert agpa.intent == "LOCAL_TOOL"
    assert agpa.include_rates is False

    bits = _normalize_plan_from_message(
        ChatPlan(intent="GENERAL"),
        "8 cents dry agri land in municipality, only 2 sides have agri land. acre rate or sq yd house site rate?",
        supported,
    )
    assert bits.intent == "LOCAL_TOOL"


def test_gemini_returns_local_classification_answers_without_metadata_leaks(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: ChatPlan(intent="GENERAL"))
    monkeypatch.setattr(agent, "_synthesize", lambda *args: pytest.fail("targeted legal replies should stay deterministic"))

    family = run_gemini_chat(session, "when i have to give this land to my mother then? how much")
    assert family.intent == "LEGAL_QUERY"
    assert "Family Settlement" in family.reply and "03_06" in family.reply
    assert "PENDING_REVIEW" not in family.reply
    assert "Source-limited" not in family.reply

    agpa = run_gemini_chat(
        session,
        "party paid 1% sd on agpa back in 2008 under go 1481. now coming for final sale deed, can we adjust that 1%?",
    )
    assert agpa.intent == "LEGAL_QUERY"
    assert "G.O.Ms.No. 1481" in agpa.reply
    assert "CCRA.3/6216/08A" in agpa.reply


def test_batch6_delay_fines_aadhaar_41a_challan_and_zero_leaks(session, monkeypatch):
    # #20, #21, #22, #24, #33, #34, #36, #38, #40
    import backend.app.engine.gemini_agent as agent
    from backend.app.engine.gemini_agent import ChatPlan, run_gemini_chat

    # #33 & #34: Calendar add_months (4m 18d -> 2x, 9 months back -> time-barred u/s 25, S.O. 240 >12m)
    from backend.app.engine.fines_calc import calculate_delay_fine
    f_4m18d = calculate_delay_fine(date(2026, 1, 1), date(2026, 5, 19), Decimal("15000"))
    assert f_4m18d.status == "OK"
    assert f_4m18d.fine_multiplier == 2
    assert f_4m18d.fine_amount == Decimal("30000")
    assert "District Registrar" in f_4m18d.condoning_authority

    q_9m = process_chat_message(
        session,
        "deed was signed 9 months back, party begging to register with 10x fine. can DR condone?",
    )
    assert q_9m.intent == "DELAY_FINE"
    assert q_9m.fine_data["status"] == "VOID"
    assert "12 months" in q_9m.reply and "8 months" in q_9m.reply
    assert "240" in q_9m.reply

    # #21: Small bits <= 10 cents (MV1/34372/92)
    q_bits = process_chat_message(
        session,
        "6 cents urban dry agricultural land surrounded on 3 sides by agricultural lands: acre rate or house site rate?",
    )
    assert q_bits.intent == "LEGAL_QUERY"
    assert "MV1/34372/92" in q_bits.reply
    assert "acre rate" in q_bits.reply.lower()

    # #22: Settlement-cum-Conveyance (S3/7423/93)
    q_settle = process_chat_message(
        session,
        "Settling ₹10 lakh property on son subject to clearing ₹3 lakh debt. Pure settlement or settlement-cum-conveyance?",
    )
    assert q_settle.intent == "LEGAL_QUERY"
    assert "S3/7423/93" in q_settle.reply
    assert "₹7,00,000.00" in q_settle.reply and "₹3,00,000.00" in q_settle.reply

    # #24: Small farmer ₹3,00,000 ceiling (G.O.Ms.No. 15/2007)
    q_farm = process_chat_message(
        session,
        "Small farmer with 3.5 acres wet land taking ₹2.5 lakh bank crop loan — is stamp duty exempt?",
    )
    assert q_farm.intent == "LEGAL_QUERY"
    assert "3,00,000" in q_farm.reply and "100% exempt" in q_farm.reply.lower()

    # #38: Aadhaar non-scanning (G1/8686/2015 / IAR/8575/2023)
    q_aadhaar = process_chat_message(
        session,
        "Should we scan the photostat Aadhaar copies of executant and scribe with the sale deed?",
    )
    assert q_aadhaar.intent == "LEGAL_QUERY"
    assert "G1/8686/2015" in q_aadhaar.reply
    assert "MUST NOT be scanned" in q_aadhaar.reply

    # #36 & #40: Section 41-A 3x penalty incl TD & Challan refund u/r 185
    q_41a = process_chat_message(
        session,
        "Section 41-A audit penalty: does 3 times penalty apply to Transfer Duty also and what is the fraud limitation?",
    )
    assert q_41a.intent == "LEGAL_QUERY"
    assert "Transfer Duty" in q_41a.reply and "10 years" in q_41a.reply and "AR1/27842/02" in q_41a.reply

    q_chal = process_chat_message(
        session,
        "How much deduction applies on unused bank challan refund for stamp duty vs registration fee?",
    )
    assert q_chal.intent == "LEGAL_QUERY"
    assert "10%" in q_chal.reply and "20%" in q_chal.reply and "Rule 185" in q_chal.reply

    # #20: Gemini post-synthesis scrubber strips prompt leaks & fabricated rupee tables, never leaks No matching rows outside RATE_LOOKUP
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(
        agent, "_plan_with_gemini",
        lambda *args: ChatPlan(intent="LEGAL_QUERY", include_rates=True, instrument_codes=["99_99"], search_query="Aadhaar"),
    )
    monkeypatch.setattr(
        agent, "_synthesize",
        lambda *args: "Zero number duplication rule applies.\n### Fee & Duty Breakdown\n| Stamp Duty | ₹9,999 |\nNo scan.",
    )
    g_aad = run_gemini_chat(session, "Should we scan Aadhaar copy of scribe with registered sale deed?")
    assert "zero number duplication" not in g_aad.reply.lower()
    assert "No matching local rate rows" not in g_aad.reply
    assert "₹9,999" not in g_aad.reply

    # #20: AREA_CONVERT Direct Answer must not echo noisy prompt string verbatim
    area_res = process_chat_message(session, "please tell me how much 1,800 sq.ft is in sq.yds")
    assert "please tell me how much" not in area_res.reply
    assert "1,800 sq.ft" in area_res.reply.lower() or "1,800.00 sq.ft" in area_res.reply.lower()

    # #20: Lease grammar 'a 36-month' (not 'an 36-month')
    lease_res = process_chat_message(session, "residential lease for 36 months at ₹20,000 monthly rent")
    assert "a 36-month" in lease_res.reply
    assert "an 36-month" not in lease_res.reply
