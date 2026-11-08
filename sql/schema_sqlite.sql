-- Federal Competitor Intelligence – initial schema (SQLite, Phases 1–3).
-- Mirrors src/database/models.py exactly (tests/test_models.py checks parity).
-- Reporting period is fixed to FY2026 and enforced with CHECK constraints.
-- Money: NUMERIC(20,2). Dates: ISO-8601 text 'YYYY-MM-DD' (SQLite).

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------ lineage
CREATE TABLE IF NOT EXISTS etl_log (
    run_id               VARCHAR(40)  PRIMARY KEY,
    run_type             VARCHAR(30)  NOT NULL,           -- phase1_probe | extract | load | refresh
    market_key           VARCHAR(60),
    started_at           DATETIME     NOT NULL,
    finished_at          DATETIME,
    records_extracted    INTEGER      NOT NULL DEFAULT 0,
    records_inserted     INTEGER      NOT NULL DEFAULT 0,
    records_updated      INTEGER      NOT NULL DEFAULT 0,
    records_excluded     INTEGER      NOT NULL DEFAULT 0,
    status               VARCHAR(10)  NOT NULL,
    error_message        TEXT,
    source_last_updated  VARCHAR(40),                      -- USAspending /awards/last_updated/
    data_through_date    DATE,                             -- MAX(action_date) actually loaded
    reporting_normal_through DATE,                         -- from reporting-lag assessment
    is_provisional       BOOLEAN      NOT NULL DEFAULT 0,  -- 1 while DoD reporting lag applies
    lag_assessment_json  TEXT,                             -- month-by-month lag classification
    CONSTRAINT ck_etl_log_status CHECK (status IN ('running','success','failed','partial'))
);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    response_id      INTEGER      PRIMARY KEY AUTOINCREMENT,
    run_id           VARCHAR(40)  NOT NULL REFERENCES etl_log(run_id),
    endpoint         VARCHAR(200) NOT NULL,
    http_method      VARCHAR(6)   NOT NULL,
    request_json     TEXT,
    http_status      INTEGER,
    response_path    TEXT         NOT NULL,                -- file under data/raw/
    response_sha256  CHAR(64)     NOT NULL,
    fetched_at       DATETIME     NOT NULL,
    elapsed_ms       INTEGER,
    CONSTRAINT uq_raw_api_responses_path UNIQUE (response_path)
);
CREATE INDEX IF NOT EXISTS ix_raw_api_responses_run ON raw_api_responses (run_id);

-- Official download files (zip) fetched from files.usaspending.gov, kept under data/raw/.
CREATE TABLE IF NOT EXISTS raw_download_files (
    download_id     INTEGER      PRIMARY KEY AUTOINCREMENT,
    run_id          VARCHAR(40)  NOT NULL REFERENCES etl_log(run_id),
    market_key      VARCHAR(60)  NOT NULL,
    basis           VARCHAR(10)  NOT NULL,                 -- naics | psc
    job_file_name   VARCHAR(200) NOT NULL,
    file_url        TEXT         NOT NULL,
    zip_path        TEXT         NOT NULL,
    sha256          CHAR(64)     NOT NULL,
    bytes           INTEGER      NOT NULL,
    expected_rows   INTEGER,
    rows_read       INTEGER,
    downloaded_at   DATETIME     NOT NULL,
    CONSTRAINT uq_raw_download_files_run_job UNIQUE (run_id, job_file_name)
);

-- --------------------------------------------------------------- dimensions
-- Company = default ranking entity. Key = reported parent UEI; a recipient
-- without a reported parent UEI is its own company. Never built from names.
CREATE TABLE IF NOT EXISTS dim_companies (
    company_id        INTEGER      PRIMARY KEY AUTOINCREMENT,
    company_key       VARCHAR(320) NOT NULL,   -- 'PARENT:<parent uei>' | 'UEI:<uei>' | 'NOUEI:<recipient_key>'
    company_uei       VARCHAR(12),             -- parent UEI, or own UEI when no parent reported
    company_name      TEXT         NOT NULL,   -- reported parent name; else recipient name
    parent_status     VARCHAR(20)  NOT NULL,
    last_seen_run_id  VARCHAR(40)  REFERENCES etl_log(run_id),
    CONSTRAINT uq_dim_companies_key UNIQUE (company_key),
    CONSTRAINT ck_dim_companies_parent_status CHECK (parent_status IN ('reported_parent','no_parent_reported','no_uei'))
);

CREATE TABLE IF NOT EXISTS dim_recipients (
    recipient_id               INTEGER      PRIMARY KEY AUTOINCREMENT,
    recipient_key              VARCHAR(300) NOT NULL,      -- 'UEI:<uei>' or 'NOUEI:<norm name>|<country>|<state>'
    recipient_uei              VARCHAR(12),
    recipient_duns             VARCHAR(9),
    recipient_name             TEXT         NOT NULL,      -- latest name seen for this key
    recipient_name_normalized  TEXT         NOT NULL,
    parent_recipient_name      TEXT,                       -- as reported in USAspending record
    parent_recipient_uei       VARCHAR(12),
    country                    VARCHAR(3),
    state                      VARCHAR(2),
    city                       VARCHAR(100),
    company_id                 INTEGER      REFERENCES dim_companies(company_id),
    parent_uei_conflict        BOOLEAN      NOT NULL DEFAULT 0,  -- >1 parent UEI reported in FY2026
    first_seen_run_id          VARCHAR(40)  REFERENCES etl_log(run_id),
    last_seen_run_id           VARCHAR(40)  REFERENCES etl_log(run_id),
    CONSTRAINT uq_dim_recipients_key UNIQUE (recipient_key)
);
CREATE INDEX IF NOT EXISTS ix_dim_recipients_uei ON dim_recipients (recipient_uei);
CREATE INDEX IF NOT EXISTS ix_dim_recipients_parent_uei ON dim_recipients (parent_recipient_uei);
CREATE INDEX IF NOT EXISTS ix_dim_recipients_company ON dim_recipients (company_id);

CREATE TABLE IF NOT EXISTS dim_agencies (
    agency_id           INTEGER      PRIMARY KEY AUTOINCREMENT,
    agency_key          VARCHAR(20)  NOT NULL,             -- '<toptier_code>|<subtier_code or ->'
    toptier_code        VARCHAR(4)   NOT NULL,
    parent_agency_name  TEXT         NOT NULL,             -- toptier name, e.g. Department of Defense
    agency_code         VARCHAR(4),                        -- subtier code
    agency_name         TEXT,                              -- subtier name, e.g. Department of the Army
    CONSTRAINT uq_dim_agencies_key UNIQUE (agency_key)
);
CREATE INDEX IF NOT EXISTS ix_dim_agencies_toptier ON dim_agencies (toptier_code);

CREATE TABLE IF NOT EXISTS dim_offices (
    office_code  VARCHAR(6)  PRIMARY KEY,
    office_name  TEXT,
    agency_id    INTEGER     REFERENCES dim_agencies(agency_id)
);

CREATE TABLE IF NOT EXISTS dim_naics (
    naics_code         VARCHAR(6)  PRIMARY KEY,
    naics_description  TEXT,
    sector             VARCHAR(2)  NOT NULL,
    CONSTRAINT ck_dim_naics_sector CHECK (sector = substr(naics_code, 1, 2))
);

CREATE TABLE IF NOT EXISTS dim_psc (
    psc_code         VARCHAR(4)  PRIMARY KEY,
    psc_description  TEXT,
    category         VARCHAR(10) NOT NULL,                 -- product | service | rnd
    psc_group        VARCHAR(2)  NOT NULL,
    CONSTRAINT ck_dim_psc_category CHECK (category IN ('product','service','rnd'))
);

-- -------------------------------------------------------------------- facts
-- One row per award that has >= 1 FY2026 transaction. Award-level amounts are
-- LIFETIME values kept for context; they are never summed as FY2026 spending.
CREATE TABLE IF NOT EXISTS fact_awards (
    award_id                VARCHAR(120) PRIMARY KEY,      -- contract_award_unique_key (CONT_AWD_...)
    piid                    VARCHAR(60),
    parent_award_piid       VARCHAR(60),
    recipient_id            INTEGER      REFERENCES dim_recipients(recipient_id),
    agency_id               INTEGER      REFERENCES dim_agencies(agency_id),
    funding_agency_id       INTEGER      REFERENCES dim_agencies(agency_id),
    naics_code              VARCHAR(6)   REFERENCES dim_naics(naics_code),
    psc_code                VARCHAR(4)   REFERENCES dim_psc(psc_code),
    award_type_code         VARCHAR(10),
    award_type              VARCHAR(100),
    award_description       TEXT,
    award_amount            NUMERIC(20,2),                 -- ceiling: base and all options (lifetime)
    current_award_value     NUMERIC(20,2),                 -- base and exercised options (lifetime)
    cumulative_obligation   NUMERIC(20,2),                 -- total obligated to date (lifetime)
    start_date              DATE,
    end_date                DATE,
    source_updated_at       DATETIME,
    usaspending_permalink   TEXT,
    first_seen_run_id       VARCHAR(40)  REFERENCES etl_log(run_id),
    last_seen_run_id        VARCHAR(40)  REFERENCES etl_log(run_id)
);
CREATE INDEX IF NOT EXISTS ix_fact_awards_recipient ON fact_awards (recipient_id);
CREATE INDEX IF NOT EXISTS ix_fact_awards_agency ON fact_awards (agency_id);

-- One row per FY2026 contract transaction (modification). Source of ALL
-- FY2026 obligation metrics. NAICS/PSC/agency are per transaction.
CREATE TABLE IF NOT EXISTS fact_transactions (
    transaction_id          VARCHAR(160) PRIMARY KEY,      -- contract_transaction_unique_key
    award_id                VARCHAR(120) NOT NULL REFERENCES fact_awards(award_id),
    recipient_id            INTEGER      REFERENCES dim_recipients(recipient_id),
    agency_id               INTEGER      REFERENCES dim_agencies(agency_id),
    funding_agency_id       INTEGER      REFERENCES dim_agencies(agency_id),
    office_code             VARCHAR(6)   REFERENCES dim_offices(office_code),
    naics_code              VARCHAR(6)   REFERENCES dim_naics(naics_code),
    psc_code                VARCHAR(4)   REFERENCES dim_psc(psc_code),
    modification_number     VARCHAR(40),
    transaction_number      VARCHAR(10),
    transaction_date        DATE         NOT NULL,         -- action_date
    transaction_amount      NUMERIC(20,2) NOT NULL,        -- federal_action_obligation
    transaction_type        VARCHAR(100),                  -- action type
    transaction_description TEXT,
    recipient_name_raw      TEXT,                          -- name exactly as on this record
    parent_recipient_uei_raw  VARCHAR(12),                 -- parent UEI exactly as on this record
    parent_recipient_name_raw TEXT,
    pop_state_code          VARCHAR(2),                    -- place of performance
    pop_country_code        VARCHAR(3),
    potential_duplicate_group VARCHAR(64),                 -- set when award/mod/date/amount match another record (Phase 2a)
    fiscal_year             INTEGER      NOT NULL,
    source_updated_at       DATETIME,
    run_id                  VARCHAR(40)  NOT NULL REFERENCES etl_log(run_id),
    CONSTRAINT ck_fact_transactions_fy CHECK (fiscal_year = 2026),
    CONSTRAINT ck_fact_transactions_date CHECK (transaction_date >= '2025-10-01' AND transaction_date <= '2026-09-30')
);
CREATE INDEX IF NOT EXISTS ix_fact_transactions_award ON fact_transactions (award_id);
CREATE INDEX IF NOT EXISTS ix_fact_transactions_recipient ON fact_transactions (recipient_id);
CREATE INDEX IF NOT EXISTS ix_fact_transactions_agency ON fact_transactions (agency_id);
CREATE INDEX IF NOT EXISTS ix_fact_transactions_naics ON fact_transactions (naics_code);
CREATE INDEX IF NOT EXISTS ix_fact_transactions_psc ON fact_transactions (psc_code);
CREATE INDEX IF NOT EXISTS ix_fact_transactions_date ON fact_transactions (transaction_date);

-- ----------------------------------------------------------------- markets
CREATE TABLE IF NOT EXISTS market_segments (
    segment_id       INTEGER      PRIMARY KEY AUTOINCREMENT,
    market_key       VARCHAR(60)  NOT NULL,
    name             TEXT         NOT NULL,
    status           VARCHAR(10)  NOT NULL,
    definition_hash  VARCHAR(16)  NOT NULL,
    definition_yaml  TEXT         NOT NULL,
    created_at       DATETIME     NOT NULL,
    CONSTRAINT uq_market_segments_key_hash UNIQUE (market_key, definition_hash),
    CONSTRAINT ck_market_segments_status CHECK (status IN ('candidate','validated','planned'))
);

CREATE TABLE IF NOT EXISTS fact_transaction_segments (
    transaction_id  VARCHAR(160) NOT NULL REFERENCES fact_transactions(transaction_id) ON DELETE CASCADE,
    segment_id      INTEGER      NOT NULL REFERENCES market_segments(segment_id),
    matched_by      VARCHAR(10)  NOT NULL,                 -- naics | psc | both | all (all-codes market)
    match_tier      VARCHAR(10)  NOT NULL,                 -- core | extended
    keyword_tags    TEXT,
    PRIMARY KEY (transaction_id, segment_id),
    CONSTRAINT ck_fts_matched_by CHECK (matched_by IN ('naics','psc','both','all')),
    CONSTRAINT ck_fts_tier CHECK (match_tier IN ('core','extended'))
);
CREATE INDEX IF NOT EXISTS ix_fts_segment ON fact_transaction_segments (segment_id);

-- ------------------------------------------------------- quality & matching
CREATE TABLE IF NOT EXISTS entity_match_candidates (
    candidate_id           INTEGER      PRIMARY KEY AUTOINCREMENT,
    recipient_id           INTEGER      NOT NULL REFERENCES dim_recipients(recipient_id),
    original_name          TEXT         NOT NULL,
    potential_parent_name  TEXT,
    potential_parent_uei   VARCHAR(12),
    matching_identifier    VARCHAR(30)  NOT NULL,          -- reported_parent_uei | uei_exact
    match_confidence       REAL         NOT NULL,
    review_status          VARCHAR(10)  NOT NULL DEFAULT 'pending',
    review_note            TEXT,
    created_at             DATETIME     NOT NULL,
    reviewed_at            DATETIME,
    CONSTRAINT uq_emc_recipient_parent UNIQUE (recipient_id, potential_parent_uei),
    CONSTRAINT ck_emc_confidence CHECK (match_confidence >= 0 AND match_confidence <= 1),
    CONSTRAINT ck_emc_review_status CHECK (review_status IN ('pending','confirmed','rejected'))
);

CREATE TABLE IF NOT EXISTS dq_issues (
    issue_id     INTEGER      PRIMARY KEY AUTOINCREMENT,
    run_id       VARCHAR(40)  NOT NULL REFERENCES etl_log(run_id),
    entity_type  VARCHAR(30)  NOT NULL,                    -- transaction | award | recipient | code
    entity_key   VARCHAR(200),
    check_name   VARCHAR(60)  NOT NULL,
    severity     VARCHAR(10)  NOT NULL,
    detail       TEXT,
    created_at   DATETIME     NOT NULL,
    CONSTRAINT ck_dq_issues_severity CHECK (severity IN ('info','warning','error'))
);
CREATE INDEX IF NOT EXISTS ix_dq_issues_run ON dq_issues (run_id);
