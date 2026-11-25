"""Stage sanitized legal-topic Dairy 2010 pages as historical evidence candidates.

This does not approve historical rates, split rules, or create runtime vectors.
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
AUDIT_PATH = APP_ROOT / "data" / "raw" / "page_extraction_audit" / "page_extraction_audit.json"
INDEX_PATH = APP_ROOT / "data" / "raw" / "dairy2010_page_topic_index.json"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
OUTPUT_PATH = APP_ROOT / "data" / "raw" / "dairy2010_sanitized_candidates.txt"
PAGE_MARKER = re.compile(
    r"(?i)^\s*(?:[-=* ]*(?:page|pg\.?|folio)\s*[:#.-]?\s*\d+|\[page\s+\d+\])\s*[-=* ]*$"
)
PERSONAL_PAGE_TERMS = (
    "phone directory", "telephone directory", "mobile number", "blood group",
    "gas number", "lic policy", "seniority list", "date of birth", "dob",
    "personal details", "email address", "staff directory",
)
REDACTIONS = (
    ("email_like", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("india_mobile_like", re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    ("aadhaar_like", re.compile(r"(?<!\d)\d{12}(?!\d)"), "[REDACTED_ID]"),
    ("pan_like", re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"), "[REDACTED_ID]"),
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def main() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    inventory = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    document = next(
        row for row in audit["documents"]
        if Path(row["source_relative_to_gov_root"]).name == "Dairy 2010.pdf"
    )
    if document["source_sha256"] != inventory["source_sha256"]:
        raise ValueError("Dairy 2010 source hash changed since the page inventory")
    text_path = APP_ROOT / document["extracted_text_local_path"]
    lines = text_path.read_text(encoding="utf-8", errors="replace").splitlines()
    marker_indexes = [i for i, line in enumerate(lines) if PAGE_MARKER.match(line)]
    if len(marker_indexes) != inventory["page_count"]:
        raise ValueError("PDF page-marker count does not match the page inventory")

    prepared: list[dict] = []
    for row in inventory["pages"]:
        page_index = row["page"] - 1
        start = marker_indexes[page_index]
        end = marker_indexes[page_index + 1] if page_index + 1 < len(marker_indexes) else len(lines)
        page_text = "\n".join(lines[start + 1:end])
        if hashlib.sha256(page_text.encode("utf-8")).hexdigest() != row["page_text_sha256"]:
            raise ValueError(f"Source text hash mismatch on PDF page {row['page']}")
        counts = {name: 0 for name, _, _ in REDACTIONS}
        safe_lines = []
        for line in page_text.splitlines():
            line_matches = False
            for name, pattern, _replacement in REDACTIONS:
                count = len(pattern.findall(line))
                counts[name] += count
                line_matches = line_matches or count > 0
            safe_lines.append(
                "[REDACTED_CONTACT_OR_IDENTIFIER_LINE]" if line_matches else line
            )
        page_text = "\n".join(safe_lines)
        if any(pattern.search(page_text) for _, pattern, _ in REDACTIONS):
            raise ValueError(f"Identifier pattern remains on PDF page {row['page']}")
        privacy_withheld = any(term in page_text.casefold() for term in PERSONAL_PAGE_TERMS)
        empty_page = not bool(page_text.strip())
        content = (
            "Page represented by locator only. Personal directory/HR content is preserved "
            "in the original source but not copied into the legal corpus."
            if privacy_withheld else
            "No text was extracted from this source page; inspect the preserved original."
            if empty_page else page_text.strip()
        )
        extraction_quality = row["extraction_quality"]
        visual_review_note = None
        if row["page"] == 78:
            content = "GIST OF IMPORTANT CIRCULARS & ORDERS — section divider; printed folio 153."
            privacy_withheld = False
            empty_page = False
            extraction_quality = "VISUALLY_REVIEWED_DIVIDER"
            visual_review_note = (
                "Rendered original PDF page 78 and visually confirmed it is a section divider, "
                "not a substantive circular or rate table."
            )
        prepared.append({
            **row,
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "redaction_counts": counts,
            "privacy_withheld": privacy_withheld,
            "empty_page": empty_page,
            "extraction_quality": extraction_quality,
            "visual_review_note": visual_review_note,
        })

    output_text = "\n\n".join(
        f"--- SOURCE PDF PAGE {row['page']}; LINES "
        f"{row['source_line_start']}-{row['source_line_end']} ---\n{row['content']}"
        for row in prepared
    )
    OUTPUT_PATH.write_text(output_text, encoding="utf-8")
    output_hash = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    relative_path = OUTPUT_PATH.relative_to(APP_ROOT).as_posix()
    source_id = stable_id(relative_path, output_hash)

    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("backend.app.db.staging_models")
    SourceDocument = models.SourceDocument
    LegalProvision = models.LegalProvision
    StagingReviewEvent = models.StagingReviewEvent
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())))
    try:
        with Session(engine) as session, session.begin():
            original = session.scalar(
                select(SourceDocument).where(
                    SourceDocument.original_filename == "Dairy 2010.pdf"
                )
            )
            if original is None:
                raise ValueError("Original Dairy 2010 source metadata is missing")
            source_fields = {
                "source_path": relative_path,
                "original_filename": "Dairy 2010.pdf — sanitized all-page historical candidates",
                "source_sha256": output_hash,
                "media_type": "text/plain",
                "source_kind": "REDACTED_SECONDARY_EXTRACT",
                "title": "Dairy 2010 sanitized all-page historical candidate corpus",
                "issuer": None,
                "document_number": None,
                "legal_domain": "REGISTRATION_STAMPS_HISTORICAL_CANDIDATE_MATERIAL",
                "authority_classification": "SECONDARY",
                "authenticity_status": "UNVERIFIED",
                "issued_on": None,
                "published_on": None,
                "effective_from": None,
                "effective_to": None,
                "supersedes_document_id": None,
                "extraction_status": "SANITIZED_PAGE_CANDIDATES_STAGED",
                "extracted_text_sha256": output_hash,
                "ocr_status": "OCR_TEXT_UNVERIFIED",
                "ocr_language": "eng",
                "ocr_quality_summary": {
                    "source_page_count": len(prepared),
                    "low_confidence_pages": [
                        page for page in inventory["low_confidence_pages"] if page != 78
                    ],
                    "visually_reviewed_pages": [78],
                    "privacy_withheld_page_count": sum(row["privacy_withheld"] for row in prepared),
                    "legal_boundary_segmentation_complete": False,
                },
                "privacy_status": "PATTERN_REDACTED_MANUAL_REVIEW_PENDING",
                "review_status": "PENDING_REVIEW",
                "source_metadata": {
                    "derived_from_original_source_id": original.id,
                    "original_source_sha256": inventory["source_sha256"],
                    "extracted_text_sha256": inventory["extracted_text_sha256"],
                    "source_page_count": len(prepared),
                    "privacy_withheld_page_count": sum(row["privacy_withheld"] for row in prepared),
                    "no_topic_page_count": sum(not row["topic_candidates"] for row in prepared),
                    "redaction_counts_only": {
                        name: sum(row["redaction_counts"][name] for row in prepared)
                        for name, _, _ in REDACTIONS
                    },
                    "historical_source_only": True,
                    "not_for_calculation": True,
                    "not_yet_split_into_legal_entries": True,
                },
            }
            source = session.get(SourceDocument, source_id)
            if source is None:
                source = session.scalar(
                    select(SourceDocument).where(
                        SourceDocument.source_path == relative_path,
                        SourceDocument.source_sha256 == output_hash,
                    )
                )
            if source is None:
                source = SourceDocument(id=source_id, **source_fields)
                session.add(source)
            else:
                for key, value in source_fields.items():
                    setattr(source, key, value)

            for row in prepared:
                page = row["page"]
                locator = f"DAIRY-2010:PAGE-CANDIDATE:{page}"
                provision_id = stable_id(source_id, locator, row["content_sha256"])
                fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": (
                        "NONLEGAL_PERSONAL_PAGE_PLACEHOLDER" if row["privacy_withheld"]
                        else "EMPTY_SOURCE_PAGE_PLACEHOLDER" if row["empty_page"]
                        else "SECONDARY_DIARY_PAGE_CANDIDATE"
                    ),
                    "legal_domain": (
                        "NONLEGAL_PERSONAL_SOURCE_MATERIAL" if row["privacy_withheld"]
                        else "SOURCE_PAGE_REVIEW" if row["empty_page"]
                        else "REGISTRATION_STAMPS_HISTORICAL_CANDIDATE_MATERIAL"
                    ),
                    "locator_key": locator,
                    "heading": f"Dairy 2010 historical candidate page {page} (not a verified legal entry)",
                    "citation": (
                        f"Dairy 2010.pdf (secondary diary), PDF page {page}, extracted-text lines "
                        f"{row['source_line_start']}-{row['source_line_end']}; page-level evidence, "
                        "OCR and legal boundaries unverified"
                    ),
                    "content": row["content"],
                    "content_sha256": row["content_sha256"],
                    "language": "en",
                    "page_start": page,
                    "page_end": page,
                    "line_start": row["source_line_start"],
                    "line_end": row["source_line_end"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "SECONDARY_DIARY_HISTORICAL_UNVERIFIED",
                    "extraction_quality": row["extraction_quality"],
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "topic_candidates": row["topic_candidates"],
                        "redaction_counts_only": row["redaction_counts"],
                        "historical_only": True,
                        "privacy_withheld": row["privacy_withheld"],
                        "no_topic_keyword_hit": not bool(row["topic_candidates"]),
                        "original_source_preserved": True,
                        "empty_page_text": row["empty_page"],
                        "not_for_vector_index": row["privacy_withheld"] or row["empty_page"],
                        "not_for_calculation": True,
                        "not_a_legal_entry": True,
                        "requires_visual_review_if_low_confidence": (
                            row["extraction_quality"] == "LOW_CONFIDENCE_VISUAL_REVIEW_REQUIRED"
                        ),
                        "visual_review_note": row["visual_review_note"],
                        "requires_boundary_and_primary_source_review": True,
                    },
                }
                provision = session.get(LegalProvision, provision_id)
                if provision is None:
                    provision = session.scalar(
                        select(LegalProvision).where(
                            LegalProvision.source_document_id == source_id,
                            LegalProvision.locator_key == locator,
                        )
                    )
                if provision is None:
                    provision = LegalProvision(id=provision_id, **fields)
                    session.add(provision)
                else:
                    for key, value in fields.items():
                        setattr(provision, key, value)
                event_id = stable_id("STAGED_DAIRY2010_PAGE_CANDIDATE", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_HISTORICAL_PAGE_CANDIDATE_FOR_REVIEW",
                        reviewer_id=None,
                        note=(
                            "Sanitized 2010 secondary historical page; visually review OCR and "
                            "split/verify legal entries before retrieval use."
                        ),
                        details={"page": page},
                    ))
        print(
            f"Staged Dairy 2010 page records: {len(prepared)}; "
            f"personal-content placeholders: {sum(row['privacy_withheld'] for row in prepared)}."
        )
        print("All remain pending review; no rates, approvals, or vectors were created.")
        print("PDF page 78 visually reviewed: section divider, not a substantive rule or rate page.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
