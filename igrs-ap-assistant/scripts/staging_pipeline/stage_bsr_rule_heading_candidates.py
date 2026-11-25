"""Stage explicit Section/Rule/Article spans from supplied B.S.R. compilations.

These compiled texts are secondary candidates. Every span needs authority,
edition, amendment, and boundary review before legal retrieval use.
"""
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
SOURCES = (
    {
        "filename": "AP State & Subordinate Service Rules.txt",
        "extracted": "data/raw/source_extraction/AP_State_Subordinate_Service_Rules.extracted.txt",
        "domain": "AP_STATE_SERVICE_RULES",
    },
    {
        "filename": "APCS(CCA) Rules, 1991.txt",
        "extracted": "data/raw/source_extraction/APCS_CCA_Rules_1991.extracted.txt",
        "domain": "APCS_CCA_DISCIPLINARY_RULES",
    },
    {
        "filename": "BSR - Chit Fund Act.txt",
        "extracted": "data/raw/source_extraction/BSR_-_Chit_Fund_Act.extracted.txt",
        "domain": "CHIT_FUND_LAW",
    },
    {
        "filename": "Criminal Procedure Code.txt",
        "extracted": "data/raw/source_extraction/Criminal_Procedure_Code.extracted.txt",
        "domain": "CRIMINAL_PROCEDURE_HISTORICAL",
    },
    {
        "filename": "All In One Scanned Book.txt",
        "extracted": "data/raw/source_extraction/All_In_One_Scanned_Book.extracted.txt",
        "domain": "ALL_IN_ONE_LEGAL_COMPILATION",
    },
    {
        "filename": "BSR - Registration Act 1908.txt",
        "extracted": "data/raw/source_extraction/BSR_-_Registration_Act_1908.extracted.txt",
        "domain": "REGISTRATION_ACT_AND_RULES",
    },
    {
        "filename": "BSR - Indian Stamp Act 1899(1).txt",
        "extracted": "data/raw/source_extraction/BSR_-_Indian_Stamp_Act_1899_1_.extracted.txt",
        "domain": "INDIAN_STAMP_ACT_AND_RULES",
    },
)
HEADING = re.compile(
    r"^\s*(?P<kind>section|sec\.?|rule|article|art\.?)\s*"
    r"(?P<number>\d+[A-Za-z]?(?:[-.][A-Za-z0-9]+)*(?:\([A-Za-z0-9]+\))*)"
    r"(?P<title>.*)$",
    re.IGNORECASE,
)
PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def safe_content(lines: list[str]) -> tuple[str, int]:
    output = []
    redacted_lines = 0
    for line in lines:
        if any(pattern.search(line) for pattern in PII_PATTERNS):
            output.append("[REDACTED_CONTACT_OR_IDENTIFIER_LINE]")
            redacted_lines += 1
        else:
            output.append(line)
    content = "\n".join(output).strip()
    if any(pattern.search(content) for pattern in PII_PATTERNS):
        raise ValueError("Identifier pattern remains after line-level redaction")
    return content, redacted_lines


def main() -> None:
    prepared: list[dict] = []
    for spec in SOURCES:
        lines = (APP_ROOT / spec["extracted"]).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        heading_rows = []
        for index, line in enumerate(lines):
            match = HEADING.match(line)
            if match:
                heading_rows.append((index, match))
        for ordinal, (start, match) in enumerate(heading_rows):
            end = heading_rows[ordinal + 1][0] if ordinal + 1 < len(heading_rows) else len(lines)
            content, redacted_lines = safe_content(lines[start:end])
            if not content:
                continue
            kind = match.group("kind").casefold().rstrip(".")
            number = match.group("number")
            label = f"{kind.title()} {number}"
            prepared.append({
                **spec,
                "line_start": start + 1,
                "line_end": end,
                "heading": label,
                "section_label": label if kind in {"section", "sec"} else None,
                "rule_label": label if kind == "rule" else None,
                "article_label": label if kind in {"article", "art"} else None,
                "content": content,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "ordinal": ordinal + 1,
                "redacted_lines": redacted_lines,
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
                source = session.scalar(select(SourceDocument).where(
                    SourceDocument.original_filename == spec["filename"]
                ))
                if source is None:
                    raise ValueError(f"Source metadata missing: {spec['filename']}")
                sources[spec["filename"]] = source
            for item in prepared:
                source = sources[item["filename"]]
                locator = f"BSR:{item['filename']}:HEADING:{item['line_start']}:{item['heading']}"
                provision_id = stable_id(source.id, locator, item["content_sha256"])
                fields = {
                    "source_document_id": source.id,
                    "parent_provision_id": None,
                    "provision_type": "SECONDARY_COMPILATION_HEADING_SPAN",
                    "legal_domain": item["domain"],
                    "locator_key": locator,
                    "section_label": item["section_label"],
                    "rule_label": item["rule_label"],
                    "article_label": item["article_label"],
                    "heading": item["heading"],
                    "citation": (
                        f"{item['filename']}, extracted-text lines "
                        f"{item['line_start']}-{item['line_end']}; supplied B.S.R. compilation, "
                        "edition/amendments and exact boundary unverified"
                    ),
                    "content": item["content"],
                    "content_sha256": item["content_sha256"],
                    "language": "en",
                    "page_start": None,
                    "page_end": None,
                    "line_start": item["line_start"],
                    "line_end": item["line_end"],
                    "effective_from": None,
                    "effective_to": None,
                    "authority_status": "SECONDARY_COMPILATION_UNVERIFIED",
                    "extraction_quality": "EXPLICIT_HEADING_BOUNDARY_CANDIDATE",
                    "review_status": "PENDING_REVIEW",
                    "is_secondary_commentary": True,
                    "metadata_json": {
                        "heading_kind": item["heading"].split(" ", 1)[0],
                        "line_boundary_method": "explicit heading to next explicit section/rule/article heading",
                        "redacted_contact_or_identifier_lines": item["redacted_lines"],
                        "verify_against_primary_act_and_AP_amendments": True,
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
                event_id = stable_id("STAGED_BSR_HEADING_SPAN", provision_id)
                if session.get(StagingReviewEvent, event_id) is None:
                    session.add(StagingReviewEvent(
                        id=event_id,
                        target_type="LEGAL_PROVISION",
                        target_id=provision_id,
                        action="STAGED_BSR_HEADING_SPAN_FOR_REVIEW",
                        reviewer_id=None,
                        note="Secondary compiled text span; verify against authoritative Act/rules and amendments.",
                        details={"heading": item["heading"], "line_start": item["line_start"]},
                    ))
        counts = {}
        for item in prepared:
            counts[item["filename"]] = counts.get(item["filename"], 0) + 1
        print("Staged explicit B.S.R. Section/Rule/Article candidates:", counts)
        print("All remain secondary and pending; no runtime index or rate table was changed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
