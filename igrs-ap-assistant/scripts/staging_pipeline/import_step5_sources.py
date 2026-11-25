"""Stage source metadata and the first source-cited Government Order provisions.

This importer is idempotent and writes only to data/source_staging.db. It does
not insert diary text or alter live legal/rate databases.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
AUTHORITY_REPORT = APP_ROOT / "data" / "source_authority_and_rate_reconciliation.json"
EXTRACTION_AUDIT = APP_ROOT / "data" / "raw" / "source_extraction" / "source_extraction_audit.json"
PDF_AUDIT = APP_ROOT / "data" / "raw" / "page_extraction_audit" / "page_extraction_audit.json"


def stable_id(*parts: str) -> str:
    value = "|".join(parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def source_class(record: dict) -> str:
    classification = record["authority_classification"]
    decision = record["provisional_decision"].casefold()
    if "exclude" in decision:
        return "OUT_OF_SCOPE"
    if classification == "government_order_candidate":
        return "PRIMARY_CANDIDATE"
    if classification == "administrative_rate_baseline":
        return "ADMIN_BASELINE"
    if classification in {
        "departmental_diary_or_manual",
        "secondary_or_compiled_rule_text",
        "secondary_manual_compilation",
        "compiled_law_text_unverified_edition",
    }:
        return "SECONDARY"
    return "UNVERIFIED"


def source_kind(authority: str, filename: str) -> str:
    if authority == "PRIMARY_CANDIDATE":
        return "GOVERNMENT_ORDER"
    if authority == "SECONDARY":
        return "MANUAL_OR_COMPILED_TEXT"
    if authority == "ADMIN_BASELINE":
        return "RATE_WORKBOOK"
    if authority == "OUT_OF_SCOPE":
        return "OUT_OF_SCOPE_REFERENCE"
    return "UNIDENTIFIED_SOURCE"


def source_document_fields(record: dict, extraction: dict | None, pdf: dict | None) -> dict:
    relative = record["source_path"]
    filename = Path(relative).name
    source_hash = record["source_sha256"]
    authority = source_class(record)
    pii_counts = record.get("privacy_pattern_counts_only") or {}
    privacy_flagged = sum(pii_counts.values()) > 0
    if privacy_flagged:
        review_status = "IN_REVIEW"
        privacy_status = "SOURCE_PATTERN_MATCHES_DERIVATIVES_REQUIRED"
    elif authority == "OUT_OF_SCOPE":
        review_status = "PENDING_REVIEW"
        privacy_status = "PENDING_REVIEW"
    else:
        review_status = "PENDING_REVIEW"
        privacy_status = "PENDING_REVIEW"

    extracted_hash = None
    extraction_status = "NOT_STARTED"
    ocr_status = "NOT_APPLICABLE"
    ocr_summary = None
    if extraction:
        extracted_hash = extraction.get("extracted_text_sha256")
        extraction_status = "TEXT_EXTRACTED"
    if pdf:
        extracted_hash = pdf.get("extracted_text_sha256")
        extraction_status = "PAGE_TEXT_EXTRACTED"
        page_rows = pdf.get("pages", [])
        ocr_pages = [page for page in page_rows if "ocr_status" in page]
        low_pages = [page for page in ocr_pages if page.get("ocr_status") != "ocr_text_detected"]
        if not ocr_pages:
            ocr_status = "TEXT_LAYER_ONLY"
        elif low_pages:
            ocr_status = "OCR_PARTIAL_LOW_CONFIDENCE"
        else:
            ocr_status = "OCR_TEXT_UNVERIFIED"
        ocr_summary = {
            "page_count": pdf.get("page_count"),
            "ocr_page_count": len(ocr_pages),
            "low_confidence_pages": [page["page"] for page in low_pages],
            "mean_word_confidences": {
                str(page["page"]): page.get("ocr_mean_word_confidence")
                for page in ocr_pages if page.get("ocr_mean_word_confidence") is not None
            },
        }

    go_number = "393" if filename == "2014REV_MS393.PDF" else "394" if filename == "2014REV_MS394.PDF" else None
    issue_date = date(2014, 11, 26) if go_number else None
    issuer = "Government of Andhra Pradesh" if go_number else None
    title = {
        "2014REV_MS393.PDF": "Revenue (Registration.I) Department G.O.Ms.No.393",
        "2014REV_MS394.PDF": "Revenue (Registration.I) Department G.O.Ms.No.394",
        "BSR - Registration Act 1908.txt": "B.S.R. Registration Manual (2007 edition indicated in source)",
        "BSR - Indian Stamp Act 1899(1).txt": "B.S.R. Indian Stamp Act and related rules compilation",
    }.get(filename, Path(filename).stem)
    metadata = {
        "provisional_authority_classification": record["authority_classification"],
        "provisional_decision": record["provisional_decision"],
        "privacy_pattern_counts_only": pii_counts,
        "authenticity_or_edition_verified": False,
        "source_relative_path": relative,
    }
    if authority == "OUT_OF_SCOPE":
        metadata["domain_separation_required"] = True
        metadata["runtime_policy"] = "Keep in its own legal domain; never mix with Registration & Stamps rate retrieval."
    if filename == "Dairy 2024.txt":
        metadata["raw_source_indexing_policy"] = "Do not index raw diary text; stage only redacted legal candidate extracts after page-level review."
    if filename == "BSR - Registration Act 1908.txt":
        metadata["edition_year_observed_on_cover"] = 2007
    return {
        "id": stable_id(relative, source_hash),
        "source_path": relative,
        "original_filename": filename,
        "source_sha256": source_hash,
        "media_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "source_kind": source_kind(authority, filename),
        "title": title,
        "issuer": issuer,
        "document_number": f"G.O.Ms.No.{go_number}" if go_number else None,
        "legal_domain": record["domain_classification"],
        "authority_classification": authority,
        "authenticity_status": "UNVERIFIED",
        "issued_on": issue_date,
        "published_on": issue_date,
        "effective_from": issue_date if go_number else None,
        "effective_to": None,
        "supersedes_document_id": None,
        "extraction_status": extraction_status,
        "extracted_text_sha256": extracted_hash,
        "ocr_status": ocr_status,
        "ocr_language": "eng" if pdf and ocr_summary and ocr_summary["ocr_page_count"] else None,
        "ocr_quality_summary": ocr_summary,
        "privacy_status": privacy_status,
        "review_status": review_status,
        "source_metadata": metadata,
    }


def extract_span(path: Path, start_phrase: str, end_phrase: str) -> tuple[str, int, int]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start_index = next((i for i, line in enumerate(lines) if start_phrase.casefold() in line.casefold()), None)
    if start_index is None:
        raise ValueError(f"Start locator not found in {path.name}")
    end_index = next((i for i in range(start_index, len(lines)) if end_phrase.casefold() in lines[i].casefold()), None)
    if end_index is None:
        raise ValueError(f"End locator not found in {path.name}")
    selected = [line.strip() for line in lines[start_index:end_index + 1] if line.strip()]
    return " ".join(selected), start_index + 1, end_index + 1


def go_provisions(pdf_docs: dict[str, dict], source_ids: dict[str, str]) -> list[dict]:
    specifications = [
        {
            "filename": "2014REV_MS393.PDF",
            "start": "In exercise of the powers conferred by section 78",
            "end": "substituted.",
            "locator": "GO:393:SECTION-78:CATEGORY-1:ITEM-1",
            "type": "GOVERNMENT_ORDER_AMENDMENT",
            "domain": "REGISTRATION_FEE",
            "section": "Section 78",
            "item": "Category (1), Sl.No.1, column (3)",
            "citation": "G.O.Ms.No.393, Revenue (Registration.I) Department, dated 26-11-2014, notification under Section 78 of the Registration Act, 1908",
            "note": "Supplied order changes the fee expression 0.5% to 1%; mapping Category (1), Sl.No.1 to a deed code is not verified because the cited G.O.Ms.No.463 fee table is not supplied.",
        },
        {
            "filename": "2014REV_MS394.PDF",
            "start": "In exercise of the powers conferred by clause (a) of sub-section (1) of Section 9",
            "end": "effect.",
            "locator": "GO:394:SECTION-9:SALE-DEED-STAMP-DUTY",
            "type": "GOVERNMENT_ORDER_NOTIFICATION",
            "domain": "STAMP_DUTY",
            "section": "Section 9(1)(a)",
            "article": "Article 47-A, Schedule I-A",
            "citation": "G.O.Ms.No.394, Revenue (Registration.I) Department, dated 26-11-2014, notification under Section 9(1)(a) of the Indian Stamp Act, 1899",
            "note": "Supplied notification rescinds G.O.Ms.No.162 and restores the sale-deed duty prescribed by G.O.Ms.No.719; operative only from its stated immediate effective date, subject to later amendments.",
        },
    ]
    provisions = []
    for spec in specifications:
        pdf = pdf_docs.get(spec["filename"])
        if not pdf:
            raise ValueError(f"PDF audit record missing for {spec['filename']}")
        text_path = APP_ROOT / pdf["extracted_text_local_path"]
        content, line_start, line_end = extract_span(text_path, spec["start"], spec["end"])
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        provision_id = stable_id(source_ids[spec["filename"]], spec["locator"], content_hash)
        provision = {
            "id": provision_id,
            "source_document_id": source_ids[spec["filename"]],
            "parent_provision_id": None,
            "provision_type": spec["type"],
            "legal_domain": spec["domain"],
            "locator_key": spec["locator"],
            "section_label": spec["section"],
            "article_label": spec.get("article"),
            "item_label": spec.get("item"),
            "heading": spec["citation"],
            "citation": spec["citation"],
            "content": content,
            "content_sha256": content_hash,
            "language": "en",
            "page_start": 1,
            "page_end": 1,
            "line_start": line_start,
            "line_end": line_end,
            "effective_from": date(2014, 11, 26),
            "effective_to": None,
            "authority_status": "PRIMARY_CANDIDATE",
            "extraction_quality": "TEXT_LAYER_UNREVIEWED",
            "review_status": "PENDING_REVIEW",
            "is_secondary_commentary": False,
            "metadata_json": {
                "authenticity_verified": False,
                "source_note": spec["note"],
                "effective_date_basis": "stated issue/Gazette date in supplied document; independently unverified",
            },
        }
        provisions.append(provision)
    return provisions


def extract_until(
    path: Path,
    start_phrase: str,
    end_pattern: str,
    start_line_hint: int | None = None,
) -> tuple[str, int, int]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if start_line_hint is not None:
        start_index = start_line_hint - 1
        if start_index < 0 or start_index >= len(lines) or start_phrase.casefold() not in lines[start_index].casefold():
            raise ValueError(f"Start line hint does not match in {path.name}: {start_line_hint}")
    else:
        start_index = next((i for i, line in enumerate(lines) if start_phrase.casefold() in line.casefold()), None)
    if start_index is None:
        raise ValueError(f"Start locator not found in {path.name}: {start_phrase}")
    stop_re = re.compile(end_pattern, re.IGNORECASE)
    stop_index = next((i for i in range(start_index + 1, len(lines)) if stop_re.search(lines[i])), None)
    if stop_index is None:
        raise ValueError(f"End boundary not found in {path.name}: {end_pattern}")
    selected = [line.strip() for line in lines[start_index:stop_index] if line.strip()]
    return " ".join(selected), start_index + 1, stop_index


def manual_provisions(extraction_by_name: dict[str, dict], source_ids: dict[str, str], source_fields: dict[str, dict]) -> list[dict]:
    pii_patterns = [
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
        re.compile(r"(?<!\d)\d{12}(?!\d)"),
        re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
    ]
    specifications = [
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "9. Power to reduce, remit or compound duties",
            "end_regex": r"^\s*Elucidation\s*:",
            "locator": "INDIAN-STAMP-ACT:SECTION:9",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "STAMP_DUTY",
            "section": "Section 9",
            "citation": "Indian Stamp Act, 1899, Section 9, as transcribed in the supplied B.S.R. compilation (edition unverified)",
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "47A, Instruments of conveyance, etc., undervalued",
            "end_regex": r"^\s*Elucidation\s*:",
            "locator": "INDIAN-STAMP-ACT:SECTION:47-A",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "STAMP_DUTY_VALUATION",
            "section": "Section 47-A",
            "citation": "Indian Stamp Act, 1899, Section 47-A, as transcribed in the supplied B.S.R. compilation (edition unverified)",
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "Elucidation :",
            "start_line_hint": 4279,
            "end_regex": r"^\s*48\.\s+Recovery of duties and penalties",
            "locator": "INDIAN-STAMP-ACT:SECTION:47-A:EDITORIAL-NOTES",
            "type": "EDITORIAL_NOTE",
            "domain": "STAMP_DUTY_VALUATION",
            "section": "Section 47-A (editorial notes and case law)",
            "citation": "B.S.R. compilation notes and case-law references following Section 47-A; secondary material, edition unverified",
            "commentary": True,
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "47A. Sale as defined in Section 54",
            "end_regex": r"^\s*Elucidation\s*:",
            "locator": "SCHEDULE-IA:ARTICLE:47-A",
            "type": "SCHEDULE_ARTICLE_TRANSCRIPTION",
            "domain": "STAMP_DUTY_SALE",
            "article": "Schedule I-A, Article 47-A",
            "citation": "Indian Stamp Act, Schedule I-A, Article 47-A, as transcribed in the supplied B.S.R. compilation (edition unverified)",
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "Elucidation :",
            "start_line_hint": 8923,
            "end_regex": r"^\s*Schedule\s*-\s*1A\s*\(Art\.\s*48\)",
            "locator": "SCHEDULE-IA:ARTICLE:47-A:EDITORIAL-NOTES",
            "type": "EDITORIAL_NOTE",
            "domain": "STAMP_DUTY_SALE",
            "article": "Schedule I-A, Article 47-A (editorial notes and case law)",
            "citation": "B.S.R. compilation explanatory notes and case-law references following Schedule I-A, Article 47-A; secondary material, edition unverified",
            "commentary": True,
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "13. __ Bill of exchange as defined by Section 2(2)",
            "end_regex": r"^\s*Schedule\s*-\s*1A\s*\(Arts\.\s*1,\s*2\s*&\s*3\)",
            "locator": "SCHEDULE-I:ARTICLE:13:TEXT",
            "type": "SCHEDULE_ARTICLE_TRANSCRIPTION",
            "domain": "STAMP_DUTY_BILLS_AND_NOTES",
            "article": "Schedule I, Article 13",
            "citation": "Indian Stamp Act, Schedule I, Article 13, as transcribed in the supplied B.S.R. compilation (edition unverified)",
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "52. Surrender of Lease -",
            "end_regex": r"^\s*53\.\s+Transfer \(whether with or",
            "locator": "SCHEDULE-IA:ARTICLE:52:TEXT",
            "type": "SCHEDULE_ARTICLE_TRANSCRIPTION",
            "domain": "STAMP_DUTY_LEASE",
            "article": "Schedule I-A, Article 52",
            "citation": "Indian Stamp Act, Schedule I-A, Article 52, as transcribed in the supplied B.S.R. compilation (edition unverified)",
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "49. Settlement -",
            "end_regex": r"^\s*Reduction\s*&\s*Exemption of Duty\s*:",
            "locator": "SCHEDULE-IA:ARTICLE:49:TEXT",
            "type": "SCHEDULE_ARTICLE_TRANSCRIPTION",
            "domain": "STAMP_DUTY_SETTLEMENT",
            "article": "Schedule I-A, Article 49",
            "citation": "Indian Stamp Act, Schedule I-A, Article 49, as transcribed in the supplied B.S.R. compilation (edition unverified)",
        },
        {
            "filename": "BSR - Indian Stamp Act 1899(1).txt",
            "start": "Reduction & Exemption of Duty :",
            "start_line_hint": 9127,
            "end_regex": r"^\s*Schedule\s*-\s*1A\s*\(Arts\.\s*50",
            "locator": "SCHEDULE-IA:ARTICLE:49:EDITORIAL-RATE-NOTES",
            "type": "EDITORIAL_RATE_NOTE",
            "domain": "STAMP_DUTY_SETTLEMENT",
            "article": "Schedule I-A, Article 49 (editorial notes)",
            "citation": "B.S.R. compilation notes under Schedule I-A, Article 49; secondary rate-history note, edition unverified",
            "commentary": True,
        },
        {
            "filename": "BSR - Registration Act 1908.txt",
            "start": "22A:- As substituted by Act 19 of 2007",
            "end_regex": r"^\s*EDITOR['’]S NOTES\s*$",
            "locator": "REGISTRATION-ACT:SECTION:22-A:AP-COMPILATION",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "REGISTRATION_PROHIBITED_PROPERTY",
            "section": "Section 22-A (Andhra Pradesh text as compiled)",
            "citation": "Registration Act, 1908, Section 22-A, as transcribed in the supplied B.S.R. Registration Manual; 2007 amendment text indicated, edition unverified",
        },
        {
            "filename": "BSR - Registration Act 1908.txt",
            "start": "25. Provision where delay in presentation is unavoidable",
            "end_regex": r"^\s*26\.\s+Document executed out of India",
            "locator": "REGISTRATION-ACT:SECTION:25",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "REGISTRATION_PRESENTATION_DELAY",
            "section": "Section 25",
            "citation": "Registration Act, 1908, Section 25, as transcribed in the supplied B.S.R. Registration Manual (2007 edition indicated; unverified)",
        },
        {
            "filename": "BSR - Registration Act 1908.txt",
            "start": "47. Time from which registered document operates",
            "end_regex": r"^\s*48\.\s+Registered documents relating to property",
            "locator": "REGISTRATION-ACT:SECTION:47",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "REGISTRATION_EFFECT",
            "section": "Section 47",
            "citation": "Registration Act, 1908, Section 47, as transcribed in the supplied B.S.R. Registration Manual (2007 edition indicated; unverified)",
        },
        {
            "filename": "BSR - Registration Act 1908.txt",
            "start": "49. Effect of non-registration of documents required to be regis-",
            "end_regex": r"^\s*NOTES\s*$",
            "locator": "REGISTRATION-ACT:SECTION:49",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "REGISTRATION_EFFECT",
            "section": "Section 49",
            "citation": "Registration Act, 1908, Section 49, as transcribed in the supplied B.S.R. Registration Manual (2007 edition indicated; unverified)",
        },
        {
            "filename": "BSR - Registration Act 1908.txt",
            "start": "NOTES",
            "start_line_hint": 3599,
            "end_regex": r"^\s*50\.\s+Certain registered documents relating to land",
            "locator": "REGISTRATION-ACT:SECTION:49:EDITORIAL-NOTES",
            "type": "EDITORIAL_NOTE",
            "domain": "REGISTRATION_EFFECT",
            "section": "Section 49 (editorial notes and case law)",
            "citation": "B.S.R. Registration Manual notes and case-law references following Section 49; secondary material, 2007 edition indicated but unverified",
            "commentary": True,
        },
        {
            "filename": "BSR - Registration Act 1908.txt",
            "start": "78. Fees to be fixed by State Government",
            "end_regex": r"^\s*79\.\s+Publication of Fees",
            "locator": "REGISTRATION-ACT:SECTION:78",
            "type": "STATUTORY_SECTION_TRANSCRIPTION",
            "domain": "REGISTRATION_FEES",
            "section": "Section 78",
            "citation": "Registration Act, 1908, Section 78, as transcribed in the supplied B.S.R. Registration Manual (2007 edition indicated; unverified)",
        },
    ]
    provisions = []
    for spec in specifications:
        extraction = extraction_by_name.get(spec["filename"])
        if not extraction:
            raise ValueError(f"Extraction audit missing for {spec['filename']}")
        text_path = APP_ROOT / extraction["extracted_text_local_path"]
        content, line_start, line_end = extract_until(
            text_path, spec["start"], spec["end_regex"], spec.get("start_line_hint")
        )
        pii_counts = [len(pattern.findall(content)) for pattern in pii_patterns]
        if any(pii_counts):
            raise ValueError(f"Possible personal data detected in selected excerpt {spec['locator']}; staging stopped without storing its text.")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_id = source_ids[spec["filename"]]
        provision_id = stable_id(source_id, spec["locator"], content_hash)
        source_is_quarantined = source_fields[spec["filename"]]["review_status"] == "QUARANTINED"
        provision = {
            "id": provision_id,
            "source_document_id": source_id,
            "parent_provision_id": None,
            "provision_type": spec["type"],
            "legal_domain": spec["domain"],
            "locator_key": spec["locator"],
            "section_label": spec.get("section"),
            "article_label": spec.get("article"),
            "heading": spec["citation"],
            "citation": spec["citation"],
            "content": content,
            "content_sha256": content_hash,
            "language": "en",
            "page_start": None,
            "page_end": None,
            "line_start": line_start,
            "line_end": line_end,
            "effective_from": None,
            "effective_to": None,
            "authority_status": "SECONDARY_COMPILATION_UNVERIFIED",
            "extraction_quality": "TEXT_EXTRACTED_UNREVIEWED",
            "review_status": "QUARANTINED" if source_is_quarantined else "PENDING_REVIEW",
            "is_secondary_commentary": spec.get("commentary", False),
            "metadata_json": {
                "source_edition_verified": False,
                "source_quality_note": "Compiled B.S.R. manual; verify against an official Act/rules publication and amendments.",
                "source_line_personal_data_pattern_counts": pii_counts,
                "source_wide_privacy_flag": source_is_quarantined,
            },
        }
        provisions.append(provision)
    return provisions


def core_stamp_act_provisions(
    extraction_by_name: dict[str, dict],
    source_ids: dict[str, str],
    source_fields: dict[str, dict],
) -> list[dict]:
    """Stage the early Stamp Act sections as separate, line-verified records."""
    filename = "BSR - Indian Stamp Act 1899(1).txt"
    extraction = extraction_by_name.get(filename)
    if not extraction:
        raise ValueError(f"Extraction audit missing for {filename}")
    path = APP_ROOT / extraction["extracted_text_local_path"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    units = [
        ("1", 1163, 1176, "1. Short Title", "2. Definitions", "STAMP_DUTY_GENERAL", True, False),
        ("2", 1176, 1196, "2. Definitions", "3. Instruments chargeable", "STAMP_DUTY_GENERAL", True, False),
        ("3", 1196, 1341, "3. Instruments chargeable", "3A. Deleted", "STAMP_DUTY_GENERAL", True, False),
        ("3A", 1341, 1385, "3A. Deleted", "5. Instruments relating", "STAMP_DUTY_GENERAL", True, True),
        ("5", 1385, 1401, "5. Instruments relating", "6. Instruments coming", "STAMP_DUTY_GENERAL", True, False),
        ("6", 1401, 1511, "6. Instruments coming", "7. Policies of Sea Insurance", "STAMP_DUTY_GENERAL", True, False),
        ("7", 1511, 1526, "7. Policies of Sea Insurance", "8. Bonds, debentures", "STAMP_DUTY_GENERAL", True, False),
        ("8", 1526, 1595, "8. Bonds, debentures", "8A. Securities Dealt", "STAMP_DUTY_GENERAL", True, False),
        ("8A", 1595, 1646, "8A. Securities Dealt", "8B. Corporatisation", "STAMP_DUTY_GENERAL", True, False),
        ("8B", 1646, 1717, "8B. Corporatisation", "9. Power to reduce", "STAMP_DUTY_GENERAL", True, False),
        ("9", 1717, 1763, "9. Power to reduce", "10. Duties how", "STAMP_DUTY", False, False),
        ("10", 1763, 1805, "10. Duties how", "10A. Payment of duty", "STAMP_DUTY_PAYMENT", True, False),
        ("10A", 1805, 1901, "10A. Payment of duty", "11. Use of adhesive stamps", "STAMP_DUTY_PAYMENT", True, False),
        ("11", 1901, 1926, "11. Use of adhesive stamps", "12. Cancellation of adhesive stamps", "STAMP_DUTY_PAYMENT", True, False),
        ("12", 1926, 1992, "12. Cancellation of adhesive stamps", "13. Instruments stamped with impressed", "STAMP_DUTY_PAYMENT", True, False),
        ("13", 1992, 2007, "13. Instruments stamped with impressed stamps", "14. Only one instrument", "STAMP_DUTY_PAYMENT", True, False),
        ("14", 2007, 2048, "14. Only one instrument to be on same stamp", "16. Denoting duty", "STAMP_DUTY_PAYMENT", True, False),
        ("16", 2048, 2135, "16. Denoting duty", "17. Instruments executed in India", "STAMP_DUTY_PAYMENT", True, False),
        ("17", 2135, 2156, "17. Instruments executed in India", "18. Instruments other than bills", "STAMP_DUTY_PAYMENT", True, False),
        ("18", 2156, 2189, "18. Instruments other than bills and notes", "19. Bills and notes drawn", "STAMP_DUTY_PAYMENT", True, False),
        ("19", 2189, 2210, "19. Bills and notes drawn out of India", "19A. Payment of duty", "STAMP_DUTY_PAYMENT", True, False),
        ("19A", 2210, 2296, "19A. Payment of duty on certain instruments", "20. Conversion of amount", "STAMP_DUTY_PAYMENT", True, False),
        ("20", 2296, 2321, "20. Conversion of amount expressed", "21. Stock and marketable securities", "STAMP_DUTY_VALUATION", True, False),
        ("21", 2321, 2337, "21. Stock and marketable securities", "22. Effect of Statement", "STAMP_DUTY_VALUATION", True, False),
        ("22", 2337, 2356, "22. Effect of Statement of rate", "23. Instruments reserving interest", "STAMP_DUTY_VALUATION", True, False),
        ("23", 2356, 2362, "23. Instruments reserving interest", "23A. Certain instruments", "STAMP_DUTY_VALUATION", True, False),
        ("23A", 2362, 2408, "23A. Certain instruments connected", "24. How transfer in consideration", "STAMP_DUTY_VALUATION", True, False),
        ("24", 2408, 2450, "24. How transfer in consideration of debt", "25. Valuation in case", "STAMP_DUTY_VALUATION", True, False),
        ("25", 2450, 2533, "25. Valuation in case of annuity", "26. Stamp where value", "STAMP_DUTY_VALUATION", True, False),
        ("26", 2533, 2578, "26. Stamp where value of subject matter", "27. Facts affecting duty", "STAMP_DUTY_VALUATION", True, False),
        ("27", 2578, 2601, "27. Facts affecting duty to be setforth", "28. Direction as to duty", "STAMP_DUTY_VALUATION", True, False),
        ("28", 2601, 2723, "28. Direction as to duty in case", "29. Duty by whom payable", "STAMP_DUTY_VALUATION", True, False),
        ("29", 2723, 2832, "29. Duty by whom payable", "30. Obligation to give receipt", "STAMP_DUTY_LIABILITY", True, False),
        ("30", 2832, 2863, "30. Obligation to give receipt", "31. Adjudication as to proper stamp", "STAMP_DUTY_LIABILITY", True, False),
        ("31", 2863, 2949, "31. Adjudication as to proper stamp", "32. Certificate by the Collector", "STAMP_DUTY_ADJUDICATION", True, False),
        ("32", 2949, 3009, "32. Certificate by the Collector", "33. Examination and impounding", "STAMP_DUTY_ADJUDICATION", True, False),
        ("33", 3009, 3074, "33. Examination and impounding of instruments", "34. Special provision", "STAMP_DUTY_ADJUDICATION", True, False),
        ("34", 3074, 3090, "34. Special provision as to unstamped receipts", "35. Instruments not duly stamped", "STAMP_DUTY_ADJUDICATION", True, False),
        ("35", 3090, 3181, "35. Instruments not duly stamped", "36. Admission of instrument", "STAMP_DUTY_ADJUDICATION", True, False),
        ("36", 3181, 3201, "36. Admission of instrument where", "37. Admission of improperly stamped", "STAMP_DUTY_ADJUDICATION", True, False),
        ("37", 3201, 3218, "37. Admission of improperly stamped instruments", "38. Instruments impounded", "STAMP_DUTY_ADJUDICATION", True, False),
        ("38", 3218, 3252, "38. Instruments impounded, how dealt", "39. Collector's power", "STAMP_DUTY_ADJUDICATION", True, False),
        ("39", 3252, 3270, "39. Collector's power to refund penalty", "40. Collector's power to Stamp", "STAMP_DUTY_ADJUDICATION", True, False),
        ("40", 3270, 3340, "40. Collector's power to Stamp instrument", "41. Instruments unduly stamped", "STAMP_DUTY_ADJUDICATION", True, False),
        ("41", 3340, 3370, "41. Instruments unduly stamped by accident", "41A. Recovery of Stamp Duty", "STAMP_DUTY_ADJUDICATION", True, False),
        ("41A", 3370, 3838, "41A. Recovery of Stamp Duty not levied", "42. Endorsement of instruments", "STAMP_DUTY_ADJUDICATION", True, False),
        ("42", 3838, 3876, "42. Endorsement of instruments", "43. Prosecution for offence", "STAMP_DUTY_ADJUDICATION", True, False),
        ("43", 3876, 3979, "43. Prosecution for offence", "45. Power to Revenue Authority", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("45", 3979, 4018, "45. Power to Revenue Authority", "46. Non-liability for loss", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("46", 4018, 4040, "46. Non-liability for loss", "47. Power of payer", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("47", 4040, 4090, "47. Power of payer to stamp bills", "47A, Instruments of conveyance", "STAMP_DUTY_PAYMENT", True, False),
        ("48", 4363, 4410, "48. Recovery of duties and penalties", "49. Allowance for spoiled stamps", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("49", 4410, 4555, "49. Allowance for spoiled stamps", "50. Application for relief", "STAMP_DUTY_REFUNDS", True, False),
        ("50", 4555, 4592, "50. Application for relief under Section 49", "51. Allowance in case", "STAMP_DUTY_REFUNDS", True, False),
        ("51", 4592, 4636, "51. Allowance in case of printed forms", "52. Allowance for misused", "STAMP_DUTY_REFUNDS", True, False),
        ("52", 4636, 4688, "52. Allowance for misused Stamps", "53. Allowance for spoiled", "STAMP_DUTY_REFUNDS", True, False),
        ("53", 4688, 4734, "53. Allowance for spoiled or misused stamps", "54A. Allowances for stamps", "STAMP_DUTY_REFUNDS", True, False),
        ("54A", 4734, 4750, "54A. Allowances for stamps in denominations", "54B. Allowances for Refugee", "STAMP_DUTY_REFUNDS", True, False),
        ("54B", 4750, 4768, "54B. Allowances for Refugee Relief Stamps", "55. Allowance on renewal", "STAMP_DUTY_REFUNDS", True, False),
        ("55", 4768, 4826, "55. Allowance on renewal of certain debentures", "56. Control of", "STAMP_DUTY_REFUNDS", True, False),
        ("56", 4826, 4866, "56. Control of, and statement of case", "57. Statement of case", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("57", 4866, 4957, "57. Statement of case by Chief Controlling", "58. Power of High Court", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("58", 4957, 4971, "58. Power of High Court to call", "59. Procedure in disposing", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("59", 4971, 4989, "59. Procedure in disposing of case stated", "60. Statement of case by other Courts", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("60", 4989, 5018, "60. Statement of case by other Courts", "61. Revision of certain decisions", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("61", 5018, 5104, "61. Revision of certain decisions of Courts", "62. Penalty for executing", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("62", 5104, 5150, "62. Penalty for executing", "63. Penalty for failure", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("63", 5150, 5162, "63. Penalty for failure to cancel", "64. Penalty for omission", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("64", 5162, 5200, "64. Penalty for omission to comply", "64A. Recovery of amount", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("64A", 5200, 5227, "64A. Recovery of amount of deficit stamp duty", "65. Penalty for refusal", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("65", 5227, 5250, "65. Penalty for refusal to give receipt", "66. Penalty for not making", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("66", 5250, 5272, "66. Penalty for not making out policy", "67. Penalty for not drawing", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("67", 5272, 5313, "67. Penalty for not drawing full number", "68. Penalty for postdating", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("68", 5313, 5340, "68. Penalty for postdating bills", "69. Penalty for breach", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("69", 5340, 5365, "69. Penalty for breach of rule", "70. Institution and conduct", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("70", 5365, 5435, "70. Institution and conduct of prosecution", "71. Jurisdiction of Magistrates", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("71", 5435, 5446, "71. Jurisdiction of Magistrates", "72. Place of trial", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("72", 5446, 5459, "72. Place of trial", "73. Books", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("73", 5459, 5492, "73. Books etc., to be kept open", "75. Power to make rules", "STAMP_DUTY_ENFORCEMENT", True, False),
        ("75", 5492, 5498, "75. Power to make rules generally", "75A. Rules made", "STAMP_DUTY_RULEMAKING", True, False),
        ("75A", 5498, 5517, "75A. Rules made by the State Government", "76. Publication of rules", "STAMP_DUTY_RULEMAKING", True, False),
        ("76", 5517, 5528, "76. Publication of rules", "76A. Delegation", "STAMP_DUTY_RULEMAKING", True, False),
        ("76A", 5528, 5559, "76A. Delegation of certain powers", "77. Saving", "STAMP_DUTY_RULEMAKING", True, False),
        ("77", 5559, 5564, "77. Saving as to Court-fees", "77A. Saving", "STAMP_DUTY_GENERAL", True, False),
        ("77A", 5564, 5570, "77A. Saving as to certain stamps", "78. Duty to be paid", "STAMP_DUTY_GENERAL", True, False),
        ("78", 5570, 5579, "78. Duty to be paid", "79. Repeal", "STAMP_DUTY_GENERAL", True, False),
        ("79", 5579, 5596, "79. Repeal", "70 Schedule - 1", "STAMP_DUTY_GENERAL", True, True),
    ]
    pii_patterns = [
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
        re.compile(r"(?<!\d)\d{12}(?!\d)"),
        re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
    ]
    note_re = re.compile(r"^\s*(?:Elucidation|Notes?|Case Law|Savings\s*\()\s*:?", re.IGNORECASE)
    source_id = source_ids[filename]
    source_is_quarantined = source_fields[filename]["review_status"] == "QUARANTINED"
    output: list[dict] = []

    def add_record(section: str, start: int, end: int, locator: str, is_note: bool, historical: bool, domain: str) -> None:
        content = " ".join(line.strip() for line in lines[start:end] if line.strip())
        if not content:
            raise ValueError(f"Empty source span for {locator}")
        pii_counts = [len(pattern.findall(content)) for pattern in pii_patterns]
        if any(pii_counts):
            raise ValueError(f"Possible personal data detected in {locator}; staging stopped without storing its text.")
        citation = (
            f"B.S.R. compilation notes following Indian Stamp Act, 1899, Section {section}; edition unverified"
            if is_note else
            f"Indian Stamp Act, 1899, Section {section}, as transcribed in the supplied B.S.R. compilation (edition unverified)"
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        metadata = {
            "source_edition_verified": False,
            "source_quality_note": "Compiled B.S.R. manual; verify against an official Act and amendments.",
            "line_boundary_method": "explicit heading locators checked against extracted source lines",
            "source_line_personal_data_pattern_counts": pii_counts,
        }
        if historical:
            metadata["historical_status_note"] = "The supplied compilation identifies this material as repealed or a historical saving; legal effect/date not independently verified."
        provision_type = "STATUTORY_HISTORY_NOTE" if historical else "EDITORIAL_NOTE" if is_note else "STATUTORY_SECTION_TRANSCRIPTION"
        output.append({
            "id": stable_id(source_id, locator, digest),
            "source_document_id": source_id,
            "parent_provision_id": None,
            "provision_type": provision_type,
            "legal_domain": domain,
            "locator_key": locator,
            "section_label": f"Section {section}" + (" (editorial notes)" if is_note else ""),
            "heading": citation,
            "citation": citation,
            "content": content,
            "content_sha256": digest,
            "language": "en",
            "page_start": None,
            "page_end": None,
            "line_start": start + 1,
            "line_end": end,
            "effective_from": None,
            "effective_to": None,
            "authority_status": "SECONDARY_COMPILATION_UNVERIFIED",
            "extraction_quality": "TEXT_EXTRACTED_UNREVIEWED",
            "review_status": "QUARANTINED" if source_is_quarantined else "PENDING_REVIEW",
            "is_secondary_commentary": is_note or historical,
            "metadata_json": metadata,
        })

    for section, start_line, end_line, start_marker, end_marker, domain, include_text, historical in units:
        start = start_line - 1
        end = end_line - 1
        if start < 0 or end > len(lines) or start >= end:
            raise ValueError(f"Invalid line boundaries for Section {section}")
        if start_marker.casefold() not in lines[start].casefold():
            raise ValueError(f"Section {section} start locator mismatch at line {start_line}")
        if end_marker.casefold() not in lines[end].casefold():
            raise ValueError(f"Section {section} end locator mismatch at line {end_line}")
        note_start = next((i for i in range(start + 1, end) if note_re.search(lines[i])), None)
        if include_text:
            text_end = note_start if note_start is not None else end
            locator = f"INDIAN-STAMP-ACT:SECTION:{section}"
            if historical:
                locator += ":HISTORICAL-TEXT"
            add_record(section, start, text_end, locator, False, historical, domain)
        if note_start is not None:
            add_record(
                section,
                note_start,
                end,
                f"INDIAN-STAMP-ACT:SECTION:{section}:EDITORIAL-NOTES",
                True,
                historical,
                domain,
            )
    return output


def upsert(session: Session, model, identifier: str, fields: dict) -> None:
    record = session.get(model, identifier)
    if record is None and model.__name__ == "LegalProvision":
        record = session.scalar(
            select(model).where(
                model.source_document_id == fields["source_document_id"],
                model.locator_key == fields["locator_key"],
            )
        )
    if record is None:
        insert_fields = {key: value for key, value in fields.items() if key != "id"}
        record = model(id=identifier, **insert_fields)
        session.add(record)
        return
    for key, value in fields.items():
        if key == "id" and record.id != identifier:
            continue
        setattr(record, key, value)


def main() -> None:
    if not STAGING_DB.is_file():
        raise SystemExit("Staging database is missing; run scripts/init_source_staging_db.py first.")
    sys.path.insert(0, str(APP_ROOT))
    from backend.app.db.staging_models import LegalProvision, SourceDocument

    authority = json.loads(AUTHORITY_REPORT.read_text(encoding="utf-8"))
    extraction = json.loads(EXTRACTION_AUDIT.read_text(encoding="utf-8"))
    pdf_audit = json.loads(PDF_AUDIT.read_text(encoding="utf-8"))
    extraction_by_name = {Path(row["source_relative_path"]).name: row for row in extraction["files"]}
    pdf_by_name = {Path(row["source_relative_to_gov_root"]).name: row for row in pdf_audit["documents"]}

    document_fields = {}
    document_ids = {}
    for review_record in authority["authority_review"]["records"]:
        filename = Path(review_record["source_path"]).name
        fields = source_document_fields(
            review_record,
            extraction_by_name.get(filename),
            pdf_by_name.get(filename),
        )
        document_fields[filename] = fields
        document_ids[filename] = fields["id"]

    provisions = go_provisions(pdf_by_name, document_ids)
    provisions.extend(manual_provisions(extraction_by_name, document_ids, document_fields))
    provisions.extend(core_stamp_act_provisions(extraction_by_name, document_ids, document_fields))
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())), echo=False)
    try:
        with Session(engine) as session, session.begin():
            for fields in document_fields.values():
                upsert(session, SourceDocument, fields["id"], fields)
            for provision in provisions:
                provision_id = provision["id"]
                upsert(session, LegalProvision, provision_id, provision)
        with Session(engine) as session:
            source_count = session.scalar(select(func.count()).select_from(SourceDocument)) or 0
            provision_count = session.scalar(select(func.count()).select_from(LegalProvision)) or 0
            quarantined_count = session.scalar(
                select(func.count()).select_from(SourceDocument).where(SourceDocument.review_status == "QUARANTINED")
            ) or 0
        print(f"Staged source documents: {source_count}; quarantined: {quarantined_count}.")
        print(f"Staged provision records: {provision_count} total; this run defined {len(provisions)} idempotent provisions.")
        print("Manual transcriptions remain secondary/unverified; sources with privacy-pattern hits stay in review, and selected excerpts are screened individually.")
        print("All staged records remain pending review; the live rate and legal-search stores were not changed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
