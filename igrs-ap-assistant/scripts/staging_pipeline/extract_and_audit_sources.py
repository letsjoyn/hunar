"""Extract non-PDF supplied sources locally; write a metadata-only audit.

Extracted contents can contain personal/internal data and stay under the
Git-ignored data/raw directory. They are never printed or transmitted.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET


from openpyxl import load_workbook

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
SOURCE_DIR = GOV_ROOT / "data"
OUTPUT_DIR = APP_ROOT / "data" / "raw" / "source_extraction"
REPORT_PATH = OUTPUT_DIR / "source_extraction_audit.json"
INVENTORY_PATH = APP_ROOT / "data" / "source_inventory_manifest.json"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
PII_PATTERNS = {
    "email_like_values": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "india_mobile_like_values": re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)"),
    "twelve_digit_id_like_values": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "pan_like_values": re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text_metrics(text: str) -> dict:
    return {
        "characters": len(text),
        "alphanumeric_characters": sum(char.isalnum() for char in text),
        "lines": len(text.splitlines()),
        "replacement_characters": text.count("\ufffd"),
        "nul_characters": text.count("\x00"),
        "pii_pattern_counts_only": {name: len(pattern.findall(text)) for name, pattern in PII_PATTERNS.items()},
    }


def extract_plain_text(source: Path, target: Path) -> tuple[str, dict]:
    raw = source.read_bytes()
    text = None
    encoding_used = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            encoding_used = encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
        encoding_used = "utf-8-with-replacement"
    target.write_text(text, encoding="utf-8", newline="")
    metrics = text_metrics(text)
    metrics["detected_encoding"] = encoding_used
    metrics["byte_order_mark_present"] = raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
    return text, metrics


def paragraph_text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.findall(".//w:t", NS))


def extract_docx(source: Path, target: Path) -> tuple[str, dict]:
    with zipfile.ZipFile(source) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find("w:body", NS)
    output = []
    paragraph_count = table_count = table_rows = 0
    if body is not None:
        for child in list(body):
            if child.tag == f"{{{W_NS}}}p":
                paragraph_count += 1
                value = paragraph_text(child)
                if value:
                    output.append(value)
            elif child.tag == f"{{{W_NS}}}tbl":
                table_count += 1
                for row in child.findall("w:tr", NS):
                    table_rows += 1
                    cells = [paragraph_text(cell) for cell in row.findall("w:tc", NS)]
                    output.append(" | ".join(cells))
    text = "\n".join(output) + "\n"
    target.write_text(text, encoding="utf-8")
    metrics = text_metrics(text)
    metrics.update({
        "paragraph_count": paragraph_count,
        "table_count": table_count,
        "table_row_count": table_rows,
        "text_extraction_scope": "main document body paragraphs and tables",
    })
    return text, metrics


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def extract_xlsx(source: Path, target: Path) -> tuple[str, dict]:
    workbook = load_workbook(source, read_only=True, data_only=False)
    output = []
    sheet_details = []
    total_rows = total_nonempty_rows = total_nonempty_cells = formula_count = 0
    for sheet in workbook.worksheets:
        output.append(f"===== SHEET: {sheet.title} =====")
        nonempty_rows = nonempty_cells = formulas = row_count = 0
        for row in sheet.iter_rows():
            values = [cell_text(cell.value) for cell in row]
            while values and not values[-1]:
                values.pop()
            if not values:
                continue
            row_count += 1
            nonempty_rows += 1
            nonempty_cells += sum(bool(value) for value in values)
            formulas += sum(value.startswith("=") for value in values)
            output.append("\t".join(values))
        total_rows += row_count
        total_nonempty_rows += nonempty_rows
        total_nonempty_cells += nonempty_cells
        formula_count += formulas
        sheet_details.append({
            "name": sheet.title,
            "state": sheet.sheet_state,
            "declared_max_row": sheet.max_row,
            "declared_max_column": sheet.max_column,
            "nonempty_rows": nonempty_rows,
            "nonempty_cells": nonempty_cells,
            "formula_cells": formulas,
        })
    workbook.close()
    text = "\n".join(output) + "\n"
    target.write_text(text, encoding="utf-8")
    metrics = text_metrics(text)
    metrics.update({
        "sheet_count": len(sheet_details),
        "nonempty_rows_total": total_rows,
        "nonempty_cells_total": total_nonempty_cells,
        "formula_cells_total": formula_count,
        "sheets": sheet_details,
    })
    return text, metrics


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    inventory_hashes = {row["relative_path"]: row["sha256"] for row in inventory["files"]}
    records = []
    source_files = sorted(path for path in SOURCE_DIR.rglob("*") if path.is_file() and path.suffix.lower() in {".txt", ".docx", ".xlsx"})
    for source in source_files:
        relative = str(source.relative_to(GOV_ROOT))
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem)
        target = OUTPUT_DIR / f"{safe_stem}.extracted.txt"
        if source.suffix.lower() == ".txt":
            _text, metrics = extract_plain_text(source, target)
            method = "local text decode; content copied to UTF-8 derivative"
        elif source.suffix.lower() == ".docx":
            _text, metrics = extract_docx(source, target)
            method = "standard-library OpenXML body-order extraction"
        else:
            _text, metrics = extract_xlsx(source, target)
            method = "openpyxl read-only workbook extraction; formulas preserved"
        pii_counts = metrics["pii_pattern_counts_only"]
        sensitive_pattern_count = sum(pii_counts.values())
        privacy_status = (
            "quarantine_pending_redaction_and_review; pattern matches may include false positives"
            if sensitive_pattern_count
            else "manual privacy review required before indexing"
        )
        records.append({
            "source_relative_path": relative,
            "source_sha256": sha256(source),
            "source_hash_matches_inventory": sha256(source) == inventory_hashes.get(relative),
            "source_size_bytes": source.stat().st_size,
            "format": source.suffix.lower(),
            "extraction_method": method,
            "extracted_text_local_path": str(target.relative_to(APP_ROOT)),
            "extracted_text_sha256": sha256(target),
            "extracted_text_size_bytes": target.stat().st_size,
            "privacy_review_status": privacy_status,
            "authority_and_domain_review_status": "not verified; classify before indexing",
            "metrics": metrics,
        })
    report = {
        "created_utc": datetime.now(UTC).isoformat(),
        "scope": "all supplied TXT, DOCX, and XLSX files under D:\\gov\\data",
        "privacy_note": "The report contains metadata and counts only. Extracted source text remains local under Git-ignored data/raw.",
        "file_count": len(records),
        "files": records,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Extracted {len(records)} files locally; see {REPORT_PATH.relative_to(APP_ROOT)}")
    print("No source contents or PII values were printed.")
    print("Workbook structure:")
    for row in records:
        if row["format"] == ".xlsx":
            for sheet in row["metrics"]["sheets"]:
                print(f"  {sheet['name']}: {sheet['nonempty_rows']} populated rows, {sheet['nonempty_cells']} populated cells, state={sheet['state']}")


if __name__ == "__main__":
    main()
