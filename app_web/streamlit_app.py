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
h3 {font-size: 1.15rem !important; margin-top: 1.4rem !important}
h4 {font-size: 1.05rem !important; padding: 0 0 .2rem !important}
.muted {font-size: .82rem; opacity: .7; margin-bottom: .4rem}
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


ROW_H = 26  # px per bar row, as in the Claude dashboard


def compact(v) -> str:
    """$4.8B / $928.1M / $612K – like Intl compact with up to 2 decimals."""
    v = float(v or 0)
    sign = "-" if v < 0 else ""
    a = abs(v)
    for d, s_ in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= d:
            return f"{sign}${a / d:.2f}".rstrip("0").rstrip(".") + s_
    return f"{sign}${a:,.0f}"


def trunc(t, n: int = 32) -> str:
    t = "—" if t is None or (isinstance(t, float) and pd.isna(t)) else str(t)
    return t if len(t) <= n else t[: n - 1] + "…"


def card(title: str, subtitle: str, height: int):
    c = st.container(border=True, height=height)
    c.markdown(f"#### {title}")
    c.markdown(f'<div class="muted">{subtitle}</div>', unsafe_allow_html=True)
    return c


import html as _html  # noqa: E402
from urllib.parse import urlencode  # noqa: E402
import json  # noqa: E402

import streamlit.components.v1 as components  # noqa: E402


# ---- page state lives in the URL (?market=…&office=…), so a click on a bar is a plain link
class _State:
    KEYS = ("market", "counting", "office", "loc", "company")

    def __getattr__(self, k):
        v = st.query_params.get(k)
        if v in (None, ""):
            return None
        if k == "company":
            return int(v) if str(v).lstrip("-").isdigit() else None
        if k == "loc":
            c, _, l_ = str(v).partition("|")
            return (c, l_ or None)
        return v

    def __setattr__(self, k, v):
        if v is None:
            if k in st.query_params:
                del st.query_params[k]
            return
        st.query_params[k] = f"{v[0]}|{v[1] or ''}" if k == "loc" else str(v)


ss = _State()


def href(**changes) -> str:
    q = {k: st.query_params.get(k) for k in _State.KEYS if st.query_params.get(k)}
    for k_, v_ in changes.items():
        if v_ is None:
            q.pop(k_, None)
        else:
            q[k_] = str(v_)
    return "?" + urlencode(q)


def _e(t) -> str:
    return _html.escape("" if t is None else str(t), quote=True)


def _hbar_html(c: dict) -> str:
    rows = c["rows"]
    mx = max([r["value"] for r in rows] + [1e-9])
    out = []
    for r in rows:
        w = max(0.0, r["value"]) / mx * 74 if mx > 0 else 0
        tip = _e(" · ".join([r["label"]] + list(r.get("tip") or [])))
        inner = (f'<span class="bl">{_e(r["label"])}</span><span class="bt">'
                 f'<span class="bb" style="width:{w:.2f}%;opacity:{0.3 if r.get("dim") else 1}"></span>'
                 f'<span class="bv">{_e(compact(r["value"]) if c.get("fmt", "usd") == "usd" else format(r["value"], ",.0f"))}</span></span>')
        if r.get("href"):
            out.append(f'<a class="br" href="{_e(r["href"])}" target="_self" title="{tip}">{inner}</a>')
        else:
            out.append(f'<div class="br" title="{tip}">{inner}</div>')
    share = int(round(c.get("labelShare", 0.5) * 100))
    return f'<div class="hb" style="grid-template-columns:minmax(0,{share}%) minmax(0,1fr)">{"".join(out)}</div>'


def _nice(mx: float, n: int = 4) -> list[float]:
    if mx <= 0:
        return [0, 1]
    import math
    step = 10 ** math.floor(math.log10(mx / n))
    e = mx / n / step
    step *= 10 if e >= 7.5 else 5 if e >= 3.5 else 2 if e >= 1.5 else 1
    ticks, v = [], 0.0
    while v <= mx + step * 0.999:
        ticks.append(v)
        v += step
    return ticks


def _vbar_html(c: dict) -> str:
    rows = c["rows"]
    fmt = compact if c.get("fmt", "usd") == "usd" else (lambda v: f"{v:,.0f}")
    ticks = _nice(max([r["value"] for r in rows] + [0]))
    top = ticks[-1] or 1
    longest = max(len(r["label"]) for r in rows)
    every = 1 if len(rows) * max(longest, 3) <= 40 else 2
    grid = "".join(f'<div class="vg" style="bottom:{t / top * 100:.2f}%"><span>{_e(fmt(t))}</span></div>' for t in ticks)
    bars = "".join(
        f'<div class="vc" title="{_e(" · ".join([r.get("title") or r["label"]] + list(r.get("tip") or [])))}">'
        f'<div class="vb{" vm" if r.get("muted") else ""}" style="height:{max(0, r["value"]) / top * 100:.2f}%"></div>'
        f'<div class="vx">{_e(r["label"]) if i % every == 0 else "&nbsp;"}</div></div>'
        for i, r in enumerate(rows))
    return f'<div class="vw"><div class="vp">{grid}<div class="vbars">{bars}</div></div></div>'


CARD_CSS = """
<style>
.bc-grid{display:grid;gap:14px;margin:.2rem 0 1rem}
.bc-card{border:1px solid rgba(128,128,128,.3);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:6px;min-width:0}
.bc-card.plain{border:none;padding:4px 0}
.bc-card h4{font-size:1.05rem !important;font-weight:600;margin:0 !important;padding:0 !important}
.bc-card.plain h4{font-size:.95rem !important}
.bc-sub{font-size:.72rem;opacity:.65;line-height:1.35;margin:0 0 4px}
.hb{display:grid;row-gap:0;font-size:.74rem}
.br{display:contents;color:inherit !important;text-decoration:none !important}
.br>span{padding:3px 0}
a.br:hover .bl{text-decoration:underline}
.bl{text-align:right;padding-right:8px !important;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bt{display:flex;align-items:center;gap:5px;min-width:0}
.bb{display:block;height:14px;border-radius:3px;background:#4C8BF5;flex:none;min-width:1px}
.bv{opacity:.62;white-space:nowrap;font-size:.72rem}
.vw{padding:4px 0 0 0}
.vp{position:relative;height:210px;margin:0 0 22px 46px}
.vg{position:absolute;left:0;right:0;border-top:1px solid rgba(128,128,128,.22)}
.vg span{position:absolute;right:calc(100% + 6px);top:-.55rem;font-size:.68rem;opacity:.62;white-space:nowrap}
.vbars{position:absolute;inset:0;display:flex;align-items:flex-end;gap:6px}
.vc{flex:1;height:100%;display:flex;flex-direction:column;justify-content:flex-end;position:relative;min-width:0}
.vb{background:#4C8BF5;border-radius:3px 3px 0 0}
.vb.vm{background:rgba(128,128,128,.45)}
.vx{position:absolute;top:calc(100% + 4px);left:-20px;right:-20px;text-align:center;font-size:.68rem;opacity:.62;white-space:nowrap}
@media (max-width: 900px){.bc-grid{grid-template-columns:minmax(0,1fr) !important}}
</style>
"""


def barcards(cards: list[dict], key: str = "", cols: int = 3, plain: bool = False):
    """Charts drawn like the Claude dashboard, as plain HTML in the page (no iframe, no gaps).
    Clickable bars are links that change the URL state."""
    parts = []
    for c in cards:
        body = "—" if not c.get("rows") else (_vbar_html(c) if c.get("kind") == "v" else _hbar_html(c))
        parts.append(f'<div class="bc-card{" plain" if plain else ""}">'
                     + (f"<h4>{_e(c['title'])}</h4>" if c.get("title") else "")
                     + (f'<p class="bc-sub">{_e(c["subtitle"])}</p>' if c.get("subtitle") else "")
                     + f"{body}</div>")
    page = f'<div class="bc-grid" style="grid-template-columns:repeat({cols},minmax(0,1fr))">{"".join(parts)}</div>'
    (st.html if hasattr(st, "html") else (lambda h: st.markdown(h, unsafe_allow_html=True)))(page)
    return None


def scroll_to(text: str) -> None:
    """After a link click the page reloads at the top: scroll to the section heading."""
    components.html(f"""<script>
    const t = {json.dumps(text)}; let n = 0;
    const go = () => {{ const d = window.parent.document;
      const h = [...d.querySelectorAll('h3')].find((e) => e.textContent.trim().startsWith(t));
      if (h) h.scrollIntoView({{block: 'start'}}); else if (n++ < 40) setTimeout(go, 150); }};
    go();</script>""", height=0)


def hrows(df: pd.DataFrame, label, n: int = 10, key: str | None = None, tip=None, dim=None, link=None) -> list[dict]:
    if df is None or df.empty:
        return []
    d = df.nlargest(n, "fy2026_obligations")
    out = []
    for _, r in d.iterrows():
        lab = label(r) if callable(label) else r[label]
        lab = "—" if lab is None or (isinstance(lab, float) and pd.isna(lab)) else str(lab)
        if len(lab) > 44:
            lab = lab[:43] + "…"
        out.append({"label": lab, "value": fnum(r["fy2026_obligations"]),
                    "key": None if key is None else str(r[key]),
                    "tip": tip(r) if tip else [compact(r["fy2026_obligations"])],
                    "dim": bool(dim(r)) if dim else False,
                    "href": link(r) if link else None})
    return out


def hcard(title, subtitle, rows, cid=None, clickable=False, share=0.56):
    return {"id": cid or title, "title": title, "subtitle": subtitle, "kind": "h", "rows": rows,
            "clickable": clickable, "labelShare": share, "fmt": "usd"}


def fnum(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if pd.isna(v) else v


def pct(v, d=1):
    return "—" if v is None or pd.isna(v) else f"{float(v):.{d}f}%"


# ---------------- header & controls ----------------
st.markdown(CSS + CARD_CSS, unsafe_allow_html=True)

mk = markets()
names = dict(zip(mk["market"], mk["market_name"]))
st.title("FY2026 DLA competitor intelligence")
st.markdown(f'<div class="src">{SOURCE.replace("[USAspending.gov](https://www.usaspending.gov)", "<a href=\'https://www.usaspending.gov\' target=\'_blank\'>USAspending.gov</a>")}</div>',
            unsafe_allow_html=True)
c1, c2, _ = st.columns([3, 2, 3])
mlist, clist = list(mk["market"]), list(COUNTINGS)
m0 = ss.market if ss.market in mlist else mlist[0]
c0 = ss.counting if ss.counting in clist else "probable_duplicates_removed"
market = c1.selectbox("Market", mlist, index=mlist.index(m0), format_func=lambda m: names.get(m, m))
counting = c2.selectbox("Counting", clist, index=clist.index(c0), format_func=COUNTINGS.get)
if market != m0:
    ss.office = ss.loc = ss.company = None
ss.market = market
ss.counting = counting

k = Q.kpis(con, market, counting)
stt = Q.status(con, market, counting)


def provisional_note(d: dict) -> str | None:
    normal, latest = d.get("reporting_normal_through"), d.get("latest_action_date_in_data")
    if not normal or pd.isna(normal):
        return None
    parts = [f"Reporting volume normal through {normal}"]
    if latest and not pd.isna(latest):
        parts.append(f"Latest action date in loaded data {latest}")
    gen = f"Snapshot generated {d.get('generated_at')}" if d.get("generated_at") else ""
    if gen and d.get("source_last_updated") and not pd.isna(d.get("source_last_updated")):
        gen += f" (USAspending updated {d['source_last_updated']})"
    if gen:
        parts.append(gen)
    first = (pd.Timestamp(normal) + pd.offsets.MonthBegin(1))
    end = pd.Timestamp(d.get("period_end") or "2026-09-30")
    gap = ""
    if first <= end:
        a_, b_ = first.strftime("%B"), end.strftime("%B %Y")
        gap = (f", so {a_ if first.month != end.month else ''}{'–' if first.month != end.month else ''}{b_} "
               f"{'are' if first.month != end.month else 'is'} largely missing")
    reload_ = d.get("scheduled_full_reload")
    text = (" · ".join(parts) + "<br>DoD contract actions reach USAspending about 90 days after they occur"
            + gap + ". All rankings and shares are provisional; missing obligations are not estimated."
            + (f" Full reload scheduled {reload_}." if reload_ and not pd.isna(reload_) else ""))
    return text


note = provisional_note(stt)
if note:
    st.markdown(f'<div class="src" style="margin-top:.2rem">{note}</div>', unsafe_allow_html=True)

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
code_lab = lambda code, desc: (lambda r: f"{r[code] or ''} {r[desc] or ''}".strip())  # noqa: E731
sup_tip = lambda r: [f"{compact(r['fy2026_obligations'])} · {pct(r.get('share_of_selected_dataset_pct'))}",  # noqa: E731
                     f"{int(r.get('supplier_count') or 0):,} suppliers", f"Top supplier: {r.get('top_supplier') or '—'}"]
normal = str(stt.get("reporting_normal_through") or "")[:7]
mon = T("monthly", market, counting, "month")
size = T("award_size", market, counting)
top15 = T("companies", market, counting, "rank", 15)
cards = [
    hcard("Top 15 companies", "Obligations by company (reported parent UEI); click a bar for the profile.",
          hrows(top15, "company_name", 15, key="company_id",
                link=lambda r: href(company=int(r["company_id"]), go="profile"),
                tip=lambda r: [f"Obligations {compact(r['fy2026_obligations'])}", f"Share {pct(r['share_of_selected_dataset_pct'])}",
                               f"{int(r['award_count']):,} awards · click for profile"]), "top", clickable=True, share=0.52),
    {"id": "month", "title": "Obligations by month", "kind": "v", "fmt": "usd", "height": 240,
     "subtitle": "Grey = month after the normal-reporting date (incomplete, not estimated).",
     "rows": [{"label": pd.Timestamp(str(r["month"]) + "-01").strftime("%b"),
               "title": pd.Timestamp(str(r["month"]) + "-01").strftime("%b %Y"),
               "value": fnum(r["fy2026_obligations"]), "muted": bool(normal) and str(r["month"]) > normal,
               "tip": [compact(r["fy2026_obligations"]), f"{int(fnum(r['actions'])):,} actions"]} for _, r in mon.iterrows()]},
    hcard("Top product & service codes (PSC)", "Obligations by PSC; hover for suppliers and the top supplier.",
          hrows(T("by_psc", market, counting), code_lab("psc_code", "psc_description"), 10, tip=sup_tip), "psc"),
    hcard("Top industries (NAICS)", "Obligations by NAICS.",
          hrows(T("by_naics", market, counting), code_lab("naics_code", "naics_description"), 10, tip=sup_tip), "naics"),
    hcard("DLA contracting offices",
          "Obligations by the DLA office that awarded the action; click an office to filter the order lists.",
          hrows(T("by_office", market, counting), code_lab("office_code", "office_name"), 12, key="office_code",
                tip=lambda r: sup_tip(r) + ["click to filter orders by this office"],
                dim=lambda r: bool(ss.office) and r["office_code"] != ss.office,
                link=lambda r: href(office=None if r["office_code"] == ss.office else r["office_code"], go="office")),
          "office", clickable=True),
    {"id": "size", "title": "Award size", "kind": "v", "fmt": "int", "height": 240,
     "subtitle": "Awards with positive net obligation in the period, by size bucket (count).",
     "rows": [{"label": str(r["bucket"]), "value": fnum(r["awards"]),
               "tip": [f"{int(r['awards']):,} awards", f"{compact(r['fy2026_obligations'])} obligations"]}
              for _, r in size.iterrows()]},
]
barcards(cards, cols=3)

if ss.office:
    offs_all = T("by_office", market, counting)
    oname = offs_all.loc[offs_all.office_code == ss.office, "office_name"]
    on = f"{ss.office} {oname.iloc[0] if len(oname) else ''}"
    h, x = st.columns([6, 1])
    h.markdown(f"### Office: {on}")
    if x.button("✕ Clear office", key="clr_office_top"):
        ss.office = None
        st.rerun()
    p_ = (ss.office,)
    share_tip = lambda col, what: (lambda r: [f"{compact(r['fy2026_obligations'])} · {pct(r[col])} of this {what}",  # noqa: E731
                                              f"{int(fnum(r['actions'])):,} actions"])
    barcards([hcard("Top companies in this office", "", hrows(T("office_companies", market, counting, extra="AND office_code = ?", params=p_),
                                                              "company_name", 10, tip=share_tip("share_of_office_pct", "office")), "of_comp", share=0.55),
              hcard("Top product & service codes in this office", "", hrows(T("office_psc", market, counting, extra="AND office_code = ?", params=p_),
                                                                           code_lab("psc_code", "psc_description"), 10,
                                                                           tip=share_tip("share_of_office_pct", "office")), "of_psc", share=0.55)],
             key="office_panel", cols=2, plain=True)

# ---------------- geography ----------------
st.markdown("### Geography")
g1, g2, _ = st.columns([2, 2, 4])
scope = g1.selectbox("Map", ["World", "US states"])
basis = g2.selectbox("Basis", ["place_of_performance", "recipient_location"],
                     format_func=lambda b_: "Place of performance" if b_ == "place_of_performance" else "Recipient location")
geo = T("by_geo", market, counting, extra="AND basis = ?", params=(basis,))
import numpy as np  # noqa: E402

MAP_SCALE = [[0, "#d6e4fd"], [0.5, "#6fa1f7"], [1, "#1f4fbf"]]
if scope == "World":
    agg = geo.groupby("country", as_index=False)[["fy2026_obligations", "actions"]].sum().rename(columns={"country": "key"})
    loc_kw = dict(locations="key")
else:
    us = geo[geo["country"] == "USA"].copy()
    us["key"] = us["location"].astype(str).str.slice(0, 2)
    agg = us.groupby("key", as_index=False)[["fy2026_obligations", "actions"]].sum()
    loc_kw = dict(locations="key", locationmode="USA-states")
agg = agg[agg["fy2026_obligations"] > 0]
# log colour: the US is ~100x larger than any other country, a linear scale hides the rest
agg["shade"] = np.log10(agg["fy2026_obligations"].clip(lower=1))
fig = px.choropleth(agg, color="shade", color_continuous_scale=MAP_SCALE,
                    custom_data=["fy2026_obligations"], **loc_kw)
fig.update_traces(hovertemplate="%{location}<br>$%{customdata[0]:,.0f}<extra></extra>",
                  marker_line_color="rgba(128,128,128,0.6)", marker_line_width=0.4)
geo_kw = dict(bgcolor="rgba(0,0,0,0)", showframe=False, showcoastlines=False,
              showland=True, landcolor="rgba(128,128,128,0.22)",
              showcountries=True, countrycolor="rgba(128,128,128,0.6)", countrywidth=0.4,
              showocean=False, showlakes=False)
if scope == "World":
    geo_kw.update(projection_type="natural earth", lataxis_range=[-57, 85], lonaxis_range=[-170, 190])
else:
    geo_kw.update(scope="usa", showsubunits=True, subunitcolor="rgba(128,128,128,0.6)", subunitwidth=0.4)
fig.update_geos(**geo_kw)
fig.update_layout(height=420, margin=dict(l=0, r=0, t=0, b=0), coloraxis_showscale=False,
                  paper_bgcolor="rgba(0,0,0,0)")
a, b = st.columns([3, 2])
with a:
    ev = st.plotly_chart(fig, use_container_width=True, key=f"map_{scope}", on_select="rerun",
                         selection_mode="points", config={"displayModeBar": False, "scrollZoom": True})
    pts = points(ev)
    v = picked(f"map_{scope}", pts[0].get("location") if pts else None)
    if v:
        ss.loc = ("USA", v) if scope == "US states" else (v, None)
        st.rerun()
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
    gc = T("geo_companies", market, counting, extra=ex, params=pp)
    gc = gc.groupby("company_name", as_index=False)[["fy2026_obligations", "actions"]].sum()
    gp = T("geo_psc", market, counting, extra=ex, params=pp)
    gp = gp.groupby(["psc_code", "psc_description"], as_index=False, dropna=False)[["fy2026_obligations", "actions"]].sum()
    loc_tip = lambda r: [compact(r["fy2026_obligations"]), f"{int(fnum(r['actions'])):,} actions"]  # noqa: E731
    barcards([hcard("Top companies in this location", "", hrows(gc, "company_name", 10, tip=loc_tip), "l_comp", share=0.55),
              hcard("Top product & service codes in this location",
                    "" if len(gp) else "PSC mix per location is exported for DLA – all contracts.",
                    hrows(gp, code_lab("psc_code", "psc_description"), 10, tip=loc_tip), "l_psc", share=0.55)],
             key="loc_panel", cols=2, plain=True)

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
    st.rerun()

# ---------------- profile ----------------
st.markdown("### Company profile")
if companies.empty:
    st.info("No companies in this market.")
else:
    opts = companies.head(500)
    ids = list(opts["company_id"])
    cur_c = ss.company if ss.company in ids else ids[0]
    cname = dict(zip(opts["company_id"], opts["company_name"]))
    crank = dict(zip(opts["company_id"], opts["rank"]))
    cid = st.selectbox("Company", ids, index=ids.index(cur_c), key=f"company_sel_{market}_{cur_c}",
                       format_func=lambda i: f"#{int(crank[i])} {cname[i]}", label_visibility="collapsed")
    if cid != cur_c:
        ss.company = int(cid)
        st.rerun()
    row = opts[opts["company_id"] == cid].iloc[0]
    tiles([("Obligations", money(row["fy2026_obligations"]), "", "FY2026 obligations to this company in the selected market."),
           ("Share of market", f"{row['share_of_selected_dataset_pct']:.2f}%", "", "Share of the selected market's obligations."),
           ("Awards", f"{int(row['award_count']):,}", "", "Distinct awards with FY2026 activity."),
           ("Rank", f"#{int(row['rank'])}", "", "Rank by obligations in the selected market.")])
    extra, p_ = "AND company_id = ?", (int(cid),)
    co = T("company_offices", market, counting, extra=extra, params=p_)
    ent_tip = lambda r: [compact(r["fy2026_obligations"]), f"{pct(r['share_of_entity_pct'])} of this company"]  # noqa: E731
    barcards([hcard("DLA contracting offices", "", hrows(co, code_lab("office_code", "office_name"), 10, tip=ent_tip), "p_office", share=0.6),
              hcard("PSC", "", hrows(T("company_psc", market, counting, extra=extra, params=p_),
                                     code_lab("psc_code", "psc_description"), 10, tip=ent_tip), "p_psc", share=0.6),
              hcard("NAICS", "", hrows(T("company_naics", market, counting, extra=extra, params=p_),
                                       code_lab("naics_code", "naics_description"), 10, tip=ent_tip), "p_naics", share=0.6)],
             key="profile_charts", cols=3, plain=True)
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

# after a bar click (page reload) scroll back to the section that changed
go = st.query_params.get("go")
if go:
    del st.query_params["go"]
    scroll_to({"profile": "Company profile", "office": "Office:"}.get(go, ""))

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
            setattr(ss, key_, None)
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
