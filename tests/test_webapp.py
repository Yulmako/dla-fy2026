"""Web-app database and queries on the TEST FIXTURE."""
from __future__ import annotations

import sqlite3

from src.analytics.snapshot import flat_frame, market_tables
from src.webapp import queries as Q
from src.webapp.build import finalize_orders, open_db, write_orders, write_tables
from tests.test_transform_load import _run


def _db(tmp_path):
    r = _run()
    f = flat_frame(r)
    con = open_db(tmp_path / "app.sqlite")
    write_orders(con, f)
    some = f["transaction_id"].iloc[0]
    finalize_orders(con, {some})
    t = market_tables(f, "dla_all", "usaspending_basis")
    import pandas as pd
    t["data_status"] = pd.DataFrame([{"market": "dla_all", "counting": "usaspending_basis",
                                      "market_name": "DLA – all contracts", "market_status": "all",
                                      "psc_group": None, "fy2026_obligations": f["transaction_amount"].sum()}])
    write_tables(con, t)
    return con, f, some


def test_search_all_and_text(tmp_path):
    con, f, copy_id = _db(tmp_path)
    rows, tot = Q.search_orders(con, "dla_all", "usaspending_basis")
    assert tot["n"] == len(f) and round(tot["total"], 2) == round(f["transaction_amount"].sum(), 2)
    word = f["transaction_description"].dropna().iloc[0].split()[0]
    rows, tot = Q.search_orders(con, "dla_all", "usaspending_basis", text=word.lower())
    assert tot["n"] >= 1 and rows["description"].str.upper().str.contains(word.upper()).all()
    _, tot2 = Q.search_orders(con, "dla_all", "probable_duplicates_removed")
    assert tot2["n"] == len(f) - 1
    assert Q.kpis(con, "dla_all", "usaspending_basis")["action_count"] == len(f)
    assert len(Q.markets(con)) == 1


def test_fts_query_is_safe():
    assert Q.fts_query('valve "; DROP TABLE x') == '"valve"* AND "DROP"* AND "TABLE"* AND "x"*'
    assert Q.fts_query("  ") is None
