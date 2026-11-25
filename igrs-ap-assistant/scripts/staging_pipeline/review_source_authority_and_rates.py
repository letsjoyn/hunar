"""Build a source-authority register and conservative workbook/source comparison.

This script outputs metadata and rate comparisons only. It never prints or
stores excerpts from source text. Diary text is redacted in memory before scan.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
SOURCE_DATA = GOV_ROOT / "data"
APP_DATA = APP_ROOT / "data"
OUTPUT = APP_DATA / "source_authority_and_rate_reconciliation.json"
EXTRACTION_AUDIT = APP_DATA / "raw" / "source_extraction" / "source_extraction_audit.json"
PDF_AUDIT = APP_DATA / "raw" / "page_extraction_audit" / "page_extraction_audit.json"
DEEDS_JSON = APP_DATA / "deeds_111.json"
WORKBOOK = SOURCE_DATA / "REVISIED DOCUMENT CLASSIFICATION AND TYPE.xlsx"

# These are provisional classifications from supplied filenames/formats and
# observed document types, not a claim that a scan is authentic or current.
SOURCE_REVIEW = {
    "AP State & Subordinate Service Rules.txt": ("secondary_or_compiled_rule_text", "employment/service rules", "exclude from IGRS rate index"),
    "APCS(CCA) Rules, 1991.txt": ("secondary_or_compiled_rule_text", "service conduct and appeals", "exclude from IGRS rate index"),
    "All In One Scanned Book.txt": ("unclassified_mixed_material", "unknown/mixed", "hold until page/topic classification"),
    "BSR - Chit Fund Act.txt": ("compiled_law_text_unverified_edition", "chit funds", "exclude unless a question specifically concerns this law"),
    "BSR - Indian Stamp Act 1899(1).txt": ("secondary_manual_compilation", "stamp duty and related AP rules", "secondary legal text; verify edition, amendments, and each incorporated enactment"),
    "BSR - Registration Act 1908.txt": ("secondary_manual_compilation", "Registration Act, AP rules, and procedure", "2007 manual compilation; secondary text, verify every enactment and amendment"),
    "Criminal Procedure Code.txt": ("secondary_or_compiled_rule_text", "criminal procedure", "exclude from IGRS rate index"),
    "Dairy 2010.pdf": ("departmental_diary_or_manual", "historical registration/stamps reference", "secondary evidence only; one OCR page is low-confidence"),
    "Dairy 2024.txt": ("departmental_diary_or_manual", "registration/stamps reference", "quarantine; redact and review privacy before any indexing"),
    "New Doc 05-19-2025 17.40.pdf": ("unverified_scan", "unknown until content/source identification", "hold for provenance and visual review"),
    "New Doc 05-19-2025 17.40_compressed.docx": ("unverified_document_conversion", "unknown until content/source identification", "hold; possible related/duplicate file, not established"),
    "REVISIED DOCUMENT CLASSIFICATION AND TYPE.xlsx": ("administrative_rate_baseline", "111 deed classifications and rate fields", "comparison baseline only; not authority for any rate"),
    "2014REV_MS393.PDF": ("government_order_candidate", "AP registration fee", "primary-source candidate; verify authenticity and official publication"),
    "2014REV_MS394.PDF": ("government_order_candidate", "AP stamp duty", "primary-source candidate; verify authenticity and official publication"),
}

PII_PATTERNS = {
    "email": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "mobile": re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    "twelve_digit_id": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "pan": re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
}
PERCENT_RE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*%")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
PAGE_MARKER = re.compile(r"^\s*=+\s*PAGE\s+(\d+)\s*=+\s*$", re.IGNORECASE)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def as_decimal(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(",", "")).normalize()
    except InvalidOperation:
        return None


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canon_value(value):
    if value is None or str(value).strip() == "":
        return None
    number = as_decimal(value)
    if number is not None:
        return format(number, "f")
    return " ".join(str(value).casefold().split())


def code_part(value) -> str:
    text = str(value).strip()
    try:
        number = int(float(text))
        return f"{number:02d}"
    except (ValueError, OverflowError):
        return text.zfill(2)


def scrub_pii(text: str) -> tuple[str, dict[str, int]]:
    counts = {}
    for label, pattern in PII_PATTERNS.items():
        text, count = pattern.subn(" [REDACTED] ", text)
        counts[label] = count
    return text, counts


def load_text_inputs() -> tuple[dict[str, dict], list[dict]]:
    text_sources: dict[str, dict] = {}
    extracted = json.loads(EXTRACTION_AUDIT.read_text(encoding="utf-8"))
    for item in extracted["files"]:
        source_name = Path(item["source_relative_path"]).name
        raw = (APP_ROOT / item["extracted_text_local_path"]).read_text(encoding="utf-8", errors="replace")
        text_sources[source_name] = {
            "text": raw,
            "source_path": item["source_relative_path"],
            "source_hash": item["source_sha256"],
            "privacy_review": item["privacy_review_status"],
            "source_kind": item["format"],
        }
    pdf_audit = json.loads(PDF_AUDIT.read_text(encoding="utf-8"))
    for item in pdf_audit["documents"]:
        source_name = Path(item["source_relative_to_gov_root"]).name
        raw = (APP_ROOT / item["extracted_text_local_path"]).read_text(encoding="utf-8", errors="replace")
        text_sources[source_name] = {
            "text": raw,
            "source_path": item["source_relative_to_gov_root"],
            "source_hash": item["source_sha256"],
            "privacy_review": "human review pending",
            "source_kind": ".pdf",
            "pdf_page_status": item["pages"],
        }
    return text_sources, pdf_audit["documents"]


def build_workbook_comparison() -> tuple[list[dict], dict]:
    deeds = json.loads(DEEDS_JSON.read_text(encoding="utf-8"))
    json_by_code = {record["code"]: record for record in deeds}
    workbook = load_workbook(WORKBOOK, read_only=True, data_only=True)
    sheet = workbook["Sheet1"]
    workbook_by_code: dict[str, list[dict]] = defaultdict(list)
    for row_number, row in enumerate(sheet.iter_rows(min_row=4, values_only=True), start=4):
        if len(row) < 12 or row[1] is None or row[2] is None:
            continue
        code = f"{code_part(row[1])}_{code_part(row[2])}"
        record = {
            "worksheet_row": row_number,
            "code": code,
            "classification": row[3],
            "type": row[4],
            "book": row[5],
            "sd": row[6],
            "td": row[7],
            "rf": row[8],
            "rf_min": row[9],
            "rf_max": row[10],
            "article": row[11],
        }
        workbook_by_code[code].append(record)
    workbook.close()

    fields = ("classification", "type", "book", "sd", "td", "rf", "rf_min", "rf_max", "article")
    comparisons = []
    for code in sorted(set(json_by_code) | set(workbook_by_code)):
        json_record = json_by_code.get(code)
        workbook_records = workbook_by_code.get(code, [])
        first = workbook_records[0] if workbook_records else None
        mismatches = []
        if json_record is None:
            mismatches.append("missing_from_json_baseline")
        elif first is None:
            mismatches.append("missing_from_workbook")
        else:
            for field in fields:
                if canon_value(first.get(field)) != canon_value(json_record.get(field)):
                    mismatches.append(field)
        variants = []
        if len(workbook_records) > 1:
            for field in fields:
                values = sorted({str(canon_value(record.get(field))) for record in workbook_records})
                if len(values) > 1:
                    variants.append(field)
        comparisons.append({
            "code": code,
            "workbook_row_count": len(workbook_records),
            "workbook_fields_vs_json_mismatched": mismatches,
            "duplicate_row_variant_fields": variants,
            "workbook_rates": {key: first.get(key) for key in ("sd", "td", "rf", "rf_min", "rf_max")} if first else None,
            "json_rates": {key: json_record.get(key) for key in ("sd", "td", "rf", "rf_min", "rf_max")} if json_record else None,
        })
    summary = {
        "workbook_unique_deed_codes": len(workbook_by_code),
        "json_unique_deed_codes": len(json_by_code),
        "codes_missing_from_json": sorted(set(workbook_by_code) - set(json_by_code)),
        "codes_missing_from_workbook": sorted(set(json_by_code) - set(workbook_by_code)),
        "field_mismatch_record_count": sum(bool(row["workbook_fields_vs_json_mismatched"]) for row in comparisons),
        "mismatch_field_counts": dict(Counter(field for row in comparisons for field in row["workbook_fields_vs_json_mismatched"])),
        "duplicate_code_count": sum(len(rows) > 1 for rows in workbook_by_code.values()),
        "duplicate_variant_count": sum(bool(row["duplicate_row_variant_fields"]) for row in comparisons),
        "comparison_limitation": "Workbook-to-JSON consistency only; JSON is not independent legal evidence.",
    }
    return comparisons, summary


def source_anchors(text: str) -> dict:
    percentages = [as_decimal(match.group(1)) for match in PERCENT_RE.finditer(text)]
    return {
        "distinct_percentages": [decimal_text(value) for value in sorted({value for value in percentages if value is not None})],
        "year_mentions": sorted(set(YEAR_RE.findall(text))),
        "go_reference_token_count": len(re.findall(r"(?i)\bG\.?O\.?\s*(?:Ms\.?\s*)?No\.?", text)),
        "has_stamp_duty_terms": bool(re.search(r"(?i)stamp\s+duty", text)),
        "has_registration_fee_terms": bool(re.search(r"(?i)registration\s+fee", text)),
    }


def source_mentions_for_deed(source: dict, title: str, article: str, expected: dict) -> dict:
    original = source["text"]
    text, redacted_counts = scrub_pii(original)
    lines = text.splitlines()
    normalized_title = " ".join(re.findall(r"[a-z0-9]+", title.casefold()))
    title_is_specific = len(normalized_title.split()) >= 2 and len(normalized_title) >= 8
    article_pattern = None
    article_norm = re.sub(r"[^a-z0-9]", "", str(article).casefold()) if article else ""
    if len(article_norm) >= 2:
        article_pattern = re.compile(r"(?<![a-z0-9])" + re.escape(article_norm) + r"(?![a-z0-9])", re.IGNORECASE)
    page = None
    matched = []
    for line_number, line in enumerate(lines, start=1):
        marker = PAGE_MARKER.match(line)
        if marker:
            page = int(marker.group(1))
            continue
        normalized_line = " ".join(re.findall(r"[a-z0-9]+", line.casefold()))
        title_match = title_is_specific and normalized_title in normalized_line
        article_match = bool(article_pattern and article_pattern.search(line))
        if title_match or article_match:
            matched.append({"line": line_number, "page": page, "title_match": bool(title_match), "article_match": bool(article_match)})
    context_lines = set()
    for hit in matched:
        context_lines.update(range(max(1, hit["line"] - 1), min(len(lines), hit["line"] + 1) + 1))
    observed = set()
    for number in context_lines:
        observed.update(as_decimal(match.group(1)) for match in PERCENT_RE.finditer(lines[number - 1]))
    observed.discard(None)
    expected_pct = {field: as_decimal(value) for field, value in expected.items() if field in {"sd", "td", "rf"}}
    candidate_matches = [field for field, value in expected_pct.items() if value is not None and value in observed]
    low_confidence_pages = {
        item["page"] for item in source.get("pdf_page_status", [])
        if item.get("ocr_status") == "ocr_low_text_visual_review_required"
    }
    matched_pages = {hit["page"] for hit in matched if hit["page"] is not None}
    return {
        "candidate_line_count": len(matched),
        "candidate_page_numbers": sorted(matched_pages),
        "candidate_on_low_confidence_ocr_page": bool(matched_pages & low_confidence_pages),
        "type_phrase_candidate_count": sum(hit["title_match"] for hit in matched),
        "article_token_candidate_count": sum(hit["article_match"] for hit in matched),
        "percentages_near_candidate_lines": [decimal_text(value) for value in sorted(observed)],
        "workbook_percentages_seen_near_candidates": candidate_matches,
        "pii_redaction_applied_before_scan": any(redacted_counts.values()),
        "comparison_status": "search candidate only; no rate validated by text proximity",
    }


def main() -> None:
    extraction_audit = json.loads(EXTRACTION_AUDIT.read_text(encoding="utf-8"))
    pdf_audit = json.loads(PDF_AUDIT.read_text(encoding="utf-8"))
    text_sources, _ = load_text_inputs()
    deed_json = json.loads(DEEDS_JSON.read_text(encoding="utf-8"))
    deed_by_code = {record["code"]: record for record in deed_json}
    workbook_rows, workbook_summary = build_workbook_comparison()
    extraction_by_name = {Path(row["source_relative_path"]).name: row for row in extraction_audit["files"]}
    pdf_by_name = {Path(row["source_relative_to_gov_root"]).name: row for row in pdf_audit["documents"]}

    authority_records = []
    for relative in sorted(set(extraction_by_name) | set(pdf_by_name)):
        authority, domain, decision = SOURCE_REVIEW.get(
            relative,
            ("unclassified", "unconfirmed", "hold from legal index until reviewed"),
        )
        source_info = extraction_by_name.get(relative) or pdf_by_name.get(relative)
        if source_info is None:
            continue
        pii_counts = source_info.get("metrics", {}).get("pii_pattern_counts_only") if relative in extraction_by_name else None
        if pii_counts and sum(pii_counts.values()) > 0:
            decision = "quarantine pending redaction and privacy review; pattern matches may include false positives"
        authority_records.append({
            "source_path": source_info.get("source_relative_path", source_info.get("source_relative_to_gov_root")),
            "source_sha256": source_info["source_sha256"],
            "authority_classification": authority,
            "domain_classification": domain,
            "provisional_decision": decision,
            "authenticity_or_edition_verified": False,
            "privacy_pattern_counts_only": pii_counts,
            "basis_and_limit": "provisional file-level triage; inspect source masthead, publication, amendment history, and provenance before legal reliance",
        })

    compare_sources = [
        "Dairy 2010.pdf",
        "Dairy 2024.txt",
        "2014REV_MS393.PDF",
        "2014REV_MS394.PDF",
    ]
    for item in workbook_rows:
        code = item["code"]
        if not item["workbook_rates"]:
            item["source_rate_candidates"] = {}
            continue
        workbook_record = deed_by_code.get(code, {})
        title = str(workbook_record.get("type", ""))
        article = str(workbook_record.get("article", ""))
        item["source_rate_candidates"] = {}
        for source_name in compare_sources:
            source = text_sources.get(source_name)
            if source:
                item["source_rate_candidates"][source_name] = source_mentions_for_deed(
                    source, title, article, item["workbook_rates"],
                )

    anchors = {}
    for source_name in ("2014REV_MS393.PDF", "2014REV_MS394.PDF"):
        if source_name in text_sources:
            anchors[source_name] = source_anchors(text_sources[source_name]["text"])

    supplied_order_findings = [
        {
            "source": "2014REV_MS393.PDF",
            "supplied_text_location": "page 1, lines 13, 27-37, 41-43",
            "finding": "G.O.Ms.No.393 is dated 26-11-2014. Its notification amends the Registration Act section 78 Table of Fees; Category (1), Sl.No.1, column (3) changes from 0.5% to 1%.",
            "limitation": "The supplied order text does not include the underlying G.O.Ms.No.463 fee table; do not map Category (1), Sl.No.1 to a deed code without that table.",
        },
        {
            "source": "2014REV_MS394.PDF",
            "supplied_text_location": "page 1, lines 14, 28-40, 47-53",
            "finding": "G.O.Ms.No.394 is dated 26-11-2014. It rescinds G.O.Ms.No.162 dated 30-03-2013 and restores sale-deed stamp duty prescribed by G.O.Ms.No.719 dated 30-07-2010, with immediate effect; the order text describes restoration to 5%.",
            "limitation": "This establishes the supplied order's stated change from 26-11-2014, not the rate for all deed types or for dates after later amendments.",
        },
    ]
    report = {
        "created_utc": datetime.now(UTC).isoformat(),
        "scope": "provisional authority triage plus all unique workbook deed-code/rate comparisons",
        "privacy": "Dairy text is processed locally only; PII patterns are redacted in-memory before keyword/rate matching; no source excerpts are stored or printed.",
        "authority_review": {
            "source_count": len(authority_records),
            "records": authority_records,
            "limitation": "Candidate authority labels are not authentication; no official external register was consulted.",
        },
        "workbook_vs_json_baseline": workbook_summary,
        "workbook_vs_json_rows": workbook_rows,
        "government_order_text_anchors": anchors,
        "findings_read_from_supplied_go_text_not_external_authentication": supplied_order_findings,
        "rate_reconciliation_limitation": "Near-line percentages are search leads only. They do not prove which duty/fee, deed variant, jurisdiction, basis, or effective period a percentage governs. Human legal review and primary-source confirmation remain required.",
    }
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"Reviewed {len(authority_records)} source files and {len(workbook_rows)} deed codes.")
    print("Workbook-vs-JSON summary:", json.dumps(workbook_summary, sort_keys=True))
    print("Government Order anchor metadata:", json.dumps(anchors, sort_keys=True, default=str))
    print(f"Metadata/rate-candidate report: {OUTPUT.relative_to(APP_ROOT)}")
    print("No document excerpts or personal-data values were printed.")


if __name__ == "__main__":
    main()
