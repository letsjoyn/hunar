"""Create a local, metadata-only page inventory for Dairy 2024 text.

The report stores no page excerpts or matched personal-data values. Pages with
pattern hits or directory/HR indicators are not eligible for legal indexing
without review.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAME = "Dairy 2024.txt"
TEXT_PATH = APP_ROOT / "data" / "raw" / "source_extraction" / "Dairy_2024.extracted.txt"
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
OUTPUT_PATH = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"

PII_PATTERNS = {
    "email_like": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "india_mobile_like": re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    "aadhaar_like": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "pan_like": re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
}
TOPICS = {
    "stamp_duty_and_rates": (
        "stamp duty", "transfer duty", "registration fee", "regn fee",
        "article 47", "art. 47", "article 49", "rate of duty", "fee table",
    ),
    "market_value_and_valuation": (
        "market value", "valuation", "guideline value", "section 47-a", "section 41-a",
    ),
    "registration_law_and_procedure": (
        "registration act", "section 22-a", "section 25", "section 78", "rule 71",
    ),
    "government_orders_and_circulars": (
        "g.o.ms", "go.ms", "government order", "notification", "circular memo", "memo no",
    ),
    "forms_and_office_procedure": (
        "procedure", "application", "form no", "checklist", "presentation of document",
    ),
}
DIRECTORY_TERMS = (
    "phone directory", "mobile number", "blood group", "gas number", "lic policy",
    "seniority list", "date of birth", "dob", "personal details", "email address",
)


def main() -> None:
    if not TEXT_PATH.is_file() or not STAGING_DB.is_file():
        raise SystemExit("Expected extracted text or staging database is missing.")
    with sqlite3.connect(STAGING_DB) as connection:
        source = connection.execute(
            "SELECT source_sha256, extracted_text_sha256 FROM source_documents "
            "WHERE original_filename = ?",
            (SOURCE_NAME,),
        ).fetchone()
    if source is None:
        raise SystemExit(f"Source metadata missing for {SOURCE_NAME}.")

    full_text = TEXT_PATH.read_text(encoding="utf-8", errors="replace")
    source_pages = full_text.split("\f")
    records = []
    current_line = 1
    for page_number, page_text in enumerate(source_pages, start=1):
        page_lines = page_text.splitlines()
        line_start = current_line
        line_end = current_line + max(len(page_lines) - 1, 0)
        current_line = line_end + 1
        normalized = page_text.casefold()
        pattern_counts = {
            name: len(pattern.findall(page_text)) for name, pattern in PII_PATTERNS.items()
        }
        privacy_hit = any(pattern_counts.values())
        directory_hit = any(term in normalized for term in DIRECTORY_TERMS)
        topic_tags = [
            topic for topic, terms in TOPICS.items()
            if any(term in normalized for term in terms)
        ]
        records.append({
            "page_sequence": page_number,
            "source_line_start": line_start,
            "source_line_end": line_end,
            "page_text_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
            "character_count": len(page_text),
            "non_ascii_character_count": sum(ord(char) > 127 for char in page_text),
            "topic_candidates": topic_tags,
            "directory_or_hr_indicator": directory_hit,
            "privacy_pattern_counts_only": pattern_counts,
            "privacy_status": "PATTERN_HIT_REVIEW_REQUIRED" if privacy_hit else "NO_PATTERN_HIT_REVIEW_STILL_REQUIRED",
            "review_status": "QUARANTINED" if privacy_hit or directory_hit else "PENDING_REVIEW",
        })

    topic_counts = {
        topic: sum(topic in record["topic_candidates"] for record in records)
        for topic in TOPICS
    }
    summary = {
        "source_filename": SOURCE_NAME,
        "source_sha256": source[0],
        "extracted_text_sha256": source[1],
        "form_feed_page_count": len(records),
        "report_contains_page_text": False,
        "privacy_policy": "Local pattern counts only; no matched values or excerpts are stored.",
        "topic_candidate_page_counts": topic_counts,
        "pages_with_privacy_pattern_hits": sum(
            record["privacy_status"] == "PATTERN_HIT_REVIEW_REQUIRED" for record in records
        ),
        "pages_with_directory_or_hr_indicators": sum(
            record["directory_or_hr_indicator"] for record in records
        ),
        "pages": records,
    }
    OUTPUT_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Metadata-only pages inventoried: {len(records)}.")
    print(f"Pages with privacy-pattern hits: {summary['pages_with_privacy_pattern_hits']}.")
    print(f"Pages with directory/HR indicators: {summary['pages_with_directory_or_hr_indicators']}.")
    print("Topic candidate page counts:")
    for topic, count in topic_counts.items():
        print(f"  {topic}: {count}")
    print(f"Report: {OUTPUT_PATH.relative_to(APP_ROOT)}")


if __name__ == "__main__":
    main()
