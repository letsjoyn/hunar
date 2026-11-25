"""Portable SQLAlchemy schema for the source-review staging corpus.

These tables are deliberately separate from the live calculator and legacy
legal-search stores. Staged records require review before production use.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class StagingBase(DeclarativeBase):
    pass


class SourceDocument(StagingBase):
    """One immutable version of a supplied source file."""

    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("source_path", "source_sha256", name="uq_source_path_hash"),
        CheckConstraint(
            "authority_classification IN ('PRIMARY_CANDIDATE', 'SECONDARY', "
            "'ADMIN_BASELINE', 'UNVERIFIED', 'OUT_OF_SCOPE')",
            name="ck_source_authority_classification",
        ),
        CheckConstraint(
            "review_status IN ('PENDING_REVIEW', 'IN_REVIEW', 'APPROVED', 'REJECTED', 'QUARANTINED')",
            name="ck_source_review_status",
        ),
        Index("ix_source_documents_hash", "source_sha256"),
        Index("ix_source_documents_domain_review", "legal_domain", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    issuer: Mapped[str | None] = mapped_column(String(300))
    document_number: Mapped[str | None] = mapped_column(String(160))
    legal_domain: Mapped[str] = mapped_column(String(100), nullable=False, default="UNCLASSIFIED")
    authority_classification: Mapped[str] = mapped_column(String(30), nullable=False, default="UNVERIFIED")
    authenticity_status: Mapped[str] = mapped_column(String(40), nullable=False, default="UNVERIFIED")
    issued_on: Mapped[date | None] = mapped_column(Date)
    published_on: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    supersedes_document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.id"))
    extraction_status: Mapped[str] = mapped_column(String(40), nullable=False, default="NOT_STARTED")
    extracted_text_sha256: Mapped[str | None] = mapped_column(String(64))
    ocr_status: Mapped[str] = mapped_column(String(40), nullable=False, default="NOT_APPLICABLE")
    ocr_language: Mapped[str | None] = mapped_column(String(30))
    ocr_quality_summary: Mapped[dict | None] = mapped_column(JSON)
    privacy_status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING_REVIEW")
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_REVIEW")
    source_metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class LegalProvision(StagingBase):
    """A separately cited section, rule, article, schedule item, or order clause."""

    __tablename__ = "legal_provisions"
    __table_args__ = (
        UniqueConstraint("source_document_id", "locator_key", name="uq_provision_source_locator"),
        CheckConstraint(
            "review_status IN ('PENDING_REVIEW', 'IN_REVIEW', 'APPROVED', 'REJECTED', 'QUARANTINED')",
            name="ck_provision_review_status",
        ),
        Index("ix_provisions_domain_locator", "legal_domain", "locator_key"),
        Index("ix_provisions_effective_period", "effective_from", "effective_to"),
        Index("ix_provisions_review_status", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False
    )
    parent_provision_id: Mapped[str | None] = mapped_column(ForeignKey("legal_provisions.id"))
    provision_type: Mapped[str] = mapped_column(String(40), nullable=False)
    legal_domain: Mapped[str] = mapped_column(String(100), nullable=False)
    locator_key: Mapped[str] = mapped_column(String(300), nullable=False)
    part_label: Mapped[str | None] = mapped_column(String(120))
    chapter_label: Mapped[str | None] = mapped_column(String(120))
    section_label: Mapped[str | None] = mapped_column(String(120))
    rule_label: Mapped[str | None] = mapped_column(String(120))
    article_label: Mapped[str | None] = mapped_column(String(120))
    schedule_label: Mapped[str | None] = mapped_column(String(120))
    item_label: Mapped[str | None] = mapped_column(String(120))
    heading: Mapped[str | None] = mapped_column(Text)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    authority_status: Mapped[str] = mapped_column(String(40), nullable=False, default="UNVERIFIED")
    extraction_quality: Mapped[str] = mapped_column(String(40), nullable=False, default="UNREVIEWED")
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_REVIEW")
    is_secondary_commentary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ProvisionRelation(StagingBase):
    """Explicit amendment, supersession, citation, or dependency edge."""

    __tablename__ = "provision_relations"
    __table_args__ = (
        UniqueConstraint("from_provision_id", "to_provision_id", "relation_type", name="uq_provision_relation"),
        Index("ix_provision_relations_to", "to_provision_id", "relation_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_provision_id: Mapped[str] = mapped_column(
        ForeignKey("legal_provisions.id", ondelete="CASCADE"), nullable=False
    )
    to_provision_id: Mapped[str] = mapped_column(
        ForeignKey("legal_provisions.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_on: Mapped[date | None] = mapped_column(Date)
    source_document_id: Mapped[str | None] = mapped_column(ForeignKey("source_documents.id"))
    evidence_locator: Mapped[str | None] = mapped_column(String(300))
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_REVIEW")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class StagedDeedRate(StagingBase):
    """Workbook/imported rate row; never used by calculations while staged."""

    __tablename__ = "staged_deed_rates"
    __table_args__ = (
        UniqueConstraint("source_document_id", "source_row_locator", name="uq_staged_rate_source_row"),
        CheckConstraint(
            "review_status IN ('PENDING_REVIEW', 'IN_REVIEW', 'APPROVED', 'REJECTED', 'QUARANTINED')",
            name="ck_staged_rate_review_status",
        ),
        Index("ix_staged_rates_deed_period", "deed_code", "effective_from", "effective_to"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), nullable=False)
    source_row_locator: Mapped[str] = mapped_column(String(120), nullable=False)
    deed_code: Mapped[str] = mapped_column(String(50), nullable=False)
    classification: Mapped[str | None] = mapped_column(String(100))
    deed_type: Mapped[str | None] = mapped_column(Text)
    article_ref: Mapped[str | None] = mapped_column(String(120))
    book_number: Mapped[int | None] = mapped_column(Integer)
    stamp_duty_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    transfer_duty_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    registration_fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    registration_fee_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    registration_fee_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    calculation_basis: Mapped[str | None] = mapped_column(String(100))
    area_type: Mapped[str] = mapped_column(String(60), nullable=False, default="UNSPECIFIED")
    property_subtype: Mapped[str] = mapped_column(String(80), nullable=False, default="UNSPECIFIED")
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="BASELINE_ONLY")
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_REVIEW")
    row_metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class RateEvidence(StagingBase):
    """A traceable source link supporting or contradicting one staged rate."""

    __tablename__ = "rate_evidence"
    __table_args__ = (
        UniqueConstraint("staged_rate_id", "source_document_id", "source_locator", "relation_type", name="uq_rate_evidence_link"),
        Index("ix_rate_evidence_source", "source_document_id", "source_locator"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    staged_rate_id: Mapped[str] = mapped_column(
        ForeignKey("staged_deed_rates.id", ondelete="CASCADE"), nullable=False
    )
    source_document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), nullable=False)
    provision_id: Mapped[str | None] = mapped_column(ForeignKey("legal_provisions.id"))
    source_locator: Mapped[str] = mapped_column(String(300), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    citation: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_REVIEW")
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class StagingReviewEvent(StagingBase):
    """Append-only audit record for a review or staging decision."""

    __tablename__ = "staging_review_events"
    __table_args__ = (Index("ix_staging_review_target", "target_type", "target_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
