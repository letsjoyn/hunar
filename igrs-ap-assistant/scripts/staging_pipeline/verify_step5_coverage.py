"""Verify Stage 5 source coverage in the isolated staging database.

This checks extraction/staging completeness and integrity, not legal correctness
or approval of rates and provisions.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from openpyxl import load_workbook

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
STAGING_DB = APP_ROOT / "data" / "source_staging.db"
INVENTORY = APP_ROOT / "data" / "source_inventory_manifest.json"
DAIRY2024_INDEX = APP_ROOT / "data" / "raw" / "dairy2024_page_topic_index.json"
DAIRY2024_MANIFEST = APP_ROOT / "data" / "raw" / "dairy2024_sanitized_manifest.json"
DAIRY2010_INDEX = APP_ROOT / "data" / "raw" / "dairy2010_page_topic_index.json"
EXTRACTION_AUDIT = APP_ROOT / "data" / "raw" / "source_extraction" / "source_extraction_audit.json"
PDF_AUDIT = APP_ROOT / "data" / "raw" / "page_extraction_audit" / "page_extraction_audit.json"
REPORT_PATH = APP_ROOT / "data" / "raw" / "stage5_coverage_report.json"


def rows_for_source(connection: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM legal_provisions WHERE source_document_id = ? ORDER BY locator_key",
        (source_id,),
    ).fetchall()


def latest_source(connection: sqlite3.Connection, filename: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM source_documents WHERE original_filename = ? ORDER BY created_at DESC LIMIT 1",
        (filename,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Staged source not found: {filename}")
    return row


def main() -> None:
    with sqlite3.connect(STAGING_DB) as connection:
        connection.row_factory = sqlite3.Row
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
        extraction = json.loads(EXTRACTION_AUDIT.read_text(encoding="utf-8"))
        extraction_by_name = {
            Path(row["source_relative_path"]).name: row for row in extraction["files"]
        }
        source_coverage = {}
        failures = []
        source_hash_mismatches = []
        for source_row in inventory["files"]:
            source_path = GOV_ROOT / Path(source_row["relative_path"])
            digest = hashlib.sha256()
            with source_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != source_row["sha256"]:
                source_hash_mismatches.append(source_row["relative_path"])
        if source_hash_mismatches:
            failures.append("One or more original source hashes changed")

        workbook_name = "REVISIED DOCUMENT CLASSIFICATION AND TYPE.xlsx"
        workbook_inventory = next(
            row for row in inventory["files"] if Path(row["relative_path"]).name == workbook_name
        )
        workbook_path = GOV_ROOT / Path(workbook_inventory["relative_path"])
        workbook = load_workbook(workbook_path, read_only=True, data_only=False)
        nonempty_rows = 0
        coded_rate_rows = 0
        for sheet in workbook.worksheets:
            rows = []
            for number, cells in enumerate(sheet.iter_rows(), start=1):
                values = [cell.value for cell in cells]
                while values and values[-1] is None:
                    values.pop()
                if any(value is not None and str(value).strip() for value in values):
                    rows.append((number, values))
            nonempty_rows += len(rows)
            header = next((number for number, values in rows if
                "DOCUMENT CLASSIFICATION" in [str(value).strip().upper() for value in values if value is not None]
                and "DOCUMENT TYPE" in [str(value).strip().upper() for value in values if value is not None]
            ), None)
            if header is None:
                continue
            for number, values in rows:
                if number <= header + 1:
                    continue
                padded = values + [None] * max(0, 5 - len(values))
                if all(padded[index] is not None and str(padded[index]).strip()
                       for index in (1, 2, 4)):
                    coded_rate_rows += 1
        workbook.close()

        workbook_source = latest_source(connection, workbook_name)
        workbook_provisions = rows_for_source(connection, workbook_source["id"])
        workbook_rows_staged = sum(
            row["provision_type"] in {
                "ADMIN_WORKBOOK_RATE_ROW_CANDIDATE", "ADMIN_WORKBOOK_REFERENCE_ROW"
            }
            for row in workbook_provisions
        )
        staged_rates = connection.execute(
            "SELECT COUNT(*) FROM staged_deed_rates WHERE source_document_id = ?",
            (workbook_source["id"],),
        ).fetchone()[0]
        source_coverage[workbook_name] = {
            "nonempty_rows_in_source": nonempty_rows,
            "workbook_rows_staged": workbook_rows_staged,
            "coded_deed_rate_rows_in_source": coded_rate_rows,
            "staged_deed_rate_rows": staged_rates,
        }
        if workbook_rows_staged != nonempty_rows:
            failures.append("Workbook nonempty row coverage mismatch")
        if staged_rates != coded_rate_rows:
            failures.append("Workbook coded deed-rate row coverage mismatch")

        docx_name = "New Doc 05-19-2025 17.40_compressed.docx"
        docx_extraction = extraction_by_name[docx_name]
        docx_text_path = APP_ROOT / docx_extraction["extracted_text_local_path"]
        docx_nonempty = sum(bool(line.strip()) for line in docx_text_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines())
        docx_source = latest_source(connection, docx_name)
        docx_rows = sum(
            row["provision_type"] == "DOCX_PARAGRAPH_CANDIDATE"
            for row in rows_for_source(connection, docx_source["id"])
        )
        source_coverage[docx_name] = {
            "nonempty_extracted_paragraphs": docx_nonempty,
            "paragraphs_staged": docx_rows,
        }
        if docx_rows != docx_nonempty:
            failures.append("DOCX paragraph coverage mismatch")

        d2024_index = json.loads(DAIRY2024_INDEX.read_text(encoding="utf-8"))
        d2024_manifest = json.loads(DAIRY2024_MANIFEST.read_text(encoding="utf-8"))
        d2024_source = latest_source(
            connection, "Dairy 2024.txt — sanitized all-page candidate corpus"
        )
        d2024_rows = rows_for_source(connection, d2024_source["id"])
        d2024_pages = {
            int(row["locator_key"].rsplit(":", 1)[-1])
            for row in d2024_rows
            if row["locator_key"].startswith("DAIRY-2024:PAGE-CANDIDATE:")
        }
        d2024_withheld = {
            int(row["locator_key"].rsplit(":", 1)[-1])
            for row in d2024_rows
            if row["locator_key"].startswith("DAIRY-2024:PERSONAL-PAGE-WITHHELD:")
        }
        d2024_total = d2024_index["form_feed_page_count"]
        source_coverage["Dairy 2024.txt"] = {
            "source_page_count": d2024_total,
            "sanitized_content_pages": len(d2024_pages),
            "personal_page_locators_only": len(d2024_withheld),
            "pages_without_topic_terms_retained": d2024_manifest.get("included_no_topic_page_count", 0),
        }
        if d2024_pages | d2024_withheld != set(range(1, d2024_total + 1)):
            failures.append("Dairy 2024 page representation coverage mismatch")
        if d2024_pages & d2024_withheld:
            failures.append("Dairy 2024 page both content-indexed and privacy-withheld")

        d2010_index = json.loads(DAIRY2010_INDEX.read_text(encoding="utf-8"))
        d2010_source = latest_source(
            connection, "Dairy 2010.pdf — sanitized all-page historical candidates"
        )
        d2010_rows = rows_for_source(connection, d2010_source["id"])
        d2010_pages = {
            int(row["locator_key"].rsplit(":", 1)[-1])
            for row in d2010_rows
            if row["locator_key"].startswith("DAIRY-2010:PAGE-CANDIDATE:")
        }
        d2010_total = d2010_index["page_count"]
        source_coverage["Dairy 2010.pdf"] = {
            "source_page_count": d2010_total,
            "page_records_staged": len(d2010_pages),
            "page_78_visual_review": "section divider; not a substantive legal/rate page",
        }
        if d2010_pages != set(range(1, d2010_total + 1)):
            failures.append("Dairy 2010 page representation coverage mismatch")

        heading_sources = (
            "AP State & Subordinate Service Rules.txt",
            "APCS(CCA) Rules, 1991.txt",
            "BSR - Chit Fund Act.txt",
            "Criminal Procedure Code.txt",
            "All In One Scanned Book.txt",
            "BSR - Registration Act 1908.txt",
            "BSR - Indian Stamp Act 1899(1).txt",
        )
        for filename in heading_sources:
            source = latest_source(connection, filename)
            count = connection.execute(
                "SELECT COUNT(*) FROM legal_provisions WHERE source_document_id=? "
                "AND provision_type='SECONDARY_COMPILATION_HEADING_SPAN'",
                (source["id"],),
            ).fetchone()[0]
            source_coverage[filename] = {"explicit_heading_spans_staged": count}
            if count == 0:
                failures.append(f"No explicit heading spans staged for {filename}")

        for filename, provision_type in (
            ("2014REV_MS393.PDF", "GOVERNMENT_ORDER_AMENDMENT"),
            ("2014REV_MS394.PDF", "GOVERNMENT_ORDER_NOTIFICATION"),
        ):
            source = latest_source(connection, filename)
            count = connection.execute(
                "SELECT COUNT(*) FROM legal_provisions WHERE source_document_id=? AND provision_type=?",
                (source["id"], provision_type),
            ).fetchone()[0]
            source_coverage[filename] = {"primary_order_candidate_provisions": count}
            if count == 0:
                failures.append(f"No order provision staged for {filename}")

        for filename, expected_count in (
            ("Dairy 2010.pdf — redacted G.O. reference excerpts", 134),
            ("Dairy 2024.txt — redacted G.O. reference excerpts", 266),
        ):
            source = latest_source(connection, filename)
            count = connection.execute(
                "SELECT COUNT(*) FROM legal_provisions WHERE source_document_id=? "
                "AND provision_type='SECONDARY_GOVERNMENT_ORDER_REFERENCE'",
                (source["id"],),
            ).fetchone()[0]
            source_coverage[filename] = {"citation_only_references_staged": count}
            if count != expected_count:
                failures.append(f"G.O. reference count changed unexpectedly for {filename}")

        pdf_audit = json.loads(PDF_AUDIT.read_text(encoding="utf-8"))
        new_pdf = next(row for row in pdf_audit["documents"]
                       if Path(row["source_relative_to_gov_root"]).name == "New Doc 05-19-2025 17.40.pdf")
        new_pdf_source = latest_source(
            connection, "New Doc 05-19-2025 17.40.pdf — sanitized local OCR pages"
        )
        new_pdf_pages = sum(
            row["provision_type"] in {
                "SECONDARY_DOCUMENT_PAGE_CANDIDATE", "NONLEGAL_PERSONAL_PAGE_PLACEHOLDER"
            }
            for row in rows_for_source(connection, new_pdf_source["id"])
        )
        source_coverage["New Doc 05-19-2025 17.40.pdf"] = {
            "audited_source_pages": new_pdf["page_count"],
            "ocr_page_records_staged": new_pdf_pages,
        }
        if new_pdf_pages != new_pdf["page_count"]:
            failures.append("New Doc PDF OCR page coverage mismatch")

        database_counts = {
            "source_documents": connection.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0],
            "legal_provisions": connection.execute("SELECT COUNT(*) FROM legal_provisions").fetchone()[0],
            "approved_provisions": connection.execute(
                "SELECT COUNT(*) FROM legal_provisions WHERE review_status='APPROVED'"
            ).fetchone()[0],
            "staged_deed_rates": connection.execute("SELECT COUNT(*) FROM staged_deed_rates").fetchone()[0],
            "approved_deed_rates": connection.execute(
                "SELECT COUNT(*) FROM staged_deed_rates WHERE review_status='APPROVED'"
            ).fetchone()[0],
            "quarantined_source_documents": connection.execute(
                "SELECT COUNT(*) FROM source_documents WHERE review_status='QUARANTINED'"
            ).fetchone()[0],
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if foreign_key_errors:
            failures.append("SQLite foreign-key check failed")
        if integrity != "ok":
            failures.append("SQLite integrity check failed")

    report = {
        "stage": "5 - source extraction and staging coverage",
        "coverage_checks_passed": not failures,
        "failures": failures,
        "source_coverage": source_coverage,
        "original_source_files_hash_checked": len(inventory["files"]),
        "original_source_hash_mismatches": source_hash_mismatches,
        "database_counts": database_counts,
        "sqlite_integrity": integrity,
        "foreign_key_error_count": len(foreign_key_errors),
        "legal_correctness_or_departmental_approval": "not assessed by this coverage audit",
        "legal_entry_boundary_review": "explicit-heading candidates staged; page-level fallbacks and cross-page boundary review remain",
        "runtime_vector_index_built_from_staging": False,
        "note": "All source originals are preserved. Staging records are candidates and are not runtime-approved.",
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Stage 5 source coverage checks:", "PASS" if not failures else "FAIL")
    for filename, metrics in source_coverage.items():
        print(filename + ":", json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    print("Database counts:", json.dumps(database_counts, sort_keys=True))
    print("Foreign-key errors:", len(foreign_key_errors), "; integrity:", integrity)
    print("Legal approval and rule correctness are outside this coverage check.")
    print(f"Report: {REPORT_PATH.relative_to(APP_ROOT)}")
    if failures:
        raise SystemExit("Stage 5 source coverage is incomplete")


if __name__ == "__main__":
    main()
