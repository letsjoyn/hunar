"""Stage selected redacted Dairy 2024 rate-source passages for review only.

All passages remain secondary evidence. This script never creates calculator
rate rows, embeddings, or live-source approvals.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

APP_ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAME = "Dairy 2024.txt"
TEXT_PATH = APP_ROOT / "data" / "raw" / "source_extraction" / "Dairy_2024.extracted.txt"
PAGE_INDEX = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
OUTPUT_DIR = APP_ROOT / "data" / "raw"

PII_PATTERNS = (
    ("email_like", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("aadhaar_like", re.compile(r"(?<!\d)\d{12}(?!\d)"), "[REDACTED_ID]"),
    ("pan_like", re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"), "[REDACTED_ID]"),
    ("india_mobile_like", re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
)
GROUPS = (
    {
        "first_page": 107,
        "last_page": 109,
        "locator": "DAIRY-2024:TEXT-PAGES:107-109:GO-463-TABLE-OF-FEES",
        "title": "G.O.Ms.No.463 Table of Fees as reproduced in Dairy 2024",
        "provision_type": "SECONDARY_RATE_TABLE_CANDIDATE",
        "legal_domain": "REGISTRATION_FEES",
        "citation": "Dairy 2024.txt (secondary diary), reproduced Table of Fees attributed to G.O.Ms.No.463 dated 17-08-2013, derived text page sequences 107-109; transcription and current applicability unverified",
    },
    {
        "first_page": 110,
        "last_page": 114,
        "locator": "DAIRY-2024:TEXT-PAGES:110-114:GO-463-FEE-TABLE-AMENDMENTS",
        "title": "Amendments to the Table of Fees after G.O.Ms.No.463 (secondary compilation)",
        "provision_type": "SECONDARY_GOVERNMENT_ORDER_AMENDMENT_DIGEST",
        "legal_domain": "REGISTRATION_FEES",
        "citation": "Dairy 2024.txt (secondary diary), amendments listed after the G.O.Ms.No.463 Table of Fees, derived text page sequences 110-114; individual orders and applicability unverified",
    },
    {
        "first_page": 127,
        "last_page": 131,
        "locator": "DAIRY-2024:TEXT-PAGES:127-131:STAMP-DUTY-AND-REGISTRATION-FEES-AT-A-GLANCE",
        "title": "Stamp Duty and Registration Fees at a Glance (unverified table)",
        "provision_type": "SECONDARY_RATE_TABLE_CANDIDATE",
        "legal_domain": "STAMP_DUTY_AND_REGISTRATION_FEES",
        "citation": "Dairy 2024.txt (secondary diary), Stamp Duty and Registration Fees at a Glance table, derived text page sequences 127-131; OCR/transcription and current applicability unverified",
    },
    {
        "first_page": 132,
        "last_page": 132,
        "locator": "DAIRY-2024:TEXT-PAGE:132:TRANSFER-DUTY-GO-CLAIMS",
        "title": "Transfer Duty G.O. claims (secondary summary)",
        "provision_type": "SECONDARY_GOVERNMENT_ORDER_DIGEST",
        "legal_domain": "TRANSFER_DUTY",
        "citation": "Dairy 2024.txt (secondary diary), transfer-duty G.O. summaries, derived text page sequence 132; cited orders not supplied here for independent verification",
    },
)
DIRECTORY_TERMS = (
    "phone directory", "mobile number", "blood group", "gas number", "lic policy",
    "seniority list", "date of birth", "personal details", "email address",
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def build_group(spec: dict, source_pages: list[str], page_meta: dict[int, dict]) -> dict:
    chunks = []
    page_details = []
    total_redactions = {name: 0 for name, _, _ in PII_PATTERNS}
    for page_number in range(spec["first_page"], spec["last_page"] + 1):
        metadata = page_meta[page_number]
        page_text = source_pages[page_number - 1]
        if metadata["directory_or_hr_indicator"]:
            raise ValueError(f"Directory/HR indicator on selected page {page_number}")
        normalized = page_text.casefold()
        if any(term in normalized for term in DIRECTORY_TERMS):
            raise ValueError(f"Directory/HR term found on selected page {page_number}")
        redaction_counts = {}
        for name, pattern, replacement in PII_PATTERNS:
            page_text, count = pattern.subn(replacement, page_text)
            redaction_counts[name] = count
            total_redactions[name] += count
        if any(pattern.search(page_text) for _, pattern, _ in PII_PATTERNS):
            raise ValueError(f"Identifier pattern remains on page {page_number}")
        chunks.append(
            f"\n--- SOURCE TEXT PAGE SEQUENCE {page_number}; "
            f"LINES {metadata['source_line_start']}-{metadata['source_line_end']} ---\n"
            f"{page_text}"
        )
        page_details.append({
            "page_sequence": page_number,
            "source_line_start": metadata["source_line_start"],
            "source_line_end": metadata["source_line_end"],
            "source_page_sha256": hashlib.sha256(
                source_pages[page_number - 1].encode("utf-8")
            ).hexdigest(),
            "redaction_counts_only": redaction_counts,
        })
    content = "\n".join(chunks).strip()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    output_name = f"dairy2024_candidate_pages_{spec['first_page']}_{spec['last_page']}.redacted.txt"
    output_path = OUTPUT_DIR / output_name
    output_path.write_text(content, encoding="utf-8")
    return {
        **spec,
        "content": content,
        "content_sha256": content_hash,
        "output_path": output_path,
        "page_details": page_details,
        "redaction_counts": total_redactions,
        "line_start": page_details[0]["source_line_start"],
        "line_end": page_details[-1]["source_line_end"],
    }


def main() -> None:
    page_index = json.loads(PAGE_INDEX.read_text(encoding="utf-8"))
    page_meta = {row["page_sequence"]: row for row in page_index["pages"]}
    source_pages = TEXT_PATH.read_text(encoding="utf-8", errors="replace").split("\f")
    if len(source_pages) != page_index["form_feed_page_count"]:
        raise ValueError("Source page segmentation changed since the privacy inventory")
    built = [build_group(spec, source_pages, page_meta) for spec in GROUPS]

    sys.path.insert(0, str(APP_ROOT))
    staging_models = importlib.import_module("backend.app.db.staging_models")
    LegalProvision = staging_models.LegalProvision
    SourceDocument = staging_models.SourceDocument
    StagingReviewEvent = staging_models.StagingReviewEvent

    engine = create_engine(
        URL.create("sqlite", database=str((APP_ROOT / "data" / "source_staging.db").resolve())),
        echo=False,
    )
    try:
        with Session(engine) as session, session.begin():
            original = session.scalar(
                select(SourceDocument).where(SourceDocument.original_filename == SOURCE_NAME)
            )
            if original is None:
                raise ValueError("Original Dairy 2024 source record not found")
            for group in built:
                relative_path = group["output_path"].relative_to(APP_ROOT).as_posix()
                source_hash = group["content_sha256"]
                source_id = stable_id(relative_path, source_hash)
                metadata = {
                    "derived_from_original_source_id": original.id,
                    "original_source_sha256": page_index["source_sha256"],
                    "page_details": group["page_details"],
                    "redaction_counts_only": group["redaction_counts"],
                    "rate_values_verified": False,
                    "effective_period_verified": False,
                    "not_for_calculation": True,
                    "requires_primary_order_cross_check": True,
                }
                source_fields = {
                    "source_path": relative_path,
                    "original_filename": f"Dairy 2024.txt — sanitized candidate pages {group['first_page']}-{group['last_page']}",
                    "source_sha256": source_hash,
                    "media_type": "text/plain",
                    "source_kind": "REDACTED_SECONDARY_EXTRACT",
                    "title": group["title"],
                    "issuer": None,
                    "document_number": None,
                    "legal_domain": group["legal_domain"],
                    "authority_classification": "SECONDARY",
                    "authenticity_status": "UNVERIFIED",
                    "issued_on": None,
                    "published_on": None,
                    "effective_from": None,
                    "effective_to": None,
                    "supersedes_document_id": None,
                    "extraction_status": "SANITIZED_TEXT_EXTRACTED",
                    "extracted_text_sha256": source_hash,
                    "ocr_status": "TEXT_TRANSCRIPTION_UNVERIFIED",
                    "ocr_language": "en",
                    "ocr_quality_summary": {
                        "page_sequence_start": group["first_page"],
                        "page_sequence_end": group["last_page"],
                        "manual_visual_review": False,
                    },
                    "privacy_status": (
                        "PATTERN_REDACTED_MANUAL_REVIEW_PENDING"
                        if any(group["redaction_counts"].values())
                        else "SCREENED_NO_PATTERN_MATCH_MANUAL_REVIEW_PENDING"
                    ),
                    "review_status": "PENDING_REVIEW",
                    "source_metadata": metadata,
                }
                source = session.get(SourceDocument, source_id)
                if source is None:
                    source = session.scalar(
                        select(SourceDocument).where(
                            SourceDocument.source_path == relative_path,
                            SourceDocument.source_sha256 == source_hash,
                        )
                    )
                if source is None:
                    source = SourceDocument(id=source_id, **source_fields)
                    session.add(source)
                else:
                    for key, value in source_fields.items():
                        setattr(source, key, value)

                provision_id = stable_id(source_id, group["locator"], group["content_sha256"])
                provision_fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": group["provision_type"],
                    "legal_domain": group["legal_domain"],
                    "locator_key": group["locator"],
                    "heading": group["title"],
                    "citation": group["citation"],
                    "content": group["content"],
                    "content_sha256": group["content_sha256"],
                    "language": "en",
                    "page_start": group["first_page"],
                    "page_end": group["last_page"],
                    "line_start": group["line_start"],
                    "line_end": group["line_end"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "SECONDARY_DIARY_UNVERIFIED",
                    "extraction_quality": "REDACTED_TEXT_UNVERIFIED",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "redaction_counts_only": group["redaction_counts"],
                        "rate_values_verified": False,
                        "effective_period_verified": False,
                        "not_for_calculation": True,
                        "requires_primary_order_cross_check": True,
                    },
                }
                provision = session.get(LegalProvision, provision_id)
                if provision is None:
                    provision = session.scalar(
                        select(LegalProvision).where(
                            LegalProvision.source_document_id == source_id,
                            LegalProvision.locator_key == group["locator"],
                        )
                    )
                if provision is None:
                    provision = LegalProvision(id=provision_id, **provision_fields)
                    session.add(provision)
                else:
                    for key, value in provision_fields.items():
                        setattr(provision, key, value)

                event_id = stable_id("STAGED_FOR_REVIEW", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_FOR_REVIEW",
                        reviewer_id=None,
                        note="Secondary redacted source passage; not approved for runtime or calculation.",
                        details={"original_source_id": original.id},
                    ))
        print(f"Staged secondary 2024 candidate passages: {len(built)}.")
        print("All records remain pending review; no rate rows or vectors were created.")
        for group in built:
            print(f"  {group['locator']}: source lines {group['line_start']}-{group['line_end']}; "
                  f"redactions={group['redaction_counts']}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
