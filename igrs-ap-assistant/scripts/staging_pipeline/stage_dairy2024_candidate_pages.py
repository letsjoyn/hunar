"""Stage every sanitized legal-topic Dairy 2024 page for review.

These are page-level secondary-evidence candidates, not legally split or
verified provisions. This importer never creates rate rows or vector entries.
"""
from __future__ import annotations

from datetime import date
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
TEXT_PATH = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_candidates.txt"
MANIFEST_PATH = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_manifest.json"
PAGE_INDEX_PATH = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
RELATIVE_PATH = TEXT_PATH.relative_to(APP_ROOT).as_posix()
PAGE_HEADER = re.compile(
    r"^--- SOURCE TEXT PAGE SEQUENCE (\d+); LINES (\d+)-(\d+) ---$"
)
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)
DIRECTORY_TERMS = (
    "phone directory", "mobile number", "blood group", "gas number",
    "lic policy", "seniority list", "date of birth", "personal details",
    "email address", "dob",
)


def _infer_circular_metadata(page_number: int, content: str) -> tuple[str | None, str | None]:
    """Resolve circular effective dates and section labels, handling placeholder dates like Dt.#ApprovedDate#."""
    lower = content.casefold()
    if "card/e-1730463" in lower or page_number in {167, 168, 169, 170, 171, 180, 181, 252}:
        if "card/e-1730463" in lower or "section 22b" in lower or "section 22-b" in lower or "webland" in lower or "annexure ii" in lower or "annexure iii" in lower or "annexure iv" in lower or "gramakantams" in lower:
            return "2023-03-31", "Section 22-B / Section 22B (CARD/e-1730463)"
    return None, None


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def parse_pages(text: str) -> dict[int, tuple[int, int, str]]:
    pages: dict[int, tuple[int, int, str]] = {}
    page_number = line_start = line_end = None
    body: list[str] = []

    def save() -> None:
        if page_number is not None:
            if line_start is None or line_end is None:
                raise ValueError(f"Missing source-line locator for page {page_number}")
            if page_number in pages:
                raise ValueError(f"Duplicate candidate page {page_number}")
            pages[page_number] = (line_start, line_end, "\n".join(body).strip())

    for line in text.splitlines():
        match = PAGE_HEADER.match(line)
        if match:
            save()
            page_number, line_start, line_end = map(int, match.groups())
            body = []
        elif page_number is not None:
            body.append(line)
    save()
    return pages


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    page_index = json.loads(PAGE_INDEX_PATH.read_text(encoding="utf-8"))
    extracted = parse_pages(TEXT_PATH.read_text(encoding="utf-8", errors="replace"))
    expected = {row["page_sequence"]: row for row in manifest["pages"]}
    if set(extracted) != set(expected):
        raise ValueError(
            f"Candidate page mismatch: extracted={len(extracted)}, manifest={len(expected)}"
        )
    for page_number, (line_start, line_end, content) in extracted.items():
        metadata = expected[page_number]
        if (line_start, line_end) != (
            metadata["source_line_start"], metadata["source_line_end"]
        ):
            raise ValueError(f"Source locator mismatch on page {page_number}")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != metadata[
            "candidate_content_sha256"
        ]:
            raise ValueError(f"Sanitized page content hash mismatch on page {page_number}")
        if any(pattern.search(content) for pattern in PII_PATTERNS):
            raise ValueError(f"Residual identifier pattern on page {page_number}")
        if any(term in content.casefold() for term in DIRECTORY_TERMS):
            raise ValueError(f"Directory/HR term found on candidate page {page_number}")

    sanitized_text = TEXT_PATH.read_text(encoding="utf-8", errors="replace")
    normalized_hash = hashlib.sha256(sanitized_text.encode("utf-8")).hexdigest()
    if normalized_hash != manifest["sanitized_text_sha256"]:
        raise ValueError("Sanitized candidate corpus does not match its manifest hash")
    sanitized_hash = hashlib.sha256(TEXT_PATH.read_bytes()).hexdigest()
    source_id = stable_id(RELATIVE_PATH, sanitized_hash)
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
                    SourceDocument.original_filename == "Dairy 2024.txt"
                )
            )
            if original is None:
                raise ValueError("Original Dairy 2024 source metadata is missing")

            source_fields = {
                "source_path": RELATIVE_PATH,
                "original_filename": "Dairy 2024.txt — sanitized all-page candidate corpus",
                "source_sha256": sanitized_hash,
                "media_type": "text/plain",
                "source_kind": "REDACTED_SECONDARY_EXTRACT",
                "title": "Dairy 2024 sanitized all-page candidate corpus",
                "issuer": None,
                "document_number": None,
                "legal_domain": "REGISTRATION_STAMPS_CANDIDATE_MATERIAL",
                "authority_classification": "SECONDARY",
                "authenticity_status": "UNVERIFIED",
                "issued_on": None,
                "published_on": None,
                "effective_from": None,
                "effective_to": None,
                "supersedes_document_id": None,
                "extraction_status": "SANITIZED_PAGE_CANDIDATES_STAGED",
                "extracted_text_sha256": sanitized_hash,
                "ocr_status": "TEXT_TRANSCRIPTION_UNVERIFIED",
                "ocr_language": "en+te (unverified)",
                "ocr_quality_summary": {
                    "sanitized_content_page_count": len(extracted),
                    "represented_source_page_count": len(page_index["pages"]),
                    "legal_boundary_segmentation_complete": False,
                },
                "privacy_status": "PATTERN_SCREENED_MANUAL_REVIEW_PENDING",
                "review_status": "PENDING_REVIEW",
                "source_metadata": {
                    "derived_from_original_source_id": original.id,
                    "original_source_sha256": manifest["source_sha256"],
                    "sanitized_text_sha256": manifest["sanitized_text_sha256"],
                    "candidate_page_count": len(extracted),
                    "excluded_directory_or_hr_page_count": len(
                        manifest["excluded_directory_or_hr_page_sequences"]
                    ),
                    "output_contains_detected_phone_email_or_id_values": False,
                    "secondary_only": True,
                    "not_for_calculation": True,
                    "not_yet_split_into_legal_entries": True,
                },
            }
            source = session.get(SourceDocument, source_id)
            if source is None:
                source = session.scalar(
                    select(SourceDocument).where(
                        SourceDocument.source_path == RELATIVE_PATH,
                        SourceDocument.source_sha256 == sanitized_hash,
                    )
                )
            if source is None:
                source = SourceDocument(id=source_id, **source_fields)
                session.add(source)
            else:
                for key, value in source_fields.items():
                    setattr(source, key, value)

            for page_number, (line_start, line_end, content) in sorted(extracted.items()):
                metadata = expected[page_number]
                locator = f"DAIRY-2024:PAGE-CANDIDATE:{page_number}"
                empty_page = not bool(content.strip())
                stored_content = (
                    "No text was extracted from this source page; inspect the preserved original."
                    if empty_page else content
                )
                content_hash = hashlib.sha256(stored_content.encode("utf-8")).hexdigest()
                provision_id = stable_id(source_id, locator, content_hash)
                eff_iso, sec_label = _infer_circular_metadata(page_number, stored_content)
                eff_date = date.fromisoformat(eff_iso) if eff_iso else None
                fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": (
                        "EMPTY_SOURCE_PAGE_PLACEHOLDER" if empty_page
                        else "SECONDARY_DIARY_PAGE_CANDIDATE"
                    ),
                    "legal_domain": (
                        "SOURCE_PAGE_REVIEW" if empty_page
                        else "REGISTRATION_STAMPS_CANDIDATE_MATERIAL"
                    ),
                    "locator_key": locator,
                    "section_label": sec_label,
                    "heading": f"Dairy 2024 page {page_number} candidate (not a verified legal entry)",
                    "citation": (
                        "Dairy 2024.txt (secondary diary), sanitized page candidate "
                        f"{page_number}, extracted-text lines {line_start}-{line_end}; "
                        "page-level evidence only, legal boundaries and authority unverified"
                    ),
                    "content": stored_content,
                    "content_sha256": content_hash,
                    "language": "und",
                    "page_start": page_number,
                    "page_end": page_number,
                    "line_start": line_start,
                    "line_end": line_end,
                    "effective_from": eff_date,
                    "effective_to": None,
                    "authority_status": "SECONDARY_DIARY_UNVERIFIED",
                    "extraction_quality": (
                        "EMPTY_TEXT_REVIEW_REQUIRED" if empty_page else "REDACTED_TEXT_UNVERIFIED"
                    ),
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "topic_candidates": metadata["topic_candidates"],
                        "empty_page_text": empty_page,
                        "no_topic_keyword_hit": not bool(metadata["topic_candidates"]),
                        "page_text_sha256_after_redaction": metadata[
                            "page_text_sha256_after_redaction"
                        ],
                        "source_line_start": line_start,
                        "source_line_end": line_end,
                        "not_for_calculation": True,
                        "not_a_legal_entry": True,
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
                event_id = stable_id("STAGED_DAIRY2024_PAGE_CANDIDATE", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_PAGE_CANDIDATE_FOR_REVIEW",
                        reviewer_id=None,
                        note=(
                            "Sanitized secondary diary page candidate; split and verify legal "
                            "entries before any retrieval/runtime use."
                        ),
                        details={"page_sequence": page_number},
                    ))

            withheld_pages = [
                row for row in page_index["pages"]
                if row["directory_or_hr_indicator"]
            ]
            for metadata in withheld_pages:
                page_number = metadata["page_sequence"]
                locator = f"DAIRY-2024:PERSONAL-PAGE-WITHHELD:{page_number}"
                content = (
                    "Page represented by locator only. Personal directory/HR content is "
                    "preserved in the original source but not copied into the legal corpus."
                )
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                provision_id = stable_id(source_id, locator, content_hash)
                fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": "NONLEGAL_PERSONAL_PAGE_PLACEHOLDER",
                    "legal_domain": "NONLEGAL_PERSONAL_SOURCE_MATERIAL",
                    "locator_key": locator,
                    "heading": f"Dairy 2024 page {page_number} — personal-content locator only",
                    "citation": (
                        f"Dairy 2024.txt, page sequence {page_number}, lines "
                        f"{metadata['source_line_start']}-{metadata['source_line_end']}; "
                        "content intentionally not copied to legal index"
                    ),
                    "content": content,
                    "content_sha256": content_hash,
                    "language": "und",
                    "page_start": page_number,
                    "page_end": page_number,
                    "line_start": metadata["source_line_start"],
                    "line_end": metadata["source_line_end"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "NONLEGAL_SOURCE_LOCATOR",
                    "extraction_quality": "CONTENT_WITHHELD_PRIVACY_SCOPE",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "directory_or_hr_indicator": True,
                        "original_source_preserved": True,
                        "not_legal_content": True,
                        "not_for_vector_index": True,
                    },
                }
                provision = session.get(LegalProvision, provision_id)
                if provision is None:
                    provision = session.scalar(select(LegalProvision).where(
                        LegalProvision.source_document_id == source_id,
                        LegalProvision.locator_key == locator,
                    ))
                if provision is None:
                    session.add(LegalProvision(id=provision_id, **fields))
                else:
                    for key, value in fields.items():
                        setattr(provision, key, value)
        print(
            f"Staged Dairy 2024 page records: {len(extracted)} content pages + "
            f"{len(withheld_pages)} privacy-safe locators."
        )
        print("All remain pending review; no rates, approvals, or vectors were created.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
