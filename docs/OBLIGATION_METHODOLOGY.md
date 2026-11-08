# FY2026 Obligation Aggregation Methodology

Approved 2026-11-06. This applies to every metric in the dashboard. The SQL is in
`sql/views.sql`, and the rules are covered by tests in `tests/test_schema_sql.py`.

## 1. The only source of FY2026 money

| Rule | Implementation |
|---|---|
| FY2026 obligations = Σ `federal_action_obligation` of **contract transactions** (types A–D) whose `action_date` is between 2025-10-01 and 2026-09-30 | `fact_transactions.transaction_amount`. The table rejects any other date or fiscal year (CHECK constraints). |
| De-obligations (negative amounts) are included | They are real FY2026 obligation changes. |
| Zero-dollar administrative modifications are kept | They count as FY2026 actions but add $0. |
| IDVs are excluded from dollar sums | Obligations are recorded on the orders placed under them. |
| Lifetime values are **never** summed into FY2026 metrics | `fact_awards.award_amount` (ceiling), `current_award_value` and `cumulative_obligation` appear only as labelled context. |
| Missing or late data is never estimated | No imputation, no extrapolation of missing months, no historical averages. |

Phase 1 verified this live. Action dates are strictly bounded by FY2026 (C07). Award Amount
differs from FY2026 obligations on real awards (C09). The sum of transactions matched
USAspending's own aggregate to the cent (C10).

## 2. De-duplication

- The key is **USAspending's unique transaction ID**: `contract_transaction_unique_key` in the
  download files, which is the primary key of `fact_transactions`.
- Records are **never** de-duplicated on contract number (PIID), modification number,
  recipient or amount. Two actions can share all four and still be different transactions.
  The same PIID can also belong to different awards (orders under different IDVs).
- The NAICS-based and PSC-based extracts are merged on the transaction ID. A transaction that
  matches both is stored once and tagged `matched_by = 'both'`.
- Search-endpoint rows are never summed. They are used only for validation (C08).
- **Potential source duplicates.** These are records with different transaction IDs but the same
  award ID, modification, action date and non-zero amount. They are profiled in Phase 2a
  (`scripts/phase2a_stage_and_profile.py`) and flagged on every loaded record
  (`fact_transactions.potential_duplicate_group`). How they are counted is set in
  `config/pipeline.yaml → transaction_counting.potential_duplicate_treatment`:
  `count_all_flag` (count all, matching USAspending totals, and show the exposure) or
  `exclude_flagged_copies` (only for records identical on every compared field except the
  transaction number and report dates: keep the lowest transaction number and log the rest).
  **Decision 2026-11-07: `exclude_flagged_copies`.** In a group whose records are identical on every
  field except report-time snapshot fields (`ignore_fields`), the record with the lowest
  transaction_number is kept and the others are excluded and logged. Groups that differ in any
  other field are kept in full and only flagged. Totals are therefore slightly below USAspending's
  own totals, which count every record; the dashboard can still show the USAspending basis.
  While the setting is `undecided`, the fact loader refuses to run. Records are never excluded
  just because their amounts match or their transaction numbers are higher.

## 3. Market membership

A transaction belongs to the market if its NAICS **or** PSC code is in an included tier
(core or extended), **and** the awarding agency is in scope (DoD). It is recorded in
`fact_transaction_segments`. Review-only and excluded codes are never included. The market
definition is a working segment, not an exhaustive definition of the industry (see
`config/market_config.yaml`).

## 4. Award-level metrics: aggregate by award first

1. All FY2026 market transactions are grouped by **unique award ID**
   (`contract_award_unique_key`) and company, giving `fy2026_net_obligation` per award
   (`v_award_company_fy2026`).
2. Each award is classified by its net FY2026 obligation: **positive**, **zero** or **negative**.
3. Metrics:

| Dashboard label | Definition |
|---|---|
| **FY2026 awards with activity** | Count of distinct award IDs with ≥ 1 FY2026 transaction. Transaction rows are never counted as awards. |
| **Avg. FY2026 obligation per award (positive-net awards)** | Σ net obligation of positive-net awards ÷ number of positive-net awards |
| **Median FY2026 obligation per award (positive-net awards)** | Median of the positive-net award values (computed in Python) |
| **Largest FY2026 award obligation** | Maximum net FY2026 obligation among positive-net awards |
| **Awards with zero net FY2026 obligation** | Count, shown separately |
| **Awards with negative net FY2026 obligation (net de-obligation)** | Count, shown separately |
| **FY2026 actions** | Count of FY2026 transactions |

If one award has FY2026 transactions under two recipients (e.g. after a novation), each
recipient's company gets only its own transactions. Company totals therefore always add up
to the market total.

## 5. Company aggregation (default ranking entity)

| Situation | Company key | `parent_status` | Dashboard flag |
|---|---|---|---|
| A parent UEI is reported on the recipient's records | `PARENT:<parent UEI>` | `reported_parent` | — |
| UEI present, no parent UEI reported | `UEI:<own UEI>` (separate entity) | `no_parent_reported` | "No reported parent UEI" |
| No UEI at all | `NOUEI:<recipient key>` (separate entity) | `no_uei` | "No UEI – unverified identity" |

- Recipient UEIs stay available as drill-down entities (`v_award_recipient_fy2026`).
- Companies are **never** merged because their names look alike. Name similarity can only
  create a row in `entity_match_candidates` with `review_status = 'pending'`, and that has
  no effect on any metric.
- If a recipient's FY2026 records report more than one parent UEI, the parent on its most
  recent action is used. The recipient gets `dim_recipients.parent_uei_conflict = 1`, the
  conflict is logged in `dq_issues`, and it is shown on the company profile.
- "Parent" here means the parent UEI as **reported in USAspending records**. It has not been
  independently verified. The dashboard says "reported parent".

## 6. Market share

`share_of_selected_dataset_pct` = company FY2026 obligations ÷ total FY2026 obligations of
the selected market segment × 100 (`v_company_fy2026_share`). It is labelled
**"Share of selected federal procurement dataset (provisional)"**, never as market share of
the commercial industry. Obligations are not revenue or profit.

## 7. Provisional data: "PROVISIONAL — DoD REPORTING LAG"

DoD contract actions reach FPDS and USAspending about 90 days after they happen. Until the
full reload, every page shows the banner with:

- **"Reporting volume normal through: 2026-07-31"** is the primary line. It is the end of the
  last month before the first month that check C12 flags as `likely_missing_reporting`.
- **"Latest action date in loaded data: 2026-09-21"** is the secondary line. It is
  `MAX(transaction_date)` of the loaded segment (`etl_log.data_through_date`), never the FY end
  date.
- **Last successful refresh:** `etl_log.finished_at` of the latest successful load.
- An explanation of the ~90-day DoD delay, and a note that all rankings and shares are
  provisional.
- **Scheduled full reload:** 2027-01-05 (`config/pipeline.yaml`).

C12 checks only the most recent N months (default 3). It compares each month's transaction
count and dollars with the median of the earlier months:

- count < threshold → `likely_missing_reporting`
- count normal, dollars below threshold → `low_obligations_normal_reporting` (genuinely low
  spending, not missing data)
- otherwise → `normal`

The threshold (default 50%), the number of months and the lag days are configurable. October
is never flagged just because spending is low at the start of the year.
