"""Split explicit legal-heading spans from the sanitized 2010/2024 diary pages.

These are secondary, OCR-derived review candidates, not approved legal entries.
The original page-level records remain intact as source evidence.
"""
from __future__ import annotations

from datetime import date
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
SOURCES = (
    {
        "filename": "Dairy 2024.txt — sanitized all-page candidate corpus",
        "relative_path": "data/raw/dairy2024_sanitized_candidates.txt",
        "header": re.compile(
            r"^--- SOURCE TEXT PAGE SEQUENCE (\d+); LINES (\d+)-(\d+) ---$"
        ),
        "locator_prefix": "DAIRY-2024",
        "domain": "REGISTRATION_STAMPS_2024_SECONDARY",
    },
    {
        "filename": "Dairy 2010.pdf — sanitized all-page historical candidates",
        "relative_path": "data/raw/dairy2010_sanitized_candidates.txt",
        "header": re.compile(r"^--- SOURCE PDF PAGE (\d+); LINES (\d+)-(\d+) ---$"),
        "locator_prefix": "DAIRY-2010",
        "domain": "REGISTRATION_STAMPS_2010_SECONDARY",
    },
)
HEADING = re.compile(
    r"^\s*(?P<label>"
    r"(?P<kind>section|sec\.?|rule|article|art\.?|schedule|chapter|part)"
    r"\s*(?P<number>\d+[A-Za-z]?(?:[-.][A-Za-z0-9]+)*(?:\([A-Za-z0-9]+\))*)"
    r"|(?P<go>g\.?\s*o\.?\s*(?:m[s5])?\.?\s*no\.?\s*\d+[A-Za-z/-]*)"
    r"|(?P<circular>circular\s+memo\s*no[:.]?\s*[A-Za-z0-9/\-]+)"
    r")\b(?P<title>.*)$",
    re.IGNORECASE,
)
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)
PLACEHOLDER_PREFIXES = (
    "page represented by locator only",
    "no text was extracted from this source page",
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def read_page_records(spec: dict) -> list[dict]:
    path = APP_ROOT / spec["relative_path"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pages = []
    page_number = line_start = line_end = None
    body: list[str] = []

    def save() -> None:
        if page_number is None:
            return
        if line_start is None or line_end is None:
            raise ValueError(f"Missing source locator in {spec['relative_path']}")
        content = "\n".join(body).strip()
        pages.append({
            "page": page_number,
            "line_start": line_start,
            "line_end": line_end,
            "content": content,
        })

    for line in lines:
        match = spec["header"].match(line)
        if match:
            save()
            page_number, line_start, line_end = map(int, match.groups())
            body = []
        elif page_number is not None:
            body.append(line)
    save()
    return pages


def label_fields(match: re.Match) -> tuple[str, str | None, str | None, str | None]:
    kind = (match.group("kind") or "").casefold().rstrip(".")
    number = match.group("number")
    go = match.group("go")
    if go:
        label = re.sub(r"\s+", "", go).upper()
        return label, None, None, None
    circular = match.groupdict().get("circular")
    if circular:
        label = re.sub(r"\s+", " ", circular).strip()
        sec_label = "Section 22-B / Section 22B" if "1730463" in label else None
        return label, sec_label, None, None
    label = f"{kind.title()} {number}"
    if kind in {"section", "sec"}:
        return label, label, None, None
    if kind == "rule":
        return label, None, label, None
    if kind in {"article", "art"}:
        return label, None, None, label
    return label, None, None, None


def main() -> None:
    prepared: list[dict] = []
    for spec in SOURCES:
        for page in read_page_records(spec):
            lowered = page["content"].casefold()
            if not page["content"] or any(
                lowered.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES
            ):
                continue
            matches = []
            page_lines = page["content"].splitlines()
            for index, line in enumerate(page_lines):
                match = HEADING.match(line)
                if match:
                    matches.append((index, match))
            for heading_index, (start, match) in enumerate(matches):
                end = matches[heading_index + 1][0] if heading_index + 1 < len(matches) else len(page_lines)
                content = "\n".join(page_lines[start:end]).strip()
                if not content or any(pattern.search(content) for pattern in PII_PATTERNS):
                    continue
                heading, section_label, rule_label, article_label = label_fields(match)
                source_line_start = page["line_start"] + start
                source_line_end = min(page["line_start"] + end - 1, page["line_end"])
                prepared.append({
                    **spec,
                    "page": page["page"],
                    "line_start": source_line_start,
                    "line_end": source_line_end,
                    "heading": heading,
                    "section_label": section_label,
                    "rule_label": rule_label,
                    "article_label": article_label,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "heading_ordinal": heading_index + 1,
                })

    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("backend.app.db.staging_models")
    SourceDocument = models.SourceDocument
    LegalProvision = models.LegalProvision
    StagingReviewEvent = models.StagingReviewEvent
    engine = create_engine(URL.create("sqlite", database=str(STAGING_DB.resolve())))
    try:
        with Session(engine) as session, session.begin():
            sources = {}
            for spec in SOURCES:
                source = session.scalar(
                    select(SourceDocument)
                    .where(SourceDocument.original_filename == spec["filename"])
                    .order_by(SourceDocument.created_at.desc())
                )
                if source is None:
                    raise ValueError(f"Staged source record missing: {spec['filename']}")
                sources[spec["filename"]] = source

            for item in prepared:
                source = sources[item["filename"]]
                locator = (
                    f"{item['locator_prefix']}:PAGE:{item['page']}:"
                    f"LEGAL-HEADING:{item['heading_ordinal']}:{item['heading'][:80]}"
                )
                provision_id = stable_id(source.id, locator, item["content_sha256"])
                fields = {
                    "source_document_id": source.id,
                    "parent_provision_id": None,
                    "provision_type": "SECONDARY_DIARY_HEADING_SPAN_CANDIDATE",
                    "legal_domain": item["domain"],
                    "locator_key": locator,
                    "section_label": item["section_label"],
                    "rule_label": item["rule_label"],
                    "article_label": item["article_label"],
                    "heading": item["heading"],
                    "citation": (
                        f"{item['filename']}, page {item['page']}, extracted-text lines "
                        f"{item['line_start']}-{item['line_end']}; OCR-derived explicit-heading span, "
                        "secondary source and legal boundary unverified"
                    ),
                    "content": item["content"],
                    "content_sha256": item["content_sha256"],
                    "language": "en",
                    "page_start": item["page"],
                    "page_end": item["page"],
                    "line_start": item["line_start"],
                    "line_end": item["line_end"],
                    "effective_from": (
                        date(2023, 3, 31)
                        if ("1730463" in item["content"] or item["locator_prefix"] == "DAIRY-2024" and item["page"] in {167, 168, 169, 170, 180, 181, 252})
                        else None
                    ),
                    "effective_to": None,
                    "authority_status": "SECONDARY_DIARY_HEADING_CANDIDATE",
                    "extraction_quality": "OCR_BOUNDARY_UNVERIFIED",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "heading_kind": item["heading"].split(" ", 1)[0],
                        "source_page_candidate_preserved": True,
                        "split_rule": "explicit line-start heading to next explicit heading on same page",
                        "cross_page_continuation_review_required": True,
                        "not_for_calculation": True,
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
                event_id = stable_id("STAGED_EXPLICIT_HEADING_SPAN", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_EXPLICIT_HEADING_SPAN_FOR_REVIEW",
                        reviewer_id=None,
                        note="OCR-derived heading span; verify complete provision boundary and primary authority.",
                        details={"page": item["page"], "heading": item["heading"]},
                    ))
        by_source = {}
        for item in prepared:
            by_source[item["locator_prefix"]] = by_source.get(item["locator_prefix"], 0) + 1
        print("Staged explicit legal-heading span candidates:", by_source)
        print("Candidates remain secondary/pending; source-page records were preserved.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
