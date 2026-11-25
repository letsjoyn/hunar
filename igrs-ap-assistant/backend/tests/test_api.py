"""
FastAPI Integration Tests.
Verifies all REST API endpoints through Starlette TestClient.
"""
import sys
from decimal import Decimal
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from backend.app.main import app

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_frontend_mounted(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Instrument Sans" in res.text
    assert "Registration &amp; Stamps" in res.text
    assert "CARD 2.0 Engine" not in res.text
    assert "billDrawer" not in res.text



def test_api_calculate_sale_2025(client):
    payload = {
        "instrument_code": "SALE",
        "area_type": "MUNICIPAL_CORP",
        "execution_date": "2025-05-01",
        "consideration_value": "1500000",
        "market_value": "1500000"
    }
    res = client.post("/api/calculate", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "OK"
    assert data["stamp_duty"] == "75000"
    assert data["transfer_duty"] == "22500"
    assert data["registration_fee"] == "15000"
    assert data["user_charge"] == "500"
    assert data["total_payable"] == "113000"


def test_api_compare_historical(client):
    payload = {
        "instrument_code": "SALE",
        "area_type": "MUNICIPAL_CORP",
        "execution_date": "2009-05-01",
        "current_date": "2025-05-01",
        "consideration_value": "1500000",
        "market_value": "1500000"
    }
    res = client.post("/api/compare", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["historical"]["total_payable"] == "142595"
    assert data["current"]["total_payable"] == "113000"
    assert data["delta_total"] == "-29595"


def test_api_structure_value(client):
    payload = {
        "structure_category": "RCC_RESIDENTIAL_GF_2F",
        "area_tier": "CORP_MUNI",
        "plinth_area_sqft": "1000",
        "floor_number": 1,
        "age_years": 5,
        "is_industrial": False,
        "execution_date": "2025-05-01",
        "completion_stage": "COMPLETE"
    }
    res = client.post("/api/structure-value", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "OK"
    assert Decimal(data["base_rate_per_sqft"]) == Decimal("1400")
    assert Decimal(data["net_building_value"]) == Decimal("1400000")


def test_api_delay_fine(client):
    payload = {
        "execution_date": "2025-01-01",
        "presentation_date": "2025-05-11",
        "proper_registration_fee": "15000",
        "is_appearance": False
    }
    res = client.post("/api/delay-fine", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "OK"
    assert data["fine_multiplier"] == 2
    assert data["fine_amount"] == "30000"


def test_api_get_deeds(client):
    res = client.get("/api/deeds")
    assert res.status_code == 200
    deeds = res.json()
    assert len(deeds) == 111
    assert deeds[0]["code"] == "01_01"


def test_api_convert_area_cent(client):
    res = client.post("/api/convert-area", json={"value": "10", "from_unit": "Cent"})
    assert res.status_code == 200
    data = res.json()
    assert Decimal(str(data["sq_yards"])) == Decimal("484")
    assert round(float(data["acres"]), 4) == 0.1



def test_api_convert_area_composite(client):
    res = client.post("/api/convert-area", json={"composite_text": "2 Acres 50 Cents"})
    assert res.status_code == 200
    data = res.json()
    assert Decimal(data["sq_yards"]) == Decimal("12100")


def test_api_legal_search(client):
    res = client.get("/api/legal-search?q=Section 22-A")
    assert res.status_code == 200
    items = res.json()
    assert len(items) > 0
    assert any("22" in item["section_or_rule"] for item in items)


def test_api_legal_search_audit(client):
    res = client.get("/api/legal-search?q=Section 41-A")
    assert res.status_code == 200
    items = res.json()
    assert len(items) > 0
    assert any("41" in item["section_or_rule"] for item in items)


def test_api_chat_auto_uses_gemini_when_configured(client, monkeypatch):
    import backend.app.api.calculator as calculator_api

    monkeypatch.setattr(calculator_api, "is_gemini_available", lambda: True)
    monkeypatch.setattr(
        calculator_api,
        "run_gemini_chat",
        lambda session, message: {"reply": "Gemini pipeline", "intent": "GEMINI_TEST"},
    )

    response = client.post("/api/chat", json={"message": "Explain Article 49"})

    assert response.status_code == 200
    assert response.json()["reply"] == "Gemini pipeline"


def test_api_chat_calculation(client):
    payload = {"message": "Calculate duty for 15 Lakh sale deed in Vijayawada Corporation in 2025"}
    res = client.post("/api/chat", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "CALCULATION_OK"
    assert data["card_slip"] is not None
    assert data["card_slip"]["total_payable"] == "113000"


def test_api_chat_audit_comparison(client):
    payload = {"message": "Compare Section 41-A audit delta for 15 Lakhs sale deed executed in May 2009 in Corporation"}
    res = client.post("/api/chat", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "AUDIT_COMPARE"
    assert data["audit_comparison"] is not None


def test_api_chat_delay_fine(client):
    payload = {"message": "Section 25 fine; registration fee ₹15,000; executed 2025-01-01; presented 2025-05-11."}
    res = client.post("/api/chat", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "DELAY_FINE"
    assert data["fine_data"]["fine_multiplier"] == 2


def test_api_chat_refusal_unverified(client):
    payload = {"message": "Calculate duty for a sale deed executed in 1985"}
    res = client.post("/api/chat", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "CALCULATION_REFUSED"
    assert "NOT_FOUND" in data["reply"]


def test_api_chat_income_tax_section_56_answers_relative_gift_eligibility(client):
    response = client.post(
        "/api/chat",
        json={
            "message": (
                "Sister is gifting property to her brother's wife—is sister-in-law eligible "
                "for 2% relative gift duty under Section 56(2) of the Income Tax Act?"
            ),
            "use_gemini": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "LEGAL_QUERY"
    assert data["legal_docs"]
    assert "56(2)" in data["reply"]
    assert "2.00%" in data["reply"] or "2%" in data["reply"]
    assert data["card_slip"] is None  # Never assumes an arbitrary rupee amount


def test_api_chat_crop_loan_question_does_not_become_area_conversion(client):
    response = client.post(
        "/api/chat",
        json={
            "message": (
                "A small farmer with 4 acres wet land takes a ₹2.5 lakh crop loan from a bank; "
                "is he exempt from Stamp Duty and Registration Fee?"
            ),
            "use_gemini": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "LEGAL_QUERY"
    assert data["legal_docs"]
    assert "AREA_CONVERT" not in data["intent"]
    assert "land measurement conversion" not in data["reply"].lower()


def test_api_chat_all_deed_rates_returns_verified_schedule(client):
    response = client.post(
        "/api/chat",
        json={"message": "Rates of deeds for 2024", "use_gemini": False},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "DEED_SCHEDULE"
    assert "01_01" in data["reply"]
    assert "5.0000%" in data["reply"] or "5%" in data["reply"]


