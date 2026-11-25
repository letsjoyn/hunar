"""Extract PDFs page by page locally and report pages needing OCR/review.

Text outputs are written under data/raw (gitignored) because they may contain
personal or internal information. The JSON audit contains metadata only.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
SOURCE_FILES = [
    GOV_ROOT / "data" / "Dairy 2010.pdf",
    GOV_ROOT / "data" / "New Doc 05-19-2025 17.40.pdf",
    GOV_ROOT / "2014REV_MS393.PDF",
    GOV_ROOT / "2014REV_MS394.PDF",
]
OUTPUT_DIR = APP_ROOT / "data" / "raw" / "page_extraction_audit"
PDFTOTEXT = Path(r"D:\Git\mingw64\bin\pdftotext.exe")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    document_rows = []
    for source in SOURCE_FILES:
        if not source.is_file():
            continue
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem)
        page_rows = []
        extracted_pages = []
        error = None
        try:
            result = subprocess.run(
                [str(PDFTOTEXT), "-layout", "-enc", "UTF-8", str(source), "-"],
                check=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            pages = result.stdout.split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
        except (OSError, subprocess.SubprocessError) as exc:
            pages = []
            error = type(exc).__name__
        for index, text in enumerate(pages, start=1):
            alnum = sum(ch.isalnum() for ch in text)
            replacement_chars = text.count("\ufffd")
            status = "usable_text_candidate" if alnum >= 80 else "needs_visual_review_and_possible_ocr"
            if error:
                status = "extraction_error"
            extracted_pages.append(f"\n===== PAGE {index} =====\n{text}\n")
            page_rows.append({
                "page": index,
                "extracted_characters": len(text),
                "alphanumeric_characters": alnum,
                "replacement_character_count": replacement_chars,
                "status": status,
                "error_type": error,
            })
        text_path = OUTPUT_DIR / f"{safe_stem}.pages.txt"
        text_path.write_text("".join(extracted_pages), encoding="utf-8")
        document_rows.append({
            "source_relative_to_gov_root": str(source.relative_to(GOV_ROOT)),
            "source_sha256": digest(source),
            "file_size_bytes": source.stat().st_size,
            "page_count": len(page_rows),
            "extracted_text_local_path": str(text_path.relative_to(APP_ROOT)),
            "extracted_text_sha256": digest(text_path),
            "ocr_performed": False,
            "page_review_status": "automated text-presence probe only; visual quality not verified",
            "pages": page_rows,
        })
    report = {
        "created_utc": datetime.now(UTC).isoformat(),
        "method": "local pdftotext page-level extraction; no OCR and no external service",
        "privacy_note": "Page text stays in gitignored data/raw; do not upload or index before privacy review.",
        "documents": document_rows,
    }
    report_path = OUTPUT_DIR / "page_extraction_audit.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Audited {len(document_rows)} PDFs across {sum(x['page_count'] for x in document_rows)} pages.")
    for doc in document_rows:
        needs = sum(p["status"] != "usable_text_candidate" for p in doc["pages"])
        print(f"{doc['source_relative_to_gov_root']}: {doc['page_count']} pages; {needs} need visual review/OCR")
    print(f"Metadata report: {report_path.relative_to(APP_ROOT)}")
    print("Extracted page text was written locally under gitignored data/raw.")


if __name__ == "__main__":
    main()
