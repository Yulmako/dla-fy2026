"""SQLAlchemy 2.0 ORM models. Portable across SQLite (dev) and PostgreSQL.

Kept in exact parity with sql/schema_sqlite.sql (tests/test_models.py).
FY2026 is enforced at the database level by CHECK constraints on
fact_transactions, so out-of-period records cannot be loaded.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index,
                        Integer, Numeric, String, Text, UniqueConstraint, false)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.fiscal import FISCAL_YEAR, FY_END, FY_START

Money = Numeric(20, 2)


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ lineage
class EtlLog(Base):
    __tablename__ = "etl_log"
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_type: Mapped[str] = mapped_column(String(30))
    market_key: Mapped[str | None] = mapped_column(String(60))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    records_extracted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    records_inserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    records_updated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    records_excluded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(10))
    error_message: Mapped[str | None] = mapped_column(Text)
    source_last_updated: Mapped[str | None] = mapped_column(String(40))
    data_through_date: Mapped[date | None] = mapped_column(Date)
    reporting_normal_through: Mapped[date | None] = mapped_column(Date)
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    lag_assessment_json: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("status IN ('running','success','failed','partial')",
                        name="ck_etl_log_status"),
    )


class RawApiResponse(Base):
    __tablename__ = "raw_api_responses"
    response_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    endpoint: Mapped[str] = mapped_column(String(200))
    http_method: Mapped[str] = mapped_column(String(6))
    request_json: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_path: Mapped[str] = mapped_column(Text)
    response_sha256: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("response_path", name="uq_raw_api_responses_path"),
        Index("ix_raw_api_responses_run", "run_id"),
    )


class RawDownloadFile(Base):
    __tablename__ = "raw_download_files"
    download_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    market_key: Mapped[str] = mapped_column(String(60))
    basis: Mapped[str] = mapped_column(String(10))
    job_file_name: Mapped[str] = mapped_column(String(200))
    file_url: Mapped[str] = mapped_column(Text)
    zip_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    expected_rows: Mapped[int | None] = mapped_column(Integer)
    rows_read: Mapped[int | None] = mapped_column(Integer)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (UniqueConstraint("run_id", "job_file_name", name="uq_raw_download_files_run_job"),)


# --------------------------------------------------------------- dimensions
class DimCompany(Base):
    """Default ranking entity, keyed on the REPORTED parent UEI (never on names)."""
    __tablename__ = "dim_companies"
    company_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_key: Mapped[str] = mapped_column(String(320))
    company_uei: Mapped[str | None] = mapped_column(String(12))
    company_name: Mapped[str] = mapped_column(Text)
    parent_status: Mapped[str] = mapped_column(String(20))
    last_seen_run_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    __table_args__ = (
        UniqueConstraint("company_key", name="uq_dim_companies_key"),
        CheckConstraint("parent_status IN ('reported_parent','no_parent_reported','no_uei')",
                        name="ck_dim_companies_parent_status"),
    )


class DimRecipient(Base):
    __tablename__ = "dim_recipients"
    recipient_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_key: Mapped[str] = mapped_column(String(300))
    recipient_uei: Mapped[str | None] = mapped_column(String(12))
    recipient_duns: Mapped[str | None] = mapped_column(String(9))
    recipient_name: Mapped[str] = mapped_column(Text)
    recipient_name_normalized: Mapped[str] = mapped_column(Text)
    parent_recipient_name: Mapped[str | None] = mapped_column(Text)
    parent_recipient_uei: Mapped[str | None] = mapped_column(String(12))
    country: Mapped[str | None] = mapped_column(String(3))
    state: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(100))
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_companies.company_id"))
    parent_uei_conflict: Mapped[bool] = mapped_column(Boolean, default=False,
                                                      server_default=false())
    first_seen_run_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    last_seen_run_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    __table_args__ = (
        UniqueConstraint("recipient_key", name="uq_dim_recipients_key"),
        Index("ix_dim_recipients_uei", "recipient_uei"),
        Index("ix_dim_recipients_parent_uei", "parent_recipient_uei"),
        Index("ix_dim_recipients_company", "company_id"),
    )


class DimAgency(Base):
    __tablename__ = "dim_agencies"
    agency_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agency_key: Mapped[str] = mapped_column(String(20))
    toptier_code: Mapped[str] = mapped_column(String(4))
    parent_agency_name: Mapped[str] = mapped_column(Text)
    agency_code: Mapped[str | None] = mapped_column(String(4))
    agency_name: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("agency_key", name="uq_dim_agencies_key"),
        Index("ix_dim_agencies_toptier", "toptier_code"),
    )


class DimOffice(Base):
    __tablename__ = "dim_offices"
    office_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    office_name: Mapped[str | None] = mapped_column(Text)
    agency_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_agencies.agency_id"))


class DimNaics(Base):
    __tablename__ = "dim_naics"
    naics_code: Mapped[str] = mapped_column(String(6), primary_key=True)
    naics_description: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str] = mapped_column(String(2))
    __table_args__ = (
        CheckConstraint("sector = substr(naics_code, 1, 2)", name="ck_dim_naics_sector"),
    )


class DimPsc(Base):
    __tablename__ = "dim_psc"
    psc_code: Mapped[str] = mapped_column(String(4), primary_key=True)
    psc_description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(10))
    psc_group: Mapped[str] = mapped_column(String(2))
    __table_args__ = (
        CheckConstraint("category IN ('product','service','rnd')", name="ck_dim_psc_category"),
    )


# -------------------------------------------------------------------- facts
class FactAward(Base):
    """Awards with >= 1 FY2026 transaction. Amounts here are LIFETIME values."""
    __tablename__ = "fact_awards"
    award_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    piid: Mapped[str | None] = mapped_column(String(60))
    parent_award_piid: Mapped[str | None] = mapped_column(String(60))
    recipient_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_recipients.recipient_id"))
    agency_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_agencies.agency_id"))
    funding_agency_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_agencies.agency_id"))
    naics_code: Mapped[str | None] = mapped_column(String(6), ForeignKey("dim_naics.naics_code"))
    psc_code: Mapped[str | None] = mapped_column(String(4), ForeignKey("dim_psc.psc_code"))
    award_type_code: Mapped[str | None] = mapped_column(String(10))
    award_type: Mapped[str | None] = mapped_column(String(100))
    award_description: Mapped[str | None] = mapped_column(Text)
    award_amount: Mapped[Decimal | None] = mapped_column(Money)          # ceiling, lifetime
    current_award_value: Mapped[Decimal | None] = mapped_column(Money)   # base+exercised, lifetime
    cumulative_obligation: Mapped[Decimal | None] = mapped_column(Money) # obligated to date
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    usaspending_permalink: Mapped[str | None] = mapped_column(Text)
    first_seen_run_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    last_seen_run_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    __table_args__ = (
        Index("ix_fact_awards_recipient", "recipient_id"),
        Index("ix_fact_awards_agency", "agency_id"),
    )


class FactTransaction(Base):
    """FY2026 contract transactions – the only source of FY2026 obligations."""
    __tablename__ = "fact_transactions"
    transaction_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    award_id: Mapped[str] = mapped_column(String(120), ForeignKey("fact_awards.award_id"))
    recipient_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_recipients.recipient_id"))
    agency_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_agencies.agency_id"))
    funding_agency_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("dim_agencies.agency_id"))
    office_code: Mapped[str | None] = mapped_column(String(6), ForeignKey("dim_offices.office_code"))
    naics_code: Mapped[str | None] = mapped_column(String(6), ForeignKey("dim_naics.naics_code"))
    psc_code: Mapped[str | None] = mapped_column(String(4), ForeignKey("dim_psc.psc_code"))
    modification_number: Mapped[str | None] = mapped_column(String(40))
    transaction_number: Mapped[str | None] = mapped_column(String(10))
    transaction_date: Mapped[date] = mapped_column(Date)
    transaction_amount: Mapped[Decimal] = mapped_column(Money)
    transaction_type: Mapped[str | None] = mapped_column(String(100))
    transaction_description: Mapped[str | None] = mapped_column(Text)
    recipient_name_raw: Mapped[str | None] = mapped_column(Text)
    parent_recipient_uei_raw: Mapped[str | None] = mapped_column(String(12))
    parent_recipient_name_raw: Mapped[str | None] = mapped_column(Text)
    pop_state_code: Mapped[str | None] = mapped_column(String(2))
    pop_country_code: Mapped[str | None] = mapped_column(String(3))
    potential_duplicate_group: Mapped[str | None] = mapped_column(String(64))
    fiscal_year: Mapped[int] = mapped_column(Integer)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    __table_args__ = (
        CheckConstraint(f"fiscal_year = {FISCAL_YEAR}", name="ck_fact_transactions_fy"),
        CheckConstraint(f"transaction_date >= '{FY_START.isoformat()}' AND "
                        f"transaction_date <= '{FY_END.isoformat()}'",
                        name="ck_fact_transactions_date"),
        Index("ix_fact_transactions_award", "award_id"),
        Index("ix_fact_transactions_recipient", "recipient_id"),
        Index("ix_fact_transactions_agency", "agency_id"),
        Index("ix_fact_transactions_naics", "naics_code"),
        Index("ix_fact_transactions_psc", "psc_code"),
        Index("ix_fact_transactions_date", "transaction_date"),
    )


# ----------------------------------------------------------------- markets
class MarketSegment(Base):
    __tablename__ = "market_segments"
    segment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10))
    definition_hash: Mapped[str] = mapped_column(String(16))
    definition_yaml: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (
        UniqueConstraint("market_key", "definition_hash", name="uq_market_segments_key_hash"),
        CheckConstraint("status IN ('candidate','validated','planned')",
                        name="ck_market_segments_status"),
    )


class FactTransactionSegment(Base):
    __tablename__ = "fact_transaction_segments"
    transaction_id: Mapped[str] = mapped_column(
        String(160), ForeignKey("fact_transactions.transaction_id", ondelete="CASCADE"),
        primary_key=True)
    segment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("market_segments.segment_id"), primary_key=True)
    matched_by: Mapped[str] = mapped_column(String(10))
    match_tier: Mapped[str] = mapped_column(String(10))
    keyword_tags: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("matched_by IN ('naics','psc','both','all')", name="ck_fts_matched_by"),
        CheckConstraint("match_tier IN ('core','extended')", name="ck_fts_tier"),
        Index("ix_fts_segment", "segment_id"),
    )


# ------------------------------------------------------- quality & matching
class EntityMatchCandidate(Base):
    __tablename__ = "entity_match_candidates"
    candidate_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_id: Mapped[int] = mapped_column(Integer, ForeignKey("dim_recipients.recipient_id"))
    original_name: Mapped[str] = mapped_column(Text)
    potential_parent_name: Mapped[str | None] = mapped_column(Text)
    potential_parent_uei: Mapped[str | None] = mapped_column(String(12))
    matching_identifier: Mapped[str] = mapped_column(String(30))
    match_confidence: Mapped[float] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(String(10), default="pending",
                                               server_default="pending")
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        UniqueConstraint("recipient_id", "potential_parent_uei", name="uq_emc_recipient_parent"),
        CheckConstraint("match_confidence >= 0 AND match_confidence <= 1",
                        name="ck_emc_confidence"),
        CheckConstraint("review_status IN ('pending','confirmed','rejected')",
                        name="ck_emc_review_status"),
    )


class DqIssue(Base):
    __tablename__ = "dq_issues"
    issue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("etl_log.run_id"))
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_key: Mapped[str | None] = mapped_column(String(200))
    check_name: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10))
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','error')", name="ck_dq_issues_severity"),
        Index("ix_dq_issues_run", "run_id"),
    )
