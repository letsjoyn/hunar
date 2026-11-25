"""Stage citation-only G.O. references from sanitized Dairy 2010 pages."""
from __future__ import annotations

import hashlib
import importlib
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

APP_ROOT = Path(__file__).resolve().parents[2]
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
TEXT_PATH = APP_ROOT / "data" / "raw" / "dairy2010_sanitized_candidates.txt"
OUTPUT_PATH = APP_ROOT / "data" / "raw" / "dairy2010_go_reference_extracts.redacted.txt"
PAGE_HEADER = re.compile(r"^--- SOURCE PDF PAGE (\d+); LINES (\d+)-(\d+) ---$")
GO_REFERENCE = re.compile(
    r"(?i)\bG\s*\.?\s*O\s*\.?\s*(?P<kind>Ms|Rt|Memo)?\s*\.?\s*"
    r"No\s*\.?\s*(?P<number>[0-9]{1,5})"
)
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def collect_references() -> list[dict]:
    lines = TEXT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    refs = []
    active_page = None
    source_line_start = None
    page_lines: list[str] = []

    def flush() -> None:
        if active_page is None:
            return
        if source_line_start is None:
            raise ValueError(f"Missing source line locator for page {active_page}")
        if page_lines and page_lines[0].casefold().startswith("page represented by locator only"):
            return
        for index, line in enumerate(page_lines):
            for match in GO_REFERENCE.finditer(line):
                selected = []
                for local_index in range(max(0, index - 1), min(len(page_lines), index + 2)):
                    context_line = page_lines[local_index]
                    if any(pattern.search(context_line) for pattern in PII_PATTERNS):
                        context_line = "[REDACTED_CONTACT_OR_IDENTIFIER_LINE]"
                    selected.append((source_line_start + local_index, context_line))
                excerpt = "\n".join(text for _, text in selected).strip()
                if not excerpt or any(pattern.search(excerpt) for pattern in PII_PATTERNS):
                    continue
                kind = (match.group("kind") or "Ms").title()
                refs.append({
                    "number": match.group("number"),
                    "kind": kind,
                    "page": active_page,
                    "line": source_line_start + index,
                    "line_start": selected[0][0],
                    "line_end": selected[-1][0],
                    "excerpt": excerpt,
                })

    for line in lines:
        match = PAGE_HEADER.match(line)
        if match:
            flush()
            active_page = int(match.group(1))
            source_line_start = int(match.group(2))
            page_lines = []
        elif active_page is not None:
            page_lines.append(line)
    flush()
    return refs


def main() -> None:
    refs = collect_references()
    if not refs:
        raise SystemExit("No G.O. references found in sanitized Dairy 2010 pages")
    derived = "\n\n".join(
        f"--- PAGE {row['page']}; LINES {row['line_start']}-{row['line_end']}; "
        f"G.O.{row['kind']}.No.{row['number']} ---\n{row['excerpt']}"
        for row in refs
    )
    OUTPUT_PATH.write_text(derived, encoding="utf-8")
    digest = hashlib.sha256(derived.encode("utf-8")).hexdigest()
    relative_path = OUTPUT_PATH.relative_to(APP_ROOT).as_posix()
    source_id = stable_id(relative_path, digest)

    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("backend.app.db.staging_models")
    SourceDocument = models.SourceDocument
    LegalProvision = models.LegalProvision
    StagingReviewEvent = models.StagingReviewEvent
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())))
    try:
        with Session(engine) as session, session.begin():
            original = session.scalar(select(SourceDocument).where(
                SourceDocument.original_filename == "Dairy 2010.pdf"
            ))
            if original is None:
                raise ValueError("Original Dairy 2010 source metadata is missing")
            fields = {
                "source_path": relative_path,
                "original_filename": "Dairy 2010.pdf — redacted G.O. reference excerpts",
                "source_sha256": digest,
                "media_type": "text/plain",
                "source_kind": "REDACTED_SECONDARY_CITATION_EXTRACT",
                "title": "G.O./circular citations in Dairy 2010 (reference only)",
                "issuer": None,
                "document_number": None,
                "legal_domain": "REGISTRATION_STAMPS_HISTORICAL_REFERENCES",
                "authority_classification": "SECONDARY",
                "authenticity_status": "DEPARTMENT_SUPPLIED_SECONDARY_REFERENCE",
                "issued_on": None,
                "published_on": None,
                "effective_from": None,
                "effective_to": None,
                "supersedes_document_id": None,
                "extraction_status": "REDACTED_GOVERNMENT_ORDER_REFERENCES_STAGED",
                "extracted_text_sha256": digest,
                "ocr_status": "OCR_TEXT_UNVERIFIED",
                "ocr_language": "eng",
                "ocr_quality_summary": {"reference_count": len(refs)},
                "privacy_status": "CONTEXT_LINES_PATTERN_SCREENED",
                "review_status": "PENDING_REVIEW",
                "source_metadata": {
                    "derived_from_original_source_id": original.id,
                    "references_are_not_order_text": True,
                    "effective_dates_not_inferred": True,
                    "runtime_eligible": False,
                },
            }
            source = session.get(SourceDocument, source_id)
            if source is None:
                source = session.scalar(select(SourceDocument).where(
                    SourceDocument.source_path == relative_path,
                    SourceDocument.source_sha256 == digest,
                ))
            if source is None:
                source = SourceDocument(id=source_id, **fields)
                session.add(source)
            else:
                for key, value in fields.items():
                    setattr(source, key, value)

            for row in refs:
                locator = (
                    f"DAIRY-2010:GO-REFERENCE:{row['kind']}-{row['number']}:"
                    f"PAGE-{row['page']}:LINE-{row['line']}"
                )
                content_hash = hashlib.sha256(row["excerpt"].encode("utf-8")).hexdigest()
                provision_id = stable_id(source_id, locator, content_hash)
                provision_fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": "SECONDARY_GOVERNMENT_ORDER_REFERENCE",
                    "legal_domain": "REGISTRATION_STAMPS_HISTORICAL_REFERENCES",
                    "locator_key": locator,
                    "heading": f"Reference to G.O.{row['kind']}.No.{row['number']} (not order text)",
                    "citation": (
                        f"Dairy 2010.pdf, page {row['page']}, extracted lines "
                        f"{row['line_start']}-{row['line_end']}; citation only, full order and effective date unverified"
                    ),
                    "content": row["excerpt"],
                    "content_sha256": content_hash,
                    "language": "en",
                    "page_start": row["page"],
                    "page_end": row["page"],
                    "line_start": row["line_start"],
                    "line_end": row["line_end"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "SECONDARY_DIARY_REFERENCE_UNVERIFIED",
                    "extraction_quality": "REDACTED_TEXT_UNVERIFIED",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "go_kind": row["kind"],
                        "go_number": row["number"],
                        "full_order_text_present": False,
                        "effective_date_verified": False,
                        "not_for_calculation": True,
                    },
                }
                provision = session.get(LegalProvision, provision_id)
                if provision is None:
                    provision = session.scalar(select(LegalProvision).where(
                        LegalProvision.source_document_id == source_id,
                        LegalProvision.locator_key == locator,
                    ))
                if provision is None:
                    session.add(LegalProvision(id=provision_id, **provision_fields))
                else:
                    for key, value in provision_fields.items():
                        setattr(provision, key, value)
                event_id = stable_id("STAGED_2010_GO_REFERENCE", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_2010_GO_REFERENCE_FOR_REVIEW",
                        reviewer_id=None,
                        note="Secondary citation only; retrieve full order before interpreting its legal effect.",
                        details={"go_number": row["number"], "kind": row["kind"], "page": row["page"]},
                    ))
        print(f"Staged 2010 G.O. citation references: {len(refs)}.")
        print(f"Unique cited number/type pairs: {len({(r['kind'], r['number']) for r in refs})}.")
        print("References remain pending and are not full G.O. text or calculator rates.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
