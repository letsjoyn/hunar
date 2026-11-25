"""Prepare redacted, legal-topic candidate pages from Dairy 2024 locally.

The source is never sent to an external service. Directory/HR pages are
excluded, detected identifiers are redacted, and all output remains pending
human privacy and legal review. This is not a vector-index importer.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAME = "Dairy 2024.txt"
SOURCE_TEXT = APP_ROOT / "data" / "raw" / "source_extraction" / "Dairy_2024.extracted.txt"
PAGE_INDEX = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"
OUTPUT_TEXT = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_candidates.txt"
OUTPUT_MANIFEST = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_manifest.json"

REDACTIONS = (
    ("email_like", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("aadhaar_like", re.compile(r"(?<!\d)\d{12}(?!\d)"), "[REDACTED_ID]"),
    ("pan_like", re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"), "[REDACTED_ID]"),
    ("india_mobile_like", re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
)


def main() -> None:
    page_index = json.loads(PAGE_INDEX.read_text(encoding="utf-8"))
    text = SOURCE_TEXT.read_text(encoding="utf-8", errors="replace")
    source_pages = text.split("\f")
    if len(source_pages) != page_index["form_feed_page_count"]:
        raise ValueError("Source page segmentation changed since the metadata audit")

    kept_pages = []
    excluded_directory_pages = []
    included_no_topic_pages = []
    page_map = []
    for page_text, metadata in zip(source_pages, page_index["pages"], strict=True):
        if not metadata["topic_candidates"]:
            included_no_topic_pages.append(metadata["page_sequence"])
        if metadata["directory_or_hr_indicator"]:
            excluded_directory_pages.append(metadata["page_sequence"])
            continue

        redaction_counts = {name: 0 for name, _, _ in REDACTIONS}
        safe_lines = []
        for line in page_text.splitlines():
            line_matches = False
            for name, pattern, _replacement in REDACTIONS:
                count = len(pattern.findall(line))
                redaction_counts[name] += count
                line_matches = line_matches or count > 0
            safe_lines.append(
                "[REDACTED_CONTACT_OR_IDENTIFIER_LINE]" if line_matches else line
            )
        redacted_text = "\n".join(safe_lines)
        residual = [
            name for name, pattern, _ in REDACTIONS if pattern.search(redacted_text)
        ]
        if residual:
            raise ValueError(
                f"Residual identifier-pattern match after redaction on page "
                f"{metadata['page_sequence']}: {residual}"
            )

        page_number = metadata["page_sequence"]
        header = (
            f"\n--- SOURCE TEXT PAGE SEQUENCE {page_number}; "
            f"LINES {metadata['source_line_start']}-{metadata['source_line_end']} ---\n"
        )
        kept_pages.append(header + redacted_text)
        page_map.append({
            "page_sequence": page_number,
            "source_line_start": metadata["source_line_start"],
            "source_line_end": metadata["source_line_end"],
            "page_text_sha256_after_redaction": hashlib.sha256(
                redacted_text.encode("utf-8")
            ).hexdigest(),
            "candidate_content_sha256": hashlib.sha256(
                redacted_text.strip().encode("utf-8")
            ).hexdigest(),
            "topic_candidates": metadata["topic_candidates"],
            "redaction_counts_only": redaction_counts,
            "source_page_had_pattern_hit": metadata["privacy_status"] == "PATTERN_HIT_REVIEW_REQUIRED",
            "privacy_review_status": "PENDING_MANUAL_REVIEW",
            "legal_review_status": "PENDING_SOURCE_VERIFICATION",
        })

    sanitized = "\n".join(kept_pages)
    OUTPUT_TEXT.write_text(sanitized, encoding="utf-8")
    manifest = {
        "source_filename": SOURCE_NAME,
        "source_sha256": page_index["source_sha256"],
        "source_extracted_text_sha256": page_index["extracted_text_sha256"],
        "sanitized_text_sha256": hashlib.sha256(sanitized.encode("utf-8")).hexdigest(),
        "source_page_count": len(source_pages),
        "included_candidate_page_count": len(page_map),
        "excluded_directory_or_hr_page_sequences": excluded_directory_pages,
        "included_no_topic_page_count": len(included_no_topic_pages),
        "excluded_no_topic_page_count": 0,
        "output_contains_detected_phone_email_or_id_values": False,
        "privacy_policy": "Local redaction only; excluded directory/HR pages; manual review remains required.",
        "authority_status": "SECONDARY_DIARY_UNVERIFIED",
        "review_status": "PENDING_REVIEW_NOT_FOR_RUNTIME_USE",
        "pages": page_map,
    }
    OUTPUT_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Sanitized non-directory candidate pages retained: {len(page_map)} of {len(source_pages)}.")
    print(f"Directory/HR pages excluded: {len(excluded_directory_pages)}.")
    print(f"Pages without detected topic terms retained for review: {len(included_no_topic_pages)}.")
    print(f"Source pages with identifier patterns retained only after redaction: "
          f"{sum(row['source_page_had_pattern_hit'] for row in page_map)}.")
    print("Detected identifier values in output: 0; privacy/legal review still pending.")
    print(f"Sanitized candidate text: {OUTPUT_TEXT.relative_to(APP_ROOT)}")
    print(f"Metadata manifest: {OUTPUT_MANIFEST.relative_to(APP_ROOT)}")


if __name__ == "__main__":
    main()
