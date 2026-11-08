"""Snapshot export with PSC-group markets, on the TEST FIXTURE (no network, no SQLAlchemy)."""
from __future__ import annotations

import contextlib
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pandas as pd

from src.analytics.psc_groups import group_of, label
from src.etl.staging import write_staging
from tests.fixtures import staged_rows

ROOT = Path(__file__).resolve().parents[1]


def test_group_of():
    assert group_of("5998") == "59" and group_of("R706") == "R" and group_of("AC12") == "AC"
    assert group_of(None) is None and group_of("") is None
    assert label("59", {"59": "ELECTRICAL AND ELECTRONIC EQUIPMENT COMPONENTS"}).startswith("59 — ELECTRICAL")
    assert label("Z", {}) == "PSC group Z"


def test_export_groups(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    df = staged_rows()
    df["_market"] = "dla_all"
    df["awarding_sub_agency_name"] = "Defense Logistics Agency"
    write_staging(con, df)

    class E:
        def connect(self):
            return contextlib.nullcontext(con)

        def begin(self):
            return contextlib.nullcontext(con)

    fake = types.ModuleType("src.database.engine")
    fake.get_engine = lambda: E()
    fake.init_db = lambda e: None
    monkeypatch.setitem(sys.modules, "src.database.engine", fake)
    spec = importlib.util.spec_from_file_location("exp", ROOT / "scripts" / "export_dashboard_snapshot.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    m.ROOT = tmp_path
    m.fetch_group_names = lambda c, p: {}
    assert m.main(["--app-db"]) == 0
    out = tmp_path / "data" / "processed" / "dashboard_snapshot"
    ds = pd.read_csv(out / "data_status.csv")
    for mode, part in ds.groupby("counting"):
        base = part[part["market"] == "dla_all"]
        groups = part[part["market_status"] == "psc_group"]
        assert len(base) == 1 and len(groups) >= 2
        assert round(groups["fy2026_obligations"].sum(), 2) == round(base["fy2026_obligations"].iloc[0], 2)
        assert groups["excluded_probable_copies"].sum() == base["excluded_probable_copies"].iloc[0]
    assert "defense_electronics" not in set(ds["market"])
    orders = pd.read_csv(out / "orders.csv")
    assert set(orders["market"]) == {"dla_all"}

    import zipfile
    from src.webapp import queries as Q
    z = tmp_path / "data" / "processed" / "app_data.sqlite.zip"
    assert z.exists() and zipfile.ZipFile(z).namelist() == ["app_data.sqlite"]
    app = sqlite3.connect(tmp_path / "data" / "processed" / "app_data.sqlite")
    mk = Q.markets(app)
    assert "dla_all" in set(mk["market"])
    _, all_ = Q.search_orders(app, "dla_all", "usaspending_basis")
    parts = [Q.search_orders(app, k, "usaspending_basis")[1]["n"] for k in mk["market"] if k != "dla_all"]
    assert sum(parts) == all_["n"] > 0
    k = Q.kpis(app, "dla_all", "probable_duplicates_removed")
    _, dd = Q.search_orders(app, "dla_all", "probable_duplicates_removed")
    assert dd["n"] == k["action_count"]
