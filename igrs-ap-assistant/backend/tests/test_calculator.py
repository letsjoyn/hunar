r"""
Comprehensive test suite for the IGRS AP Duty Calculator.

Tests the exact scenarios from the Master Spec Phase 1 acceptance criteria,
plus edge cases. ALL tests use the temporal SQL database — no hardcoded math.

Run: cd d:\gov\igrs-ap-assistant && python -m pytest backend/tests/ -v
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from backend.app.db.models import create_tables, get_engine, get_session_factory, DutyRate
from backend.app.db.seed import seed_all
from backend.app.engine.duty_calculator import calculate_deed_duty, compare_historical_vs_current
from backend.app.engine.structure_calc import calculate_building_value
from backend.app.engine.fines_calc import calculate_delay_fine
from backend.app.core.config import SEEDS_DIR


@pytest.fixture(scope="module")
def session():
    """Create an in-memory SQLite database, seed it, and return a session."""
    engine = get_engine("sqlite:///:memory:")
    create_tables(engine)
    Session = get_session_factory(engine)
    sess = Session()
    seed_all(sess, SEEDS_DIR)
    yield sess
    sess.close()


# ============================================================
# TEST 1: Sale Deed, Rs.15L, Municipal Corp, 2025-05-01
# Expected: SD 75,000 (5%) + TD 22,500 (1.5%) + RF 15,000 (1%) + UC 500 = 1,13,000
# ============================================================
class TestSaleDeed2025:
    def test_sale_deed_current(self, session):
        r = calculate_deed_duty(
            session, "SALE", "MUNICIPAL_CORP", date(2025, 5, 1),
            consideration_value=Decimal("1500000"),
            market_value=Decimal("1500000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("75000"), f"SD should be 75000, got {r.stamp_duty}"
        assert r.transfer_duty == Decimal("22500"), f"TD should be 22500, got {r.transfer_duty}"
        assert r.registration_fee == Decimal("15000"), f"RF should be 15000, got {r.registration_fee}"
        assert r.user_charge == Decimal("500"), f"UC should be 500, got {r.user_charge}"
        assert r.total_payable == Decimal("113000"), f"Total should be 113000, got {r.total_payable}"


# ============================================================
# TEST 2: Sale Deed, Rs.15L, Municipal Corp, 2009-05-01
# Expected: SD 1,05,000 (7%) + TD 30,000 (2%) + RF 7,500 (0.5%) + UC 95 = 1,42,595
# ============================================================
class TestSaleDeed2009:
    def test_sale_deed_historical(self, session):
        r = calculate_deed_duty(
            session, "SALE", "MUNICIPAL_CORP", date(2009, 5, 1),
            consideration_value=Decimal("1500000"),
            market_value=Decimal("1500000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("105000"), f"SD should be 105000, got {r.stamp_duty}"
        assert r.transfer_duty == Decimal("30000"), f"TD should be 30000, got {r.transfer_duty}"
        assert r.registration_fee == Decimal("7500"), f"RF should be 7500, got {r.registration_fee}"
        assert r.user_charge == Decimal("95"), f"UC should be 95, got {r.user_charge}"
        assert r.total_payable == Decimal("142595"), f"Total should be 142595, got {r.total_payable}"
        assert r.is_historical is True


# ============================================================
# TEST 3: Residential Flat, Rs.10L, Municipal Corp, 2009 vs 2016
# 2009: SD 5% + TD 2% + RF 0.5% = 75,000 (+ UC)
# 2016: SD 5% + TD 1.5% + RF 1% = 75,000 (+ UC)
# Same total duty+fee, different splits
# ============================================================
class TestFlatComparison:
    def test_flat_2009(self, session):
        r = calculate_deed_duty(
            session, "SALE", "MUNICIPAL_CORP", date(2009, 5, 1),
            consideration_value=Decimal("1000000"),
            market_value=Decimal("1000000"),
            property_subtype="FLAT_APARTMENT",
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("50000"), f"SD should be 50000, got {r.stamp_duty}"  # 5% flat rate
        assert r.transfer_duty == Decimal("20000"), f"TD should be 20000, got {r.transfer_duty}"  # 2%
        assert r.registration_fee == Decimal("5000"), f"RF should be 5000, got {r.registration_fee}"  # 0.5%

    def test_flat_2016(self, session):
        r = calculate_deed_duty(
            session, "SALE", "MUNICIPAL_CORP", date(2016, 5, 1),
            consideration_value=Decimal("1000000"),
            market_value=Decimal("1000000"),
            property_subtype="FLAT_APARTMENT",
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("50000"), f"SD should be 50000, got {r.stamp_duty}"  # 5%
        assert r.transfer_duty == Decimal("15000"), f"TD should be 15000, got {r.transfer_duty}"  # 1.5%
        assert r.registration_fee == Decimal("10000"), f"RF should be 10000, got {r.registration_fee}"  # 1%


# ============================================================
# TEST 4: Gift (Relative), Rs.20L, Municipal Corp, 2025
# Expected: SD 40,000 (2%) + TD 30,000 (1.5%) + RF 10,000 (capped) + UC 500 = 80,500
# ============================================================
class TestGiftRelativeMunicipal:
    def test_gift_relative_municipal(self, session):
        r = calculate_deed_duty(
            session, "GIFT_RELATIVE", "MUNICIPAL_CORP", date(2025, 5, 1),
            market_value=Decimal("2000000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("40000"), f"SD should be 40000, got {r.stamp_duty}"
        assert r.transfer_duty == Decimal("30000"), f"TD should be 30000, got {r.transfer_duty}"
        assert r.registration_fee == Decimal("10000"), f"RF should be 10000 (capped), got {r.registration_fee}"
        assert r.user_charge == Decimal("500")
        assert r.total_payable == Decimal("80500")


# ============================================================
# TEST 5: Gift (Relative), Rs.20L, Gram Panchayat, 2025
# Expected: SD 40,000 (2%) + TD 10,000 (0.5%) + RF 10,000 (capped) + UC 500 = 60,500
# ============================================================
class TestGiftRelativePanchayat:
    def test_gift_relative_panchayat(self, session):
        r = calculate_deed_duty(
            session, "GIFT_RELATIVE", "GRAM_PANCHAYAT", date(2025, 5, 1),
            market_value=Decimal("2000000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("40000"), f"SD should be 40000, got {r.stamp_duty}"
        assert r.transfer_duty == Decimal("10000"), f"TD should be 10000 (0.5%), got {r.transfer_duty}"
        assert r.registration_fee == Decimal("10000"), f"RF should be 10000 (capped), got {r.registration_fee}"
        assert r.total_payable == Decimal("60500")


# ============================================================
# TEST 6: Partition (Family), 2 shares [30L, 20L], 2025
# VSS = 50L - 30L = 20L → SD 20,000 (1%) + RF 1,000 + UC 500 = 21,500
# ============================================================
class TestPartitionFamily:
    def test_partition_vss(self, session):
        r = calculate_deed_duty(
            session, "PARTITION_FAMILY", "ALL", date(2025, 5, 1),
            shares=[Decimal("3000000"), Decimal("2000000")],
        )
        assert r.status == "OK"
        assert r.chargeable_value == Decimal("2000000"), f"VSS should be 20L, got {r.chargeable_value}"
        assert r.stamp_duty == Decimal("20000"), f"SD should be 20000, got {r.stamp_duty}"
        assert r.transfer_duty == Decimal("0"), f"TD should be 0, got {r.transfer_duty}"
        assert r.registration_fee == Decimal("1000"), f"RF should be 1000, got {r.registration_fee}"
        assert r.user_charge == Decimal("500")
        assert r.total_payable == Decimal("21500"), f"Total should be 21500, got {r.total_payable}"

    def test_partition_three_shares(self, session):
        """3 shares: [40L, 30L, 30L] → VSS = 100L - 40L = 60L"""
        r = calculate_deed_duty(
            session, "PARTITION_FAMILY", "ALL", date(2025, 5, 1),
            shares=[Decimal("4000000"), Decimal("3000000"), Decimal("3000000")],
        )
        assert r.status == "OK"
        assert r.chargeable_value == Decimal("6000000")
        assert r.stamp_duty == Decimal("60000")


# ============================================================
# TEST 7: Deposit of Title Deeds, Rs.1 Cr loan, 2025
# Expected: SD 50,000 (0.5% capped) + RF 10,000 (0.1% capped) + UC 100 = 60,100
# ============================================================
class TestDOTD:
    def test_dotd_caps(self, session):
        r = calculate_deed_duty(
            session, "DOTD", "ALL", date(2025, 5, 1),
            loan_amount=Decimal("10000000"),  # 1 Crore
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("50000"), f"SD should be 50000 (capped), got {r.stamp_duty}"
        assert r.registration_fee == Decimal("10000"), f"RF should be 10000 (capped), got {r.registration_fee}"
        assert r.user_charge == Decimal("100"), f"UC should be 100, got {r.user_charge}"
        assert r.total_payable == Decimal("60100"), f"Total should be 60100, got {r.total_payable}"


# ============================================================
# TEST 8: Lease (Residential), AAR Rs.1,20,000, 7 years, 2025
# Expected: SD 1,200 (1% AAR) + RF 120 (0.1% AAR) + UC 100
# ============================================================
class TestLeaseResidential:
    def test_lease_7yr_residential(self, session):
        r = calculate_deed_duty(
            session, "LEASE", "ALL", date(2025, 5, 1),
            property_subtype="RESIDENTIAL",
            lease_months=84,  # 7 years
            annual_rent=Decimal("120000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("1200"), f"SD should be 1200, got {r.stamp_duty}"
        assert r.registration_fee == Decimal("120"), f"RF should be 120, got {r.registration_fee}"
        assert r.user_charge == Decimal("100"), f"UC should be 100, got {r.user_charge}"


# ============================================================
# TEST 9: Delayed presentation — 10 days past 4 months
# Fine = 2x registration fee
# ============================================================
class TestDelayedPresentation:
    def test_10_days_late(self):
        r = calculate_delay_fine(
            execution_date=date(2025, 1, 1),
            presentation_date=date(2025, 5, 11),  # ~4 months + 10 days
            proper_registration_fee=Decimal("15000"),
        )
        assert r.status == "OK"
        assert r.fine_multiplier == 2
        assert r.fine_amount == Decimal("30000")

    def test_presentation_at_9_months(self):
        """9 months = beyond 8-month limit → VOID"""
        r = calculate_delay_fine(
            execution_date=date(2025, 1, 1),
            presentation_date=date(2025, 10, 1),
            proper_registration_fee=Decimal("15000"),
        )
        assert r.status == "VOID"


# ============================================================
# TEST 10: Unverified date (1985 — pre-database) → Must return NOT_FOUND
# ============================================================
class TestNotFound:
    def test_unverified_year(self, session):
        r = calculate_deed_duty(
            session, "SALE", "MUNICIPAL_CORP", date(1985, 1, 1),
            consideration_value=Decimal("1500000"),
        )
        assert r.status == "NOT_FOUND"
        assert "No verified rate record" in r.message


# ============================================================
# No verified pre-2005 rate source has been reviewed for this prototype.
# Unknown historical dates must refuse rather than inherit a guessed era.
# ============================================================
class TestSaleDeed1995Unverified:
    def test_sale_deed_1995_returns_not_found(self, session):
        r = calculate_deed_duty(
            session, "SALE", "MUNICIPAL_CORP", date(1995, 5, 1),
            consideration_value=Decimal("1000000"),
            market_value=Decimal("1000000"),
        )
        assert r.status == "NOT_FOUND"
        assert "No verified rate record" in r.message


# ============================================================
# TEST 11: AGPA, Rs.25L, 2025
# SD 5% (4% adjustable) + RF Rs.2000 fixed + UC 500
# ============================================================
class TestAGPA:
    def test_agpa(self, session):
        r = calculate_deed_duty(
            session, "AGPA", "ALL", date(2025, 5, 1),
            consideration_value=Decimal("2500000"),
            market_value=Decimal("2500000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("125000"), f"SD should be 125000 (5%), got {r.stamp_duty}"
        assert r.registration_fee == Decimal("2000"), f"RF should be 2000 (fixed), got {r.registration_fee}"
        assert r.user_charge == Decimal("500")
        assert r.total_payable == Decimal("127500")


# ============================================================
# TEST 12: Sale Agreement with Possession (adjustable)
# ============================================================
class TestSaleAgreementWithPossession:
    def test_sale_agreement_possession(self, session):
        r = calculate_deed_duty(
            session, "SALE_AGREEMENT_WITH_POSSESSION", "ALL", date(2025, 5, 1),
            consideration_value=Decimal("2000000"),
            market_value=Decimal("2000000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("80000"), f"SD should be 80000 (4%), got {r.stamp_duty}"
        assert r.is_adjustable is True
        assert r.registration_fee >= Decimal("1000")  # min cap
        assert r.registration_fee <= Decimal("20000")  # max cap


# ============================================================
# TEST 13: Structure value — 2023 rates
# ============================================================
class TestStructureValue:
    def test_rcc_residential_2023(self, session):
        r = calculate_building_value(
            session,
            structure_category="RCC_RESIDENTIAL_GF_2F",
            area_tier="CORP_MUNI",
            plinth_area_sqft=Decimal("1000"),
            floor_number=1,
            age_years=5,
            is_industrial=False,
            execution_date=date(2025, 5, 1),
        )
        assert r.status == "OK"
        assert r.base_rate_per_sqft == Decimal("1400")
        assert r.gross_value == Decimal("1400000")
        assert r.depreciation_pct == Decimal("0")  # < 10 years
        assert r.net_building_value == Decimal("1400000")

    def test_depreciation_non_industrial(self, session):
        """15-year-old building: 5% depreciation (1% × 5 years over 10)"""
        r = calculate_building_value(
            session,
            structure_category="RCC_RESIDENTIAL_GF_2F",
            area_tier="CORP_MUNI",
            plinth_area_sqft=Decimal("1000"),
            floor_number=0,
            age_years=15,
            is_industrial=False,
            execution_date=date(2025, 5, 1),
        )
        assert r.status == "OK"
        assert r.depreciation_pct == Decimal("5")
        assert r.net_building_value == Decimal("1330000")  # 1400000 * 0.95

    def test_unfinished_slab(self, session):
        """Slab level = 65% of full value"""
        r = calculate_building_value(
            session,
            structure_category="RCC_RESIDENTIAL_GF_2F",
            area_tier="CORP_MUNI",
            plinth_area_sqft=Decimal("1000"),
            floor_number=0,
            age_years=0,
            is_industrial=False,
            execution_date=date(2025, 5, 1),
            completion_stage="SLAB",
        )
        assert r.status == "OK"
        assert r.after_completion == Decimal("910000")  # 1400000 * 0.65

    def test_2008_vs_2023_rates(self, session):
        """Same building, 2009 vs 2025 — rate should jump from 550 to 1400"""
        r_old = calculate_building_value(
            session,
            structure_category="RCC_RESIDENTIAL_GF_2F",
            area_tier="CORP_MUNI",
            plinth_area_sqft=Decimal("1000"),
            floor_number=1, age_years=0, is_industrial=False,
            execution_date=date(2009, 5, 1),
        )
        r_new = calculate_building_value(
            session,
            structure_category="RCC_RESIDENTIAL_GF_2F",
            area_tier="CORP_MUNI",
            plinth_area_sqft=Decimal("1000"),
            floor_number=1, age_years=0, is_industrial=False,
            execution_date=date(2025, 5, 1),
        )
        assert r_old.status == "OK"
        assert r_new.status == "OK"
        assert r_old.base_rate_per_sqft == Decimal("550")
        assert r_new.base_rate_per_sqft == Decimal("1400")


# ============================================================
# TEST 14: Historical audit comparison
# ============================================================
class TestHistoricalComparison:
    def test_audit_delta(self, session):
        result = compare_historical_vs_current(
            session,
            "SALE", "MUNICIPAL_CORP",
            execution_date=date(2009, 5, 1),
            current_date=date(2025, 5, 1),
            consideration_value=Decimal("1500000"),
            market_value=Decimal("1500000"),
        )
        h = result["historical"]
        c = result["current"]
        assert h.status == "OK"
        assert c.status == "OK"
        # Historical total should be higher (9.5% vs 7.5%)
        assert h.total_payable > c.total_payable


# ============================================================
# TEST 15: Settlement family vs other
# ============================================================
class TestSettlement:
    def test_settlement_family_2025(self, session):
        r = calculate_deed_duty(
            session, "SETTLEMENT_FAMILY", "ALL", date(2025, 5, 1),
            market_value=Decimal("1000000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("20000")  # 2%
        assert r.transfer_duty == Decimal("0")

    def test_settlement_other_2025(self, session):
        r = calculate_deed_duty(
            session, "SETTLEMENT_OTHER", "ALL", date(2025, 5, 1),
            market_value=Decimal("1000000"),
        )
        assert r.status == "OK"
        assert r.stamp_duty == Decimal("30000")  # 3%


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
