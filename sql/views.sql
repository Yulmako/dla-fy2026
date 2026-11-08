-- FY2026 obligation aggregation views (portable SQLite / PostgreSQL).
-- Executed after the tables by src/database/engine.py::init_db.
--
-- Rules (approved 2026-11-06):
--   * Money = SUM(fact_transactions.transaction_amount) for FY2026 transactions only.
--     fact_transactions cannot hold other years (CHECK constraints).
--   * Transactions are first aggregated per unique award ID; award metrics use
--     that net FY2026 obligation, never transaction rows and never lifetime values.
--   * Award count = distinct award IDs with >= 1 FY2026 transaction.
--   * Average / largest award: only awards with POSITIVE net FY2026 obligation.
--     Zero-net and negative-net awards are counted separately.
--   * Median is computed in Python (src/analytics) from v_award_company_fy2026.
--   * Default entity = company (reported parent UEI); UEI drill-down via recipient views.
--   * An award whose transactions name two recipients in FY2026 (e.g. novation)
--     contributes each recipient's own FY2026 transactions to that recipient's company.
--   * fact_awards amounts (ceiling / current value / cumulative) are NOT used here.

CREATE VIEW IF NOT EXISTS v_award_company_fy2026 AS
SELECT fts.segment_id,
       t.award_id,
       r.company_id,
       COUNT(*)                            AS fy2026_action_count,
       ROUND(SUM(t.transaction_amount), 2) AS fy2026_net_obligation,
       CASE WHEN ROUND(SUM(t.transaction_amount), 2) > 0 THEN 'positive'
            WHEN ROUND(SUM(t.transaction_amount), 2) < 0 THEN 'negative'
            ELSE 'zero' END                AS net_obligation_class
FROM fact_transactions t
JOIN fact_transaction_segments fts ON fts.transaction_id = t.transaction_id
LEFT JOIN dim_recipients r ON r.recipient_id = t.recipient_id
GROUP BY fts.segment_id, t.award_id, r.company_id;

CREATE VIEW IF NOT EXISTS v_award_recipient_fy2026 AS
SELECT fts.segment_id,
       t.award_id,
       t.recipient_id,
       COUNT(*)                            AS fy2026_action_count,
       ROUND(SUM(t.transaction_amount), 2) AS fy2026_net_obligation,
       CASE WHEN ROUND(SUM(t.transaction_amount), 2) > 0 THEN 'positive'
            WHEN ROUND(SUM(t.transaction_amount), 2) < 0 THEN 'negative'
            ELSE 'zero' END                AS net_obligation_class
FROM fact_transactions t
JOIN fact_transaction_segments fts ON fts.transaction_id = t.transaction_id
GROUP BY fts.segment_id, t.award_id, t.recipient_id;

CREATE VIEW IF NOT EXISTS v_company_fy2026_summary AS
SELECT segment_id,
       company_id,
       ROUND(SUM(fy2026_net_obligation), 2)                                    AS fy2026_obligations,
       COUNT(*)                                                                AS award_count,
       SUM(CASE WHEN net_obligation_class = 'positive' THEN 1 ELSE 0 END)      AS positive_net_award_count,
       SUM(CASE WHEN net_obligation_class = 'zero'     THEN 1 ELSE 0 END)      AS zero_net_award_count,
       SUM(CASE WHEN net_obligation_class = 'negative' THEN 1 ELSE 0 END)      AS negative_net_award_count,
       ROUND(SUM(CASE WHEN net_obligation_class = 'positive' THEN fy2026_net_obligation END)
             / NULLIF(SUM(CASE WHEN net_obligation_class = 'positive' THEN 1 ELSE 0 END), 0), 2)
                                                                               AS avg_positive_award_fy2026,
       MAX(CASE WHEN net_obligation_class = 'positive' THEN fy2026_net_obligation END)
                                                                               AS largest_award_fy2026,
       SUM(fy2026_action_count)                                                AS fy2026_action_count
FROM v_award_company_fy2026
GROUP BY segment_id, company_id;

CREATE VIEW IF NOT EXISTS v_segment_fy2026_totals AS
SELECT fts.segment_id,
       ROUND(SUM(t.transaction_amount), 2) AS fy2026_obligations,
       COUNT(DISTINCT t.award_id)          AS award_count,
       COUNT(DISTINCT t.recipient_id)      AS recipient_count,
       COUNT(*)                            AS fy2026_action_count,
       MAX(t.transaction_date)             AS data_through_date
FROM fact_transactions t
JOIN fact_transaction_segments fts ON fts.transaction_id = t.transaction_id
GROUP BY fts.segment_id;

-- Share of the SELECTED federal procurement dataset (not commercial market share).
CREATE VIEW IF NOT EXISTS v_company_fy2026_share AS
SELECT c.segment_id,
       c.company_id,
       c.fy2026_obligations,
       ROUND(100.0 * c.fy2026_obligations / NULLIF(s.fy2026_obligations, 0), 4)
                                           AS share_of_selected_dataset_pct
FROM v_company_fy2026_summary c
JOIN v_segment_fy2026_totals s ON s.segment_id = c.segment_id;

-- One flat row per (segment, FY2026 transaction) with display attributes. The
-- Streamlit app loads this view once and computes every metric from it with
-- the same rules as above (src/analytics/metrics.py).
CREATE VIEW IF NOT EXISTS v_market_transactions_fy2026 AS
SELECT fts.segment_id,
       fts.matched_by,
       fts.match_tier,
       fts.keyword_tags,
       t.transaction_id,
       t.award_id,
       a.piid,
       a.award_description,
       a.award_amount            AS award_ceiling_lifetime,
       a.cumulative_obligation   AS award_cumulative_obligation_lifetime,
       t.modification_number,
       t.transaction_number,
       t.transaction_date,
       t.transaction_amount,
       t.transaction_type,
       a.award_type_code,
       a.award_type,
       t.potential_duplicate_group,
       r.recipient_id,
       r.recipient_uei,
       r.recipient_name,
       r.country                 AS recipient_country,
       r.state                   AS recipient_state,
       r.city                    AS recipient_city,
       r.parent_uei_conflict,
       c.company_id,
       c.company_key,
       c.company_uei,
       c.company_name,
       c.parent_status,
       ag.agency_id,
       ag.agency_name            AS awarding_sub_agency,
       ag.parent_agency_name     AS awarding_agency,
       t.naics_code,
       n.naics_description,
       t.psc_code,
       p.psc_description,
       p.category                AS psc_category,
       t.pop_state_code,
       t.pop_country_code
FROM fact_transactions t
JOIN fact_transaction_segments fts ON fts.transaction_id = t.transaction_id
JOIN fact_awards a ON a.award_id = t.award_id
LEFT JOIN dim_recipients r ON r.recipient_id = t.recipient_id
LEFT JOIN dim_companies c ON c.company_id = r.company_id
LEFT JOIN dim_agencies ag ON ag.agency_id = t.agency_id
LEFT JOIN dim_naics n ON n.naics_code = t.naics_code
LEFT JOIN dim_psc p ON p.psc_code = t.psc_code;
