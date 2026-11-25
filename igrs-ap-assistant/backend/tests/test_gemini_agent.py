"""
Unit Tests for Phase 8: Gemini API Agent Integration.
"""
from decimal import Decimal
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import create_tables
from backend.app.db.seed import seed_all
from backend.app.db.seed_prohibited import seed_prohibited_properties
from backend.app.core.config import SEEDS_DIR
from backend.app.engine.gemini_agent import (
    ChatPlan,
    is_gemini_available,
    run_gemini_chat,
)
from backend.app.engine.legal_search import LegalSearchResult


@pytest.fixture
def session():
    test_engine = create_engine("sqlite:///:memory:")
    create_tables(test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    sess = TestingSession()
    seed_all(sess, SEEDS_DIR)
    seed_prohibited_properties(sess)
    yield sess
    sess.close()


def test_gemini_available_check():
    # In test environment without GEMINI_API_KEY, it should return False
    # and handle fallback gracefully
    assert isinstance(is_gemini_available(), bool)


def test_gemini_chat_fallback(session, monkeypatch):
    # Ensure offline test execution by isolating the client
    monkeypatch.setattr("backend.app.engine.gemini_agent.get_gemini_client", lambda: None)
    res = run_gemini_chat(session, "Calculate duty for 15 Lakh sale deed in Vijayawada Corporation in 2025")
    assert res.intent == "CALCULATION_OK"
    assert res.card_slip is not None
    assert res.card_slip["total_payable"] == "113000"
    assert res.card_slip["integrity_verified"] is True
    assert res.card_slip["is_verified"] is False
    assert res.card_slip["reference_no"].startswith("AP-CARD-")


def test_gemini_legal_plan_uses_local_sources_and_rate_rows(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent

    query = "Can a family settlement be 2% for a step-mother or great-grandson under Art. 49?"
    plan = ChatPlan(
        intent="LEGAL_QUERY",
        search_query="Art. 49 family definition settlement step-mother great-grandson",
        instrument_codes=["03_02", "03_03"],
        rate_dates=["2025-01-01"],
        include_rates=True,
    )
    source = LegalSearchResult(
        id=1,
        act_name="Indian Stamp Act, AP Schedule I-A",
        section_or_rule="Art. 49",
        title="Family settlement definition",
        content="Local source excerpt for Article 49.",
        sro_guidance="Check the source text.",
        authority_citation="Local test citation",
        snippet="Article 49 source",
    )
    calls = []
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: plan)
    monkeypatch.setattr(agent, "search_legal_knowledge", lambda q, limit, **kwargs: calls.append(q) or [source])
    monkeypatch.setattr(agent, "_synthesize", lambda *args: "Source-grounded answer.")

    result = run_gemini_chat(session, query)

    assert result.intent == "LEGAL_QUERY"
    assert "Source-grounded answer." in result.reply
    assert "03_02" in result.reply and "2.0000%" in result.reply
    assert "03_03" in result.reply and "3.0000%" in result.reply
    assert result.legal_docs is not None
    assert result.legal_docs[0]["section_or_rule"] == "Art. 49"
    assert any("Art. 49" in item for item in calls)


def test_rate_only_historical_query_infers_sale_and_returns_percent_changes(session):
    from datetime import date
    import backend.app.engine.gemini_agent as agent

    query = (
        "Sale deed for a ₹15 lakh house in Visakhapatnam Municipal Corporation "
        "executed on 10-03-2009: what were SD, TD, RF rates and the difference vs today?"
    )
    supported = agent._supported_codes(session)
    plan = agent.ChatPlan(intent="HISTORICAL_COMPARISON")
    normalized = agent._normalize_plan_from_message(plan, query, supported)

    assert normalized.intent == "RATE_LOOKUP"
    assert normalized.instrument_codes == ["01_01"]
    assert normalized.jurisdiction == "MUNICIPAL_CORP"
    assert normalized.property_subtype == "NON_FLAT"
    assert normalized.rate_dates[0] == "2009-03-10"
    assert normalized.rate_dates[-1] == date.today().isoformat()

    rows, reply = agent._make_local_rate_evidence(session, normalized, query, supported)
    old = next(row for row in rows if row["lookup_date"] == "2009-03-10")
    current = next(row for row in rows if row["lookup_date"] == date.today().isoformat())
    assert old["stamp_duty_value"] == "7.0000"
    assert old["transfer_duty_pct"] == "2.0000"
    assert old["reg_fee_value"] == "0.5000"
    assert current["stamp_duty_value"] == "5.0000"
    assert "SD -2.0000% points" in reply
    assert "VERIFIED" in reply


def test_gemini_rate_query_returns_local_rates_without_calculation_clarification(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent

    query = "Sale deed rates in Visakhapatnam Corporation on 10-03-2009 vs today: SD, TD, RF?"
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: ChatPlan(intent="HISTORICAL_COMPARISON"))
    monkeypatch.setattr(agent, "_synthesize", lambda *args: pytest.fail("rate-only replies should be deterministic"))

    result = run_gemini_chat(session, query)

    assert result.intent == "RATE_LOOKUP"
    assert "2009-03-10" in result.reply
    assert "Applicable rates" in result.reply
    assert "Rate change" in result.reply
    assert "Which deed" not in result.reply
    assert "How much" not in result.reply


def test_rate_only_commercial_eight_year_lease_uses_duration_row(session):
    import backend.app.engine.gemini_agent as agent

    query = "Commercial shop lease for 8 years, monthly rent ₹25,000. How much SD and RF?"
    supported = agent._supported_codes(session)
    plan = agent._normalize_plan_from_message(
        agent.ChatPlan(intent="CALCULATION"), query, supported
    )

    assert plan.intent == "RATE_LOOKUP"
    assert plan.instrument_codes == ["LEASE"]
    assert plan.property_subtype == "COMMERCIAL"
    assert plan.lease_months == 96

    rows, reply = agent._make_local_rate_evidence(session, plan, query, supported)
    assert len(rows) == 1
    assert rows[0]["stamp_duty_value"] == "2.0000"
    assert rows[0]["reg_fee_value"] == "0.1000"
    assert "2.0000%" in reply and "0.1000%" in reply
    assert "No matching" not in reply


def test_exemption_eligibility_is_not_reclassified_as_rate_only(session):
    import backend.app.engine.gemini_agent as agent

    query = "Is a small farmer exempt from stamp duty and registration fee on a crop loan?"
    normalized = agent._normalize_plan_from_message(
        ChatPlan(intent="LEGAL_QUERY"), query, agent._supported_codes(session)
    )

    assert normalized.intent == "LEGAL_QUERY"
    assert not agent._wants_rates_only(query)


def test_gemini_calculation_keeps_local_numbers(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent

    plan = ChatPlan(
        intent="CALCULATION",
        instrument_codes=["01_01"],
        jurisdiction="MUNICIPAL_CORP",
        execution_date="2025-05-01",
        amount_rupees="1500000",
    )
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: plan)
    monkeypatch.setattr(agent, "_synthesize", lambda *args: "Clean explanation.")

    result = run_gemini_chat(session, "Calculate 15 lakh sale deed for 2025 in a corporation")

    assert result.intent == "CALCULATION_OK"
    assert result.card_slip is not None
    assert "Clean explanation." in result.reply
    assert "Fee & Duty Breakdown" in result.reply
    assert "Deterministic calculation (local result)" not in result.reply
    assert "| **Stamp Duty (SD)** | 5.0000% | ₹75,000.00 |" in result.reply
    assert "| **Total Payable** | — | **₹1,13,000.00** |" in result.reply


def test_gemini_calculation_blocks_rupee_hallucinations_and_duplicate_rates(session, monkeypatch):
    import backend.app.engine.gemini_agent as agent

    plan = ChatPlan(
        intent="CALCULATION",
        instrument_codes=["01_01"],
        jurisdiction="MUNICIPAL_CORP",
        amount_rupees="4200000",
        include_rates=True,
    )
    monkeypatch.setattr(agent, "get_gemini_client", lambda: object())
    monkeypatch.setattr(agent, "_plan_with_gemini", lambda *args: plan)
    monkeypatch.setattr(
        agent,
        "_synthesize",
        lambda *args: "Registration Fee (1.0%): ₹50,00,000.00 and Total ₹53,25,540.00",
    )

    msg = "sale deed vijyawada corp, sale price 42 lakhs but mv is 50 lakhs, 14 pages. total how much with user charges?"
    result = run_gemini_chat(session, msg)

    assert result.intent == "CALCULATION_OK"
    assert result.card_slip is not None
    assert Decimal(str(result.card_slip["chargeable_value"])) == Decimal("5000000")
    assert Decimal(str(result.card_slip["registration_fee"])) == Decimal("50000")
    assert Decimal(str(result.card_slip["user_charge"])) == Decimal("540")
    assert Decimal(str(result.card_slip["total_payable"])) == Decimal("375540")
    assert "50,00,000.00 | " not in result.reply
    assert "Registration Fee (1.0%): ₹50,00,000.00" not in result.reply
    assert "₹50,000.00" in result.reply
    assert "₹540.00" in result.reply
    assert "₹3,75,540.00" in result.reply
    assert "### Applicable rates" not in result.reply
    assert "Deterministic calculation (local result)" not in result.reply
