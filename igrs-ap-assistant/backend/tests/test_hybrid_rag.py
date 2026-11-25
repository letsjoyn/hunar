import hashlib
import sqlite3

from backend.app.engine import hybrid_rag


def _create_source_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE source_documents (
            id TEXT PRIMARY KEY, source_path TEXT, original_filename TEXT,
            source_kind TEXT, title TEXT, legal_domain TEXT,
            authority_classification TEXT, authenticity_status TEXT,
            review_status TEXT, privacy_status TEXT
        );
        CREATE TABLE legal_provisions (
            id TEXT PRIMARY KEY, source_document_id TEXT, legal_domain TEXT,
            provision_type TEXT, locator_key TEXT, part_label TEXT,
            chapter_label TEXT, section_label TEXT, rule_label TEXT,
            article_label TEXT, schedule_label TEXT, item_label TEXT,
            heading TEXT, citation TEXT, content TEXT, content_sha256 TEXT,
            language TEXT, page_start INTEGER, page_end INTEGER,
            line_start INTEGER, line_end INTEGER, effective_from TEXT,
            effective_to TEXT, authority_status TEXT, extraction_quality TEXT,
            review_status TEXT, is_secondary_commentary INTEGER, metadata_json TEXT
        );
        """
    )
    docs = [
        ("doc-reg", "data/law.txt", "AP Registration Act.txt", "MANUAL_OR_COMPILED_TEXT", "Registration Act", "Registration Act and rules", "SECONDARY", "UNVERIFIED", "PENDING_REVIEW", "SCREENED"),
        ("doc-chit", "data/chit.txt", "Chit Fund Act.txt", "OUT_OF_SCOPE_REFERENCE", "Chit Fund Act", "chit funds", "OUT_OF_SCOPE", "UNVERIFIED", "PENDING_REVIEW", "SCREENED"),
        ("doc-pii", "data/personal.txt", "Personal.txt", "UNIDENTIFIED_SOURCE", "Personal", "registration/stamps", "UNVERIFIED", "UNVERIFIED", "PENDING_REVIEW", "PENDING_REVIEW"),
    ]
    connection.executemany(
        "INSERT INTO source_documents VALUES (?,?,?,?,?,?,?,?,?,?)", docs
    )
    provisions = [
        ("p-section", "doc-reg", "REGISTRATION_ACT_AND_RULES", "STATUTORY_SECTION", "REGISTRATION-ACT:SECTION:22-A", None, None, "Section 22-A", None, None, None, None, "Section 22-A: Prohibited Registration", "Registration Act, section 22-A, page 12", "Registration of documents relating to assigned land is prohibited under the listed conditions.", "", "en", 12, 12, 50, 55, None, None, "SECONDARY_COMPILATION_UNVERIFIED", "EXPLICIT_HEADING_BOUNDARY_CANDIDATE", "PENDING_REVIEW", 1, "{}"),
        ("p-dated", "doc-reg", "REGISTRATION_ACT_AND_RULES", "GOVERNMENT_ORDER", "GO:TEST:2010", None, None, None, None, None, None, None, "Historical presentation rule", "Test order page 4", "Historical registration rule for a dated test query.", "", "en", 4, 4, 10, 14, "2010-01-01", "2012-12-31", "PRIMARY_CANDIDATE", "TEXT_LAYER_UNREVIEWED", "PENDING_REVIEW", 0, "{}"),
        ("p-unknown", "doc-reg", "REGISTRATION_ACT_AND_RULES", "RULE_CANDIDATE", "RULE:TEST:UNKNOWN-DATE", None, None, None, "Rule 88", None, None, None, "Date unknown rule", "Manual, page 18", "Un-dated registration presentation rule candidate.", "", "en", 18, 18, 3, 8, None, None, "SECONDARY_COMPILATION_UNVERIFIED", "TEXT_EXTRACTED_UNREVIEWED", "PENDING_REVIEW", 1, "{}"),
        ("p-chit", "doc-chit", "CHIT_FUND_LAW", "STATUTORY_SECTION", "CHIT:SECTION:1", None, None, "Section 1", None, None, None, None, "Chit Fund Act section 1", "Chit Fund Act, section 1", "Definitions and scope of chit fund legislation.", "", "en", 1, 1, 1, 5, None, None, "SECONDARY_COMPILATION_UNVERIFIED", "TEXT_EXTRACTED_UNREVIEWED", "PENDING_REVIEW", 1, "{}"),
        ("p-private", "doc-pii", "REGISTRATION_STAMPS", "PAGE_CANDIDATE", "PRIVATE:PAGE:1", None, None, None, None, None, None, None, "Private contact record", "Personal page", "Contact the applicant on 9876543210 about the registration matter.", "", "en", 1, 1, 1, 4, None, None, "SOURCE_UNVERIFIED", "TEXT_EXTRACTED_UNREVIEWED", "PENDING_REVIEW", 0, "{}"),
        ("p-withheld", "doc-pii", "NONLEGAL_PERSONAL_SOURCE_MATERIAL", "PAGE_CANDIDATE", "PRIVATE:PAGE:2", None, None, None, None, None, None, None, "Withheld page", "Personal page", "", "", "en", 2, 2, 1, 1, None, None, "NONLEGAL_SOURCE_LOCATOR", "CONTENT_WITHHELD_PRIVACY_SCOPE", "PENDING_REVIEW", 1, "{}"),
    ]
    provisions = [
        row[:15] + (hashlib.sha256(row[14].encode("utf-8")).hexdigest(),) + row[16:]
        for row in provisions
    ]
    connection.executemany(
        "INSERT INTO legal_provisions VALUES (" + ",".join("?" for _ in provisions[0]) + ")",
        provisions,
    )
    connection.commit()
    connection.close()


def test_stage7_builder_filters_privacy_and_preserves_review_metadata(tmp_path, monkeypatch):
    source_db = tmp_path / "source_staging.db"
    index_db = tmp_path / "stage7.db"
    manifest_path = tmp_path / "manifest.json"
    _create_source_database(source_db)
    monkeypatch.setattr(hybrid_rag, "INDEX_DB_PATH", index_db)

    manifest = hybrid_rag.build_stage7_index(source_db, index_db, manifest_path)

    assert manifest["generated_records"] == 6
    assert manifest["indexed_records"] == 4
    assert manifest["excluded_counts"] == {"privacy_scope": 1, "pii_pattern": 1}
    assert manifest["runtime_rate_data_changed"] is False
    results = hybrid_rag.search_stage7("assigned land prohibited registration", limit=3)
    assert results
    assert results[0]["locator_key" if "locator_key" in results[0] else "source_locator"] == "REGISTRATION-ACT:SECTION:22-A"
    assert results[0]["review_status"] == "PENDING_REVIEW"
    assert results[0]["authority_status"] == "SECONDARY_COMPILATION_UNVERIFIED"
    assert results[0]["retrieval_methods"]


def test_stage7_uses_index_embedding_model_and_falls_back_to_bm25(tmp_path, monkeypatch):
    source_db = tmp_path / "source_staging.db"
    index_db = tmp_path / "stage7.db"
    manifest_path = tmp_path / "manifest.json"
    _create_source_database(source_db)
    monkeypatch.setattr(hybrid_rag, "INDEX_DB_PATH", index_db)

    class FakeModel:
        def encode(self, text, **kwargs):
            return [1.0, 0.0]

        def get_sentence_embedding_dimension(self):
            return 2

    monkeypatch.setattr(hybrid_rag, "_sentence_model", lambda _name: FakeModel())
    manifest = hybrid_rag.build_stage7_index(
        source_db, index_db, manifest_path, embedding_model="cached-test-model"
    )
    assert manifest["retrieval"]["embedding_dimensions"] == 2
    results = hybrid_rag.search_stage7("assigned land prohibited registration")
    assert results
    assert "dense_vector" in results[0]["retrieval_methods"]

    def missing_model(_name):
        raise RuntimeError("local model unavailable")

    monkeypatch.setattr(hybrid_rag, "_sentence_model", missing_model)
    fallback = hybrid_rag.search_stage7("assigned land prohibited registration")
    assert fallback
    assert all("dense_vector" not in result["retrieval_methods"] for result in fallback)
    assert any("bm25" in result["retrieval_methods"] for result in fallback)


def test_stage7_domain_and_temporal_filters_fail_closed_without_dates(tmp_path, monkeypatch):
    source_db = tmp_path / "source_staging.db"
    index_db = tmp_path / "stage7.db"
    manifest_path = tmp_path / "manifest.json"
    _create_source_database(source_db)
    monkeypatch.setattr(hybrid_rag, "INDEX_DB_PATH", index_db)
    hybrid_rag.build_stage7_index(source_db, index_db, manifest_path)

    registration_results = hybrid_rag.search_stage7("definitions chit fund", limit=5)
    assert all(result["source_scope"] == "registration_stamps" for result in registration_results)
    chit_results = hybrid_rag.search_stage7("definitions chit fund", limit=5, domain="chit_funds")
    assert chit_results
    assert chit_results[0]["source_scope"] == "chit_funds"

    # Known expired evidence is omitted by default, but can be retrieved with an
    # explicit include_superseded request and remains labelled as historical.
    current = hybrid_rag.search_stage7("historical registration rule", limit=5, as_of_date=__import__("datetime").date(2024, 1, 1))
    assert all(result["source_locator"] != "GO:TEST:2010" for result in current)
    historical = hybrid_rag.search_stage7(
        "historical registration rule",
        limit=5,
        as_of_date=__import__("datetime").date(2024, 1, 1),
        include_superseded=True,
    )
    assert any(result["temporal_status"] == "SUPERSEDED_BEFORE_QUERY_DATE" for result in historical)
    assert all(result["review_status"] == "PENDING_REVIEW" for result in historical)
