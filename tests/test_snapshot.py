"""Snapshot export on the TEST FIXTURE: matches the loaded-view analytics."""
import pandas as pd
import pytest

from src.analytics import metrics as M
from src.analytics.snapshot import combine, flat_frame, market_tables
from src.etl import runlog
from src.etl.load import load_snapshot
from tests.test_transform_load import _db, _run


def test_flat_frame_matches_view_and_tables():
    r = _run()
    flat = flat_frame(r)
    con = _db()
    runlog.start_run(con, "load_test", "load", "x")
    load_snapshot(con, r, "load_test")
    view = pd.read_sql("SELECT * FROM v_market_transactions_fy2026", con)
    assert len(flat) == len(view)
    assert flat["transaction_amount"].sum() == pytest.approx(view["transaction_amount"].sum())
    a = M.entity_summary(flat).set_index("name")["fy2026_obligations"]
    b = M.entity_summary(view).set_index("name")["fy2026_obligations"]
    assert a.sort_index().tolist() == pytest.approx(b.sort_index().tolist())
    t = market_tables(flat, "defense_electronics", "usaspending_basis")
    assert {"kpis", "companies", "by_agency", "by_naics", "by_psc", "monthly", "award_size",
            "company_agencies", "company_psc", "company_naics"} <= set(t)
    assert t["monthly"]["fy2026_obligations"].sum() == pytest.approx(1196)
    r2 = _run("exclude_flagged_copies")
    t2 = market_tables(flat_frame(r2), "defense_electronics", "probable_duplicates_removed")
    both = combine([t, t2])
    k = both["kpis"].set_index("counting")["fy2026_obligations"]
    assert k["usaspending_basis"] - k["probable_duplicates_removed"] == pytest.approx(315)


def test_geography_bases_each_sum_to_total():
    from src.analytics.snapshot import flat_frame, market_tables
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    g = market_tables(f, "defense_electronics", "usaspending_basis")["by_geo"]
    assert set(g["basis"]) == {"place_of_performance", "recipient_location"}
    for _, part in g.groupby("basis"):
        assert round(part["fy2026_obligations"].sum(), 2) == round(f["transaction_amount"].sum(), 2)


def test_orders_list_all_and_capped():
    from src.analytics.snapshot import flat_frame, orders
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    o = orders(f, "defense_electronics")
    assert len(o) == len(f) and set(o["coverage"]) == {"all_actions"}
    assert o["transaction_id"].is_unique and o["description"].str.len().max() <= 140
    c = orders(f, "defense_electronics", max_rows=3, per_company=1, top_companies=2)
    assert c["company_id"].nunique() <= 2 and len(c) <= 2
    assert c["coverage"].iloc[0].startswith("top2_companies")


def test_action_split_adds_up():
    from src.analytics.snapshot import action_split, flat_frame, market_tables
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    s = action_split(f)
    assert round(s["new_award_obligations"] + s["modification_obligations"], 2) == round(f["transaction_amount"].sum(), 2)
    base = f["modification_number"].fillna("").astype(str).str.fullmatch(r"0*")
    assert s["modification_count"] == int((~base).sum())
    k = market_tables(f, "defense_electronics", "usaspending_basis")["kpis"]
    assert {"new_award_count", "modification_count"} <= set(k.columns)


def test_geo_top_consistent_with_geography():
    from src.analytics.snapshot import flat_frame, market_tables
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    t = market_tables(f, "defense_electronics", "usaspending_basis")
    g, gc, gp = t["by_geo"], t["geo_companies"], t["geo_psc"]
    k = ["basis", "country", "location"]
    assert set(map(tuple, gc[k].values)) == set(map(tuple, g[k].values))
    assert (gc.groupby(k).size() <= 10).all() and set(gp["basis"]) == set(g["basis"])
    # with <=10 companies per location the per-location sums equal the location totals
    m = gc.groupby(k)["fy2026_obligations"].sum().round(2)
    tot = g.set_index(k)["fy2026_obligations"].round(2)
    assert (m.reindex(tot.index) == tot).all()
    assert "geo_psc" not in market_tables(f, "x:59", "usaspending_basis")


def test_companies_have_action_count():
    from src.analytics.snapshot import flat_frame, market_tables
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    c = market_tables(f, "defense_electronics", "usaspending_basis")["companies"]
    assert c["action_count"].sum() == len(f)


def test_office_top():
    from src.analytics.snapshot import flat_frame, market_tables
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    t = market_tables(f, "defense_electronics", "usaspending_basis")
    oc, op = t["office_companies"], t["office_psc"]
    assert (oc.groupby("office_code").size() <= 10).all()
    assert set(oc["office_code"]) == set(t["by_office"]["office_code"].dropna())
    assert round(oc["fy2026_obligations"].sum(), 2) == round(f["transaction_amount"].sum(), 2)
    assert "office_psc" not in market_tables(f, "x:59", "usaspending_basis")


def test_company_offices():
    from src.analytics.snapshot import flat_frame, market_tables
    from tests.test_transform_load import _run
    f = flat_frame(_run())
    t = market_tables(f, "defense_electronics", "usaspending_basis")
    co = t["company_offices"]
    assert {"company_id", "office_code", "office_name", "fy2026_obligations"} <= set(co.columns)
    one = co.groupby("company_id")["fy2026_obligations"].sum().round(2)
    tot = f.groupby("company_id")["transaction_amount"].sum().round(2)
    assert (one == tot.reindex(one.index)).all()
