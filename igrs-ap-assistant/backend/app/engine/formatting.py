"""Shared numeric parsing and Indian Rupee formatting helpers."""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Union


def strip_numeric_commas(text: str) -> str:
    """Remove thousands/lakhs commas embedded inside numbers (e.g. '1,800' -> '1800')."""
    return re.sub(r"(?<=\d),(?=\d)", "", text or "")


def format_indian_number(value: Union[Decimal, int, float, str], decimals: int = 2) -> str:
    """Format a number in Indian 2,2,3 digit grouping (e.g. 12000000 -> 1,20,00,000.00)."""
    dec = Decimal(str(value))
    sign = "-" if dec < 0 else ""
    dec = abs(dec)

    if decimals > 0:
        quant = Decimal("1." + "0" * decimals)
        dec = dec.quantize(quant, rounding=ROUND_HALF_UP)
        int_part, frac_part = f"{dec:.{decimals}f}".split(".")
    else:
        dec = dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        int_part, frac_part = str(dec), ""

    if len(int_part) <= 3:
        grouped = int_part
    else:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.append(rest)
        grouped = ",".join(reversed(groups)) + "," + last3

    return f"{sign}{grouped}.{frac_part}" if decimals > 0 else f"{sign}{grouped}"


def format_inr(value: Union[Decimal, int, float, str], decimals: int = 2) -> str:
    """Format a rupee amount in Indian numbering with the '₹' prefix."""
    dec = Decimal(str(value))
    sign = "-" if dec < 0 else ""
    return f"{sign}₹{format_indian_number(abs(dec), decimals=decimals)}"
