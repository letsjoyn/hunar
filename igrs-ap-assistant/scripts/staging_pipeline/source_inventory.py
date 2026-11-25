"""Create a metadata-only inventory of supplied source documents.

The manifest deliberately excludes extracted document contents and PII values.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
SOURCE_DIR = GOV_ROOT / "data"
GO_FILES = [GOV_ROOT / "2014REV_MS393.PDF", GOV_ROOT / "2014REV_MS394.PDF"]
OUTPUT = APP_ROOT / "data" / "source_inventory_manifest.json"
PDFTOTEXT = Path(r"D:\Git\mingw64\bin\pdftotext.exe")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def classify(path: Path) -> tuple[str, str, str]:
    name = path.name.lower()
    if "2014rev_ms" in name:
        return "government_order_candidate", "AP registration/stamps", "primary-source candidate; verify issuing details"
    if path.suffix.lower() == ".xlsx":
        return "administrative_workbook", "deed classification and rates", "baseline only; authority/source not yet verified"
    if "dairy" in name or "diary" in name:
        return "departmental_manual_or_diary", "registration/stamps (candidate)", "secondary/manual source; not primary law"
    if "stamp" in name or "registration" in name:
        return "act_or_rules_text_candidate", "registration/stamps", "text source; edition/amendments not yet verified"
    if "service" in name or "cca" in name or "criminal" in name or "chit" in name:
        return "other_legal_material", "non-IGRS or unconfirmed", "domain relevance requires review"
    return "unclassified_source", "unconfirmed", "authority/domain requires review"


def inspect_text(path: Path) -> tuple[str, int | None, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if not PDFTOTEXT.exists():
            return "unknown_tool_unavailable", None, "pending"
        try:
            result = subprocess.run(
                [str(PDFTOTEXT), "-f", "1", "-l", "2", "-enc", "UTF-8", str(path), "-"],
                check=False, capture_output=True, timeout=60,
            )
            text = result.stdout.decode("utf-8", errors="ignore")
            count = sum(char.isalnum() for char in text)
            return ("usable_text_detected" if count >= 80 else "little_or_no_text_detected"), count, "pending page-level review/OCR"
        except (OSError, subprocess.TimeoutExpired):
            return "probe_failed", None, "pending"
    if suffix in {".txt", ".md", ".csv"}:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            count = sum(char.isalnum() for char in text)
            return ("text_available" if count else "empty_or_unreadable"), count, "pending quality/domain review"
        except OSError:
            return "probe_failed", None, "pending"
    if suffix in {".docx", ".xlsx"}:
        return "container_extractable_not_checked", None, "pending extraction and quality review"
    return "not_checked", None, "pending"


def page_audit_status(path: Path) -> tuple[str, str]:
    if path.suffix.lower() != ".pdf":
        return "not_applicable_or_pending", "pending quality/domain review"
    audit_path = APP_ROOT / "data" / "raw" / "page_extraction_audit" / "page_extraction_audit.json"
    if not audit_path.is_file():
        return "not_performed; page-level audit pending", "pending page-level review/OCR"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        relative_path = str(path.relative_to(GOV_ROOT))
        document = next((item for item in audit["documents"] if item["source_relative_to_gov_root"] == relative_path), None)
        if document is None:
            return "not_performed; absent from page audit", "pending page-level review/OCR"
        pages = document["pages"]
        flagged = [page for page in pages if page["status"] != "usable_text_candidate"]
        processed = [page for page in flagged if "ocr_status" in page]
        if not flagged:
            return "not_needed_by_text_probe", "page text extracted; visual/legal quality review pending"
        failed = sum(page["ocr_status"] == "ocr_failed" for page in processed)
        low = sum(page["ocr_status"] == "ocr_low_text_visual_review_required" for page in processed)
        if not processed:
            status = f"not_performed; {len(flagged)} pages flagged"
        else:
            status = f"performed_on_{len(processed)}_pages; failed_{failed}; low_text_{low}"
        return status, "OCR text unverified; visual review pending"
    except (OSError, ValueError, KeyError, TypeError):
        return "audit_read_failed", "pending page-level review/OCR"


def main() -> None:
    files = sorted(path for path in SOURCE_DIR.rglob("*") if path.is_file())
    files.extend(path for path in GO_FILES if path.is_file())
    extraction_audit_path = APP_ROOT / "data" / "raw" / "source_extraction" / "source_extraction_audit.json"
    extraction_rows = []
    if extraction_audit_path.is_file():
        try:
            extraction_rows = json.loads(extraction_audit_path.read_text(encoding="utf-8")).get("files", [])
        except (OSError, ValueError):
            extraction_rows = []
    extraction_by_path = {row["source_relative_path"]: row for row in extraction_rows}
    records = []
    for path in files:
        authority, domain, source_status = classify(path)
        text_status, sampled_chars, quality_status = inspect_text(path)
        ocr_status, page_quality_status = page_audit_status(path)
        if path.suffix.lower() == ".pdf":
            quality_status = page_quality_status
        relative_path = str(path.relative_to(GOV_ROOT))
        extraction_row = extraction_by_path.get(relative_path)
        if extraction_row:
            text_status = "locally_extracted"
            quality_status = "extracted; privacy, source authority, and content quality review pending"
            privacy = extraction_row["privacy_review_status"]
        else:
            privacy = "review_required" if any(term in path.name.lower() for term in ("dairy", "diary", "new doc", "scanned book")) else "not_assessed"
        records.append({
            "relative_path": relative_path,
            "extension": path.suffix.lower(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "authority_classification": authority,
            "domain_classification": domain,
            "source_status": source_status,
            "text_availability_probe": text_status,
            "local_extraction_status": "complete" if extraction_row else ("not_applicable" if path.suffix.lower() == ".pdf" else "pending"),
            "extracted_text_local_path": extraction_row["extracted_text_local_path"] if extraction_row else None,
            "pii_pattern_counts_only": extraction_row["metrics"]["pii_pattern_counts_only"] if extraction_row else None,
            "sampled_alphanumeric_characters_first_two_pdf_pages_or_full_text": sampled_chars,
            "ocr_status": ocr_status,
            "quality_review_status": quality_status,
            "privacy_review_status": privacy,
        })
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "D:\\gov\\data recursive files and the two supplied 2014REV_MS PDFs",
        "privacy_note": "Metadata only; no source text or personal-data values are stored here.",
        "file_count": len(records),
        "files": records,
    }
    OUTPUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote metadata-only manifest for {len(records)} files: {OUTPUT.relative_to(APP_ROOT)}")
    counts = {}
    for row in records:
        counts[row["text_availability_probe"]] = counts.get(row["text_availability_probe"], 0) + 1
    print("Text probe counts:", json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
