"""Analytics tests on the TEST FIXTURE, cross-checked against the SQL views
(independent second implementation of the same rules)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.analytics import metrics as M
from src.etl import runlog
from src.etl.load import load_snapshot
from tests.test_transform_load import _db, _run


@pytest.fixture(scope="module")
def loaded():
    con = _db()
    runlog.start_run(con, "load_test", "load", "defense_electronics")
    load_snapshot(con, _run(), "load_test")
    df = pd.read_sql("SELECT * FROM v_market_transactions_fy2026", con)
    return con, df


def test_entity_summary_matches_sql_views(loaded):
    con, df = loaded
    py = M.entity_summary(df, "company").set_index("company_id")
    sql = pd.read_sql("SELECT * FROM v_company_fy2026_summary", con).set_index("company_id")
    for cid, r in sql.iterrows():
        p = py.loc[cid]
        assert p["fy2026_obligations"] == pytest.approx(r["fy2026_obligations"])
        assert p["award_count"] == r["award_count"]
        assert p["positive_net_award_count"] == r["positive_net_award_count"]
        assert p["zero_net_award_count"] == r["zero_net_award_count"]
        assert p["negative_net_award_count"] == r["negative_net_award_count"]
        if pd.notna(r["avg_positive_award_fy2026"]):
            assert p["avg_positive_award_fy2026"] == pytest.approx(r["avg_positive_award_fy2026"])
            assert p["largest_award_fy2026"] == pytest.approx(r["largest_award_fy2026"])
    share = pd.read_sql("SELECT * FROM v_company_fy2026_share", con).set_index("company_id")
    for cid, r in share.iterrows():
        assert py.loc[cid, "share_of_selected_dataset_pct"] == pytest.approx(
            r["share_of_selected_dataset_pct"], abs=1e-3)


def test_parent_company_rules(loaded):
    _, df = loaded
    s = M.entity_summary(df, "company").set_index("name")
    p = s.loc["TEST PARENT HOLDINGS"]
    # A1 +150, A2 0, A3 -5, B1 +30, F1 +630, G1 +140 (E1 now under TESTPARENT02)
    assert p["fy2026_obligations"] == 945
    assert (p["award_count"], p["positive_net_award_count"], p["zero_net_award_count"],
            p["negative_net_award_count"]) == (6, 4, 1, 1)
    assert p["median_positive_award_fy2026"] == pytest.approx((140 + 150) / 2)
    assert p["recipient_uei_count"] == 2
    assert s.loc["TEST CHARLIE", "parent_status"] == "no_parent_reported"


def test_market_kpis_and_concentration(loaded):
    _, df = loaded
    k = M.market_kpis(df)
    assert k["fy2026_obligations"] == pytest.approx(1196)
    assert k["award_count"] == 9 and k["action_count"] == 14
    assert k["cr5_pct"] == 100.0 and k["entities"] == 4   # PARENT01, PARENT02, C, no-UEI D
    assert M.concentration(pd.Series([50, 50]))["hhi"] == 5000


def test_dimensions_portfolio_overlap(loaded):
    _, df = loaded
    ag = M.obligations_by(df, "awarding_sub_agency")
    assert set(ag["awarding_sub_agency"]) == {"Department of the Army", "Department of the Navy"}
    assert ag["fy2026_obligations"].sum() == pytest.approx(1196)
    cid = df.loc[df["company_name"] == "TEST PARENT HOLDINGS", "company_id"].iloc[0]
    port = M.portfolio(df, cid, "psc_code")
    assert port["share_of_entity_pct"].sum() == pytest.approx(100)
    div = M.diversification(df, cid)
    assert 0 <= div["psc_code"]["diversification_index"] <= 1
    ids = list(df["company_id"].unique()[:3])
    ov = M.category_overlap(df, ids)
    assert all(ov.loc[i, i] in (0.0, 100.0) for i in ids)


def test_award_list_and_dq(loaded):
    _, df = loaded
    cid = df.loc[df["company_name"] == "TEST PARENT HOLDINGS", "company_id"].iloc[0]
    al = M.award_list(df, cid)
    assert al.iloc[0]["award_id"] == "CONT_AWD_F1" and al.iloc[0]["potential_duplicate_records"] == 2
    dq = M.data_quality_summary(df)
    assert dq["potential_duplicate_groups"] == 2 and dq["potential_duplicate_amount"] == 385
    assert dq["missing_recipient_uei"] == 1
    dist = M.obligation_distribution(df)
    assert dist["awards"].sum() == 7   # A1 B1 C1 D1 E1 F1 G1
