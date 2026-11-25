"""OCR only PDF pages flagged by audit_pdf_text.py, using local tools."""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from statistics import mean

APP_ROOT = Path(__file__).resolve().parents[2]
GOV_ROOT = APP_ROOT.parent
AUDIT_DIR = APP_ROOT / "data" / "raw" / "page_extraction_audit"
AUDIT_PATH = AUDIT_DIR / "page_extraction_audit.json"
POPPLER_ROOT = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
POPPLER_MATCHES = sorted(POPPLER_ROOT.glob("oschwartz10612.Poppler_*/poppler-*/Library/bin/pdftoppm.exe"))
PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
TESSERACT = PROGRAM_FILES / "Tesseract-OCR" / "tesseract.exe"
DPI = 300
LANGUAGE = "eng"
MIN_OCR_ALNUM = 40
MIN_MEAN_WORD_CONFIDENCE = 60.0


def alnum_count(text: str) -> int:
    return sum(char.isalnum() for char in text)


def main() -> None:
    if not AUDIT_PATH.is_file():
        raise SystemExit("Page audit not found; run scripts/audit_pdf_text.py first.")
    if not POPPLER_MATCHES:
        raise SystemExit("Poppler pdftoppm.exe was not found in the WinGet install location.")
    if not TESSERACT.is_file():
        raise SystemExit("Tesseract executable was not found under Program Files.")

    pdftoppm = POPPLER_MATCHES[-1]
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    attempted = succeeded = low_quality = failed = 0

    for document in audit["documents"]:
        source = GOV_ROOT / Path(document["source_relative_to_gov_root"])
        safe_name = Path(document["extracted_text_local_path"]).name.removesuffix(".pages.txt")
        output_path = AUDIT_DIR / f"{safe_name}.ocr.pages.txt"
        recognized_pages = []
        for page in document["pages"]:
            if page["status"] == "usable_text_candidate":
                continue
            attempted += 1
            page_number = page["page"]
            try:
                with tempfile.TemporaryDirectory(prefix="igrs_ocr_", dir=AUDIT_DIR) as temp_name:
                    image_base = Path(temp_name) / "page"
                    subprocess.run(
                        [str(pdftoppm), "-f", str(page_number), "-l", str(page_number), "-r", str(DPI), "-gray", "-png", "-singlefile", str(source), str(image_base)],
                        check=True, capture_output=True, timeout=180,
                    )
                    image_path = image_base.with_suffix(".png")
                    if not image_path.is_file():
                        raise RuntimeError("Poppler did not create the expected rendered page image")
                    result = subprocess.run(
                        [str(TESSERACT), str(image_path), "stdout", "-l", LANGUAGE, "--psm", "3", "tsv"],
                        check=True, capture_output=True, encoding="utf-8", errors="replace", timeout=180,
                    )
                    rows = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
                    words = []
                    confidences = []
                    line_words: dict[tuple[str, str, str], list[str]] = {}
                    for row in rows:
                        word = row.get("text", "").strip()
                        try:
                            confidence = float(row.get("conf", "-1"))
                        except ValueError:
                            confidence = -1
                        if row.get("level") == "5" and word and confidence >= 0:
                            words.append(word)
                            confidences.append(confidence)
                            key = (row.get("block_num", ""), row.get("par_num", ""), row.get("line_num", ""))
                            line_words.setdefault(key, []).append(word)
                    text = "\n".join(" ".join(line) for line in line_words.values())
                chars = alnum_count(text)
                avg_confidence = mean(confidences) if confidences else 0.0
                page["ocr_status"] = (
                    "ocr_text_detected"
                    if chars >= MIN_OCR_ALNUM and avg_confidence >= MIN_MEAN_WORD_CONFIDENCE
                    else "ocr_low_text_visual_review_required"
                )
                page["ocr_alphanumeric_characters"] = chars
                page["ocr_word_count"] = len(words)
                page["ocr_mean_word_confidence"] = round(avg_confidence, 1)
                page["ocr_low_confidence_word_ratio"] = round(sum(value < MIN_MEAN_WORD_CONFIDENCE for value in confidences) / len(confidences), 3) if confidences else 1.0
                page["ocr_engine"] = "Tesseract 5.4.0"
                page["ocr_language"] = LANGUAGE
                page["ocr_dpi"] = DPI
                succeeded += 1
                if page["ocr_status"] != "ocr_text_detected":
                    low_quality += 1
                recognized_pages.append(f"\n===== OCR PAGE {page_number} =====\n{text}\n")
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                page["ocr_status"] = "ocr_failed"
                page["ocr_error_type"] = type(exc).__name__
                failed += 1
                recognized_pages.append(f"\n===== OCR PAGE {page_number}: FAILED =====\n")
        output_path.write_text("".join(recognized_pages), encoding="utf-8")
        document["ocr_text_local_path"] = str(output_path.relative_to(APP_ROOT))

    audit["ocr_run"] = {
        "engine": "Tesseract 5.4.0",
        "renderer": "Poppler pdftoppm",
        "language": LANGUAGE,
        "dpi": DPI,
        "minimum_mean_word_confidence": MIN_MEAN_WORD_CONFIDENCE,
        "scope": "only pages previously flagged for visual review/OCR",
        "attempted_pages": attempted,
        "completed_pages": succeeded,
        "failed_pages": failed,
        "low_text_pages_requiring_visual_review": low_quality,
        "privacy_note": "OCR text remains in gitignored local data/raw; no external service used.",
    }
    AUDIT_PATH.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OCR attempted: {attempted}; completed: {succeeded}; failed: {failed}; low-text review needed: {low_quality}.")
    print("Recognized text is saved locally under gitignored data/raw.")
    for document in audit["documents"]:
        pages = document["pages"]
        ocr_pages = [page for page in pages if "ocr_status" in page]
        poor = sum(page.get("ocr_status") != "ocr_text_detected" for page in ocr_pages)
        if ocr_pages:
            print(f"{document['source_relative_to_gov_root']}: {len(ocr_pages)} OCR pages; {poor} still require visual review.")


if __name__ == "__main__":
    main()
