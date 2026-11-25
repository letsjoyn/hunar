"""
Unit tests for AP Land Measurement Unit Converter.
Validates Sheet 2 statutory conversion rates from REVISIED DOCUMENT CLASSIFICATION AND TYPE.xlsx:
- 1 Ankanam = 8 Sq.Yards
- 1 Cent = 48.4 Sq.Yards
- 1 Gunta = 121 Sq.Yards
- 1 Ground = 266.2 Sq.Yards
- 1 Acre = 4,840 Sq.Yards = 100 Cents = 605 Ankanams = 40 Guntas
- 1 Hectare = 11,959.88 Sq.Yards (~2.471 Acres)
- 1 Sq.Meter = 1.19599 Sq.Yards
- 1 Sq.Foot = 1/9 Sq.Yard
"""
from decimal import Decimal
import pytest

from backend.app.engine.area_converter import (
    convert_area,
    to_sq_yards,
    from_sq_yards,
    parse_composite_land_string,
)


def test_ankanam_conversion():
    # 100 Ankanams = 800 Sq.Yards
    res = convert_area(Decimal("100"), "Ankanam")
    assert res.sq_yards == Decimal("800.0000")
    assert res.sq_feet == Decimal("7200.0000")


def test_cent_conversion():
    # 10 Cents = 484 Sq.Yards = 0.1 Acre
    res = convert_area(Decimal("10"), "Cent")
    assert res.sq_yards == Decimal("484.0000")
    assert res.acres == Decimal("0.1000")


def test_acre_conversion():
    # 1 Acre = 4,840 Sq.Yards = 100 Cents = 605 Ankanams = 40 Guntas
    res = convert_area(Decimal("1"), "Acre")
    assert res.sq_yards == Decimal("4840.0000")
    assert res.cents == Decimal("100.0000")
    assert res.ankanams == Decimal("605.0000")
    assert res.guntas == Decimal("40.0000")


def test_gunta_conversion():
    # 40 Guntas = 1 Acre = 4840 Sq.Yds
    res = convert_area(Decimal("40"), "Gunta")
    assert res.sq_yards == Decimal("4840.0000")
    assert res.acres == Decimal("1.0000")


def test_ground_conversion():
    # 1 Ground = 266.2 Sq.Yds
    res = convert_area(Decimal("1"), "Ground")
    assert res.sq_yards == Decimal("266.2000")


def test_sq_meters_conversion():
    # 100 Sq.Mtrs = 119.599 Sq.Yds
    res = convert_area(Decimal("100"), "Sq.Mtrs")
    assert res.sq_yards == Decimal("119.5990")


def test_composite_land_string_parsing():
    # "2 Acres 50 Cents" = 2 * 4840 + 50 * 48.4 = 9680 + 2420 = 12100 Sq.Yds
    sq_yds = parse_composite_land_string("2 Acres 50 Cents")
    assert sq_yds == Decimal("12100.0000")

    # "100 Ankanams 10 Sq.Yds" = 800 + 10 = 810 Sq.Yds
    sq_yds2 = parse_composite_land_string("100 Ankanams 10 Sq.Yds")
    assert sq_yds2 == Decimal("810.0000")


def test_invalid_unit_raises():
    with pytest.raises(ValueError):
        convert_area(Decimal("10"), "BIGHAS")
