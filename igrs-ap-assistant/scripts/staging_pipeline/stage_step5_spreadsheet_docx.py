"""Stage all supplied XLSX rows and DOCX paragraphs for Stage 5 review.

Workbook rate rows are copied into staged_deed_rates as unapproved baselines;
no effective dates or legal authority are inferred from the workbook.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
INVENTORY_PATH = APP_ROOT / "data" / "source_inventory_manifest.json"
EXTRACTION_AUDIT_PATH = APP_ROOT / "data" / "raw" / "source_extraction" / "source_extraction_audit.json"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def safe_cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def as_decimal(value) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*%?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def formatted_code(value) -> str:
    text = safe_cell(value)
    if text.isdigit() and len(text) < 2:
        return text.zfill(2)
    return text


def check_no_pii(text: str, locator: str) -> None:
    if any(pattern.search(text) for pattern in PII_PATTERNS):
        raise ValueError(f"Identifier pattern detected in {locator}; nothing from that row was staged")


def main() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    extraction = json.loads(EXTRACTION_AUDIT_PATH.read_text(encoding="utf-8"))
    extraction_by_name = {Path(row["source_relative_path"]).name: row for row in extraction["files"]}
    xlsx_rows: list[dict] = []
    xlsx_rate_rows: list[dict] = []
    docx_paragraphs: list[dict] = []

    for inventory_row in inventory["files"]:
        filename = Path(inventory_row["relative_path"]).name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".docx"}:
            continue
        audit_row = extraction_by_name.get(filename)
        if not audit_row:
            raise ValueError(f"Extraction audit missing for {filename}")
        if audit_row["source_sha256"] != inventory_row["sha256"]:
            raise ValueError(f"Source hash mismatch for {filename}")
        extracted_path = APP_ROOT / audit_row["extracted_text_local_path"]
        source_path = GOV_ROOT / Path(inventory_row["relative_path"])
        if suffix == ".docx":
            lines = extracted_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, content in enumerate(lines, start=1):
                content = content.strip()
                if not content:
                    continue
                locator = f"DOCX:{filename}:PARAGRAPH-LINE:{line_number}"
                check_no_pii(content, locator)
                docx_paragraphs.append({
                    "filename": filename,
                    "source_sha256": inventory_row["sha256"],
                    "relative_path": audit_row["extracted_text_local_path"],
                    "line": line_number,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "locator": locator,
                })
            continue

        workbook = load_workbook(source_path, read_only=True, data_only=False)
        for sheet in workbook.worksheets:
            values_by_row: dict[int, list] = {}
            for row_number, cells in enumerate(sheet.iter_rows(), start=1):
                values = [cell.value for cell in cells]
                while values and values[-1] is None:
                    values.pop()
                if any(value is not None and safe_cell(value) for value in values):
                    values_by_row[row_number] = values
            if not values_by_row:
                continue

            header_row = None
            for row_number, values in values_by_row.items():
                labels = [safe_cell(value).casefold() for value in values]
                if "document classification" in labels and "document type" in labels:
                    header_row = row_number
                    break

            for row_number, values in values_by_row.items():
                rendered = [safe_cell(value) for value in values]
                content = " | ".join(rendered)
                locator = f"XLSX:{filename}:SHEET:{sheet.title}:ROW:{row_number}"
                check_no_pii(content, locator)
                # The supplied deed-rate sheet uses this fixed column order.
                # Treat a row as a rate only when its code pair and deed label exist.
                padded = values + [None] * max(0, 13 - len(values))
                major = formatted_code(padded[1]) if len(padded) > 1 else ""
                minor = formatted_code(padded[2]) if len(padded) > 2 else ""
                deed_type = safe_cell(padded[4]) if len(padded) > 4 else ""
                is_rate_row = (
                    header_row is not None
                    and row_number > header_row + 1
                    and bool(major and minor and deed_type)
                )
                xlsx_rows.append({
                    "filename": filename,
                    "source_sha256": inventory_row["sha256"],
                    "relative_path": audit_row["extracted_text_local_path"],
                    "sheet": sheet.title,
                    "row": row_number,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "locator": locator,
                    "classification": "DEED_RATE_ROW" if is_rate_row else "WORKBOOK_REFERENCE_ROW",
                })
                if not is_rate_row:
                    continue
                xlsx_rate_rows.append({
                    "filename": filename,
                    "source_sha256": inventory_row["sha256"],
                    "sheet": sheet.title,
                    "row": row_number,
                    "locator": f"{sheet.title}!{row_number}",
                    "deed_code": f"{major}-{minor}",
                    "classification": safe_cell(padded[3]) or None,
                    "deed_type": deed_type,
                    "book_number": as_decimal(padded[5]),
                    "stamp_duty_percent": as_decimal(padded[6]),
                    "transfer_duty_percent": as_decimal(padded[7]),
                    "registration_fee_percent": as_decimal(padded[8]),
                    "registration_fee_min": as_decimal(padded[9]),
                    "registration_fee_max": as_decimal(padded[10]),
                    "article_ref": safe_cell(padded[11]) or None,
                    "calculation_basis": safe_cell(padded[8]) if as_decimal(padded[8]) is None else None,
                    "row_content": content,
                })
        workbook.close()

    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("backend.app.db.staging_models")
    SourceDocument = models.SourceDocument
    LegalProvision = models.LegalProvision
    StagedDeedRate = models.StagedDeedRate
    StagingReviewEvent = models.StagingReviewEvent
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())))
    try:
        with Session(engine) as session, session.begin():
            source_rows = {
                row.original_filename: row
                for row in session.scalars(select(SourceDocument)).all()
            }
            all_input_rows = xlsx_rows + [
                {**paragraph, "sheet": None, "row": paragraph["line"], "classification": "DOCX_PARAGRAPH"}
                for paragraph in docx_paragraphs
            ]
            for item in all_input_rows:
                source = source_rows.get(item["filename"])
                if source is None:
                    raise ValueError(f"Staging source record missing for {item['filename']}")
                locator = item["locator"]
                provision_id = stable_id(source.id, locator, item["content_sha256"])
                is_rate = item["classification"] == "DEED_RATE_ROW"
                fields = {
                    "source_document_id": source.id,
                    "parent_provision_id": None,
                    "provision_type": (
                        "ADMIN_WORKBOOK_RATE_ROW_CANDIDATE" if is_rate else
                        "DOCX_PARAGRAPH_CANDIDATE" if item["classification"] == "DOCX_PARAGRAPH" else
                        "ADMIN_WORKBOOK_REFERENCE_ROW"
                    ),
                    "legal_domain": (
                        "DEED_RATE_BASELINE" if is_rate else
                        "UNKNOWN_SOURCE_REVIEW" if item["classification"] == "DOCX_PARAGRAPH" else
                        "WORKBOOK_REFERENCE"
                    ),
                    "locator_key": locator,
                    "heading": f"{item['filename']} — {item['classification']} at {locator}",
                    "citation": f"Supplied source {item['filename']}, extracted locator {locator}; review and authority unverified",
                    "content": item["content"],
                    "content_sha256": item["content_sha256"],
                    "language": "en",
                    "page_start": None,
                    "page_end": None,
                    "line_start": item.get("line", item.get("row")),
                    "line_end": item.get("line", item.get("row")),
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "ADMINISTRATIVE_BASELINE_UNVERIFIED" if is_rate else "SOURCE_UNVERIFIED",
                    "extraction_quality": "EXTRACTED_TEXT_CANDIDATE",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": not is_rate,
                    "metadata_json": {
                        "source_kind": "department_supplied_workbook_or_docx",
                        "sheet": item.get("sheet"),
                        "row_or_paragraph": item.get("row"),
                        "original_source_sha256": item["source_sha256"],
                        "not_for_calculation": True,
                        "review_required": True,
                    },
                }
                provision = session.get(LegalProvision, provision_id)
                if provision is None:
                    provision = session.scalar(select(LegalProvision).where(
                        LegalProvision.source_document_id == source.id,
                        LegalProvision.locator_key == locator,
                    ))
                if provision is None:
                    session.add(LegalProvision(id=provision_id, **fields))
                else:
                    for key, value in fields.items():
                        setattr(provision, key, value)

            for item in xlsx_rate_rows:
                source = source_rows.get(item["filename"])
                if source is None:
                    raise ValueError(f"Staging source record missing for {item['filename']}")
                rate_id = stable_id(source.id, item["locator"], item["deed_code"])
                fields = {
                    "source_document_id": source.id,
                    "source_row_locator": item["locator"],
                    "deed_code": item["deed_code"],
                    "classification": item["classification"],
                    "deed_type": item["deed_type"],
                    "article_ref": item["article_ref"],
                    "book_number": int(item["book_number"]) if item["book_number"] is not None else None,
                    "stamp_duty_percent": item["stamp_duty_percent"],
                    "transfer_duty_percent": item["transfer_duty_percent"],
                    "registration_fee_percent": item["registration_fee_percent"],
                    "registration_fee_min": item["registration_fee_min"],
                    "registration_fee_max": item["registration_fee_max"],
                    "calculation_basis": item["calculation_basis"],
                    "area_type": "UNSPECIFIED",
                    "property_subtype": "UNSPECIFIED",
                    "effective_from": None,
                    "effective_to": None,
                    "source_status": "ADMIN_WORKBOOK_BASELINE_UNVERIFIED",
                    "review_status": "PENDING_REVIEW",
                    "row_metadata": {
                        "source_filename": item["filename"],
                        "source_sha256": item["source_sha256"],
                        "sheet": item["sheet"],
                        "row": item["row"],
                        "raw_row_text": item["row_content"],
                        "effective_dates_not_supplied": True,
                        "not_for_calculation": True,
                    },
                }
                rate = session.get(StagedDeedRate, rate_id)
                if rate is None:
                    rate = session.scalar(select(StagedDeedRate).where(
                        StagedDeedRate.source_document_id == source.id,
                        StagedDeedRate.source_row_locator == item["locator"],
                    ))
                if rate is None:
                    session.add(StagedDeedRate(id=rate_id, **fields))
                else:
                    for key, value in fields.items():
                        setattr(rate, key, value)
                event_id = stable_id("WORKBOOK_RATE_STAGED", rate_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="STAGED_DEED_RATE",
                        target_id=rate_id,
                        action="STAGED_WORKBOOK_RATE_FOR_RECONCILIATION",
                        reviewer_id=None,
                        note="Department-supplied workbook baseline only; verify source, effective dates and deed mapping before calculation.",
                        details={"filename": item["filename"], "sheet": item["sheet"], "row": item["row"]},
                    ))
        print(f"Indexed workbook nonempty rows: {len(xlsx_rows)}; DOCX paragraphs: {len(docx_paragraphs)}.")
        print(f"Staged workbook deed-rate candidates: {len(xlsx_rate_rows)}; all pending reconciliation.")
        print("No effective dates were invented; no live calculations or vectors were changed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
