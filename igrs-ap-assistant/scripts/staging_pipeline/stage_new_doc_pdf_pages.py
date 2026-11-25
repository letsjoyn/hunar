"""Stage locally OCR'd pages from the supplied 2025 New Doc PDF for review."""
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
AUDIT_DIR = APP_ROOT / "data" / "raw" / "page_extraction_audit"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
PAGE_MARKER = re.compile(r"^===== OCR PAGE (\d+) =====$")
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)
PERSONAL_TERMS = (
    "phone directory", "telephone directory", "mobile number", "blood group",
    "gas number", "lic policy", "seniority list", "date of birth", "personal details",
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def read_pages(path: Path) -> dict[int, str]:
    pages: dict[int, str] = {}
    current = None
    body: list[str] = []

    def save() -> None:
        if current is not None:
            if current in pages:
                raise ValueError(f"Duplicate OCR page {current}")
            pages[current] = "\n".join(body).strip()

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = PAGE_MARKER.match(line)
        if match:
            save()
            current = int(match.group(1))
            body = []
        elif current is not None:
            body.append(line)
    save()
    return pages


def main() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    document = next(
        row for row in audit["documents"]
        if Path(row["source_relative_to_gov_root"]).name == "New Doc 05-19-2025 17.40.pdf"
    )
    ocr_path = APP_ROOT / document.get("ocr_text_local_path", "")
    if not ocr_path.is_file():
        raise ValueError("Local OCR text for the supplied PDF is missing")
    pages = read_pages(ocr_path)
    expected_pages = {row["page"]: row for row in document["pages"]}
    if set(pages) != set(expected_pages):
        raise ValueError("OCR page set does not match the PDF page audit")

    prepared = []
    for page, text in sorted(pages.items()):
        redacted_lines = []
        redaction_count = 0
        for line in text.splitlines():
            matched = any(pattern.search(line) for pattern in PII_PATTERNS)
            if matched:
                redaction_count += 1
                redacted_lines.append("[REDACTED_CONTACT_OR_IDENTIFIER_LINE]")
            else:
                redacted_lines.append(line)
        safe_text = "\n".join(redacted_lines).strip()
        if any(pattern.search(safe_text) for pattern in PII_PATTERNS):
            raise ValueError(f"Identifier pattern remains on OCR page {page}")
        personal = any(term in safe_text.casefold() for term in PERSONAL_TERMS)
        content = (
            "Page represented by locator only. Personal content is preserved in the original."
            if personal else
            "No text was extracted; inspect the preserved original scan."
            if not safe_text else safe_text
        )
        prepared.append({
            "page": page,
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "redacted_lines": redaction_count,
            "personal": personal,
            "quality": expected_pages[page].get("ocr_status", "OCR_TEXT_UNVERIFIED"),
        })

    output_path = AUDIT_DIR / "New_Doc_05-19-2025_17.40.sanitized_ocr.pages.txt"
    output_text = "\n\n".join(
        f"--- SOURCE PDF PAGE {row['page']} ---\n{row['content']}" for row in prepared
    )
    output_path.write_text(output_text, encoding="utf-8")
    output_hash = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    relative_path = output_path.relative_to(APP_ROOT).as_posix()

    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("backend.app.db.staging_models")
    SourceDocument = models.SourceDocument
    LegalProvision = models.LegalProvision
    StagingReviewEvent = models.StagingReviewEvent
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())))
    try:
        with Session(engine) as session, session.begin():
            original = session.scalar(select(SourceDocument).where(
                SourceDocument.original_filename == "New Doc 05-19-2025 17.40.pdf"
            ))
            if original is None:
                raise ValueError("Original PDF source metadata is missing")
            source_id = stable_id(relative_path, output_hash)
            source_fields = {
                "source_path": relative_path,
                "original_filename": "New Doc 05-19-2025 17.40.pdf — sanitized local OCR pages",
                "source_sha256": output_hash,
                "media_type": "text/plain",
                "source_kind": "REDACTED_OCR_EXTRACT",
                "title": "Supplied New Doc PDF — local OCR candidate pages",
                "issuer": None,
                "document_number": None,
                "legal_domain": "REGISTRATION_PROCEDURE_CANDIDATE",
                "authority_classification": "UNVERIFIED",
                "authenticity_status": "USER_SUPPLIED_SOURCE_UNVERIFIED",
                "issued_on": None,
                "published_on": None,
                "effective_from": None,
                "effective_to": None,
                "supersedes_document_id": None,
                "extraction_status": "LOCAL_OCR_PAGES_STAGED",
                "extracted_text_sha256": output_hash,
                "ocr_status": "OCR_TEXT_UNVERIFIED",
                "ocr_language": "eng",
                "ocr_quality_summary": {
                    "page_count": len(prepared),
                    "ocr_engine": audit.get("ocr_run", {}).get("engine"),
                    "low_confidence_pages": [
                        row["page"] for row in prepared if row["quality"] != "ocr_text_detected"
                    ],
                },
                "privacy_status": "LINE_REDACTED_MANUAL_REVIEW_PENDING",
                "review_status": "PENDING_REVIEW",
                "source_metadata": {
                    "derived_from_original_source_id": original.id,
                    "original_pdf_sha256": document["source_sha256"],
                    "ocr_text_sha256": document.get("ocr_text_sha256"),
                    "page_count": len(prepared),
                    "redacted_line_count": sum(row["redacted_lines"] for row in prepared),
                    "personal_page_count": sum(row["personal"] for row in prepared),
                    "not_for_calculation": True,
                },
            }
            source = session.get(SourceDocument, source_id)
            if source is None:
                source = session.scalar(select(SourceDocument).where(
                    SourceDocument.source_path == relative_path,
                    SourceDocument.source_sha256 == output_hash,
                ))
            if source is None:
                source = SourceDocument(id=source_id, **source_fields)
                session.add(source)
            else:
                for key, value in source_fields.items():
                    setattr(source, key, value)

            for row in prepared:
                locator = f"NEW-DOC-PDF:PAGE:{row['page']}"
                provision_id = stable_id(source_id, locator, row["content_sha256"])
                is_locator_only = row["personal"] or row["quality"] != "ocr_text_detected"
                fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": "NONLEGAL_PERSONAL_PAGE_PLACEHOLDER" if row["personal"] else "SECONDARY_DOCUMENT_PAGE_CANDIDATE",
                    "legal_domain": "NONLEGAL_PERSONAL_SOURCE_MATERIAL" if row["personal"] else "REGISTRATION_PROCEDURE_CANDIDATE",
                    "locator_key": locator,
                    "heading": f"New Doc PDF OCR page {row['page']} candidate",
                    "citation": f"New Doc 05-19-2025 17.40.pdf, PDF page {row['page']}; locally OCR'd candidate, source authority and OCR text unverified",
                    "content": row["content"],
                    "content_sha256": row["content_sha256"],
                    "language": "en",
                    "page_start": row["page"],
                    "page_end": row["page"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "UNVERIFIED_SOURCE_CANDIDATE",
                    "extraction_quality": row["quality"],
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "redacted_contact_or_identifier_lines": row["redacted_lines"],
                        "personal_content_withheld": row["personal"],
                        "not_for_vector_index": is_locator_only,
                        "not_for_calculation": True,
                        "docx_derivative_also_supplied": True,
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
                event_id = stable_id("STAGED_NEW_DOC_PDF_PAGE", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_LOCAL_OCR_PAGE_FOR_REVIEW",
                        reviewer_id=None,
                        note="Supplied PDF OCR evidence; compare against the supplied DOCX and original scan.",
                        details={"page": row["page"], "ocr_status": row["quality"]},
                    ))
        print(f"Staged locally OCR'd PDF page records: {len(prepared)}.")
        print("OCR text remains unverified and pending; no live index or calculations changed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
