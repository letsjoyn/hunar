"""Export a concise, per-deed Stage 6 review queue from the reconciliation report."""
from __future__ import annotations

import csv
import json
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
SOURCE = APP_ROOT / "data" / "stage6_rate_reconciliation.json"
OUTPUT = APP_ROOT / "data" / "stage6_actionable_review_queue.csv"

def csv_value(value):
    return "" if value is None else str(value)


STATUS_LABELS = {
    "2024_SNAPSHOT_RATE_CANDIDATE": "SNAPSHOT_CANDIDATE_NEEDS_REVIEW",
    "2024_SOURCE_CONFLICT_OR_MAPPING_GAP": "CONFLICT_OR_MAPPING_GAP",
    "2024_CONTEXT_ONLY_NO_RATE_CANDIDATE": "CONTEXT_ONLY_NO_RATE_CANDIDATE",
}


def main() -> None:
    report = json.loads(SOURCE.read_text(encoding="utf-8"))
    output_rows = []
    for row in report["rows"]:
        disposition = row["snapshot_disposition"]
        evidence = row.get("evidence", [])
        evidence_summary = []
        for item in evidence:
            citation = item.get("citation") or item.get("source_locator") or ""
            note = item.get("limitations") or ""
            evidence_summary.append(
                f"{item.get('source_filename', 'Unknown source')} — {citation}"
                + (f"; limitation: {note}" if note else "")
            )

        if disposition == "2024_SNAPSHOT_RATE_CANDIDATE":
            next_action = (
                "Verify every component, calculation basis, scope, and effective interval "
                "against the operative source; then obtain authorized review."
            )
        elif disposition == "2024_SOURCE_CONFLICT_OR_MAPPING_GAP":
            next_action = (
                "Resolve the cited conflict or article/deed mapping, locate the operative "
                "source and dates, then obtain authorized review."
            )
        else:
            next_action = (
                "Locate a deed-specific rate source or confirm this is context-only; "
                "do not infer a rate."
            )

        output_rows.append({
            "deed_code": row["deed_code"],
            "deed_type": row["deed_type"],
            "workbook_article_ref": row.get("article_ref") or "",
            "2024_snapshot_disposition": disposition,
            "evidence_review_status": STATUS_LABELS.get(disposition, "REVIEW_REQUIRED"),
            "snapshot_stamp_duty_value": csv_value(row.get("stamp_duty_percent")),
            "snapshot_transfer_duty_value": csv_value(row.get("transfer_duty_percent")),
            "snapshot_registration_fee_value": csv_value(row.get("registration_fee_percent")),
            "snapshot_registration_min": csv_value(row.get("registration_fee_min")),
            "snapshot_registration_max": csv_value(row.get("registration_fee_max")),
            "2010_historical_evidence_count": row.get("historical_evidence_count", 0),
            "effective_from": row.get("effective_from") or "",
            "effective_to": row.get("effective_to") or "",
            "calculation_basis": row.get("calculation_basis") or "",
            "area_type": row.get("area_type") or "",
            "property_subtype": row.get("property_subtype") or "",
            "evidence_citations_and_limitations": " || ".join(evidence_summary),
            "existing_gaps": " | ".join(row.get("unresolved_reasons", [])),
            "next_action": next_action,
            "runtime_eligibility": "NOT_FOR_CALCULATION",
            "legal_review_status": "PENDING_REVIEW",
        })

    if len(output_rows) != 111 or len({row["deed_code"] for row in output_rows}) != 111:
        raise SystemExit(f"Expected 111 unique deed codes; found {len(output_rows)} rows")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Wrote {len(output_rows)} deed-code review rows to {OUTPUT.relative_to(APP_ROOT)}")
    print("No effective dates or approvals were inferred; all rows remain NOT_FOR_CALCULATION.")


if __name__ == "__main__":
    main()
