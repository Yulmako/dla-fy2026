"""Schema tests using Python's built-in sqlite3 (no SQLAlchemy needed)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

DDL = Path(__file__).resolve().parents[1] / "sql" / "schema_sqlite.sql"
VIEWS = Path(__file__).resolve().parents[1] / "sql" / "views.sql"
EXPECTED_TABLES = {
    "dim_companies", "etl_log", "raw_api_responses", "dim_recipients", "dim_agencies", "dim_offices",
    "dim_naics", "dim_psc", "fact_awards", "fact_transactions", "market_segments",
    "fact_transaction_segments", "entity_match_candidates", "dq_issues",
}


@pytest.fixture()
def db():
    con = sqlite3.connect(":memory:")
    con.executescript(DDL.read_text())
    con.executescript(VIEWS.read_text())
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("INSERT INTO etl_log (run_id, run_type, started_at, status) "
                "VALUES ('r1', 'load', '2026-11-06T00:00:00', 'running')")
    con.execute("INSERT INTO fact_awards (award_id) VALUES ('CONT_AWD_X')")
    yield con
    con.close()


def _txn(con, tid="T1", d="2026-03-01", fy=2026, award="CONT_AWD_X"):
    con.execute("INSERT INTO fact_transactions (transaction_id, award_id, transaction_date, "
                "transaction_amount, fiscal_year, run_id) VALUES (?, ?, ?, 100.00, ?, 'r1')",
                (tid, award, d, fy))


def test_all_tables_created(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_fy2026_transaction_accepted(db):
    _txn(db)
    _txn(db, "T2", "2025-10-01")
    _txn(db, "T3", "2026-09-30")
    assert db.execute("SELECT COUNT(*) FROM fact_transactions").fetchone()[0] == 3


@pytest.mark.parametrize("d,fy", [("2025-09-30", 2026), ("2026-10-01", 2026), ("2026-03-01", 2025)])
def test_out_of_period_rejected(db, d, fy):
    with pytest.raises(sqlite3.IntegrityError):
        _txn(db, "TX", d, fy)


def test_duplicate_transaction_rejected(db):
    _txn(db)
    with pytest.raises(sqlite3.IntegrityError):
        _txn(db)


def test_transaction_requires_existing_award(db):
    with pytest.raises(sqlite3.IntegrityError):
        _txn(db, "T9", award="CONT_AWD_MISSING")


def test_match_candidate_constraints(db):
    db.execute("INSERT INTO dim_recipients (recipient_key, recipient_name, "
               "recipient_name_normalized) VALUES ('UEI:ABC', 'n', 'n')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO entity_match_candidates (recipient_id, original_name, "
                   "matching_identifier, match_confidence, review_status, created_at) "
                   "VALUES (1, 'n', 'reported_parent_uei', 0.9, 'merged', '2026-11-06')")


def test_naics_sector_consistency(db):
    db.execute("INSERT INTO dim_naics VALUES ('334511', 'x', '33')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO dim_naics VALUES ('334220', 'x', '54')")


# ----------------------------------------------------------- aggregation rules
def _load_aggregation_case(con):
    """Tiny hand-built case (test fixture, not real data)."""
    q = con.execute
    q("INSERT INTO market_segments (segment_id, market_key, name, status, definition_hash, "
      "definition_yaml, created_at) VALUES (1, 'm', 'M', 'validated', 'h', 'y', '2026-11-06')")
    q("INSERT INTO dim_companies (company_id, company_key, company_uei, company_name, parent_status) "
      "VALUES (1, 'PARENT:P1', 'P1', 'PARENT ONE', 'reported_parent'), "
      "(2, 'UEI:U3', 'U3', 'SOLO', 'no_parent_reported')")
    q("INSERT INTO dim_recipients (recipient_id, recipient_key, recipient_uei, recipient_name, "
      "recipient_name_normalized, parent_recipient_uei, company_id) VALUES "
      "(1, 'UEI:U1', 'U1', 'A', 'a', 'P1', 1), (2, 'UEI:U2', 'U2', 'B', 'b', 'P1', 1), "
      "(3, 'UEI:U3', 'U3', 'C', 'c', NULL, 2)")
    for a in ("A1", "A2", "A3", "A4", "B1", "OUT"):
        q("INSERT INTO fact_awards (award_id, award_amount, cumulative_obligation) "
          "VALUES (?, 999999, 888888)", (a,))
    txns = [("t1", "A1", 1, 100), ("t2", "A1", 1, 50),             # net +150
            ("t3", "A2", 2, 0.1), ("t4", "A2", 2, 0.2), ("t5", "A2", 2, -0.3),  # net 0
            ("t6", "A3", 1, -5),                                       # net -5
            ("t7", "A4", 2, 30),                                       # net +30
            ("t8", "B1", 3, 200),                                      # other company
            ("t9", "OUT", 1, 1000)]                                    # not in segment
    for tid, aid, rid, amt in txns:
        q("INSERT INTO fact_transactions (transaction_id, award_id, recipient_id, "
          "transaction_date, transaction_amount, fiscal_year, run_id) "
          "VALUES (?, ?, ?, '2026-05-01', ?, 2026, 'r1')", (tid, aid, rid, amt))
        if aid != "OUT":
            q("INSERT INTO fact_transaction_segments VALUES (?, 1, 'naics', 'core', NULL)", (tid,))


def test_company_summary_rules(db):
    _load_aggregation_case(db)
    db.row_factory = sqlite3.Row
    rows = {r["company_id"]: dict(r) for r in
            db.execute("SELECT * FROM v_company_fy2026_summary WHERE segment_id = 1")}
    c1 = rows[1]
    assert c1["fy2026_obligations"] == 175          # 150 + 0 - 5 + 30, lifetime values ignored
    assert c1["award_count"] == 4                   # awards, not 7 transaction rows
    assert (c1["positive_net_award_count"], c1["zero_net_award_count"],
            c1["negative_net_award_count"]) == (2, 1, 1)
    assert c1["avg_positive_award_fy2026"] == 90    # (150 + 30) / 2 – zero/negative excluded
    assert c1["largest_award_fy2026"] == 150
    assert c1["fy2026_action_count"] == 7
    assert rows[2]["fy2026_obligations"] == 200


def test_segment_totals_and_share(db):
    _load_aggregation_case(db)
    total, awards, actions = db.execute(
        "SELECT fy2026_obligations, award_count, fy2026_action_count "
        "FROM v_segment_fy2026_totals WHERE segment_id = 1").fetchone()
    assert (total, awards, actions) == (375, 5, 8)  # 'OUT' transaction excluded
    share = dict(db.execute("SELECT company_id, share_of_selected_dataset_pct "
                            "FROM v_company_fy2026_share").fetchall())
    assert share[1] == round(100 * 175 / 375, 4) and share[2] == round(100 * 200 / 375, 4)


def test_uei_drilldown_view(db):
    _load_aggregation_case(db)
    rows = db.execute("SELECT recipient_id, award_id, fy2026_net_obligation, net_obligation_class "
                      "FROM v_award_recipient_fy2026 ORDER BY recipient_id, award_id").fetchall()
    assert (1, "A1", 150, "positive") in rows and (1, "A3", -5, "negative") in rows
    assert (2, "A2", 0, "zero") in rows


def test_company_parent_status_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO dim_companies (company_key, company_name, parent_status) "
                   "VALUES ('X', 'x', 'name_match')")
