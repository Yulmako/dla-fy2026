"""FY2026 DLA competitor intelligence – public web app (Streamlit Community Cloud).

Data: public USAspending.gov contract transactions, FY2026 only, packed into one SQLite file
by `python scripts/export_dashboard_snapshot.py --app-db`. The app downloads that file from
APP_DATA_URL (a GitHub release asset) on first start, or uses data/processed/app_data.sqlite
when run locally. Federal obligations are NOT company revenue or profit.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.webapp import queries as Q  # noqa: E402

st.set_page_config(page_title="FY2026 DLA competitor intelligence", layout="wide")
COUNTINGS = {"probable_duplicates_removed": "Probable duplicates removed",
             "usaspending_basis": "All records (USAspending basis)"}
SOURCE = ("Source: [USAspending.gov](https://www.usaspending.gov) public contract transactions, "
          "awarding sub-agency Defense Logistics Agency, FY2026 (Oct 2025 – Sep 2026). "
          "Obligations are federal contract obligations, not company revenue or profit.")


def _secret(name: str) -> str | None:
    try:
        return st.secrets.get(name) or os.environ.get(name)
    except Exception:
        return os.environ.get(name)


@st.cache_resource(show_spinner="Loading FY2026 data …")
def connect() -> sqlite3.Connection:
    local = ROOT / "data" / "processed" / "app_data.sqlite"
    if local.exists():
        path = local
    else:
        url = _secret("APP_DATA_URL")
        if not url:
            st.error("APP_DATA_URL is not set – see DEPLOY.md.")
            st.stop()
        tmp = Path(tempfile.gettempdir()) / "fci_app"
        tmp.mkdir(exist_ok=True)
        path = tmp / "app_data.sqlite"
        if not path.exists():
            z = tmp / "app_data.sqlite.zip"
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                with open(z, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            with zipfile.ZipFile(z) as zf:
                zf.extract("app_data.sqlite", tmp)
            z.unlink()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    return con


con = connect()


@st.cache_data(show_spinner=False)
def T(name: str, market: str, counting: str, order: str | None = None, limit: int | None = None,
      extra: str = "", params: tuple = ()) -> pd.DataFrame:
    return Q.table(con, name, market, counting, order, limit, extra, params)


@st.cache_data(show_spinner=False)
def markets() -> pd.DataFrame:
    return Q.markets(con)


@st.cache_data(show_spinner="Searching …")
def search(market, counting, text, company_id, office, loc, basis, limit):
    return Q.search_orders(con, market, counting, text, company_id, office, loc, basis, limit)


def money(x) -> str:
    x = float(x or 0)
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= d:
            return f"${x / d:,.2f}{s}"
    return f"${x:,.0f}"


def hbar(df: pd.DataFrame, label: str, title: str, n: int = 15):
    if df.empty:
        st.caption(f"{title}: no data")
        return
    d = df.nlargest(n, "fy2026_obligations").iloc[::-1]
    fig = px.bar(d, x="fy2026_obligations", y=label, orientation="h", title=title,
                 labels={"fy2026_obligations": "FY2026 obligations ($)", label: ""})
    fig.update_layout(height=max(260, 22 * len(d) + 80), margin=dict(l=0, r=10, t=40, b=0),
                      font=dict(size=11))
    st.plotly_chart(fig, use_container_width=True)


# ---------------- sidebar ----------------
mk = markets()
names = dict(zip(mk["market"], mk["market_name"]))
st.sidebar.title("FY2026 DLA")
market = st.sidebar.selectbox("Market (official PSC group)", list(mk["market"]),
                              format_func=lambda m: names.get(m, m))
counting = st.sidebar.radio("Counting", list(COUNTINGS), format_func=COUNTINGS.get)
st.sidebar.caption(SOURCE)

k = Q.kpis(con, market, counting)
stt = Q.status(con, market, counting)
st.title("FY2026 DLA competitor intelligence")
st.caption(SOURCE)
if stt.get("period_label"):
    st.caption(f"Data period: {stt['period_label']}")

tabs = st.tabs(["Overview", "Ranking", "Company profile", "Geography", "Orders search"])

# ---------------- overview ----------------
with tabs[0]:
    c = st.columns(6)
    c[0].metric("Obligations", money(k.get("fy2026_obligations")),
                help="Sum of FY2026 federal action obligations – not revenue.")
    c[1].metric("Awards with activity", f"{int(k.get('award_count') or 0):,}",
                help="Distinct contract awards with at least one FY2026 action.")
    c[2].metric("New awards in FY2026", f"{int(k.get('new_award_count') or 0):,}",
                money(k.get("new_award_obligations")), delta_color="off",
                help="Base actions (modification 0) dated in FY2026.")
    c[3].metric("Modifications", f"{int(k.get('modification_count') or 0):,}",
                money(k.get("modification_obligations")), delta_color="off",
                help="Actions changing existing awards (modification ≠ 0).")
    c[4].metric("Companies", f"{int(k.get('company_count') or 0):,}",
                help="Companies grouped by parent UEI where reported, else recipient UEI.")
    c[5].metric("Top-10 concentration", f"{float(k.get('cr10_pct') or 0):.1f}%",
                help="Share of obligations received by the 10 largest companies.")
    a, b = st.columns(2)
    with a:
        hbar(T("companies", market, counting, "rank", 15), "company_name", "Top 15 companies")
        hbar(T("by_psc", market, counting), "psc_description", "PSC (product / service)")
        hbar(T("by_office", market, counting), "office_name", "DLA contracting offices")
    with b:
        m = T("monthly", market, counting, "month")
        if len(m):
            fig = px.bar(m, x="month", y="fy2026_obligations", title="Obligations by month",
                         labels={"fy2026_obligations": "$", "month": ""})
            fig.update_layout(height=340, margin=dict(l=0, r=10, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)
        hbar(T("by_naics", market, counting), "naics_description", "NAICS (industry)")
        s = T("award_size", market, counting)
        if len(s):
            fig = px.bar(s, x="bucket", y="awards", title="Award size (net FY2026 obligations)",
                         labels={"bucket": "", "awards": "Awards"})
            fig.update_layout(height=320, margin=dict(l=0, r=10, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True)

# ---------------- ranking ----------------
companies = T("companies", market, counting, "rank")
with tabs[1]:
    cols = [c for c in ("rank", "company_name", "company_uei", "fy2026_obligations",
                        "share_of_selected_dataset_pct", "award_count", "median_positive_award_fy2026")
            if c in companies]
    st.dataframe(companies[cols], hide_index=True, use_container_width=True, height=600,
                 column_config={"fy2026_obligations": st.column_config.NumberColumn("Obligations", format="$%,.0f"),
                                "share_of_selected_dataset_pct": st.column_config.NumberColumn("Share %", format="%.2f"),
                                "rank": "#", "company_name": "Company", "company_uei": "UEI",
                                "award_count": "Awards", "median_positive_award_fy2026": st.column_config.NumberColumn("Median award", format="$%,.0f")})

# ---------------- profile ----------------
with tabs[2]:
    if companies.empty:
        st.info("No companies in this market.")
    else:
        opts = companies.head(500)
        cid = st.selectbox("Company (top 500 by obligations)", list(opts["company_id"]),
                           format_func=lambda i: f"#{int(opts.loc[opts.company_id == i, 'rank'].iloc[0])} "
                                                 f"{opts.loc[opts.company_id == i, 'company_name'].iloc[0]}")
        row = opts[opts["company_id"] == cid].iloc[0]
        c = st.columns(4)
        c[0].metric("Obligations", money(row["fy2026_obligations"]))
        c[1].metric("Share of market", f"{row['share_of_selected_dataset_pct']:.2f}%")
        c[2].metric("Awards", f"{int(row['award_count']):,}")
        c[3].metric("Rank", f"#{int(row['rank'])}")
        a, b, d = st.columns(3)
        extra, p = "AND company_id = ?", (int(cid),)
        with a:
            hbar(T("company_offices", market, counting, extra=extra, params=p), "office_name", "DLA contracting offices", 10)
        with b:
            hbar(T("company_psc", market, counting, extra=extra, params=p), "psc_description", "PSC", 10)
        with d:
            hbar(T("company_naics", market, counting, extra=extra, params=p), "naics_description", "NAICS", 10)
        rows, tot = search(market, counting, "", int(cid), None, None, "place_of_performance", 50)
        st.subheader(f"Largest orders ({tot['n']:,} actions, {money(tot['total'])})")
        st.dataframe(rows[["transaction_date", "transaction_amount", "piid", "psc_code", "description",
                           "office_code", "pop_state_code", "pop_country_code"]],
                     hide_index=True, use_container_width=True)
        if T("company_offices", market, counting, extra=extra, params=p).empty:
            st.caption("Detailed breakdowns are kept for the largest companies of each market only.")

# ---------------- geography ----------------
with tabs[3]:
    g1, g2 = st.columns(2)
    scope = g1.radio("Map", ["World", "US states"], horizontal=True)
    basis = g2.radio("Basis", ["place_of_performance", "recipient_location"], horizontal=True,
                     format_func=lambda b: "Place of performance" if b == "place_of_performance" else "Recipient location")
    geo = T("by_geo", market, counting, extra="AND basis = ?", params=(basis,))
    if scope == "World":
        agg = geo.groupby("country", as_index=False)[["fy2026_obligations", "actions"]].sum()
        fig = px.choropleth(agg, locations="country", color="fy2026_obligations",
                            color_continuous_scale="Blues", labels={"fy2026_obligations": "$"})
        keys = agg.rename(columns={"country": "key"})
    else:
        us = geo[geo["country"] == "USA"].copy()
        us["state"] = us["location"].astype(str).str.slice(0, 2)
        agg = us.groupby("state", as_index=False)[["fy2026_obligations", "actions"]].sum()
        fig = px.choropleth(agg, locations="state", locationmode="USA-states", scope="usa",
                            color="fy2026_obligations", color_continuous_scale="Blues",
                            labels={"fy2026_obligations": "$"})
        keys = agg.rename(columns={"state": "key"})
    fig.update_layout(height=460, margin=dict(l=0, r=0, t=10, b=0))
    a, b = st.columns([3, 2])
    a.plotly_chart(fig, use_container_width=True)
    keys = keys.sort_values("fy2026_obligations", ascending=False)
    b.dataframe(keys, hide_index=True, use_container_width=True, height=460,
                column_config={"key": "Location", "fy2026_obligations": st.column_config.NumberColumn("Obligations", format="$%,.0f")})
    pick = st.selectbox("Location details", ["—"] + list(keys["key"]))
    if pick != "—":
        if scope == "World":
            ex, pp = "AND basis = ? AND country = ?", (basis, pick)
        else:
            ex, pp = "AND basis = ? AND country = 'USA' AND substr(location, 1, 2) = ?", (basis, pick)
        a, b = st.columns(2)
        with a:
            gc = T("geo_companies", market, counting, extra=ex, params=pp)
            hbar(gc.groupby("company_name", as_index=False)["fy2026_obligations"].sum(), "company_name", f"Top companies – {pick}", 10)
        with b:
            gp = T("geo_psc", Q.BASE if ":" not in market else market, counting, extra=ex, params=pp)
            if len(gp):
                hbar(gp.groupby("psc_description", as_index=False)["fy2026_obligations"].sum(), "psc_description", f"PSC – {pick}", 10)

# ---------------- orders ----------------
with tabs[4]:
    st.caption("Searches every FY2026 DLA contract action (word prefixes, all words must match) in the "
               "description, company name, PIID and PSC code. Totals cover all matches.")
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    text = f1.text_input("Search", placeholder="e.g. valve, seal, SPE4A6…")
    comp_opts = companies.head(1000)
    comp = f2.selectbox("Company", [None] + list(comp_opts["company_id"]),
                        format_func=lambda i: "All" if i is None else comp_opts.loc[comp_opts.company_id == i, "company_name"].iloc[0])
    offs = T("by_office", market, counting, "fy2026_obligations DESC")
    office = f3.selectbox("DLA office", [None] + list(offs["office_code"]),
                          format_func=lambda o: "All" if o is None else f"{o} {offs.loc[offs.office_code == o, 'office_name'].iloc[0] or ''}")
    geo_all = T("by_geo", market, counting, extra="AND basis = 'place_of_performance'")
    ctry = sorted(set(geo_all["country"].dropna()))
    country = f4.selectbox("Place of performance", [None] + ctry, format_func=lambda c: "All" if c is None else c)
    loc = None
    if country == "USA":
        states = sorted(set(geo_all.loc[geo_all.country == "USA", "location"].astype(str).str.slice(0, 2)))
        state = st.selectbox("State", [None] + states, format_func=lambda s: "All US" if s is None else s)
        loc = ("USA", state)
    elif country:
        loc = (country, None)
    rows, tot = search(market, counting, text, comp, office, loc, "place_of_performance", 500)
    st.subheader(f"{tot['n']:,} actions · {money(tot['total'])}")
    st.dataframe(rows.drop(columns=["excluded_as_copy"], errors="ignore"), hide_index=True,
                 use_container_width=True, height=560,
                 column_config={"transaction_amount": st.column_config.NumberColumn("Amount", format="$%,.2f")})
    st.download_button("Download shown rows (CSV)", rows.to_csv(index=False).encode(), "orders.csv", "text/csv")
    if tot["n"] > len(rows):
        st.caption(f"Showing the {len(rows):,} largest of {tot['n']:,} matches – narrow the search to see more.")
