"""Create a metadata-only topic/privacy inventory for Dairy 2010 pages.

The report deliberately stores no page excerpts or raw personal data.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = APP_ROOT / "data" / "raw" / "page_extraction_audit" / "page_extraction_audit.json"
OUTPUT_PATH = APP_ROOT / "data" / "raw" / "dairy2010_page_topic_index.json"
SOURCE_NAME = "Dairy 2010.pdf"

PAGE_MARKER = re.compile(
    r"(?i)^\s*(?:[-=* ]*(?:page|pg\.?|folio)\s*[:#.-]?\s*\d+|\[page\s+\d+\])\s*[-=* ]*$"
)
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


def main() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    document = next(
        row for row in audit["documents"]
        if Path(row["source_relative_to_gov_root"]).name == SOURCE_NAME
    )
    text_path = APP_ROOT / document["extracted_text_local_path"]
    lines = text_path.read_text(encoding="utf-8", errors="replace").splitlines()
    marker_indexes = [i for i, line in enumerate(lines) if PAGE_MARKER.match(line)]
    if len(marker_indexes) != document["page_count"]:
        raise ValueError(
            f"Expected {document['page_count']} page markers, found {len(marker_indexes)}"
        )

    low_pages = {
        page["page"] for page in document.get("pages", [])
        if page.get("ocr_status") == "ocr_low_text_visual_review_required"
    }
    records = []
    for index, start in enumerate(marker_indexes):
        end = marker_indexes[index + 1] if index + 1 < len(marker_indexes) else len(lines)
        page_lines = lines[start + 1:end]
        page_text = "\n".join(page_lines)
        normalized = page_text.casefold()
        pattern_counts = {
            key: len(pattern.findall(page_text)) for key, pattern in PII_PATTERNS.items()
        }
        tags = [
            topic for topic, terms in TOPICS.items()
            if any(term in normalized for term in terms)
        ]
        has_privacy_hit = any(pattern_counts.values())
        records.append({
            "page": index + 1,
            "source_line_start": start + 2,
            "source_line_end": end,
            "page_text_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
            "character_count": len(page_text),
            "topic_candidates": tags,
            "privacy_pattern_counts_only": pattern_counts,
            "privacy_status": "QUARANTINED_PATTERN_MATCH" if has_privacy_hit else "NO_PATTERN_MATCH_REVIEW_STILL_REQUIRED",
            "extraction_quality": "LOW_CONFIDENCE_VISUAL_REVIEW_REQUIRED" if index + 1 in low_pages else "TEXT_LAYER_UNVERIFIED",
            "review_status": "QUARANTINED" if has_privacy_hit else "PENDING_REVIEW",
        })

    summary = {
        "source_filename": SOURCE_NAME,
        "source_sha256": document["source_sha256"],
        "extracted_text_sha256": document["extracted_text_sha256"],
        "page_count": document["page_count"],
        "report_contains_page_text": False,
        "privacy_policy": "Local pattern counts only; no page excerpts or matched values are written.",
        "topic_candidate_page_counts": {
            topic: sum(topic in row["topic_candidates"] for row in records)
            for topic in TOPICS
        },
        "pages_with_any_privacy_pattern": sum(
            row["review_status"] == "QUARANTINED" for row in records
        ),
        "low_confidence_pages": sorted(low_pages),
        "pages": records,
    }
    OUTPUT_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Page metadata indexed: {len(records)}; no page excerpts stored.")
    print(f"Pages with privacy-pattern matches: {summary['pages_with_any_privacy_pattern']}.")
    print(f"Low-confidence pages: {summary['low_confidence_pages']}.")
    print("Topic candidate page counts:")
    for topic, count in summary["topic_candidate_page_counts"].items():
        print(f"  {topic}: {count}")
    print(f"Metadata report: {OUTPUT_PATH.relative_to(APP_ROOT)}")


if __name__ == "__main__":
    main()
