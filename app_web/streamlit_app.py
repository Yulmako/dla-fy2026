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

st.set_page_config(page_title="FY2026 DLA competitor intelligence", layout="wide",
                   initial_sidebar_state="collapsed")
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


ACCENT = "#4C8BF5"
CSS = """
<style>
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"] {display:none}
.block-container {max-width: 1280px; padding-top: 2.2rem; padding-bottom: 3rem}
h1 {font-size: 1.7rem !important; margin-bottom: 0 !important}
h3 {font-size: 1.05rem !important; margin-top: 1.4rem !important}
.src {font-size: .85rem; opacity: .75; margin: .2rem 0 1rem}
.tiles {display:grid; grid-template-columns: repeat(6, minmax(0,1fr)); gap:.6rem; margin:.4rem 0 1rem}
.tile {border:1px solid rgba(128,128,128,.3); border-radius:8px; padding:.6rem .75rem; cursor:help}
.tile .l {font-size:.75rem; opacity:.75; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.tile .v {font-size:1.35rem; font-weight:600; line-height:1.3}
.tile .s {font-size:.75rem; opacity:.7}
@media (max-width: 900px) {.tiles {grid-template-columns: repeat(2, minmax(0,1fr))}}
.chip {display:inline-block; font-size:.8rem; padding:.1rem .55rem; border-radius:12px;
       border:1px solid rgba(128,128,128,.4); margin-right:.4rem}
</style>
"""


def tiles(items):
    html = "".join(f'<div class="tile" title="{tip}"><div class="l">{lab}</div><div class="v">{val}</div>'
                   f'<div class="s">{sub or "&nbsp;"}</div></div>' for lab, val, sub, tip in items)
    st.markdown(f'<div class="tiles">{html}</div>', unsafe_allow_html=True)


def picked(key: str, value):
    """Apply a chart/table click only when it changes (so clearing a filter sticks)."""
    if value is None:
        return None
    if st.session_state.get(f"_last_{key}") == value:
        return None
    st.session_state[f"_last_{key}"] = value
    return value


def points(ev) -> list:
    try:
        return list(ev.get("selection", {}).get("points", [])) if ev else []
    except Exception:
        return []


def hbar(df: pd.DataFrame, label: str, title: str, n: int = 15, key: str | None = None,
         custom: str | None = None):
    st.markdown(f"**{title}**")
    if df.empty:
        st.caption("No data")
        return None
    d = df.nlargest(n, "fy2026_obligations").iloc[::-1].copy()
    d[label] = d[label].fillna("—").astype(str).str.slice(0, 48)
    fig = px.bar(d, x="fy2026_obligations", y=label, orientation="h",
                 custom_data=[custom] if custom else None,
                 labels={"fy2026_obligations": "FY2026 obligations ($)", label: ""})
    fig.update_traces(marker_color=ACCENT, hovertemplate="%{y}<br>$%{x:,.0f}<extra></extra>")
    fig.update_layout(height=max(200, 19 * len(d) + 60), margin=dict(l=0, r=10, t=4, b=0),
                      font=dict(size=10), xaxis_title=None, dragmode=False)
    if key:
        ev = st.plotly_chart(fig, use_container_width=True, key=key, on_select="rerun",
                             selection_mode="points", config={"displayModeBar": False})
        pts = points(ev)
        if pts:
            cd = pts[0].get("customdata")
            return cd[0] if isinstance(cd, (list, tuple)) and cd else pts[0].get("y")
        return None
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    return None


# ---------------- header & controls ----------------
st.markdown(CSS, unsafe_allow_html=True)
ss = st.session_state
for k_, v_ in (("office", None), ("loc", None), ("company", None)):
    ss.setdefault(k_, v_)

mk = markets()
names = dict(zip(mk["market"], mk["market_name"]))
st.title("FY2026 DLA competitor intelligence")
st.markdown(f'<div class="src">{SOURCE.replace("[USAspending.gov](https://www.usaspending.gov)", "<a href=\'https://www.usaspending.gov\' target=\'_blank\'>USAspending.gov</a>")}</div>',
            unsafe_allow_html=True)
c1, c2, _ = st.columns([3, 2, 3])
market = c1.selectbox("Market", list(mk["market"]), format_func=lambda m: names.get(m, m))
counting = c2.selectbox("Counting", list(COUNTINGS), format_func=COUNTINGS.get)
if ss.get("_market") != (market, counting):
    ss["_market"] = (market, counting)
    ss.office = ss.loc = ss.company = None

k = Q.kpis(con, market, counting)
stt = Q.status(con, market, counting)
if stt.get("period_label") and "partial" in str(stt.get("market_status", "")):
    st.warning(f"Partial year: {stt['period_label']}")

tiles([
    ("Obligations", money(k.get("fy2026_obligations")), "",
     "Sum of FY2026 federal action obligations. Not company revenue or profit."),
    ("Awards with activity", f"{int(k.get('award_count') or 0):,}", "",
     "Distinct contract awards with at least one FY2026 action."),
    ("New awards in FY2026", f"{int(k.get('new_award_count') or 0):,}", money(k.get("new_award_obligations")),
     "Base actions (modification 0) dated in FY2026, and their obligations."),
    ("Modifications", f"{int(k.get('modification_count') or 0):,}", money(k.get("modification_obligations")),
     "Actions that change existing awards (modification other than 0), and their obligations."),
    ("Companies", f"{int(k.get('company_count') or 0):,}", "",
     "Companies grouped by parent UEI where reported, otherwise recipient UEI."),
    ("Top-10 concentration", f"{float(k.get('cr10_pct') or 0):.1f}%", "",
     "Share of obligations received by the 10 largest companies."),
])

# ---------------- overview ----------------
companies = T("companies", market, counting, "rank")
a, b = st.columns(2)
with a:
    hbar(T("companies", market, counting, "rank", 15), "company_name", "Top 15 companies")
with b:
    st.markdown("**Obligations by month**")
    m = T("monthly", market, counting, "month")
    if len(m):
        fig = px.bar(m, x="month", y="fy2026_obligations", labels={"fy2026_obligations": "", "month": ""})
        fig.update_traces(marker_color=ACCENT, hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>")
        fig.update_layout(height=345, margin=dict(l=0, r=10, t=4, b=0), font=dict(size=10))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
a, b = st.columns(2)
with a:
    hbar(T("by_psc", market, counting), "psc_description", "PSC (product / service)")
with b:
    hbar(T("by_naics", market, counting), "naics_description", "NAICS (industry)")
a, b = st.columns(2)
with a:
    v = picked("office", hbar(T("by_office", market, counting), "office_name",
                              "DLA contracting offices (click to filter)", key="office_chart", custom="office_code"))
    if v:
        ss.office = v
with b:
    st.markdown("**Award size (net FY2026 obligations)**")
    s_ = T("award_size", market, counting)
    if len(s_):
        fig = px.bar(s_, x="bucket", y="awards", labels={"bucket": "", "awards": "Awards"})
        fig.update_traces(marker_color=ACCENT)
        fig.update_layout(height=300, margin=dict(l=0, r=10, t=4, b=0), font=dict(size=10))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

if ss.office:
    offs_all = T("by_office", market, counting)
    oname = offs_all.loc[offs_all.office_code == ss.office, "office_name"]
    on = f"{ss.office} {oname.iloc[0] if len(oname) else ''}"
    h, x = st.columns([6, 1])
    h.markdown(f"### Office: {on}")
    if x.button("✕ Clear office", key="clr_office_top"):
        ss.office = None
        st.rerun()
    a, b = st.columns(2)
    p_ = (ss.office,)
    with a:
        hbar(T("office_companies", market, counting, extra="AND office_code = ?", params=p_),
             "company_name", "Top companies in this office", 10)
    with b:
        hbar(T("office_psc", market, counting, extra="AND office_code = ?", params=p_),
             "psc_description", "PSC in this office", 10)

# ---------------- geography ----------------
st.markdown("### Geography")
g1, g2, _ = st.columns([2, 2, 4])
scope = g1.selectbox("Map", ["World", "US states"])
basis = g2.selectbox("Basis", ["place_of_performance", "recipient_location"],
                     format_func=lambda b_: "Place of performance" if b_ == "place_of_performance" else "Recipient location")
geo = T("by_geo", market, counting, extra="AND basis = ?", params=(basis,))
if scope == "World":
    agg = geo.groupby("country", as_index=False)[["fy2026_obligations", "actions"]].sum().rename(columns={"country": "key"})
    fig = px.choropleth(agg, locations="key", color="fy2026_obligations", color_continuous_scale="Blues")
else:
    us = geo[geo["country"] == "USA"].copy()
    us["key"] = us["location"].astype(str).str.slice(0, 2)
    agg = us.groupby("key", as_index=False)[["fy2026_obligations", "actions"]].sum()
    fig = px.choropleth(agg, locations="key", locationmode="USA-states", scope="usa",
                        color="fy2026_obligations", color_continuous_scale="Blues")
fig.update_traces(hovertemplate="%{location}<br>$%{z:,.0f}<extra></extra>")
fig.update_layout(height=420, margin=dict(l=0, r=0, t=0, b=0), coloraxis_showscale=False,
                  geo=dict(bgcolor="rgba(0,0,0,0)", showframe=False, showcoastlines=False))
a, b = st.columns([3, 2])
with a:
    ev = st.plotly_chart(fig, use_container_width=True, key=f"map_{scope}", on_select="rerun",
                         selection_mode="points", config={"displayModeBar": False, "scrollZoom": True})
    pts = points(ev)
    v = picked(f"map_{scope}", pts[0].get("location") if pts else None)
    if v:
        ss.loc = ("USA", v) if scope == "US states" else (v, None)
    st.caption("Click a country or state to filter. Scroll to zoom.")
with b:
    agg = agg.sort_values("fy2026_obligations", ascending=False)
    st.dataframe(agg[["key", "fy2026_obligations"]], hide_index=True, use_container_width=True, height=420,
                 column_config={"key": "Location",
                                "fy2026_obligations": st.column_config.NumberColumn("Obligations", format="$%,.0f")})

if ss.loc:
    country, state = ss.loc
    name = f"{state}, USA" if state else country
    h, x = st.columns([6, 1])
    h.markdown(f"### Location: {name}")
    if x.button("✕ Clear location", key="clr_loc_top"):
        ss.loc = None
        st.rerun()
    if state:
        ex, pp = "AND basis = ? AND country = 'USA' AND substr(location, 1, 2) = ?", (basis, state)
    else:
        ex, pp = "AND basis = ? AND country = ?", (basis, country)
    a, b = st.columns(2)
    with a:
        gc = T("geo_companies", market, counting, extra=ex, params=pp)
        hbar(gc.groupby("company_name", as_index=False)["fy2026_obligations"].sum(), "company_name",
             "Top companies here", 10)
    with b:
        gp = T("geo_psc", market, counting, extra=ex, params=pp)
        if len(gp):
            hbar(gp.groupby("psc_description", as_index=False)["fy2026_obligations"].sum(),
                 "psc_description", "PSC here", 10)

# ---------------- ranking ----------------
st.markdown("### Company ranking")
st.caption("Click a row to open the company profile below.")
cols = [c for c in ("rank", "company_name", "company_uei", "fy2026_obligations",
                    "share_of_selected_dataset_pct", "award_count", "median_positive_award_fy2026")
        if c in companies]
ev = st.dataframe(companies[cols], hide_index=True, use_container_width=True, height=420,
                  on_select="rerun", selection_mode="single-row", key="ranking",
                  column_config={"rank": "#", "company_name": "Company", "company_uei": "UEI",
                                 "fy2026_obligations": st.column_config.NumberColumn("Obligations", format="$%,.0f"),
                                 "share_of_selected_dataset_pct": st.column_config.NumberColumn("Share", format="%.2f%%"),
                                 "award_count": "Awards",
                                 "median_positive_award_fy2026": st.column_config.NumberColumn("Median award", format="$%,.0f")})
try:
    rows_sel = list(ev.get("selection", {}).get("rows", [])) if ev else []
except Exception:
    rows_sel = []
v = picked("ranking", int(companies.iloc[rows_sel[0]]["company_id"]) if rows_sel else None)
if v is not None:
    ss.company = v

# ---------------- profile ----------------
st.markdown("### Company profile")
if companies.empty:
    st.info("No companies in this market.")
else:
    opts = companies.head(500)
    ids = list(opts["company_id"])
    if ss.company not in ids:
        ss.company = ids[0]
    cname = dict(zip(opts["company_id"], opts["company_name"]))
    crank = dict(zip(opts["company_id"], opts["rank"]))
    cid = st.selectbox("Company", ids, key="company", format_func=lambda i: f"#{int(crank[i])} {cname[i]}",
                       label_visibility="collapsed")
    row = opts[opts["company_id"] == cid].iloc[0]
    tiles([("Obligations", money(row["fy2026_obligations"]), "", "FY2026 obligations to this company in the selected market."),
           ("Share of market", f"{row['share_of_selected_dataset_pct']:.2f}%", "", "Share of the selected market's obligations."),
           ("Awards", f"{int(row['award_count']):,}", "", "Distinct awards with FY2026 activity."),
           ("Rank", f"#{int(row['rank'])}", "", "Rank by obligations in the selected market.")])
    extra, p_ = "AND company_id = ?", (int(cid),)
    co = T("company_offices", market, counting, extra=extra, params=p_)
    a, b, d = st.columns(3)
    with a:
        hbar(co, "office_name", "DLA contracting offices", 10)
    with b:
        hbar(T("company_psc", market, counting, extra=extra, params=p_), "psc_description", "PSC", 10)
    with d:
        hbar(T("company_naics", market, counting, extra=extra, params=p_), "naics_description", "NAICS", 10)
    if co.empty:
        st.caption("Detailed breakdowns are kept for the largest companies of each market only.")
    rows, tot = search(market, counting, "", int(cid), None, None, "place_of_performance", 25)
    st.markdown(f"**Largest orders** · {tot['n']:,} actions · {money(tot['total'])}")
    st.dataframe(rows[["transaction_date", "transaction_amount", "piid", "psc_code", "description",
                       "office_code", "pop_state_code", "pop_country_code"]],
                 hide_index=True, use_container_width=True,
                 column_config={"transaction_date": "Date", "piid": "PIID", "psc_code": "PSC",
                                "description": "Description", "office_code": "Office",
                                "pop_state_code": "State", "pop_country_code": "Country",
                                "transaction_amount": st.column_config.NumberColumn("Amount", format="$%,.2f")})

# ---------------- orders search ----------------
st.markdown("### Orders search")
st.caption("Every FY2026 DLA contract action of the selected market. All words must match "
           "(description, company, PIID, PSC). Totals cover all matches.")
f1, f2 = st.columns([3, 2])
text = f1.text_input("Search", placeholder="e.g. valve, seal, SPE4A6…", label_visibility="collapsed")
only_company = f2.checkbox("Only the company shown in the profile", value=False)
chips = []
if ss.office:
    chips.append(("office", f"Office: {ss.office}"))
if ss.loc:
    chips.append(("loc", "Location: " + (f"{ss.loc[1]}, USA" if ss.loc[1] else ss.loc[0])))
if only_company and ss.company is not None:
    chips.append(("company_chip", f"Company: {companies.loc[companies.company_id == ss.company, 'company_name'].iloc[0]}"))
if chips:
    cc = st.columns(len(chips) + 3)
    for i_, (key_, lab_) in enumerate(chips):
        if key_ != "company_chip" and cc[i_].button(f"✕ {lab_}", key=f"chip_{key_}"):
            ss[key_] = None
            st.rerun()
        elif key_ == "company_chip":
            cc[i_].markdown(f'<span class="chip">{lab_}</span>', unsafe_allow_html=True)
rows, tot = search(market, counting, text, ss.company if only_company else None, ss.office,
                   ss.loc, basis, 500)
st.markdown(f"**{tot['n']:,} actions · {money(tot['total'])}**")
st.dataframe(rows.drop(columns=["excluded_as_copy", "company_id", "potential_duplicate", "psc_group"], errors="ignore"),
             hide_index=True, use_container_width=True, height=520,
             column_config={"transaction_amount": st.column_config.NumberColumn("Amount", format="$%,.2f")})
st.download_button("Download shown rows (CSV)", rows.to_csv(index=False).encode(), "orders.csv", "text/csv")
if tot["n"] > len(rows):
    st.caption(f"Showing the {len(rows):,} largest of {tot['n']:,} matches – narrow the search to see more.")
