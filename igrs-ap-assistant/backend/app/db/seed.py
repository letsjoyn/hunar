"""
Seed loader: reads verified CSVs and populates the database.
Skips comment lines (starting with #) in CSVs.
"""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from backend.app.db.models import DutyRate, StructureRate, UserCharge


def _parse_date(s: str) -> Optional[date]:
    s = s.strip()
    if not s:
        return None
    return date.fromisoformat(s)


def _parse_decimal(s: str) -> Optional[Decimal]:
    s = s.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _parse_int(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None
    return int(s)


def _parse_bool(s: str) -> bool:
    return s.strip().lower() in ("true", "1", "yes")


def _read_csv_rows(path: Path) -> list[dict]:
    """Read CSV, skip comment lines starting with #."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        # Read all lines, filter out comments
        lines = [line for line in f if not line.strip().startswith("#")]
    # Re-parse filtered lines
    reader = csv.DictReader(lines)
    for row in reader:
        rows.append(row)
    return rows


def load_duty_rates(session: Session, csv_path: Path):
    """Load duty_rates.csv and lease_rates.csv into the duty_rates table."""
    for path in [csv_path / "duty_rates.csv", csv_path / "lease_rates.csv"]:
        if not path.exists():
            continue
        for row in _read_csv_rows(path):
            rate = DutyRate(
                instrument_code=row["instrument_code"].strip(),
                article_ref=row["article_ref"].strip(),
                area_type=row["area_type"].strip(),
                property_subtype=row.get("property_subtype", "ALL").strip() or "ALL",
                duration_min_months=_parse_int(row.get("duration_min_months", "")),
                duration_max_months=_parse_int(row.get("duration_max_months", "")),
                calc_basis=row["calc_basis"].strip(),
                stamp_duty_type=row["stamp_duty_type"].strip(),
                stamp_duty_value=_parse_decimal(row["stamp_duty_value"]),
                stamp_duty_min=_parse_decimal(row.get("stamp_duty_min", "")),
                stamp_duty_max=_parse_decimal(row.get("stamp_duty_max", "")),
                transfer_duty_pct=_parse_decimal(row.get("transfer_duty_pct", "0")) or Decimal("0"),
                reg_fee_type=row["reg_fee_type"].strip(),
                reg_fee_value=_parse_decimal(row["reg_fee_value"]),
                reg_fee_min=_parse_decimal(row.get("reg_fee_min", "")),
                reg_fee_max=_parse_decimal(row.get("reg_fee_max", "")),
                is_adjustable=_parse_bool(row.get("is_adjustable", "false")),
                effective_from=_parse_date(row["effective_from"]),
                effective_to=_parse_date(row.get("effective_to", "")),
                authority_citation=row["authority_citation"].strip(),
                verification_status=row.get("verification_status", "PENDING_REVIEW").strip(),
                notes=row.get("notes", "").strip() or None,
            )
            session.add(rate)
    session.commit()


def load_user_charges(session: Session, csv_path: Path):
    """Load user_charges.csv into the user_charges table."""
    path = csv_path / "user_charges.csv"
    if not path.exists():
        return
    for row in _read_csv_rows(path):
        charge = UserCharge(
            service_code=row["service_code"].strip(),
            description=row.get("description", "").strip() or None,
            value_threshold_min=_parse_decimal(row.get("value_threshold_min", "0")) or Decimal("0"),
            value_threshold_max=_parse_decimal(row.get("value_threshold_max", "")),
            included_sheets=_parse_int(row.get("included_sheets", "10")) or 10,
            charge_amount=_parse_decimal(row["charge_amount"]),
            effective_from=_parse_date(row["effective_from"]),
            effective_to=_parse_date(row.get("effective_to", "")),
            authority_citation=row["authority_citation"].strip(),
            verification_status=row.get("verification_status", "PENDING_REVIEW").strip(),
            notes=row.get("notes", "").strip() or None,
        )
        session.add(charge)
    session.commit()


def load_structure_rates(session: Session, csv_path: Path):
    """Load structure_rates.csv into the structure_rates table."""
    path = csv_path / "structure_rates.csv"
    if not path.exists():
        return
    for row in _read_csv_rows(path):
        sr = StructureRate(
            structure_category=row["structure_category"].strip(),
            area_tier=row["area_tier"].strip(),
            rate_per_sqft=_parse_decimal(row["rate_per_sqft"]),
            extra_floor_adder=_parse_decimal(row.get("extra_floor_adder", "0")) or Decimal("0"),
            effective_from=_parse_date(row["effective_from"]),
            effective_to=_parse_date(row.get("effective_to", "")),
            authority_citation=row["authority_citation"].strip(),
        )
        session.add(sr)
    session.commit()


def seed_all(session: Session, seeds_dir: Path):
    """Load all seed data into the database."""
    load_duty_rates(session, seeds_dir)
    load_user_charges(session, seeds_dir)
    load_structure_rates(session, seeds_dir)
