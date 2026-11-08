"""Bucketed (memory-bounded) transform must give the same results as one pass.
TEST FIXTURE data only (tests/fixtures.py)."""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from src.etl.partitioned import bucket_sql, transform_staged
from src.etl.staging import read_staging, write_staging
from tests.fixtures import staged_rows
from tests.test_transform_load import IGNORE, _spec


def _con():
    con = sqlite3.connect(":memory:")
    df = staged_rows()
    df["_market"] = "defense_electronics"
    write_staging(con, df)
    return con


def _norm(df, by):
    df = df.sort_values(by).reset_index(drop=True).astype(object)
    return df.where(df.notna(), None)


def _company_totals(r):
    t = r.tables
    f = (t["fact_transactions"].merge(t["dim_recipients"][["recipient_id", "company_id"]], on="recipient_id")
         .merge(t["dim_companies"][["company_id", "company_key", "company_name"]], on="company_id"))
    return f.groupby(["company_key", "company_name"])["transaction_amount"].sum().round(2).sort_index()


@pytest.mark.parametrize("treatment", ["count_all_flag", "exclude_flagged_copies"])
def test_buckets_match_single_pass(treatment):
    con = _con()
    nonempty = sum(len(read_staging(con, None, where_sql=w)) > 0 for w in bucket_sql(4))
    assert nonempty >= 2, "fixture should spread over several buckets"
    specs = [_spec()]
    one = transform_staged(con, ["defense_electronics"], specs, "t", treatment, IGNORE, buckets=1)
    many = transform_staged(con, ["defense_electronics"], specs, "t", treatment, IGNORE, buckets=4,
                            log=lambda _: None)
    for k in ("staged_rows", "included_transactions", "excluded_transactions",
              "included_obligations", "excluded_by_reason", "markets",
              "potential_duplicate_groups", "excluded_flagged_copies", "companies",
              "recipients", "awards", "data_through_date"):
        assert one.stats[k] == many.stats[k], k
    pd.testing.assert_series_equal(_company_totals(one), _company_totals(many))
    a, b = one.tables, many.tables
    assert set(a["fact_transactions"]["transaction_id"]) == set(b["fact_transactions"]["transaction_id"])
    key = ["award_id", "piid", "naics_code", "psc_code", "cumulative_obligation"]
    pd.testing.assert_frame_equal(_norm(a["fact_awards"][key], "award_id"),
                                  _norm(b["fact_awards"][key], "award_id"))
    cols = ["recipient_key", "recipient_name", "parent_recipient_uei", "parent_uei_conflict"]
    pd.testing.assert_frame_equal(_norm(a["dim_recipients"][cols], "recipient_key"),
                                  _norm(b["dim_recipients"][cols], "recipient_key"))
    seg = lambda r: r.tables["fact_transaction_segments"].groupby(["segment_id", "matched_by"]).size()
    pd.testing.assert_series_equal(seg(one), seg(many))
    dq = lambda r: sorted(map(tuple, r.dq_issues[["entity_type", "entity_key", "check_name"]].values))
    assert dq(one) == dq(many)
    # ids are consistent after renumbering
    t = b
    assert t["fact_transactions"]["recipient_id"].isin(t["dim_recipients"]["recipient_id"]).all()
    assert t["fact_transactions"]["agency_id"].dropna().isin(t["dim_agencies"]["agency_id"]).all()
    assert t["dim_recipients"]["company_id"].isin(t["dim_companies"]["company_id"]).all()


def test_bucketed_result_loads():
    from src.etl import runlog
    from src.etl.load import load_snapshot
    from tests.test_transform_load import _db
    r = transform_staged(_con(), ["defense_electronics"], [_spec()], "load_test", "count_all_flag",
                         IGNORE, buckets=4, log=lambda _: None)
    con = _db()
    runlog.start_run(con, "load_test", "load", "defense_electronics")
    counts = load_snapshot(con, r, "load_test")
    assert counts["fact_transactions"] == r.stats["included_transactions"]
    total = con.execute("SELECT fy2026_obligations FROM v_segment_fy2026_totals").fetchone()[0]
    assert round(total, 2) == r.stats["included_obligations"]
    flat = pd.read_sql("SELECT * FROM v_market_transactions_fy2026", con)
    assert flat["company_name"].notna().all()


def test_read_staging_chunks_with_empty_columns():
    """A column empty in one chunk and filled in another must combine (older pandas
    raised 'dtype of categories must be the same')."""
    con = sqlite3.connect(":memory:")
    df = staged_rows()
    df["_market"] = "defense_electronics"
    df["recipient_county_name"] = None
    df.loc[df.index[-1], "recipient_county_name"] = "FAIRFAX"
    write_staging(con, df)
    out = read_staging(con, ["defense_electronics"], chunksize=3)
    assert len(out) == len(df)
    assert out["recipient_county_name"].astype(object).dropna().tolist() == ["FAIRFAX"]
    for c in out.columns:          # text categories (object, or 'str' on pandas 3)
        assert out[c].cat.categories.dtype == object or out[c].cat.categories.dtype.kind in "OUT" \
            or str(out[c].cat.categories.dtype) == "str"
