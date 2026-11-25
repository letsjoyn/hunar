"""Stage G.O. citation references from redacted Dairy 2024 candidate pages.

Each record is reference-only: the diary citation is not the full G.O. text or
proof that the cited provision is currently in force.
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
SANITIZED_TEXT = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_candidates.txt"
PAGE_INDEX = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
DERIVED_PATH = APP_ROOT / "data" / "raw" / "dairy2024_go_reference_extracts.redacted.txt"
PAGE_HEADER = re.compile(
    r"^--- SOURCE TEXT PAGE SEQUENCE (\d+); LINES (\d+)-(\d+) ---$"
)
GO_REFERENCE = re.compile(
    r"(?i)\bG\s*\.?\s*[O0]\s*\.?\s*M[s5]?\s*\.?\s*No\s*\.?\s*([0-9]{1,5})"
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
    lines = SANITIZED_TEXT.read_text(encoding="utf-8", errors="replace").splitlines()
    page_index = json.loads(PAGE_INDEX.read_text(encoding="utf-8"))
    page_metadata = {row["page_sequence"]: row for row in page_index["pages"]}
    refs = []
    active_page = None
    source_line_start = None
    body_lines: list[tuple[int, str]] = []

    def flush_page() -> None:
        if active_page is None:
            return
        page_info = page_metadata[active_page]
        if page_info["directory_or_hr_indicator"]:
            return
        for index, (source_line, line) in enumerate(body_lines):
            for match in GO_REFERENCE.finditer(line):
                left = max(index - 1, 0)
                right = min(index + 2, len(body_lines))
                excerpt_lines = body_lines[left:right]
                excerpt = "\n".join(text for _, text in excerpt_lines).strip()
                if not excerpt or any(pattern.search(excerpt) for pattern in PII_PATTERNS):
                    continue
                refs.append({
                    "go_number": match.group(1),
                    "page_sequence": active_page,
                    "source_line_start": excerpt_lines[0][0],
                    "source_line_end": excerpt_lines[-1][0],
                    "matched_source_line": source_line,
                    "excerpt": excerpt,
                })

    for line in lines:
        header = PAGE_HEADER.match(line)
        if header:
            flush_page()
            active_page = int(header.group(1))
            source_line_start = int(header.group(2))
            body_lines = []
            continue
        if active_page is not None:
            if source_line_start is None:
                raise ValueError("Page body started without a source line locator")
            body_lines.append((source_line_start + len(body_lines), line))
    flush_page()
    return refs


def main() -> None:
    refs = collect_references()
    if not refs:
        raise SystemExit("No G.O. references found in sanitized candidate pages")
    derived_text = "\n\n".join(
        f"--- PAGE {r['page_sequence']}; LINES {r['source_line_start']}-{r['source_line_end']}; "
        f"G.O.Ms.No.{r['go_number']} ---\n{r['excerpt']}"
        for r in refs
    )
    DERIVED_PATH.write_text(derived_text, encoding="utf-8")
    derived_hash = hashlib.sha256(derived_text.encode("utf-8")).hexdigest()
    relative_path = DERIVED_PATH.relative_to(APP_ROOT).as_posix()
    source_id = stable_id(relative_path, derived_hash)

    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("backend.app.db.staging_models")
    SourceDocument = models.SourceDocument
    LegalProvision = models.LegalProvision
    StagingReviewEvent = models.StagingReviewEvent
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())), echo=False)
    try:
        with Session(engine) as session, session.begin():
            original = session.scalar(
                select(SourceDocument).where(SourceDocument.original_filename == "Dairy 2024.txt")
            )
            if original is None:
                raise ValueError("Original Dairy 2024 source metadata not found")
            source_fields = {
                "source_path": relative_path,
                "original_filename": "Dairy 2024.txt — redacted G.O. reference excerpts",
                "source_sha256": derived_hash,
                "media_type": "text/plain",
                "source_kind": "REDACTED_SECONDARY_CITATION_EXTRACT",
                "title": "G.O. citation references found in Dairy 2024",
                "issuer": None,
                "document_number": None,
                "legal_domain": "REGISTRATION_STAMP_DUTY_REFERENCES",
                "authority_classification": "SECONDARY",
                "authenticity_status": "UNVERIFIED",
                "issued_on": None,
                "published_on": None,
                "effective_from": None,
                "effective_to": None,
                "supersedes_document_id": None,
                "extraction_status": "SANITIZED_REFERENCE_EXTRACTED",
                "extracted_text_sha256": derived_hash,
                "ocr_status": "TEXT_TRANSCRIPTION_UNVERIFIED",
                "ocr_language": "en",
                "ocr_quality_summary": {"reference_count": len(refs)},
                "privacy_status": "SCREENED_NO_PATTERN_MATCH_MANUAL_REVIEW_PENDING",
                "review_status": "PENDING_REVIEW",
                "source_metadata": {
                    "derived_from_original_source_id": original.id,
                    "original_source_sha256": original.source_sha256,
                    "records_are_reference_only": True,
                    "full_gos_independently_verified": False,
                    "runtime_eligible": False,
                },
            }
            source = session.get(SourceDocument, source_id)
            if source is None:
                source = session.scalar(
                    select(SourceDocument).where(
                        SourceDocument.source_path == relative_path,
                        SourceDocument.source_sha256 == derived_hash,
                    )
                )
            if source is None:
                source = SourceDocument(id=source_id, **source_fields)
                session.add(source)
            else:
                for key, value in source_fields.items():
                    setattr(source, key, value)

            for ref in refs:
                locator = (
                    f"DAIRY-2024:GO-REFERENCE:{ref['go_number']}:"
                    f"PAGE-{ref['page_sequence']}:LINE-{ref['matched_source_line']}"
                )
                content = ref["excerpt"]
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                citation = (
                    f"Dairy 2024.txt (secondary diary), reference to G.O.Ms.No.{ref['go_number']}, "
                    f"derived text page sequence {ref['page_sequence']}, source lines "
                    f"{ref['source_line_start']}-{ref['source_line_end']}; citation only, full order unverified"
                )
                provision_id = stable_id(source_id, locator, content_hash)
                fields = {
                    "source_document_id": source_id,
                    "parent_provision_id": None,
                    "provision_type": "SECONDARY_GOVERNMENT_ORDER_REFERENCE",
                    "legal_domain": "REGISTRATION_STAMP_DUTY_REFERENCES",
                    "locator_key": locator,
                    "heading": f"Reference to G.O.Ms.No.{ref['go_number']} (not the order text)",
                    "citation": citation,
                    "content": content,
                    "content_sha256": content_hash,
                    "language": "en",
                    "page_start": ref["page_sequence"],
                    "page_end": ref["page_sequence"],
                    "line_start": ref["source_line_start"],
                    "line_end": ref["source_line_end"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "SECONDARY_DIARY_REFERENCE_UNVERIFIED",
                    "extraction_quality": "REDACTED_TEXT_UNVERIFIED",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "go_number_cited": ref["go_number"],
                        "reference_only": True,
                        "full_go_text_present": False,
                        "effective_date_verified": False,
                        "not_for_calculation": True,
                        "matched_source_line": ref["matched_source_line"],
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
                event_id = stable_id("STAGED_REFERENCE_FOR_REVIEW", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_REFERENCE_FOR_REVIEW",
                        reviewer_id=None,
                        note="Secondary G.O. citation reference only; do not treat as governing text.",
                        details={"go_number": ref["go_number"], "original_source_id": original.id},
                    ))
        print(f"Staged G.O. citation-reference records: {len(refs)}; unique cited numbers: "
              f"{len({r['go_number'] for r in refs})}.")
        print("References are pending review and are not full G.O. texts or calculator rates.")
        print(f"PII-pattern-free derived reference file: {relative_path}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
