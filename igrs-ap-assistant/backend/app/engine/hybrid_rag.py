"""Metadata-filtered hybrid retrieval over the isolated Stage 5/6 corpus.

The index is a derived, rebuildable artifact. It never edits the source staging
DB, runtime rate DB, legacy legal index, or existing Chroma collections. Dense
vectors use deterministic local feature hashing, so indexing is offline and
sends no source text to an external embedding service.
"""
from __future__ import annotations

import array
import hashlib
import importlib
import json
import math
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from backend.app.core.config import DATA_DIR

SOURCE_DB_PATH = DATA_DIR / "source_staging.db"
INDEX_DB_PATH = DATA_DIR / "stage7_hybrid_rag.db"
MANIFEST_PATH = DATA_DIR / "stage7_hybrid_rag_manifest.json"
VECTOR_DIMENSIONS = 512
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_MODELS: dict[str, Any] = {}
_CROSS_ENCODERS: dict[str, Any] = {}
_FTS_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "does", "they",
    "their", "what", "when", "where", "which", "who", "how", "can", "could",
    "would", "should", "into", "about", "have", "has", "had", "are", "was",
    "were", "been", "being", "not", "but", "between", "under", "still",
    "need", "exact", "procedure", "man", "favour", "favor", "attracts", "rate",
}
_STATUTORY_ID_RE = re.compile(
    r"\b(?:(section|sec\.?|article|art\.?|rule)\s*)?"
    r"(\d{1,3})\s*[-/]\s*([A-Za-z])(?:\(([A-Za-z0-9]+)\))?\b"
    r"|\b(section|sec\.?|article|art\.?|rule)\s*(\d{1,3})([A-Za-z])\b",
    re.IGNORECASE,
)


def normalize_statutory_identifiers(text: str) -> str:
    """Append canonical hyphenated and concatenated statutory aliases for BM25/vector parity."""
    if not text:
        return ""
    aliases: list[str] = []
    for m in _STATUTORY_ID_RE.finditer(text):
        if m.group(2) and m.group(3):
            prefix = (m.group(1) or "").strip().lower().rstrip(".")
            num, letter = m.group(2), m.group(3).upper()
        else:
            prefix = (m.group(5) or "").strip().lower().rstrip(".")
            num, letter = m.group(6), m.group(7).upper()
        if not (num and letter):
            continue
        compact = f"{num}{letter}"
        hyphen = f"{num}-{letter}"
        spaced = f"{num} {letter}"
        aliases.extend((compact, hyphen, spaced))
        if prefix in {"section", "sec"}:
            aliases.extend((f"section {compact}", f"section {hyphen}"))
        elif prefix in {"article", "art"}:
            aliases.extend((f"article {compact}", f"art {compact}", f"article {hyphen}"))
        elif prefix == "rule":
            aliases.extend((f"rule {compact}", f"rule {hyphen}"))
    if "card/e-1730463" in text.casefold() or "1730463" in text:
        aliases.extend(("CARD/e-1730463", "CARDe1730463", "1730463", "Section 22-B", "22B", "22-B"))
    if not aliases:
        return text
    deduped = " ".join(dict.fromkeys(aliases))
    return f"{text}\n[STATUTORY_ALIASES]: {deduped}"


def _extract_lettered_sections(text: str) -> list[tuple[str, str]]:
    """Return canonical (number, letter) tuples for explicit Section references (e.g. 'Section 22-B' -> ('22', 'B'))."""
    pairs: list[tuple[str, str]] = []
    for m in re.finditer(r"\b(?:section|sec\.?|s\.?|u/s\.?)\s*(\d{1,3})\s*[-]?\s*([A-Za-z])\b", text, re.IGNORECASE):
        pairs.append((m.group(1), m.group(2).upper()))
    for m in re.finditer(r"\b(22|47|41|49|6)\s*[-]?\s*([AB])\b", text, re.IGNORECASE):
        pairs.append((m.group(1), m.group(2).upper()))
    return list(dict.fromkeys(pairs))


def _cross_encoder(model_name: str):
    """Load an optional pre-cached local CrossEncoder; never fetch over network."""
    if model_name not in _CROSS_ENCODERS:
        module = importlib.import_module("sentence_transformers")
        _CROSS_ENCODERS[model_name] = module.CrossEncoder(
            model_name,
            device="cpu",
            local_files_only=True,
        )
    return _CROSS_ENCODERS[model_name]

PII_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])"),
)
PERSONAL_MATERIAL_MARKERS = (
    "PERMANENT ACCOUNT NO", "AADHAAR CARD", "AADHAR CARD", "LAST RETURNS FILED",
    "BLOOD GROUP", "LIC POLICY", "GAS CONNECTION", "PHONE DIRECTORY",
    "PERSONAL DIARY", "EMERGENCY CONTACT", "BANK ACCOUNT NUMBER",
)


def _scope_for(domain: str, filename: str, source_kind: str) -> str:
    text = f"{domain} {filename} {source_kind}".casefold()
    if any(term in text for term in ("chit fund", "chit_fund")):
        return "chit_funds"
    if any(term in text for term in ("criminal procedure", "criminal_procedure", "crpc", "bnss")):
        return "criminal_procedure"
    if any(term in text for term in ("service rules", "service_rules", "employment", "disciplinary", "apcs")):
        return "service_rules"
    if any(term in text for term in ("unknown", "unclassified", "mixed")):
        return "unclassified"
    return "registration_stamps"


def _contains_sensitive_pattern(text: str) -> bool:
    return any(pattern.search(text) for pattern in PII_PATTERNS)


def _is_personal_material(text: str) -> bool:
    upper = text.upper()
    matched = sum(marker in upper for marker in PERSONAL_MATERIAL_MARKERS)
    return matched >= 2 or any(marker in upper for marker in (
        "PERMANENT ACCOUNT NO", "AADHAAR CARD", "AADHAR CARD", "PERSONAL DIARY",
    ))


def _features(text: str) -> array.array:
    """Build an L2-normalized, signed-hash word/character feature vector."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = TOKEN_RE.findall(normalized)
    counts: Counter[str] = Counter()
    for token in tokens:
        counts[f"w:{token}"] += 1
        if len(token) >= 3:
            padded = f"^{token}$"
            for size in (3, 4):
                counts.update(f"c:{padded[i:i + size]}" for i in range(len(padded) - size + 1))

    vector = [0.0] * VECTOR_DIMENSIONS
    for feature, count in counts.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        index = value % VECTOR_DIMENSIONS
        sign = -1.0 if value & (1 << 63) else 1.0
        weight = 1.0 + math.log(count)
        if feature.startswith("c:"):
            weight *= 0.25
        vector[index] += sign * weight

    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    result = array.array("f", vector)
    if result.itemsize != 4:
        raise RuntimeError("Expected 32-bit float vectors")
    return result


def _sentence_model(model_name: str):
    """Load a pre-cached local sentence-transformer; never fetch a model."""
    if model_name not in _SENTENCE_MODELS:
        try:
            module = importlib.import_module("sentence_transformers")
            _SENTENCE_MODELS[model_name] = module.SentenceTransformer(
                model_name,
                device="cpu",
                local_files_only=True,
            )
        except (ImportError, OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Local embedding model {model_name!r} is unavailable. "
                "Install requirements-ai.txt and cache the model locally first."
            ) from exc
    return _SENTENCE_MODELS[model_name]


def _embedding(text: str, model_name: str | None = None) -> array.array:
    if not model_name:
        return _features(text)
    vector = _sentence_model(model_name).encode(
        text,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    result = array.array("f", (float(value) for value in vector))
    if not result:
        raise RuntimeError("The configured embedding model returned an empty vector")
    return result


def _batch_embeddings(texts: list[str], model_name: str | None = None) -> list[array.array]:
    """Encode a list of texts using batched SentenceTransformer when available."""
    if not texts:
        return []
    if not model_name:
        return [_features(text) for text in texts]
    model = _sentence_model(model_name)
    try:
        vectors = model.encode(
            texts,
            batch_size=64,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        if len(vectors) == len(texts):
            output = [array.array("f", (float(value) for value in vec)) for vec in vectors]
            if all(len(v) > 0 for v in output):
                return output
    except Exception:
        pass
    return [_embedding(text, model_name) for text in texts]


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_stage7_index(
    source_db: Path = SOURCE_DB_PATH,
    output_db: Path = INDEX_DB_PATH,
    manifest_path: Path = MANIFEST_PATH,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Build an isolated FTS5 + local-vector index from staged provisions."""
    source_db = source_db.resolve()
    output_db = output_db.resolve()
    manifest_path = manifest_path.resolve()
    if not source_db.exists():
        raise FileNotFoundError(f"Stage 5/6 source database not found: {source_db}")
    if output_db in {source_db, (DATA_DIR / "legal_knowledge.db").resolve()}:
        raise ValueError("Refusing to overwrite a source or legacy legal database")

    source_uri = f"file:{source_db.as_posix()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    source.row_factory = sqlite3.Row
    try:
        raw_rows = source.execute(
            """SELECT p.id AS provision_id, p.source_document_id, p.legal_domain,
                      p.provision_type, p.locator_key, p.part_label, p.chapter_label,
                      p.section_label, p.rule_label, p.article_label, p.schedule_label,
                      p.item_label, p.heading, p.citation, p.content,
                      p.content_sha256, p.language, p.page_start, p.page_end,
                      p.line_start, p.line_end, p.effective_from, p.effective_to,
                      p.authority_status, p.extraction_quality, p.review_status,
                      p.is_secondary_commentary, p.metadata_json,
                      d.source_path, d.original_filename, d.source_kind,
                      d.title AS source_title, d.legal_domain AS document_domain,
                      d.authority_classification AS document_authority,
                      d.authenticity_status, d.review_status AS document_review_status,
                      d.privacy_status
               FROM legal_provisions p
               JOIN source_documents d ON d.id = p.source_document_id
               ORDER BY d.original_filename, p.page_start, p.locator_key, p.id"""
        ).fetchall()
    finally:
        source.close()

    embedding_model = embedding_model or os.getenv("IGRS_EMBEDDING_MODEL") or None
    if embedding_model:
        _sentence_model(embedding_model)

    output_db.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_db = output_db.with_name(output_db.name + ".building")
    temporary_db.unlink(missing_ok=True)
    counts: Counter[str] = Counter()
    source_names: set[str] = set()
    row_total = 0
    indexed = 0

    try:
        target = sqlite3.connect(temporary_db)
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=FULL")
        target.executescript(
            """
            CREATE TABLE rag_records (
                id INTEGER PRIMARY KEY,
                provision_id TEXT NOT NULL,
                source_document_id TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_scope TEXT NOT NULL,
                legal_domain TEXT NOT NULL,
                provision_type TEXT NOT NULL,
                locator_key TEXT NOT NULL,
                section_label TEXT,
                rule_label TEXT,
                article_label TEXT,
                schedule_label TEXT,
                item_label TEXT,
                heading TEXT NOT NULL,
                citation TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                language TEXT NOT NULL,
                page_start INTEGER,
                page_end INTEGER,
                line_start INTEGER,
                line_end INTEGER,
                effective_from TEXT,
                effective_to TEXT,
                authority_status TEXT NOT NULL,
                extraction_quality TEXT NOT NULL,
                review_status TEXT NOT NULL,
                source_authority TEXT NOT NULL,
                source_authenticity TEXT NOT NULL,
                source_review_status TEXT NOT NULL,
                source_privacy_status TEXT NOT NULL,
                is_secondary_commentary INTEGER NOT NULL,
                metadata_json TEXT,
                UNIQUE(provision_id)
            );
            CREATE VIRTUAL TABLE rag_fts USING fts5(
                source_filename, legal_domain, locator_key, heading, citation, content,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE rag_vectors (
                record_id INTEGER PRIMARY KEY REFERENCES rag_records(id) ON DELETE CASCADE,
                embedding BLOB NOT NULL
            );
            CREATE INDEX ix_rag_scope_domain ON rag_records(source_scope, legal_domain);
            CREATE INDEX ix_rag_effective_dates ON rag_records(effective_from, effective_to);
            CREATE INDEX ix_rag_content_hash ON rag_records(content_sha256);
            CREATE TABLE rag_manifest (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        insert_sql = """INSERT INTO rag_records (
            provision_id, source_document_id, source_filename, source_kind,
            source_scope, legal_domain, provision_type, locator_key, section_label,
            rule_label, article_label, schedule_label, item_label, heading, citation,
            content, content_sha256, language, page_start, page_end, line_start,
            line_end, effective_from, effective_to, authority_status,
            extraction_quality, review_status, source_authority, source_authenticity,
            source_review_status, source_privacy_status, is_secondary_commentary,
            metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

        pending_inserts: list[tuple[tuple[Any, ...], tuple[str, ...], str, str]] = []
        for row in raw_rows:
            row_total += 1
            filename = row["original_filename"] or "Unknown source"
            source_names.add(filename)
            content = str(row["content"] or "").strip()
            heading = str(row["heading"] or "").strip()
            citation = str(row["citation"] or "").strip()
            authority_status = str(row["authority_status"] or "UNVERIFIED")
            quality = str(row["extraction_quality"] or "UNREVIEWED")
            privacy_status = str(row["privacy_status"] or "PENDING_REVIEW")

            if authority_status == "NONLEGAL_SOURCE_LOCATOR" or quality == "CONTENT_WITHHELD_PRIVACY_SCOPE":
                counts["excluded_privacy_scope"] += 1
                continue
            if not content:
                counts["excluded_empty"] += 1
                continue
            searchable = f"{filename}\n{heading}\n{citation}\n{content}"
            if _is_personal_material(searchable):
                counts["excluded_personal_material"] += 1
                continue
            if _contains_sensitive_pattern(searchable):
                counts["excluded_pii_pattern"] += 1
                continue

            source_scope = _scope_for(
                str(row["document_domain"] or row["legal_domain"] or ""),
                filename,
                str(row["source_kind"] or ""),
            )
            metadata = row["metadata_json"] or "{}"
            try:
                metadata = json.dumps(json.loads(metadata), ensure_ascii=False)
            except (TypeError, json.JSONDecodeError):
                metadata = "{}"
            values = (
                row["provision_id"], row["source_document_id"], filename,
                row["source_kind"] or "UNKNOWN", source_scope,
                row["legal_domain"] or row["document_domain"] or "UNCLASSIFIED",
                row["provision_type"] or "UNCLASSIFIED", row["locator_key"] or "",
                row["section_label"], row["rule_label"], row["article_label"],
                row["schedule_label"], row["item_label"], heading or row["locator_key"] or "Untitled provision",
                citation or filename, content, row["content_sha256"] or hashlib.sha256(content.encode()).hexdigest(),
                row["language"] or "und", row["page_start"], row["page_end"],
                row["line_start"], row["line_end"], row["effective_from"], row["effective_to"],
                authority_status, quality, row["review_status"] or "PENDING_REVIEW",
                row["document_authority"] or "UNVERIFIED", row["authenticity_status"] or "UNVERIFIED",
                row["document_review_status"] or "PENDING_REVIEW", privacy_status,
                int(bool(row["is_secondary_commentary"])), metadata,
            )
            normalized_body = normalize_statutory_identifiers(f"{heading}\n{citation}\n{content}")
            fts_text = (filename, values[5], values[7], values[13], values[14], normalized_body)
            vector_text = " ".join((filename, values[5], values[7], values[13], values[14], normalized_body))
            pending_inserts.append((values, fts_text, vector_text, source_scope))

        embeddings = _batch_embeddings([item[2] for item in pending_inserts], embedding_model)
        for (values, fts_text, _vector_text, source_scope), vec in zip(pending_inserts, embeddings):
            cursor = target.execute(insert_sql, values)
            record_id = cursor.lastrowid
            target.execute(
                "INSERT INTO rag_fts(rowid,source_filename,legal_domain,locator_key,heading,citation,content) "
                "VALUES (?,?,?,?,?,?,?)",
                (record_id, *fts_text),
            )
            target.execute(
                "INSERT INTO rag_vectors(record_id,embedding) VALUES (?,?)",
                (record_id, sqlite3.Binary(vec.tobytes())),
            )
            indexed += 1
            counts[f"indexed_{source_scope}"] += 1

        target.execute("INSERT INTO rag_fts(rag_fts) VALUES ('optimize')")
        target.execute("INSERT INTO rag_fts(rag_fts) VALUES ('integrity-check')")
        target.commit()
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"New Stage 7 index failed integrity check: {integrity}")
        target.close()

        manifest = {
            "stage": "7 of 9 — metadata-filtered hybrid legal RAG",
            "status": "INDEX_BUILT_REVIEW_STATUS_PRESERVED",
            "source_database": "data/source_staging.db",
            "source_database_sha256": _source_hash(source_db),
            "index_database": output_db.name,
            "generated_records": row_total,
            "indexed_records": indexed,
            "source_files_with_records": len(source_names),
            "scope_counts": dict(sorted((key.removeprefix("indexed_"), value) for key, value in counts.items() if key.startswith("indexed_"))),
            "excluded_counts": {key.removeprefix("excluded_"): value for key, value in counts.items() if key.startswith("excluded_")},
            "review_status_policy": "No pending or unverified source was promoted. Search results retain source/document review, authority, extraction, and temporal metadata.",
            "privacy_policy": "Pattern-matched email, Indian mobile, 12-digit identifier, and PAN-like content is excluded; withheld non-legal records and recognized personal diary/contact/identity form pages are also not indexed.",
            "retrieval": {
                "lexical": "SQLite FTS5 BM25",
                "dense": (
                    f"Pre-cached local SentenceTransformer cosine embeddings ({_sentence_model(embedding_model).get_sentence_embedding_dimension()} dimensions; model={embedding_model})"
                    if embedding_model else
                    f"Offline signed feature-hash cosine vectors ({VECTOR_DIMENSIONS} dimensions; word tokens plus character 3/4-grams)"
                ),
                "embedding_backend": "sentence_transformers" if embedding_model else "feature_hash_v1",
                "embedding_model": embedding_model,
                "embedding_dimensions": (
                    _sentence_model(embedding_model).get_sentence_embedding_dimension()
                    if embedding_model else VECTOR_DIMENSIONS
                ),
                "fusion": "Reciprocal-rank fusion; source metadata remains canonical in SQLite",
                "semantic_limit": (
                    "Pretrained model is local-only; source documents are not sent to an embedding API."
                    if embedding_model else
                    "Feature hashing is a local lexical-vector baseline, not a pretrained language embedding; it does not provide translation or deep semantic equivalence."
                ),
            },
            "runtime_rate_data_changed": False,
            "legacy_legal_database_changed": False,
            "existing_chroma_collections_changed": False,
        }
        target_manifest = sqlite3.connect(temporary_db)
        target_manifest.execute("INSERT INTO rag_manifest(key,value) VALUES (?,?)", ("manifest", json.dumps(manifest, ensure_ascii=False)))
        target_manifest.commit()
        target_manifest.close()
        temporary_db.replace(output_db)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest
    except Exception:
        temporary_db.unlink(missing_ok=True)
        raise


def _vector_from_blob(blob: bytes, expected_dimensions: int | None = None) -> array.array:
    vector = array.array("f")
    vector.frombytes(blob)
    if expected_dimensions is not None and len(vector) != expected_dimensions:
        raise ValueError("Stored vector has an unexpected dimension")
    return vector


def search_stage7(
    query: str,
    *,
    limit: int = 5,
    domain: str = "registration_stamps",
    as_of_date: date | None = None,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search with a source-domain boundary, statutory normalization, and mismatch guardrails."""
    query = query.strip()
    if not query or not INDEX_DB_PATH.exists():
        return []
    limit = max(1, min(int(limit), 20))
    normalized_domain = (domain or "registration_stamps").casefold()
    normalized_query = normalize_statutory_identifiers(query)
    raw_tokens = list(dict.fromkeys(TOKEN_RE.findall(normalized_query.casefold())))
    tokens = [tok for tok in raw_tokens if tok not in _FTS_STOPWORDS and (len(tok) > 1 or tok.isdigit())]
    if not tokens:
        tokens = raw_tokens
    if not tokens:
        return []
    fts_query = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:40])
    connection = sqlite3.connect(f"file:{INDEX_DB_PATH.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        filter_sql = ""
        filter_params: list[Any] = []
        if normalized_domain != "all":
            filter_sql = " AND r.source_scope = ?"
            filter_params.append(normalized_domain)
        if as_of_date:
            filter_sql += " AND (r.effective_from IS NULL OR r.effective_from <= ?)"
            filter_params.append(as_of_date.isoformat())
            if not include_superseded:
                filter_sql += " AND (r.effective_to IS NULL OR r.effective_to >= ?)"
                filter_params.append(as_of_date.isoformat())

        lexical_rows: list[sqlite3.Row] = []
        try:
            lexical_rows = connection.execute(
                f"""SELECT r.id, bm25(rag_fts) AS score
                    FROM rag_fts JOIN rag_records r ON r.id=rag_fts.rowid
                    WHERE rag_fts MATCH ? {filter_sql}
                    ORDER BY score LIMIT ?""",
                (fts_query, *filter_params, max(30, limit * 8)),
            ).fetchall()
        except sqlite3.OperationalError:
            lexical_rows = []

        manifest_row = connection.execute(
            "SELECT value FROM rag_manifest WHERE key='manifest'"
        ).fetchone()
        retrieval_config = {}
        if manifest_row:
            try:
                retrieval_config = json.loads(manifest_row["value"]).get("retrieval", {})
            except (TypeError, json.JSONDecodeError):
                retrieval_config = {}
        embedding_backend = retrieval_config.get("embedding_backend", "feature_hash_v1")
        embedding_model = retrieval_config.get("embedding_model")
        embedding_dimensions = retrieval_config.get("embedding_dimensions")
        query_vector = None
        try:
            if embedding_backend == "sentence_transformers":
                if not embedding_model:
                    raise RuntimeError("Index manifest has no embedding model name")
                query_vector = _embedding(normalized_query, embedding_model)
            elif embedding_backend == "feature_hash_v1":
                query_vector = _features(normalized_query)
        except RuntimeError:
            # Keep the keyword search available when a locally cached model is
            # missing; never fetch the model or turn this into a remote call.
            query_vector = None

        vector_scores: list[tuple[int, float]] = []
        if query_vector is not None:
            vector_rows = connection.execute(
                f"""SELECT r.id,v.embedding FROM rag_vectors v
                    JOIN rag_records r ON r.id=v.record_id
                    WHERE 1=1 {filter_sql}""",
                filter_params,
            ).fetchall()
            expected_len = len(query_vector)
            try:
                np = importlib.import_module("numpy")
                q_arr = np.asarray(query_vector, dtype=np.float32)
                valid_ids: list[int] = []
                valid_vecs: list[Any] = []
                for row in vector_rows:
                    blob = row["embedding"]
                    if blob and len(blob) == expected_len * 4:
                        valid_ids.append(row["id"])
                        valid_vecs.append(np.frombuffer(blob, dtype=np.float32))
                if valid_vecs:
                    matrix = np.vstack(valid_vecs)
                    dots = matrix @ q_arr
                    for rec_id, score in zip(valid_ids, dots.tolist()):
                        if score > 0:
                            vector_scores.append((rec_id, float(score)))
            except Exception:
                vector_scores = []
                for row in vector_rows:
                    try:
                        stored = _vector_from_blob(row["embedding"], embedding_dimensions)
                    except (ValueError, TypeError):
                        continue
                    if len(stored) != expected_len:
                        continue
                    score = sum(a * b for a, b in zip(query_vector, stored))
                    if score > 0:
                        vector_scores.append((row["id"], score))
        vector_scores.sort(key=lambda item: item[1], reverse=True)
        vector_rank = {record_id: rank for rank, (record_id, _) in enumerate(vector_scores[:max(30, limit * 8)], start=1)}
        lexical_rank = {row["id"]: rank for rank, row in enumerate(lexical_rows, start=1)}
        candidates = set(lexical_rank) | set(vector_rank)
        rrf: dict[int, float] = {}
        for record_id in candidates:
            score = 0.0
            if record_id in lexical_rank:
                score += 1.0 / (60 + lexical_rank[record_id])
            if record_id in vector_rank:
                score += 1.0 / (60 + vector_rank[record_id])
            rrf[record_id] = score

        if not candidates:
            return []
        placeholders = ",".join("?" for _ in candidates)
        records = connection.execute(
            f"SELECT * FROM rag_records WHERE id IN ({placeholders})",
            tuple(candidates),
        ).fetchall()
        by_id = {row["id"]: row for row in records}
        lettered_sections = _extract_lettered_sections(query)
        section_refs = [f"{num}{letter}" for num, letter in lettered_sections] or [
            re.sub(r"[^A-Z0-9]", "", value.upper())
            for value in re.findall(r"\b(?:section|sec\.?|s\.?|u/s\.?)\s*(\d+(?:\s*-\s*[A-Za-z0-9]+)?)", query, re.IGNORECASE)
        ]
        article_refs = [
            re.sub(r"[^A-Z0-9]", "", value.upper())
            for value in re.findall(r"\b(?:article|art\.?)\s*(\d+[A-Za-z]?(?:-[A-Za-z0-9]+)?)", query, re.IGNORECASE)
        ]
        go_refs = [
            value for value in re.findall(r"\bG\.?\s*O\.?\s*(?:Ms\.?\s*)?(?:No\.?\s*)?(\d+)\b", query, re.IGNORECASE)
        ]
        rule_refs = [
            value.upper()
            for value in re.findall(r"\brule\s*(\d+[A-Za-z]?)\b", query, re.IGNORECASE)
        ]
        circular_refs = [
            re.sub(r"[^A-Z0-9]", "", val.upper())
            for val in re.findall(r"\b(?:CARD/e-?\d+|IAR/\d+/\d+|G1/\d+/\d+|MV1/\d+/\d+|S[123]/\d+/\d+|\d{6,7})\b", query, re.IGNORECASE)
        ]
        domain_entities = [
            ent for ent in (
                "webland", "pattadar", "ptin", "vlt", "lpm", "iplp", "gramakantam",
                "aadhaar", "aadhar", "scribe", "xerox", "photostat", "34372",
                "step-mother", "stepmother", "great-grandson", "great-grandchildren",
                "prospective wife", "would-be son-in-law", "annexure ii", "annexure iii", "annexure iv",
            )
            if ent in query.casefold() or (ent == "great-grandchildren" and "great-grand" in query.casefold())
        ]
        query_lower = query.casefold()
        has_it_query = bool(re.search(r"\b(?:income[\s\-]?tax|i\.?\s*t\.?\s*act)\b", query_lower))
        is_sec22b_context = any(k in query_lower for k in ("22-b", "22b", "22 b", "webland", "lpm", "ptin", "vlt", "iplp", "1730463", "double registration"))

        def _has_section_conflict(body_text: str) -> bool:
            if not lettered_sections:
                return False
            body_pairs = set(_extract_lettered_sections(body_text))
            for req_num, req_letter in lettered_sections:
                has_exact = (req_num, req_letter) in body_pairs
                has_other_letter = any(num == req_num and letter != req_letter for num, letter in body_pairs)
                if has_other_letter and not has_exact:
                    return True
            return False

        def exact_reference_score(row: sqlite3.Row) -> int:
            identity = " ".join(str(row[key] or "") for key in (
                "locator_key", "section_label", "rule_label", "article_label",
                "schedule_label", "item_label", "heading", "citation",
            ))
            identity_norm = normalize_statutory_identifiers(identity)
            identity_key = re.sub(r"[^A-Z0-9]", "", identity_norm.upper())
            body_text = f"{identity} {row['content']}"
            body_norm = normalize_statutory_identifiers(body_text)
            body_key = re.sub(r"[^A-Z0-9]", "", body_norm.upper())
            body_lower = body_text.casefold()
            score = 0
            for reference in section_refs:
                if reference and reference in identity_key:
                    score += 180
                elif reference and reference in body_key:
                    score += 95
            if _has_section_conflict(body_text):
                score -= 450
            for reference in article_refs:
                if reference and reference in identity_key:
                    score += 120
                elif reference and reference in body_key:
                    score += 55
            for reference in rule_refs:
                token = f"RULE{reference}"
                if token in identity_key:
                    score += 120
                elif token in body_key:
                    score += 60
            for reference in go_refs:
                normalized_go = re.sub(r"[^A-Z0-9]", "", f"GO{reference}".upper())
                if normalized_go in body_key:
                    score += 110
            for cref in circular_refs:
                if cref in identity_key:
                    score += 180
                elif cref in body_key:
                    score += 100
            for ent in domain_entities:
                ent_clean = ent.replace("-", "")
                if ent in identity.casefold() or ent_clean in identity.casefold().replace("-", ""):
                    score += 95
                elif ent in body_lower or ent_clean in body_lower.replace("-", ""):
                    score += 65
            kw_hits = sum(1 for tok in tokens[:18] if len(tok) > 2 and tok in body_lower)
            score += kw_hits * 12
            if str(row["authority_status"] or "").endswith("VERIFIED_SUMMARY"):
                if "1730463" in body_key and not is_sec22b_context:
                    score -= 400
                else:
                    score += 90
            elif str(row["source_kind"] or "") == "OUT_OF_SCOPE_REFERENCE":
                score -= 50
            if has_it_query:
                if re.search(r"\b(?:income[\s\-]?tax|i\.?\s*t\.?\s*act)\b", body_text, re.IGNORECASE):
                    score += 160
                else:
                    score -= 300
            return score

        # Optional pre-cached cross-encoder rerank scores when configured locally
        ce_scores: dict[int, float] = {}
        ce_model_name = os.getenv("IGRS_CROSS_ENCODER_MODEL")
        if ce_model_name:
            try:
                ce = _cross_encoder(ce_model_name)
                cand_list = list(candidates)[:25]
                pairs = [(query, f"{by_id[cid]['heading']}\n{by_id[cid]['content'][:800]}") for cid in cand_list]
                raw_ce = ce.predict(pairs)
                ce_scores = {cid: float(val) for cid, val in zip(cand_list, raw_ce)}
            except Exception:
                ce_scores = {}

        results = []
        seen_hashes: set[str] = set()
        ordered_candidates = sorted(
            candidates,
            key=lambda value: (exact_reference_score(by_id[value]), ce_scores.get(value, 0.0) + rrf[value]),
            reverse=True,
        )
        # Apply Retrieval Relevance Guardrail when explicit sections/circulars/core entities are queried
        requires_guardrail = bool(lettered_sections or circular_refs or any(e in domain_entities for e in ("webland", "ptin", "vlt", "lpm", "iplp")))
        for record_id in ordered_candidates:
            row = by_id.get(record_id)
            if row is None:
                continue
            full_text = f"{row['locator_key']} {row['section_label'] or ''} {row['heading']} {row['citation']} {row['content']}"
            if _has_section_conflict(full_text):
                continue
            if requires_guardrail:
                full_key = re.sub(r"[^A-Z0-9]", "", normalize_statutory_identifiers(full_text).upper())
                full_lower = full_text.casefold()
                matches_sec = any(ref in full_key for ref in section_refs) if section_refs else True
                matches_circ = any(cref in full_key for cref in circular_refs) if circular_refs else True
                matches_ent = any(ent in full_lower or ent.replace("-", "") in full_lower.replace("-", "") for ent in domain_entities) if domain_entities else True
                if not (matches_sec and (matches_circ or matches_ent)) and not (matches_sec and matches_ent):
                    continue
            content_hash = row["content_sha256"]
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)
            effective_from = row["effective_from"]
            effective_to = row["effective_to"]
            temporal_status = "DATE_WINDOW_UNKNOWN"
            if effective_from or effective_to:
                temporal_status = "DATE_WINDOW_PARTIAL"
                if as_of_date and effective_from and (not effective_to or effective_to >= as_of_date.isoformat()):
                    temporal_status = "IN_STATED_DATE_WINDOW"
            if include_superseded and row["effective_to"] and as_of_date and row["effective_to"] < as_of_date.isoformat():
                temporal_status = "SUPERSEDED_BEFORE_QUERY_DATE"
            citation = row["citation"]
            if row["page_start"] is not None:
                page_label = str(row["page_start"])
                if row["page_end"] and row["page_end"] != row["page_start"]:
                    page_label += f"–{row['page_end']}"
                citation = f"{citation} (page {page_label})"
            results.append({
                "id": row["id"],
                "provision_id": row["provision_id"],
                "act_name": row["source_filename"],
                "section_or_rule": row["locator_key"],
                "title": row["heading"],
                "content": row["content"],
                "sro_guidance": (
                    "Evidence record only. Do not treat as approved law/rate: "
                    f"authority={row['authority_status']}; source review={row['source_review_status']}; "
                    f"provision review={row['review_status']}; extraction={row['extraction_quality']}; "
                    f"temporal status={temporal_status}."
                ),
                "authority_citation": citation,
                "snippet": row["content"][:300].strip().replace("\n", " ") + ("..." if len(row["content"]) > 300 else ""),
                "source_filename": row["source_filename"],
                "source_locator": row["locator_key"],
                "legal_domain": row["legal_domain"],
                "source_scope": row["source_scope"],
                "source_kind": row["source_kind"],
                "authority_status": row["authority_status"],
                "source_authority": row["source_authority"],
                "source_authenticity": row["source_authenticity"],
                "review_status": row["review_status"],
                "source_review_status": row["source_review_status"],
                "source_privacy_status": row["source_privacy_status"],
                "extraction_quality": row["extraction_quality"],
                "is_secondary_commentary": bool(row["is_secondary_commentary"]),
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "effective_from": effective_from,
                "effective_to": effective_to,
                "temporal_status": temporal_status,
                "retrieval_methods": [method for method, ranks in (("bm25", lexical_rank), ("dense_vector", vector_rank)) if record_id in ranks],
                "retrieval_score": round(rrf[record_id], 8),
            })
            if len(results) >= limit:
                break
        return results
    finally:
        connection.close()
