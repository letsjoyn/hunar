"""Create an auditable Stage 6 rate reconciliation from staged workbook rows.

This is additive and conservative: the source workbook and staging records are
never modified, no staged row is promoted, and unsupported effective dates stay
NULL. Only specific supplied-order findings are attached as limited evidence.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import load_workbook

APP_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = APP_ROOT / "data" / "source_staging.db"
CSV_PATH = APP_ROOT / "data" / "stage6_rate_reconciliation.csv"
JSON_PATH = APP_ROOT / "data" / "stage6_rate_reconciliation.json"
EVENTS_CSV_PATH = APP_ROOT / "data" / "stage6_temporal_events.csv"
SOURCE_MATCH_PATH = APP_ROOT / "data" / "source_authority_and_rate_reconciliation.json"
FINDINGS_PATH = APP_ROOT / "data" / "stage6_source_findings.json"
WORKBOOK_PATH = APP_ROOT.parent / "data" / "REVISIED DOCUMENT CLASSIFICATION AND TYPE.xlsx"
LEGACY_RUNTIME_DB = APP_ROOT / "igrs_ap.db"

# These two source passages are exact findings in supplied order text already
# staged in the isolated review database. They are intentionally narrow.
LIMITED_FINDINGS = {
    "01-01": [
        {
            "source_filename": "2014REV_MS394.PDF",
            "provision_locator": "GO:394:SECTION-9:SALE-DEED-STAMP-DUTY",
            "source_locator": "PDF page 1, lines 28-53; recital and notification",
            "relation_type": "PRIMARY_ORDER_RATE_HISTORY",
            "citation": "G.O.Ms.No.394, Revenue (Registration.I) Department, dated 26-11-2014, supplied PDF page 1, paragraphs 1-3 and notification",
            "note": "The G.O.394 recital states G.O.Ms.No.719 reduced sale-deed duty from 7%/6% to 5% effective 01-09-2010; G.O.Ms.No.162 reduced 5% to 4% (its effective date is not stated in this recital); G.O.394 rescinds G.O.Ms.No.162 and restores the G.O.Ms.No.719 level with immediate effect on 26-11-2014. Full G.O.719/162 scope and intervening effective boundary remain unverified; this evidence does not set transfer duty, registration fee, exemptions, or all sale variants.",
        },
        {
            "source_filename": "2014REV_MS393.PDF",
            "provision_locator": "GO:393:SECTION-78:CATEGORY-1:ITEM-1",
            "source_locator": "PDF page 1; Section 78 amendment, Table of Fees Category (1), Sl. No. 1, column (3)",
            "relation_type": "MAPPING_REQUIRES_TABLE",
            "citation": "G.O.Ms.No.393, Revenue (Registration.I) Department, dated 26-11-2014, Section 78, supplied PDF page 1",
            "note": "The order changes the specified Table of Fees item from 0.5% to 1%. The supplied order does not contain the full G.O.Ms.No.463 table needed to prove mapping to workbook deed code 01-01; retain as mapping lead, not approval.",
        },
        {
            "source_filename": "Dairy 2024.txt — sanitized candidate pages 107-109",
            "source_locator": "Dairy 2024 text page sequence 107; G.O.Ms.No.463 Table of Fees, Category (1), Sl.No.1 Sale, with G.O.Ms.No.393 amendment annotation",
            "provision_locator": "DAIRY-2024:TEXT-PAGES:107-109:GO-463-TABLE-OF-FEES",
            "relation_type": "SECONDARY_MAPPING_SUPPORT",
            "citation": "Dairy 2024 (departmental secondary compilation), derived text page sequence 107, reproduced G.O.Ms.No.463 Table of Fees with G.O.Ms.No.393 annotation",
            "note": "The transcription maps Category (1), Sl.No.1 to Sale and shows the 1% G.O.Ms.No.393 annotation. This supports the mapping lead, but is secondary OCR text with flattened columns; manual source-page verification is still required and it is not approval.",
        },
    ]
}


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def as_num(value):
    if value is None:
        return None
    return str(value)


def classify_raw_component(value: str | None) -> dict:
    """Preserve cell meaning without coercing blanks, zeroes, caps, or money."""
    raw = "" if value is None else str(value).strip()
    upper = raw.upper()
    if not raw:
        kind = "BLANK_NOT_ASSUMED_ZERO"
    elif upper in {"---", "-", "N/A", "NA"}:
        kind = "EXPLICIT_NOT_APPLICABLE_MARKER"
    elif "FIXED" in upper or upper.startswith(("RS.", "RS ")):
        kind = "FIXED_RUPEE_AMOUNT"
    elif "MAX" in upper or "MAXIMUM" in upper or "SUBJECT TO" in upper:
        kind = "PERCENT_OR_AMOUNT_WITH_CAP_REQUIRES_PARSE_REVIEW"
    else:
        try:
            number = float(raw.replace(",", ""))
        except ValueError:
            kind = "UNPARSED_TEXT_REQUIRES_REVIEW"
        else:
            kind = "EXPLICIT_ZERO" if number == 0 else "NUMERIC_BASELINE_VALUE"
    return {"raw": raw or None, "classification": kind}


def inspect_workbook_properties() -> dict:
    if not WORKBOOK_PATH.exists():
        return {"available": False}
    workbook = load_workbook(WORKBOOK_PATH, read_only=True, data_only=True)
    properties = workbook.properties
    record = {
        "available": True,
        "created": properties.created.isoformat() if properties.created else None,
        "modified": properties.modified.isoformat() if properties.modified else None,
        "interpretation": "Embedded document metadata only; not proof of the rate schedule's legal effective date or last substantive review.",
    }
    workbook.close()
    return record


def inspect_legacy_runtime_db() -> dict:
    """Read-only audit; never alters legacy calculator data."""
    if not LEGACY_RUNTIME_DB.exists():
        return {"available": False, "path": str(LEGACY_RUNTIME_DB.relative_to(APP_ROOT))}
    uri = f"file:{LEGACY_RUNTIME_DB.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as legacy:
        tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "duty_rates" not in tables:
            return {"available": True, "path": str(LEGACY_RUNTIME_DB.relative_to(APP_ROOT)), "duty_rates_table": False}
        by_status = {
            row[0]: row[1]
            for row in legacy.execute("SELECT verification_status, count(*) FROM duty_rates GROUP BY verification_status")
        }
        dates = legacy.execute(
            "SELECT count(*), min(effective_from), max(effective_from), "
            "sum(effective_from='2014-11-26') FROM duty_rates"
        ).fetchone()
    return {
        "available": True,
        "path": str(LEGACY_RUNTIME_DB.relative_to(APP_ROOT)),
        "row_count": dates[0],
        "rows_by_verification_status": by_status,
        "min_effective_from": dates[1],
        "max_effective_from": dates[2],
        "rows_with_2014_11_26_start": dates[3] or 0,
        "interpretation": "Legacy database audit only. Existing status labels and date windows were not validated or changed by Stage 6.",
    }


SNAPSHOT_RATE_RELATIONS = {
    "SECONDARY_RATE_SNAPSHOT_CANDIDATE",
    "CONDITIONAL_RATE_SNAPSHOT_CANDIDATE",
    "PARTIAL_RATE_SNAPSHOT_CANDIDATE",
    "SECONDARY_FIXED_FEE_CANDIDATE",
    "CONDITIONAL_FIXED_CAP_FEE_CANDIDATE",
    "SECONDARY_TRANSFER_DUTY_EVENT_CANDIDATE",
    "SECONDARY_TRANSFER_DUTY_EXCEPTION_CANDIDATE",
}


def snapshot_disposition(findings: list[dict]) -> tuple[str, list[dict], list[dict]]:
    snapshot_findings = [f for f in findings if "Dairy 2024.txt" in f.get("source_filename", "")]
    historical_findings = [f for f in findings if "historical" in f.get("era", "")]
    has_conflict = any(
        "CONFLICT" in f.get("relation_type", "")
        or "conflict" in f.get("era", "")
        or "gap" in f.get("era", "")
        or "UNRESOLVED" in f.get("relation_type", "")
        or "NOT_ESTABLISHED" in f.get("relation_type", "")
        or f.get("relation_type") in {"NO_CLASS_SPECIFIC_RATE_MATCH", "SUBTYPE_NOT_SEPARATELY_STATED"}
        for f in snapshot_findings
    )
    has_rate_candidate = any(f.get("relation_type") in SNAPSHOT_RATE_RELATIONS for f in snapshot_findings)
    if not snapshot_findings:
        status = "NO_2024_SOURCE_EVIDENCE"
    elif has_conflict:
        status = "2024_SOURCE_CONFLICT_OR_MAPPING_GAP"
    elif has_rate_candidate:
        status = "2024_SNAPSHOT_RATE_CANDIDATE"
    else:
        status = "2024_CONTEXT_ONLY_NO_RATE_CANDIDATE"
    return status, snapshot_findings, historical_findings


def row_state(row: sqlite3.Row) -> tuple[str, list[str]]:
    reasons = []
    if row["effective_from"] is None:
        reasons.append("effective_from_missing")
    if row["effective_to"] is None:
        reasons.append("effective_to_not_supplied; do not assume this row is current")
    if row["calculation_basis"] is None or not str(row["calculation_basis"]).strip():
        reasons.append("calculation_basis_missing")
    if row["article_ref"] is None or not str(row["article_ref"]).strip():
        reasons.append("article_mapping_missing")
    if row["stamp_duty_percent"] is None and row["transfer_duty_percent"] is None and row["registration_fee_percent"] is None:
        reasons.append("no_numeric_percentage_fields; inspect raw workbook values for fixed/amount-based fees")
    raw = json.loads(row["row_metadata"] or "{}")
    raw_text = str(raw.get("raw_row_text", ""))
    if "RS." in raw_text.upper() or "FIXED" in raw_text.upper() or "MAX=" in raw_text.upper() or "SUBJECT TO" in raw_text.upper():
        reasons.append("raw_workbook_contains_fixed_amount_or_cap_not_represented_as_percentage_only")
    if any(value is None for value in (row["registration_fee_min"], row["registration_fee_max"])):
        reasons.append("registration_fee_min_or_max_missing_not_assumed_zero")
    if row["deed_code"] == "01-01":
        reasons.append("only_limited_2014_order_evidence; transfer_duty_and_complete_temporal_period_unverified")
    else:
        reasons.append("no_deed_specific_primary_order_evidence_linked")
    return "UNRESOLVED_NOT_FOR_CALCULATION", reasons


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Staging database not found: {DB_PATH}")
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT r.*, d.original_filename AS workbook_filename, d.source_sha256 AS workbook_sha256 "
            "FROM staged_deed_rates r JOIN source_documents d ON d.id=r.source_document_id "
            "ORDER BY r.deed_code, r.source_row_locator"
        ).fetchall()
        if len(rows) != 111:
            raise SystemExit(f"Expected 111 staged workbook rows; found {len(rows)}")
        source_match_report = json.loads(SOURCE_MATCH_PATH.read_text(encoding="utf-8"))
        findings_document = json.loads(FINDINGS_PATH.read_text(encoding="utf-8"))
        source_findings = findings_document["findings"]
        temporal_events = findings_document["temporal_events"]
        workbook_codes = {row["deed_code"] for row in rows}
        finding_codes = {code for finding in source_findings for code in finding.get("codes", [])}
        unknown_finding_codes = sorted(finding_codes - workbook_codes)
        if unknown_finding_codes:
            raise SystemExit(f"Source findings reference unknown deed codes: {unknown_finding_codes}")
        malformed_findings = [
            index for index, finding in enumerate(source_findings, start=1)
            if not finding.get("codes") or not finding.get("source_filename")
            or not finding.get("source_locator") or not finding.get("relation_type")
            or not finding.get("citation")
        ]
        if malformed_findings:
            raise SystemExit(f"Source findings missing required citation fields: {malformed_findings}")

        source_match_by_code = {
            item["code"].replace("_", "-"): item
            for item in source_match_report["workbook_vs_json_rows"]
        }
        def resolve_source_and_provision(finding: dict) -> tuple[str, str | None]:
            match = connection.execute(
                "SELECT d.id, p.id FROM source_documents d "
                "LEFT JOIN legal_provisions p ON p.source_document_id=d.id "
                "AND p.locator_key=? WHERE d.original_filename=? "
                "ORDER BY d.created_at DESC LIMIT 1",
                (finding.get("provision_locator"), finding["source_filename"]),
            ).fetchone()
            if match is None:
                raise SystemExit(f"Evidence source missing: {finding['source_filename']}")
            if finding.get("provision_locator") and match[1] is None:
                raise SystemExit(f"Evidence provision missing: {finding['provision_locator']}")
            return match[0], match[1]

        findings_by_code: dict[str, list[dict]] = {}
        for deed_code, findings in LIMITED_FINDINGS.items():
            for finding in findings:
                findings_by_code.setdefault(deed_code, []).append(finding)
        for finding in source_findings:
            for deed_code in finding["codes"]:
                findings_by_code.setdefault(deed_code, []).append(finding)

        # Store each pinpoint as a pending evidence link; never approve rates here.
        evidence_added = 0
        for staged in rows:
            for finding in findings_by_code.get(staged["deed_code"], []):
                source_id, provision_id = resolve_source_and_provision(finding)
                exists = connection.execute(
                    "SELECT id FROM rate_evidence WHERE staged_rate_id=? AND source_document_id=? "
                    "AND source_locator=? AND relation_type=?",
                    (staged["id"], source_id, finding["source_locator"], finding["relation_type"]),
                ).fetchone()
                note = finding.get("note", finding.get("limitations", ""))
                if finding.get("component_findings"):
                    note = json.dumps(finding["component_findings"], ensure_ascii=False) + " Limitations: " + note
                if exists:
                    evidence_id = exists[0]
                    connection.execute(
                        "UPDATE rate_evidence SET provision_id=?, citation=?, reviewer_note=?, review_status='PENDING_REVIEW' WHERE id=?",
                        (provision_id, finding["citation"], note, evidence_id),
                    )
                    continue
                evidence_id = stable_id(staged["id"], source_id, finding["source_locator"], finding["relation_type"])
                connection.execute(
                    "INSERT INTO rate_evidence (id, staged_rate_id, source_document_id, provision_id, "
                    "source_locator, relation_type, citation, review_status, reviewer_note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING_REVIEW', ?)",
                    (evidence_id, staged["id"], source_id, provision_id, finding["source_locator"],
                     finding["relation_type"], finding["citation"], note),
                )
                evidence_added += 1
        connection.commit()

        output_rows = []
        status_counts = Counter()
        missing_counts = Counter()
        snapshot_disposition_counts = Counter()
        code_set = set()
        for row in rows:
            state, reasons = row_state(row)
            status_counts[state] += 1
            code_set.add(row["deed_code"])
            for reason in reasons:
                if reason.endswith("missing") or "missing_" in reason:
                    missing_counts[reason] += 1
            metadata = json.loads(row["row_metadata"] or "{}")
            code_findings = findings_by_code.get(row["deed_code"], [])
            snapshot_status, snapshot_findings, historical_findings = snapshot_disposition(code_findings)
            snapshot_disposition_counts[snapshot_status] += 1
            evidence = [
                {
                    "source_filename": finding["source_filename"],
                    "source_locator": finding["source_locator"],
                    "provision_locator": finding.get("provision_locator"),
                    "era": finding.get("era", "order_evidence"),
                    "relation_type": finding["relation_type"],
                    "citation": finding["citation"],
                    "component_findings": finding.get("component_findings"),
                    "limitations": finding.get("limitations", finding.get("note")),
                    "review_status": "PENDING_REVIEW",
                }
                for finding in code_findings
            ]
            search_leads = {}
            for source_name in ("Dairy 2010.pdf", "Dairy 2024.txt"):
                match = source_match_by_code.get(row["deed_code"], {}).get("source_rate_candidates", {}).get(source_name, {})
                search_leads[source_name] = {
                    "candidate_pages": match.get("candidate_page_numbers", []),
                    "candidate_line_count": match.get("candidate_line_count", 0),
                    "workbook_percentages_seen_near_candidates": match.get("workbook_percentages_seen_near_candidates", []),
                    "status": "SEARCH_LEAD_ONLY_NOT_VALIDATION",
                }
            raw_parts = (metadata.get("raw_row_text") or "").split(" | ")
            raw_cell_values = [raw_parts[index] if len(raw_parts) > index else None for index in range(11)]
            rate_input_normalization = {
                "stamp_duty": classify_raw_component(raw_cell_values[6]),
                "transfer_duty": classify_raw_component(raw_cell_values[7]),
                "registration_fee": classify_raw_component(raw_cell_values[8]),
                "registration_fee_min": classify_raw_component(raw_cell_values[9]),
                "registration_fee_max": classify_raw_component(raw_cell_values[10]),
            }
            output_rows.append({
                "deed_code": row["deed_code"],
                "source_row_locator": row["source_row_locator"],
                "workbook_filename": row["workbook_filename"],
                "workbook_sha256": row["workbook_sha256"],
                "classification": row["classification"],
                "deed_type": row["deed_type"],
                "article_ref": row["article_ref"],
                "book_number": row["book_number"],
                "stamp_duty_percent": as_num(row["stamp_duty_percent"]),
                "transfer_duty_percent": as_num(row["transfer_duty_percent"]),
                "registration_fee_percent": as_num(row["registration_fee_percent"]),
                "registration_fee_min": as_num(row["registration_fee_min"]),
                "registration_fee_max": as_num(row["registration_fee_max"]),
                "calculation_basis": row["calculation_basis"],
                "area_type": row["area_type"],
                "property_subtype": row["property_subtype"],
                "effective_from": row["effective_from"],
                "effective_to": row["effective_to"],
                "raw_stamp_duty_cell": raw_cell_values[6],
                "raw_transfer_duty_cell": raw_cell_values[7],
                "raw_registration_fee_cell": raw_cell_values[8],
                "raw_registration_min_cell": raw_cell_values[9],
                "raw_registration_max_cell": raw_cell_values[10],
                "rate_input_normalization": rate_input_normalization,
                "raw_workbook_row": metadata.get("raw_row_text"),
                "snapshot_basis": "Dairy 2024 supplied snapshot; not an inferred effective period",
                "snapshot_disposition": snapshot_status,
                "snapshot_evidence_count": len(snapshot_findings),
                "historical_evidence_count": len(historical_findings),
                "evidence": evidence,
                "diary_search_leads": search_leads,
                "reconciliation_status": state,
                "unresolved_reasons": reasons,
                "review_status": row["review_status"],
                "not_for_calculation": True,
            })

        summary = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "stage": "6 of 9 — temporal rate reconciliation",
            "status": "SNAPSHOT_CROSSWALK_COMPLETE_TEMPORAL_RATE_APPROVAL_PENDING",
            "staging_database": "data/source_staging.db",
            "workbook_source_metadata": inspect_workbook_properties(),
            "legacy_runtime_database_audit": inspect_legacy_runtime_db(),
            "workbook_rows_reconciled": len(output_rows),
            "unique_deed_codes": len(code_set),
            "evidence_links_added_this_run": evidence_added,
            "evidence_links_total_for_workbook_rows": connection.execute(
                "SELECT count(*) FROM rate_evidence WHERE staged_rate_id IN "
                "(SELECT id FROM staged_deed_rates)"
            ).fetchone()[0],
            "approved_rate_rows": connection.execute(
                "SELECT count(*) FROM staged_deed_rates WHERE review_status='APPROVED'"
            ).fetchone()[0],
            "rows_with_effective_from": connection.execute(
                "SELECT count(*) FROM staged_deed_rates WHERE effective_from IS NOT NULL"
            ).fetchone()[0],
            "rows_with_effective_to": connection.execute(
                "SELECT count(*) FROM staged_deed_rates WHERE effective_to IS NOT NULL"
            ).fetchone()[0],
            "runtime_or_vector_changes": False,
            "source_rows_modified_or_deleted": False,
            "reconciliation_counts": dict(status_counts),
            "snapshot_crosswalk": {
                "complete": len(code_set) == 111 and sum(snapshot_disposition_counts.values()) == len(output_rows),
                "source_basis": "Use Dairy 2024 as the supplied current snapshot and Dairy 2010 as historical evidence; do not infer legal effective intervals from publication year.",
                "deed_codes_classified": len(code_set),
                "disposition_counts": dict(snapshot_disposition_counts),
                "rate_rows_approved": 0,
                "date_effective_runtime_ready": False,
                "interpretation": "Every deed code has a 2024 source disposition in this crosswalk. Candidate, conflict, and gap labels are explicit; this does not mean every rate is supported or safe for calculation.",
            },
            "missing_field_reason_counts": dict(missing_counts),
            "raw_component_classification_counts": {
                component: dict(Counter(
                    row["rate_input_normalization"][component]["classification"]
                    for row in output_rows
                ))
                for component in ("stamp_duty", "transfer_duty", "registration_fee", "registration_fee_min", "registration_fee_max")
            },
            "date_window_validation": {
                "rows_missing_effective_from": sum(row["effective_from"] is None for row in output_rows),
                "rows_with_effective_from": sum(row["effective_from"] is not None for row in output_rows),
                "rows_with_explicit_effective_to": sum(row["effective_to"] is not None for row in output_rows),
                "overlap_check": "NOT_RUN: no date-effective rows exist to compare",
                "ready_for_temporal_runtime": False,
            },
            "diary_search_lead_summary": {
                source_name: {
                    "rows_with_any_percentage_near_candidate": sum(
                        bool(source_match_by_code.get(row["deed_code"], {}).get("source_rate_candidates", {}).get(source_name, {}).get("workbook_percentages_seen_near_candidates"))
                        for row in rows
                    ),
                    "interpretation": "OCR/text proximity is a locator lead only; it does not prove rate meaning, applicability, or effective dates.",
                }
                for source_name in ("Dairy 2010.pdf", "Dairy 2024.txt")
            },
            "limited_primary_order_findings": [
                {
                    "go": "G.O.Ms.No.394",
                    "date": "2014-11-26",
                    "locator": "supplied PDF page 1, lines 28-53",
                    "finding": "The recital says G.O.Ms.No.719 reduced sale-deed stamp duty from 7%/6% to 5% effective 01-09-2010; G.O.Ms.No.162 reduced 5% to 4%; G.O.Ms.No.394 rescinds G.O.Ms.No.162 and restores the G.O.Ms.No.719 level with immediate effect on 26-11-2014.",
                    "scope_limit": "G.O.Ms.No.719 and G.O.Ms.No.162 are referenced, not supplied in full. G.O.394 does not give G.O.Ms.No.162's effective date or complete locality/subtype scope. It does not establish transfer duty or registration fee.",
                },
                {
                    "go": "G.O.Ms.No.393",
                    "date": "2014-11-26",
                    "locator": "supplied PDF page 1, Section 78 Table of Fees Category (1), Sl. No. 1",
                    "finding": "Changes that listed fee from 0.5% to 1%.",
                    "scope_limit": "The full G.O.Ms.No.463 table is needed to establish the precise deed-code mapping; not approved for broad mapping.",
                },
            ],
            "temporal_event_summary": {
                "event_count": len(temporal_events),
                "events_with_explicit_effective_from": sum(event["effective_from"] is not None for event in temporal_events),
                "events_without_explicit_effective_from": sum(event["effective_from"] is None for event in temporal_events),
                "event_date_status_counts": dict(Counter(event["effective_date_status"] for event in temporal_events)),
                "interpretation": "Event facts remain separate from complete deed-rate rows. Null effective dates stay null; no date is inferred from an order date or diary year.",
            },
            "source_findings_summary": {
                "manual_findings": len(source_findings),
                "source_finding_links_by_relation": dict(Counter(finding["relation_type"] for finding in source_findings)),
                "codes_with_pinpoint_evidence": sum(bool(findings_by_code.get(row["deed_code"])) for row in rows),
                "codes_without_manual_findings": sorted(workbook_codes - finding_codes),
                "unknown_finding_codes": unknown_finding_codes,
                "codes_with_2024_source_findings": sorted({code for finding in source_findings if "Dairy 2024.txt" in finding.get("source_filename", "") for code in finding["codes"]}),
                "codes_with_2024_rate_or_fee_candidates": sorted({code for finding in source_findings if "Dairy 2024.txt" in finding.get("source_filename", "") and finding.get("relation_type") in {"SECONDARY_RATE_SNAPSHOT_CANDIDATE", "CONDITIONAL_RATE_SNAPSHOT_CANDIDATE", "PARTIAL_RATE_SNAPSHOT_CANDIDATE", "SECONDARY_FIXED_FEE_CANDIDATE", "CONDITIONAL_FIXED_CAP_FEE_CANDIDATE", "SECONDARY_TRANSFER_DUTY_EVENT_CANDIDATE", "SECONDARY_TRANSFER_DUTY_EXCEPTION_CANDIDATE"} for code in finding["codes"]}),
                "codes_with_2024_rate_or_fee_conflicts": sorted({code for finding in source_findings if "Dairy 2024.txt" in finding.get("source_filename", "") and ("CONFLICT" in finding.get("relation_type", "") or "conflict" in finding.get("era", "")) for code in finding["codes"]}),
                "codes_with_2024_mapping_gaps_or_conflicts": sorted({code for finding in source_findings if "Dairy 2024.txt" in finding.get("source_filename", "") and ("conflict" in finding.get("era", "") or "gap" in finding.get("era", "") or "UNRESOLVED" in finding.get("relation_type", "") or "NOT_ESTABLISHED" in finding.get("relation_type", "")) for code in finding["codes"]}),
                "codes_with_2024_transfer_duty_order_candidates": sorted({code for finding in source_findings if finding.get("relation_type") == "SECONDARY_TRANSFER_DUTY_EVENT_CANDIDATE" for code in finding["codes"]}),
                "codes_with_2024_transfer_duty_exception_candidates": sorted({code for finding in source_findings if finding.get("relation_type") == "SECONDARY_TRANSFER_DUTY_EXCEPTION_CANDIDATE" for code in finding["codes"]}),
                "codes_with_2024_gift_relative_definition_candidates": sorted({code for finding in source_findings if finding.get("relation_type") == "SECONDARY_GIFT_RELATIVE_DEFINITION_CANDIDATE" for code in finding["codes"]}),
                "codes_with_2010_historical_candidates": sorted({code for finding in source_findings if "historical" in finding.get("era", "") for code in finding["codes"]}),
                "codes_with_source_conflicts": sorted({code for finding in source_findings if "conflict" in finding.get("era", "") for code in finding["codes"]}),
                "interpretation": "Pinpoint evidence makes partial source support and contradictions discoverable. It does not approve complete rate rows or invent effective windows.",
            },
            "critical_gaps": [
                "The 2024 snapshot is the project-selected latest source, but publication year is not a legal effective date and does not establish post-2024 changes.",
                "Dairy 2024 and Dairy 2010 are secondary compilations; OCR/table alignment and some workbook-to-article mappings remain conflicts or gaps.",
                "The 2010 evidence is historical coverage for only a subset of deed codes; absent source entries are explicitly not inferred.",
                "Fixed rupee amounts, caps, minimums, blanks, explicit zero values, geography, and conditional branches must remain distinct.",
                "No calculator or vector/runtime data was modified; all 111 staged rate rows remain pending.",
            ],
            "next_work": [
                "Resolve the listed 2024 source conflicts and mapping gaps from the supplied page images/text and cited orders where available.",
                "Extend historical 2010 component mappings only where the 2010 source contains a deed-specific rule and date.",
                "Add calculation/date-window tests and promote only rows whose value, basis, scope, and effective period are explicitly supported.",
            ],
            "rows": output_rows,
        }
        JSON_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        event_fields = list(temporal_events[0]) if temporal_events else []
        with EVENTS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as stream:
            event_writer = csv.DictWriter(stream, fieldnames=event_fields)
            event_writer.writeheader()
            event_writer.writerows(temporal_events)
        fieldnames = list(output_rows[0]) if output_rows else []
        with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            for record in output_rows:
                flattened = dict(record)
                flattened["evidence"] = json.dumps(record["evidence"], ensure_ascii=False)
                flattened["rate_input_normalization"] = json.dumps(record["rate_input_normalization"], ensure_ascii=False)
                flattened["diary_search_leads"] = json.dumps(record["diary_search_leads"], ensure_ascii=False)
                flattened["unresolved_reasons"] = " | ".join(record["unresolved_reasons"])
                writer.writerow(flattened)
        print(f"Stage 6 reconciliation exported: {len(output_rows)} workbook rows / {len(code_set)} deed codes.")
        print(f"Evidence links added this run: {evidence_added}; all remain PENDING_REVIEW.")
        print("Approved rows: 0; effective dates assigned: 0; runtime/vector data changed: no.")
        print(f"Reports: {JSON_PATH.relative_to(APP_ROOT)}, {CSV_PATH.relative_to(APP_ROOT)}, and {EVENTS_CSV_PATH.relative_to(APP_ROOT)}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
