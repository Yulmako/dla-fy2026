# Phase 1 — Architecture, Methodology and Market Definition

Reporting period: **FY2026 only (2025-10-01 – 2026-09-30)**, defined once in `src/fiscal.py`
and enforced in the database by CHECK constraints.

## 1. Architecture

```
USAspending public API ──► src/api/client.py ──► data/raw/ (immutable, checksummed)
  (no key, no SAM.gov)       throttle · retry ·      + raw_api_responses table
                             budget · logging
        │
        ├─ Phase 1: src/api/probe.py ─► validation report (JSON + MD)    ◄── we are here
        │
        └─ Phase 2–3: extract (segment transaction downloads) → stage → transform
                      → data-quality checks → reconcile with API aggregates
                      → atomic load into SQLite (PostgreSQL later via DATABASE_URL)
                             │
                      src/analytics (pure, tested functions) ─► Streamlit app (Phase 5)
```

Design rules:
- The **transaction** is the unit of truth for money. Awards carry lifetime context only.
- Every load is a full re-pull of the fixed FY2026 window, staged and validated before it
  replaces the previous snapshot. Late corrections and deletions are picked up on the next refresh.
- Every load is reconciled against `spending_by_category` control totals. A variance above
  0.5% fails the run.

## 2. Endpoints

| Use | Endpoint |
|---|---|
| Freshness | `GET /api/v2/awards/last_updated/` |
| Agency scope | `GET /api/v2/references/toptier_agencies/` |
| Code validation | `POST /api/v2/search/spending_by_category/{naics,psc,recipient}/`, `GET /api/v2/references/naics/<code>/`, `GET /api/v2/references/filter_tree/psc/<path>/` |
| Counts and limits | `POST /api/v2/search/spending_by_award_count/`, `…/spending_by_transaction_count/`, `/api/v2/download/count/` |
| Transaction sample | `POST /api/v2/search/spending_by_transaction/` |
| Award sample and detail | `POST /api/v2/search/spending_by_award/`, `GET /api/v2/awards/<id>/`, `POST /api/v2/transactions/` |
| Bulk (listed only in Phase 1) | `POST /api/v2/bulk_download/list_monthly_files/`; Phase 2: `POST /api/v2/download/transactions/` + `GET /api/v2/download/status/` |
| Monthly completeness | `POST /api/v2/search/spending_over_time/` (group=month, FY2026 filter only) |

## 3. FY2026 financial methodology

| Measure | Definition | Where used |
|---|---|---|
| **FY2026 obligations** (primary) | Σ `federal_action_obligation` of contract transactions (types A–D) with `action_date` in FY2026, including de-obligations | Rankings, share, every FY2026 KPI |
| Transaction amount | `federal_action_obligation` of one action | Drill-down |
| Cumulative award value | Award `total_obligation` (all years) | Profile context, labelled "lifetime" |
| Total award amount (ceiling) | Award base + all options | Profile context, labelled "ceiling" |

- IDVs are excluded from dollar sums, because obligations are recorded on their orders.
- Per-award FY2026 obligation = Σ of that award's FY2026 transactions. This drives the
  average, median and largest award.
- Transactions with a missing or unparseable amount or date are excluded from dollar
  metrics and logged to `dq_issues`. No value is ever imputed.
- Market share = company FY2026 obligations ÷ total FY2026 obligations of the selected
  dataset × 100. It is labelled as share of the selected federal procurement dataset.

C07, C09 and C10 in the probe test these rules against live data.

## 4. Defense Electronics — inclusion criteria

A **transaction** belongs to the market if:

1. its NAICS code is one of the included NAICS codes, **or** its PSC code is one of the
   included PSC codes; **and**
2. the awarding toptier agency is in the scope (v1: Department of Defense); **and**
3. it is a contract action (A–D) dated in FY2026.

Codes are tiered:
- `core` — specific to defense electronics.
- `extended` — electronic components, test equipment and repair. These are often
  dual-use, so each record is flagged with its tier.
- `review` — scanned by the probe but never included until promoted (e.g., 541330
  Engineering Services, 541715 R&D, 5935 Connectors).

Keywords only tag records. They never include records.

Code sources: the 2022 NAICS structure (U.S. Census Bureau) and the Federal PSC Manual
(GSA / Acquisition.gov). Official descriptions and FY2026 activity are taken from
USAspending by probe check C04. C04 also lists active codes in related families that
are not yet in the config. Nothing is added automatically.

Classification limitations:
- NAICS and PSC are assigned per action by contracting officers and can differ between
  modifications of the same award. Classification is applied per transaction.
- No code is exclusively "defense electronics". Large platform contracts (e.g., aircraft or
  ships with embedded electronics) are usually coded to the platform and fall outside
  this market.
- A NAICS-based total and a PSC-based total overlap. The market total is their
  de-duplicated union, computed after extraction.
- The DoD-only scope excludes defense-related electronics bought by other agencies.

## 5. Known data limitations

- **DoD reporting lag.** DoD and USACE contract actions appear in FPDS about 90 days late.
  As of early November 2026, late-FY2026 DoD months are incomplete. C12 measures this.
  FY2026 figures stay provisional until a refresh after about early January 2027.
- Parent company comes from the parent UEI and name reported on each record. It can be
  missing or out of date after mergers. Roll-ups use reported parent UEIs only, and
  uncertain links go to `entity_match_candidates` with status `pending`. There is no
  merging by name similarity.
- Recipient UEI can be missing, especially for older or foreign vendors. DUNS is legacy.
- Classified and some sensitive awards are not published. Redacted recipients exist.
- Obligations are not outlays, and they are not company revenue or profit.

## 6. Database (initial schema)

See `src/database/models.py` (SQLAlchemy 2.0) and `sql/schema_sqlite.sql`. Tables:

- Dimensions: `dim_companies` (reported parent UEI, the default ranking entity), `dim_recipients`, `dim_agencies` (subtier rows with their toptier parent),
  `dim_offices`, `dim_naics`, `dim_psc`.
- Views (`sql/views.sql`): award-level, company-level and share aggregations of FY2026 obligations.
- Facts: `fact_awards` (lifetime context), `fact_transactions` (FY2026 only, enforced by CHECK
  constraints).
- Markets: `market_segments`, `fact_transaction_segments`.
- Quality and lineage: `entity_match_candidates`, `dq_issues`, `etl_log`, `raw_api_responses`.

Natural keys from the source (`contract_transaction_unique_key`, `contract_award_unique_key`)
are the primary keys, so duplicates are rejected and reloads are idempotent.
`fact_transactions` stores NAICS, PSC and agency per transaction, because they can change
across modifications.

## 7. Decisions of 2026-11-06 (after first live run)

- Market codes: added 811219, 811213, 334416 (NAICS) and 1290 (PSC) as extended.
  334519, 334210, 5930, 5945, 5965 and 5975 are review-only. The other proposed codes are
  documented exclusions. Defense Electronics is a **validated working segment**, not an
  exhaustive industry definition.
- Data completeness: "PROVISIONAL — DoD REPORTING LAG" banner, actual data-through date,
  no estimation, full reload on 2027-01-05.
- Ranking entity: reported parent UEI, with UEI drill-down and no name-based merging.
- Award metrics: aggregate by award first. Average and median use positive-net awards only.
  Zero-net and negative-net awards are counted separately.
- Probe fixes: C08 de-duplicates on transaction ID; C12 checks trailing months using counts
  vs. dollars; no retries for non-retryable errors (HTTP 500 retried once at most); DoD
  bulk-download agency ID resolved.

Full rules: `docs/OBLIGATION_METHODOLOGY.md`.

## 8. Phase 1 exit criteria (review gate)

- [ ] C07 PASS: date filter bounds `Action Date`.
- [ ] C09 PASS: Award Amount ≠ FY2026 obligations is demonstrated on real awards.
- [ ] C10 PASS, or explained: transaction sum reconciles with the API aggregate.
- [ ] C04 reviewed: codes promoted, dropped or added, and market status set to `validated`.
- [ ] C05/C08 reviewed: extraction plan (single download vs split by sub-agency × month).
- [ ] C12 reviewed: decision on the provisional-data banner and reload date.
