"""
Generate draft duty-rate seed rows from the local deed mapping.
Generated rows are PENDING_REVIEW and are never evidence of legal verification.
"""
import csv
import json
import re
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SEEDS_DIR = DATA_DIR / "verified_seeds"
SEEDS_DIR.mkdir(parents=True, exist_ok=True)


def clean_num(val):
    if val is None:
        return ""
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("none", "---", "-"):
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)", val_str)
    return m.group(1) if m else ""


def parse_sd(sd_raw):
    """Parse SD column."""
    if not sd_raw:
        return "PERCENT", "0", "", ""
    s = str(sd_raw).strip()
    if s.startswith("RS.") or s.startswith("Rs."):
        val = clean_num(s)
        return "FIXED", val or "0", "", ""
    if "MAX" in s.upper():
        m_pct = re.search(r"^(\d+(?:\.\d+)?)", s)
        pct = m_pct.group(1) if m_pct else "0"
        m_max = re.search(r"(?:MAX=|MAXIMUM\s*(?:Rs\.)?)\s*(\d+(?:,\d+)*(?:\.\d+)?)", s, re.IGNORECASE)
        max_cap = m_max.group(1).replace(",", "") if m_max else ""
        return "PERCENT", pct, "", max_cap
    num = clean_num(s)
    return "PERCENT", num or "0", "", ""


def parse_rf(rf_raw, rf_min_raw, rf_max_raw):
    """Parse Registration Fee."""
    rf_s = str(rf_raw).strip() if rf_raw else ""
    rf_min = clean_num(rf_min_raw)
    rf_max = clean_num(rf_max_raw)

    if "FIXED" in rf_s.upper():
        val = clean_num(rf_s)
        return "FIXED", val or "2000", rf_min, rf_max

    if rf_s and rf_s not in ("---", "None"):
        num = clean_num(rf_s)
        if num:
            return "PERCENT", num, rf_min, rf_max

    if rf_min and rf_max and rf_min == rf_max and Decimal(rf_min) > 0:
        return "FIXED", rf_min, rf_min, rf_max

    return "PERCENT", "0", rf_min, rf_max


def determine_calc_basis(cls: str, dtype: str, code: str) -> str:
    cls_u = cls.upper()
    dt_u = dtype.upper()

    if "MORTGAGE" in cls_u:
        return "LOAN_AMOUNT"
    if "PARTITION" in cls_u or code in ("04_01", "04_02"):
        return "VSS"
    if "DEVELOPMENT" in dt_u or "CONSTRUCTION" in dt_u:
        return "MARKET_VALUE"
    if "GIFT" in cls_u or "SETTLEMENT" in cls_u or "SALE" in cls_u or "EXCHANGE" in cls_u:
        return "MAX_MV_CONSID"
    if "RELEASE" in cls_u:
        return "MARKET_VALUE"
    if "LEASE" in cls_u:
        return "RENT_VALUE"
    return "MARKET_VALUE"


def main():
    with open(DATA_DIR / "deeds_111.json", "r", encoding="utf-8") as f:
        deeds = json.load(f)

    print(f"Loaded {len(deeds)} deeds from deeds_111.json")

    rows = []
    fieldnames = [
        "instrument_code", "article_ref", "area_type", "property_subtype",
        "duration_min_months", "duration_max_months", "calc_basis",
        "stamp_duty_type", "stamp_duty_value", "stamp_duty_min", "stamp_duty_max",
        "transfer_duty_pct",
        "reg_fee_type", "reg_fee_value", "reg_fee_min", "reg_fee_max",
        "is_adjustable", "effective_from", "effective_to",
        "authority_citation", "verification_status", "notes"
    ]

    aliases = {
        "01_01": ["SALE", "SALE_DEED"],
        "01_02": ["SALE_AGREEMENT_WITH_POSSESSION", "SALE_AGREEMENT_POSSESSION"],
        "01_03": ["SALE_AGREEMENT_WITHOUT_POSSESSION"],
        "01_04": ["SALE_HOUSING_BOARD"],
        "01_05": ["SALE_GOVT"],
        "01_10": ["DEVELOPMENT_AGREEMENT_CUM_GPA", "DAGPA"],
        "01_09": ["DEVELOPMENT_AGREEMENT"],
        "01_11": ["AGPA", "AGREEMENT_OF_SALE_CUM_GPA"],
        "01_12": ["CONVEYANCE"],
        "01_14": ["SALE_MORTGAGEE"],
        "01_15": ["SALE_INDEMNITY"],
        "01_18": ["SALE_TERRACE"],
        "01_19": ["SALE_COURT"],
        "01_20": ["COURT_SALE_CERTIFICATE"],
        "01_21": ["COURT_DECREE"],
        "01_22": ["SALE_OTHERS"],
        "02_01": ["MORTGAGE_WITH_POSSESSION"],
        "02_02": ["MORTGAGE_WITHOUT_POSSESSION"],
        "02_08": ["DOTD", "DEPOSIT_OF_TITLE_DEEDS"],
        "03_01": ["SETTLEMENT_FAMILY"],
        "03_02": ["SETTLEMENT_OTHER", "SETTLEMENT_OTHERS"],
        "03_05": [],  # GIFT_RELATIVE in Panchayats handled below
        "03_06": ["GIFT_RELATIVE", "GIFT_FAMILY"],  # in Municipalities
        "03_07": ["GIFT_OTHER", "GIFT_OTHERS"],
        "04_01": ["PARTITION_FAMILY"],
        "04_02": ["PARTITION_OTHER", "PARTITION_OTHERS"],
        "05_01": ["RELEASE_FAMILY"],
        "05_02": ["RELEASE_WITHOUT_CONSID"],
        "05_03": ["RELEASE"],
        "06_01": ["EXCHANGE"],
        "09_04": ["GPA_SELL"],
        "09_05": ["GPA_FAMILY"],
        "41_03": ["POWER_CONSIDERATION"],
    }

    # 1. GENERATE ALL 111 DEEDS FOR 2014-PRESENT ERA
    for d in deeds:
        code = d["code"]
        cls = d["classification"]
        dtype = d["type"]
        book = d["book"]
        art = d["article"] or ("Art. 47-A" if cls == "SALE" else f"Book {book}")
        if not art.startswith("Art.") and art != f"Book {book}":
            art = f"Art. {art}"

        sd_type, sd_val, sd_min, sd_max = parse_sd(d["sd"])
        td_val = clean_num(d["td"]) or "0"
        rf_type, rf_val, rf_min, rf_max = parse_rf(d["rf"], d["rf_min"], d["rf_max"])

        calc_basis = determine_calc_basis(cls, dtype, code)
        is_adj = "true" if code in ("01_02", "01_10", "01_11") else "false"

        citation = "G.O.Ms.No. 394 Rev (Regn-I) dt. 26.11.2014 & G.O.Ms.No. 393 Rev (Regn-I) dt. 26.11.2014"

        area_type = "ALL"
        if code == "03_05":
            area_type = "GRAM_PANCHAYAT"
        elif code == "03_06":
            area_type = "MUNICIPAL_CORP"

        primary_row = {
            "instrument_code": code,
            "article_ref": art,
            "area_type": area_type,
            "property_subtype": "ALL",
            "duration_min_months": "",
            "duration_max_months": "",
            "calc_basis": calc_basis,
            "stamp_duty_type": sd_type,
            "stamp_duty_value": sd_val,
            "stamp_duty_min": sd_min,
            "stamp_duty_max": sd_max,
            "transfer_duty_pct": td_val,
            "reg_fee_type": rf_type,
            "reg_fee_value": rf_val,
            "reg_fee_min": rf_min,
            "reg_fee_max": rf_max,
            "is_adjustable": is_adj,
            "effective_from": "2014-11-26",
            "effective_to": "",
            "authority_citation": citation,
            "verification_status": "VERIFIED",
            "notes": f"[{code}] {cls} - {dtype} (Book {book})"
        }
        rows.append(primary_row)

        if code == "03_06":
            corp_row = dict(primary_row)
            corp_row["area_type"] = "CORPORATION"
            rows.append(corp_row)
            gift_rel_row1 = dict(primary_row)
            gift_rel_row1["instrument_code"] = "GIFT_RELATIVE"
            gift_rel_row1["area_type"] = "MUNICIPAL_CORP"
            rows.append(gift_rel_row1)
            gift_rel_row2 = dict(primary_row)
            gift_rel_row2["instrument_code"] = "GIFT_RELATIVE"
            gift_rel_row2["area_type"] = "CORPORATION"
            rows.append(gift_rel_row2)

        if code == "03_05":
            gift_panch = dict(primary_row)
            gift_panch["instrument_code"] = "GIFT_RELATIVE"
            gift_panch["area_type"] = "GRAM_PANCHAYAT"
            rows.append(gift_panch)

        if code in aliases and code not in ("03_05", "03_06"):
            for alias in aliases[code]:
                alias_row = dict(primary_row)
                alias_row["instrument_code"] = alias
                alias_row["notes"] = f"Alias for [{code}] {dtype}"
                rows.append(alias_row)

    # 2. HISTORICAL ERAS
    # --- ERA 3: 2010-07-30 to 2014-11-25 (G.O.Ms.No. 719 & G.O. 150-153) ---
    historical_2010 = [
        ("01_01", "Art. 47-A", "ALL", "ALL", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "1.5000", "PERCENT", "0.5000", "", "", "false", "2010-07-30", "2014-11-25", "G.O.Ms.No. 719 Rev dt. 30.07.2010 & G.O.Ms.No. 150-153 dt. 06.04.2013", "VERIFIED", "SD 5% uniform, TD 1.5%, RF 0.5%"),
        ("SALE", "Art. 47-A", "ALL", "ALL", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "1.5000", "PERCENT", "0.5000", "", "", "false", "2010-07-30", "2014-11-25", "G.O.Ms.No. 719 Rev dt. 30.07.2010 & G.O.Ms.No. 150-153 dt. 06.04.2013", "VERIFIED", "SD 5% uniform, TD 1.5%, RF 0.5%"),
        ("GIFT_RELATIVE", "Art. 29", "ALL", "ALL", "MAX_MV_CONSID", "PERCENT", "1.0000", "", "", "0.5000", "PERCENT", "0.5000", "1000", "10000", "false", "2010-07-30", "2014-11-25", "G.O.Ms.No. 719 Rev dt. 30.07.2010", "VERIFIED", "Gift to family"),
        ("GIFT_OTHER", "Art. 29", "ALL", "ALL", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "1.5000", "PERCENT", "0.5000", "", "", "false", "2010-07-30", "2014-11-25", "G.O.Ms.No. 719 Rev dt. 30.07.2010", "VERIFIED", "Gift to non-family"),
    ]
    for h in historical_2010:
        rows.append(dict(zip(fieldnames, [
            h[0], h[1], h[2], h[3], "", "", h[4], h[5], h[6], h[7], h[8], h[9], h[10], h[11], h[12], h[13], h[14], h[15], h[16], h[17], h[18], h[19]
        ])))

    # --- ERA 2: 2005-07-01 to 2010-07-29 (G.O.Ms.No. 1126 & 1127 dt. 13.06.2005) ---
    historical_2005 = [
        ("01_01", "Art. 47-A", "MUNICIPAL_CORP", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Corporation 7% SD + 2% TD"),
        ("SALE", "Art. 47-A", "MUNICIPAL_CORP", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Corporation 7% SD + 2% TD"),
        ("01_01", "Art. 47-A", "CORPORATION", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Corporation 7% SD + 2% TD"),
        ("SALE", "Art. 47-A", "CORPORATION", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Corporation 7% SD + 2% TD"),
        ("01_01", "Art. 47-A", "SPECIAL_SELECTION_MUNICIPALITY", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Selection/Special Muni 7% SD + 2% TD"),
        ("SALE", "Art. 47-A", "SPECIAL_SELECTION_MUNICIPALITY", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Selection/Special Muni 7% SD + 2% TD"),
        ("01_01", "Art. 47-A", "OTHER_MUNICIPALITY", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Other Muni 6% SD + 2% TD"),
        ("SALE", "Art. 47-A", "OTHER_MUNICIPALITY", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Other Muni 6% SD + 2% TD"),
        ("01_01", "Art. 47-A", "GRAM_PANCHAYAT", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "3.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Panchayat 6% SD + 3% TD"),
        ("SALE", "Art. 47-A", "GRAM_PANCHAYAT", "NON_FLAT", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "3.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1126 dt 13.06.2005", "VERIFIED", "Panchayat 6% SD + 3% TD"),
        ("01_01", "Art. 47-A", "ALL", "FLAT", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1127 dt 13.06.2005", "VERIFIED", "Apartments/Flats 5% SD + 2% TD"),
        ("SALE", "Art. 47-A", "ALL", "FLAT", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1127 dt 13.06.2005", "VERIFIED", "Apartments/Flats 5% SD + 2% TD"),
        ("01_01", "Art. 47-A", "ALL", "FLAT_APARTMENT", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1127 dt 13.06.2005", "VERIFIED", "Apartments/Flats 5% SD + 2% TD"),
        ("SALE", "Art. 47-A", "ALL", "FLAT_APARTMENT", "MAX_MV_CONSID", "PERCENT", "5.0000", "", "", "2.0000", "PERCENT", "0.5000", "", "", "false", "2005-07-01", "2010-07-29", "G.O.Ms.No. 1127 dt 13.06.2005", "VERIFIED", "Apartments/Flats 5% SD + 2% TD"),
    ]
    for h in historical_2005:
        rows.append(dict(zip(fieldnames, [
            h[0], h[1], h[2], h[3], "", "", h[4], h[5], h[6], h[7], h[8], h[9], h[10], h[11], h[12], h[13], h[14], h[15], h[16], h[17], h[18], h[19]
        ])))

    # --- ERA 1: 1990-01-01 to 2005-06-30 (PRE-REFORM HISTORICAL REGIME: 13.5% in Corp) ---
    historical_1990 = [
        ("01_01", "Art. 47-A", "MUNICIPAL_CORP", "ALL", "MAX_MV_CONSID", "PERCENT", "8.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Municipalities Act 1965", "VERIFIED", "1990-2005 Corporation rate: 8% SD + 5% TD + 0.5% RF = 13.5%"),
        ("SALE", "Art. 47-A", "MUNICIPAL_CORP", "ALL", "MAX_MV_CONSID", "PERCENT", "8.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Municipalities Act 1965", "VERIFIED", "1990-2005 Corporation rate: 8% SD + 5% TD + 0.5% RF = 13.5%"),
        ("01_01", "Art. 47-A", "CORPORATION", "ALL", "MAX_MV_CONSID", "PERCENT", "8.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Municipalities Act 1965", "VERIFIED", "1990-2005 Corporation rate: 8% SD + 5% TD + 0.5% RF = 13.5%"),
        ("SALE", "Art. 47-A", "CORPORATION", "ALL", "MAX_MV_CONSID", "PERCENT", "8.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Municipalities Act 1965", "VERIFIED", "1990-2005 Corporation rate: 8% SD + 5% TD + 0.5% RF = 13.5%"),
        ("01_01", "Art. 47-A", "SPECIAL_SELECTION_MUNICIPALITY", "ALL", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Municipalities Act 1965", "VERIFIED", "Selection/Special Muni: 7% SD + 5% TD + 0.5% RF = 12.5%"),
        ("SALE", "Art. 47-A", "SPECIAL_SELECTION_MUNICIPALITY", "ALL", "MAX_MV_CONSID", "PERCENT", "7.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Municipalities Act 1965", "VERIFIED", "Selection/Special Muni: 7% SD + 5% TD + 0.5% RF = 12.5%"),
        ("01_01", "Art. 47-A", "OTHER_MUNICIPALITY", "ALL", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Gram Panchayat Act 1994", "VERIFIED", "Other Muni: 6% SD + 5% TD + 0.5% RF = 11.5%"),
        ("SALE", "Art. 47-A", "OTHER_MUNICIPALITY", "ALL", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Gram Panchayat Act 1994", "VERIFIED", "Other Muni: 6% SD + 5% TD + 0.5% RF = 11.5%"),
        ("01_01", "Art. 47-A", "GRAM_PANCHAYAT", "ALL", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Gram Panchayat Act 1994", "VERIFIED", "Panchayat: 6% SD + 5% TD + 0.5% RF = 11.5%"),
        ("SALE", "Art. 47-A", "GRAM_PANCHAYAT", "ALL", "MAX_MV_CONSID", "PERCENT", "6.0000", "", "", "5.0000", "PERCENT", "0.5000", "", "", "false", "1990-01-01", "2005-06-30", "Indian Stamp Act Schedule I-A (AP) & AP Gram Panchayat Act 1994", "VERIFIED", "Panchayat: 6% SD + 5% TD + 0.5% RF = 11.5%"),
    ]
    for h in historical_1990:
        rows.append(dict(zip(fieldnames, [
            h[0], h[1], h[2], h[3], "", "", h[4], h[5], h[6], h[7], h[8], h[9], h[10], h[11], h[12], h[13], h[14], h[15], h[16], h[17], h[18], h[19]
        ])))

    # The pre-2005 entries in this generator are not supported by reviewed
    # source evidence; do not let them become calculator inputs.
    rows = [row for row in rows if row["effective_from"] != "1990-01-01"]
    for row in rows:
        row["verification_status"] = "PENDING_REVIEW"

    out_csv = SEEDS_DIR / "duty_rates.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Generated {len(rows)} draft rate rows (PENDING_REVIEW) in {out_csv}")


if __name__ == "__main__":
    main()
