-- ====================================================================
-- PostgreSQL 16 DDL for AP IGRS Platform
-- ====================================================================
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. Temporal Duty & Fee Rates (Single Source of Truth)
CREATE TABLE IF NOT EXISTS duty_rates (
    id                  SERIAL PRIMARY KEY,
    instrument_code     VARCHAR(50) NOT NULL,
    article_ref         VARCHAR(50) NOT NULL,
    area_type           VARCHAR(50) NOT NULL,
    property_subtype    VARCHAR(50) DEFAULT 'ALL',
    duration_min_months INT DEFAULT NULL,
    duration_max_months INT DEFAULT NULL,
    calc_basis          VARCHAR(50) NOT NULL,
    stamp_duty_type     VARCHAR(20) NOT NULL,
    stamp_duty_value    NUMERIC(12,4) NOT NULL,
    stamp_duty_min      NUMERIC(12,2) DEFAULT NULL,
    stamp_duty_max      NUMERIC(12,2) DEFAULT NULL,
    transfer_duty_pct   NUMERIC(8,4) DEFAULT 0,
    reg_fee_type        VARCHAR(20) NOT NULL,
    reg_fee_value       NUMERIC(12,4) NOT NULL,
    reg_fee_min         NUMERIC(12,2) DEFAULT NULL,
    reg_fee_max         NUMERIC(12,2) DEFAULT NULL,
    is_adjustable       BOOLEAN DEFAULT FALSE,
    effective_from      DATE NOT NULL,
    effective_to        DATE DEFAULT NULL,
    authority_citation  TEXT NOT NULL,
    verification_status VARCHAR(20) DEFAULT 'VERIFIED',
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_duty_lookup ON duty_rates (instrument_code, area_type, effective_from, effective_to);

-- 2. Temporal Structure & Depreciation Rates
CREATE TABLE IF NOT EXISTS structure_rates (
    id                  SERIAL PRIMARY KEY,
    structure_category  VARCHAR(80) NOT NULL,
    area_tier           VARCHAR(50) NOT NULL,
    rate_per_sqft       NUMERIC(10,2) NOT NULL,
    extra_floor_adder   NUMERIC(10,2) DEFAULT 0,
    effective_from      DATE NOT NULL,
    effective_to        DATE DEFAULT NULL,
    authority_citation  TEXT NOT NULL
);

-- 3. Temporal CARD User Charges
CREATE TABLE IF NOT EXISTS user_charges (
    id                  SERIAL PRIMARY KEY,
    service_code        VARCHAR(50) NOT NULL,
    description         TEXT,
    value_threshold_min NUMERIC(14,2) DEFAULT 0,
    value_threshold_max NUMERIC(14,2) DEFAULT NULL,
    included_sheets     INT DEFAULT 10,
    charge_amount       NUMERIC(10,2) NOT NULL,
    effective_from      DATE NOT NULL,
    effective_to        DATE DEFAULT NULL,
    authority_citation  TEXT NOT NULL,
    verification_status VARCHAR(20) DEFAULT 'VERIFIED',
    notes               TEXT
);

-- 4. Legal Documents & Supersession Graph
CREATE TABLE IF NOT EXISTS legal_documents (
    id                  SERIAL PRIMARY KEY,
    domain              VARCHAR(40) NOT NULL,
    doc_type            VARCHAR(30) NOT NULL,
    identifier          VARCHAR(120) UNIQUE NOT NULL,
    title               TEXT NOT NULL,
    effective_date      DATE,
    status              VARCHAR(20) NOT NULL DEFAULT 'active',
    superseded_by_id    INT REFERENCES legal_documents(id),
    source_pdf_path     TEXT,
    verified_by         VARCHAR(100),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- 5. Hybrid RAG Chunks (pgvector + PostgreSQL Full-Text Search)
CREATE TABLE IF NOT EXISTS legal_chunks (
    id                  BIGSERIAL PRIMARY KEY,
    document_id         INT NOT NULL REFERENCES legal_documents(id) ON DELETE CASCADE,
    domain              VARCHAR(40) NOT NULL,
    section_or_ref      VARCHAR(120),
    page_number         INT,
    content             TEXT NOT NULL,
    language            VARCHAR(10) DEFAULT 'en',
    effective_date      DATE,
    status              VARCHAR(20) NOT NULL DEFAULT 'active',
    embedding           vector(1024),
    fts_vector          tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON legal_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_fts ON legal_chunks USING GIN (fts_vector);
CREATE INDEX IF NOT EXISTS idx_chunks_meta ON legal_chunks (domain, status, effective_date);

-- 6. Admin Yearly Update Staging & Audit Logs
CREATE TABLE IF NOT EXISTS rate_change_proposals (
    id                  SERIAL PRIMARY KEY,
    uploaded_pdf_path   TEXT NOT NULL,
    proposed_json       JSONB NOT NULL,
    status              VARCHAR(20) DEFAULT 'PENDING',
    reviewed_by         VARCHAR(100),
    reviewed_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS query_audit_logs (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             VARCHAR(100),
    user_role           VARCHAR(30),
    query_text          TEXT NOT NULL,
    tools_called        JSONB,
    response_text       TEXT NOT NULL,
    citations           JSONB,
    feedback_score      SMALLINT,
    feedback_note       TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
