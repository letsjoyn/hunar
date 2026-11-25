"""Stage one redacted 2024 diary table as secondary review evidence only.

This script does not create calculator-rate rows or vector embeddings.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

APP_ROOT = Path(__file__).resolve().parents[2]
TEXT_PATH = APP_ROOT / "data" / "raw" / "source_extraction" / "Dairy_2024.extracted.txt"
PAGE_INDEX = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"
SANITIZED_MANIFEST = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_manifest.json"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
PAGE_SEQUENCE = 133

REDACTIONS = (
    ("email_like", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("aadhaar_like", re.compile(r"(?<!\d)\d{12}(?!\d)"), "[REDACTED_ID]"),
    ("pan_like", re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"), "[REDACTED_ID]"),
    ("india_mobile_like", re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def main() -> None:
    page_index = json.loads(PAGE_INDEX.read_text(encoding="utf-8"))
    sanitized_manifest = json.loads(SANITIZED_MANIFEST.read_text(encoding="utf-8"))
    page_metadata = next(
        row for row in page_index["pages"] if row["page_sequence"] == PAGE_SEQUENCE
    )
    safe_metadata = next(
        row for row in sanitized_manifest["pages"] if row["page_sequence"] == PAGE_SEQUENCE
    )
    if page_metadata["directory_or_hr_indicator"]:
        raise ValueError("Selected page has a directory/HR indicator")
    if "stamp_duty_and_rates" not in page_metadata["topic_candidates"]:
        raise ValueError("Selected page is not tagged as a rate-topic candidate")

    source_pages = TEXT_PATH.read_text(encoding="utf-8", errors="replace").split("\f")
    page_text = source_pages[PAGE_SEQUENCE - 1]
    redaction_counts = {}
    for name, pattern, replacement in REDACTIONS:
        page_text, count = pattern.subn(replacement, page_text)
        redaction_counts[name] = count
    page_hash = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    if page_hash != safe_metadata["page_text_sha256_after_redaction"]:
        raise ValueError("Redacted page hash does not match the reviewed sanitized manifest")
    if any(pattern.search(page_text) for _, pattern, _ in REDACTIONS):
        raise ValueError("Detected identifier pattern remains after redaction")
    if "transfer duty" not in page_text.casefold():
        raise ValueError("Selected page no longer contains the expected table heading")

    relative_path = f"data/raw/dairy2024_sanitized_candidates.txt#text-page-{PAGE_SEQUENCE}"
    source_id = stable_id(relative_path, page_hash)
    locator = f"DAIRY-2024:TEXT-PAGE:{PAGE_SEQUENCE}:TRANSFER-DUTY-AT-A-GLANCE"
    citation = (
        "Dairy 2024.txt (secondary diary), ‘Transfer Duty at a Glance’ table, "
        f"derived text page sequence {PAGE_SEQUENCE}, source lines "
        f"{safe_metadata['source_line_start']}-{safe_metadata['source_line_end']}; "
        "pattern-redacted transcription, not primary authority"
    )
    provision_id = stable_id(source_id, locator, page_hash)
    source_metadata = {
        "derived_from_original_source_id": None,
        "original_source_sha256": page_index["source_sha256"],
        "derived_page_sequence": PAGE_SEQUENCE,
        "source_line_start": safe_metadata["source_line_start"],
        "source_line_end": safe_metadata["source_line_end"],
        "redaction_counts_only": redaction_counts,
        "rate_values_verified": False,
        "warning": "OCR/table-header interpretation requires primary-order and human review; not for calculation.",
    }

    sys.path.insert(0, str(APP_ROOT))
    from backend.app.db.staging_models import LegalProvision, SourceDocument

    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())), echo=False)
    try:
        with Session(engine) as session, session.begin():
            original = session.scalar(
                select(SourceDocument).where(SourceDocument.original_filename == "Dairy 2024.txt")
            )
            if original is None:
                raise ValueError("Original Dairy 2024 source record not found")
            source_metadata["derived_from_original_source_id"] = original.id
            source = session.get(SourceDocument, source_id)
            if source is None:
                source = session.scalar(
                    select(SourceDocument).where(
                        SourceDocument.source_path == relative_path,
                        SourceDocument.source_sha256 == page_hash,
                    )
                )
            source_fields = {
                "id": source_id,
                "source_path": relative_path,
                "original_filename": f"Dairy 2024.txt — sanitized candidate page {PAGE_SEQUENCE}",
                "source_sha256": page_hash,
                "media_type": "text/plain",
                "source_kind": "SANITIZED_SECONDARY_EXTRACT",
                "title": "Transfer Duty at a Glance — unverified candidate table",
                "issuer": None,
                "document_number": None,
                "legal_domain": "STAMP_DUTY_AND_REGISTRATION_FEES",
                "authority_classification": "SECONDARY",
                "authenticity_status": "UNVERIFIED",
                "issued_on": None,
                "published_on": None,
                "effective_from": None,
                "effective_to": None,
                "supersedes_document_id": None,
                "extraction_status": "SANITIZED_TEXT_EXTRACTED",
                "extracted_text_sha256": page_hash,
                "ocr_status": "TEXT_TRANSCRIPTION_UNVERIFIED",
                "ocr_language": "en",
                "ocr_quality_summary": {"page_sequence": PAGE_SEQUENCE, "manual_visual_review": False},
                "privacy_status": (
                    "PATTERN_REDACTED_MANUAL_REVIEW_PENDING"
                    if any(redaction_counts.values())
                    else "SCREENED_NO_PATTERN_MATCH_MANUAL_REVIEW_PENDING"
                ),
                "review_status": "PENDING_REVIEW",
                "source_metadata": source_metadata,
            }
            if source is None:
                source = SourceDocument(**{k: v for k, v in source_fields.items() if k != "id"}, id=source_id)
                session.add(source)
            else:
                for key, value in source_fields.items():
                    setattr(source, key, value)

            provision = session.get(LegalProvision, provision_id)
            if provision is None:
                provision = session.scalar(
                    select(LegalProvision).where(
                        LegalProvision.source_document_id == source_id,
                        LegalProvision.locator_key == locator,
                    )
                )
            fields = {
                "id": provision_id,
                "source_document_id": source_id,
                "parent_provision_id": None,
                "provision_type": "SECONDARY_RATE_TABLE_CANDIDATE",
                "legal_domain": "STAMP_DUTY_AND_REGISTRATION_FEES",
                "locator_key": locator,
                "heading": "Transfer Duty at a Glance (secondary table; unverified)",
                "citation": citation,
                "content": page_text.strip(),
                "content_sha256": hashlib.sha256(page_text.strip().encode("utf-8")).hexdigest(),
                "language": "en",
                "page_start": PAGE_SEQUENCE,
                "page_end": PAGE_SEQUENCE,
                "line_start": safe_metadata["source_line_start"],
                "line_end": safe_metadata["source_line_end"],
                "effective_from": None,
                "effective_to": None,
                "authority_status": "SECONDARY_DIARY_UNVERIFIED",
                "extraction_quality": "REDACTED_TEXT_UNVERIFIED",
                "review_status": "PENDING_REVIEW",
                "is_secondary_commentary": True,
                "metadata_json": {
                    "redaction_counts_only": redaction_counts,
                    "rate_values_verified": False,
                    "effective_period_verified": False,
                    "not_for_calculation": True,
                    "requires_primary_order_cross_check": True,
                    "source_line_locator_kind": "line numbers in supplied extracted text",
                },
            }
            if provision is None:
                provision = LegalProvision(**{k: v for k, v in fields.items() if k != "id"}, id=provision_id)
                session.add(provision)
            else:
                for key, value in fields.items():
                    setattr(provision, key, value)
        print("Staged one redacted 2024 transfer-duty table as pending secondary evidence.")
        print("No staged_deed_rates rows or vector embeddings were created.")
        print("Original Dairy 2024 source remains quarantined; this derived page is not approved.")
        print(f"Source line locator: {safe_metadata['source_line_start']}-{safe_metadata['source_line_end']}.")
        print(f"Redactions by pattern: {redaction_counts}.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
