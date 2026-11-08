"""Dashboard snapshot: compact CSV tables of FY2026 aggregates for a hosted page.

Built from STAGED data with the same transform and metrics as the app, in both
counting modes, so the page can show either until the duplicate rule is chosen:
  usaspending_basis           – every record counted (matches USAspending totals)
  probable_duplicates_removed – exclude_flagged_copies rule from pipeline.yaml
Only aggregates are exported (no transaction-level rows).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.analytics import metrics as M
from src.etl.transform import TransformResult

MODES = {"usaspending_basis": "count_all_flag",
         "probable_duplicates_removed": "exclude_flagged_copies"}


def flat_frame(r: TransformResult, segment_id: int | None = None) -> pd.DataFrame:
    """Pandas equivalent of the v_market_transactions_fy2026 view (optionally
    for one segment only, which keeps memory bounded for very large markets)."""
    t = r.tables
    fts = t["fact_transaction_segments"]
    if segment_id is not None:
        fts = fts[fts["segment_id"] == segment_id]
    rec = t["dim_recipients"][["recipient_id", "recipient_uei", "recipient_name", "country",
                               "state", "parent_uei_conflict", "company_id"]].rename(
        columns={"country": "recipient_country", "state": "recipient_state"})
    comp = t["dim_companies"][["company_id", "company_key", "company_uei", "company_name",
                               "parent_status"]]
    ag = t["dim_agencies"][["agency_id", "agency_name", "parent_agency_name"]].rename(
        columns={"agency_name": "awarding_sub_agency", "parent_agency_name": "awarding_agency"})
    aw = t["fact_awards"][["award_id", "piid", "award_description", "award_amount",
                           "cumulative_obligation", "award_type_code", "award_type"]].rename(
        columns={"award_amount": "award_ceiling_lifetime",
                 "cumulative_obligation": "award_cumulative_obligation_lifetime"})
    df = (fts
          .merge(t["fact_transactions"], on="transaction_id")
          .merge(aw, on="award_id", how="left")
          .merge(rec, on="recipient_id", how="left")
          .merge(comp, on="company_id", how="left")
          .merge(ag, on="agency_id", how="left")
          .merge(t["dim_naics"][["naics_code", "naics_description"]], on="naics_code", how="left")
          .merge(t["dim_psc"][["psc_code", "psc_description"]], on="psc_code", how="left")
          .merge(t["dim_offices"][["office_code", "office_name"]].drop_duplicates("office_code"),
                 on="office_code", how="left"))
    df["parent_uei_conflict"] = df["parent_uei_conflict"].fillna(False).astype(bool)
    return df


def _tag(df: pd.DataFrame, market: str, mode: str) -> pd.DataFrame:
    df = df.copy()
    df.insert(0, "counting", mode)
    df.insert(0, "market", market)
    return df


def market_tables(df: pd.DataFrame, market: str, mode: str, top_profiles: int = 50) -> dict:
    out: dict[str, pd.DataFrame] = {}
    k = M.market_kpis(df, "company")
    k.pop("data_through_date", None)
    k.update(action_split(df))
    out["kpis"] = _tag(pd.DataFrame([k]), market, mode)
    comp = M.entity_summary(df, "company")
    out["companies"] = _tag(comp[[
        "rank", "company_id", "name", "uei", "parent_status", "recipient_uei_count",
        "fy2026_obligations", "share_of_selected_dataset_pct", "award_count",
        "positive_net_award_count", "zero_net_award_count", "negative_net_award_count",
        "avg_positive_award_fy2026", "median_positive_award_fy2026", "largest_award_fy2026",
        "agency_count", "naics_count", "psc_count"]].rename(
        columns={"name": "company_name", "uei": "company_uei"}).merge(
        df.groupby("company_id").size().rename("action_count").reset_index(),
        on="company_id", how="left"), market, mode)
    for dim, label, name in (("awarding_sub_agency", None, "by_agency"),
                             ("office_code", "office_name", "by_office"),
                             ("naics_code", "naics_description", "by_naics"),
                             ("psc_code", "psc_description", "by_psc")):
        out[name] = _tag(M.obligations_by(df, dim, label), market, mode)
    months = pd.to_datetime(df["transaction_date"]).dt.to_period("M").astype(str)
    out["monthly"] = _tag(df.assign(month=months).groupby("month").agg(
        fy2026_obligations=("transaction_amount", "sum"),
        actions=("transaction_id", "count")).reset_index(), market, mode)
    out["award_size"] = _tag(M.obligation_distribution(df), market, mode)
    out["by_geo"] = _tag(geography(df), market, mode)
    out["geo_companies"] = _tag(geo_top(df, "company_id", "company_name", 10), market, mode)
    out["office_companies"] = _tag(office_top(df, "company_id", "company_name", 10), market, mode)
    if ":" not in market:            # PSC mix per location / office for the base market only
        out["geo_psc"] = _tag(geo_top(df, "psc_code", "psc_description", 10), market, mode)
        out["office_psc"] = _tag(office_top(df, "psc_code", "psc_description", 10), market, mode)
    top = comp.head(top_profiles)["company_id"].tolist()
    prof_a, prof_p, prof_n, prof_g, prof_o = [], [], [], [], []
    for cid in top:
        for dim, label, bucket in (("awarding_sub_agency", None, prof_a),
                                   ("office_code", "office_name", prof_o),
                                   ("psc_code", "psc_description", prof_p),
                                   ("naics_code", "naics_description", prof_n)):
            p = M.portfolio(df, cid, dim, label).head(10)
            p.insert(0, "company_id", cid)
            bucket.append(p)
        g = geography(df[df["company_id"] == cid], basis_only="place_of_performance").head(15)
        g.insert(0, "company_id", cid)
        prof_g.append(g)
    for name, parts in (("company_agencies", prof_a), ("company_psc", prof_p),
                        ("company_naics", prof_n), ("company_geo", prof_g),
                        ("company_offices", prof_o)):
        out[name] = _tag(pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(),
                         market, mode)
    return out


def action_split(df: pd.DataFrame) -> dict:
    """New awards vs modifications. A base action (modification number 0) in the
    period means the award (contract or order) was made in the period; every other
    action changes an existing award (funding, options, de-obligation, close-out)."""
    if df.empty:
        return {"new_award_count": 0, "new_award_obligations": 0.0,
                "modification_count": 0, "modification_obligations": 0.0}
    mod = df["modification_number"].fillna("").astype(str).str.strip()
    base = mod.str.fullmatch(r"0*")
    return {"new_award_count": int(df.loc[base, "award_id"].nunique()),
            "new_award_obligations": round(float(df.loc[base, "transaction_amount"].sum()), 2),
            "modification_count": int((~base).sum()),
            "modification_obligations": round(float(df.loc[~base, "transaction_amount"].sum()), 2)}


ORDER_COLUMNS = ["transaction_id", "piid", "modification_number", "transaction_date",
                 "transaction_amount", "company_id", "company_name", "recipient_name",
                 "psc_code", "psc_description", "naics_code", "office_code",
                 "pop_state_code", "pop_country_code", "recipient_state", "recipient_country",
                 "description", "potential_duplicate"]


def orders(df: pd.DataFrame, market: str, max_rows: int = 120_000, per_company: int = 1_500,
           top_companies: int = 50, desc_chars: int = 140) -> pd.DataFrame:
    """Transaction-level order list (one row per contract action) for search and
    company drill-down. Small markets: every action. Large markets (more than
    `max_rows` actions): only the top companies' largest actions, so the hosted
    page stays light – the column `coverage` says which. Descriptions are the
    free text reported by the contracting office (no line items, quantities or
    unit prices exist in USAspending)."""
    if df.empty:
        return pd.DataFrame(columns=["market", "coverage"] + ORDER_COLUMNS)
    d = df.copy()
    desc = d["transaction_description"].fillna(d["award_description"]).fillna("")
    d["description"] = desc.astype(str).str.replace(r"\s+", " ", regex=True).str.slice(0, desc_chars)
    d["potential_duplicate"] = d["potential_duplicate_group"].notna()
    coverage = "all_actions"
    if len(d) > max_rows:
        tot = d.groupby("company_id")["transaction_amount"].sum().sort_values(ascending=False)
        keep = set(tot.head(top_companies).index)
        d = d[d["company_id"].isin(keep)]
        d = (d.assign(_abs=d["transaction_amount"].abs()).sort_values("_abs", ascending=False)
             .groupby("company_id").head(per_company).drop(columns="_abs"))
        coverage = f"top{top_companies}_companies_largest_{per_company}_actions"
    out = d[ORDER_COLUMNS].sort_values(["transaction_date", "transaction_amount"],
                                       ascending=[False, False]).reset_index(drop=True)
    out["transaction_amount"] = out["transaction_amount"].round(2)
    out.insert(0, "coverage", coverage)
    out.insert(0, "market", market)
    return out


GEO_BASES = {"place_of_performance": ("pop_country_code", "pop_state_code"),
             "recipient_location": ("recipient_country", "recipient_state")}


def _locations(df: pd.DataFrame, ccol: str, scol: str) -> tuple[pd.Series, pd.Series]:
    """(country, location): US rows by 2-letter state, all others by country code."""
    country = df[ccol].fillna("(not reported)").astype(str).str.upper()
    us = country.isin(["USA", "US", "UNITED STATES"])
    country = pd.Series(np.where(us, "USA", country), index=df.index)
    state = df[scol].fillna("(not reported)").astype(str).str.upper().str[:2]
    return country, pd.Series(np.where(us, state, country), index=df.index)


def geo_top(df: pd.DataFrame, dim: str, label: str | None, top: int) -> pd.DataFrame:
    """For every location (both bases): its `top` largest members of `dim`
    (companies or PSC codes) by obligations – what the location filter shows."""
    parts = []
    for basis, (ccol, scol) in GEO_BASES.items():
        if df.empty or ccol not in df or scol not in df:
            continue
        country, location = _locations(df, ccol, scol)
        keys = [dim] + ([label] if label else [])
        d = df[keys + ["transaction_amount"]].assign(country=country, location=location)
        g = (d.groupby(["country", "location"] + keys, dropna=False)
             .agg(fy2026_obligations=("transaction_amount", "sum"),
                  actions=("transaction_amount", "size")).reset_index())
        tot = g.groupby(["country", "location"])["fy2026_obligations"].transform("sum")
        g["share_of_location_pct"] = np.where(tot != 0, g["fy2026_obligations"] / tot * 100, np.nan)
        g = g.sort_values(["country", "location", "fy2026_obligations"], ascending=[True, True, False])
        g = g.groupby(["country", "location"]).head(top)
        g.insert(0, "basis", basis)
        parts.append(g)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def office_top(df: pd.DataFrame, dim: str, label: str | None, top: int) -> pd.DataFrame:
    """For every DLA contracting office: its `top` largest companies or PSC codes."""
    if df.empty or "office_code" not in df:
        return pd.DataFrame()
    keys = [dim] + ([label] if label else [])
    g = (df.groupby(["office_code"] + keys, dropna=False)
         .agg(fy2026_obligations=("transaction_amount", "sum"),
              actions=("transaction_amount", "size")).reset_index())
    tot = g.groupby("office_code", dropna=False)["fy2026_obligations"].transform("sum")
    g["share_of_office_pct"] = np.where(tot != 0, g["fy2026_obligations"] / tot * 100, np.nan)
    g = g.sort_values(["office_code", "fy2026_obligations"], ascending=[True, False])
    return g.groupby("office_code", dropna=False).head(top).reset_index(drop=True)


def geography(df: pd.DataFrame, basis_only: str | None = None) -> pd.DataFrame:
    """FY2026 obligations by location, for two bases kept apart (never added):
    place_of_performance – where the work is performed (for DLA supply buys usually the
    vendor's plant or shipping point, NOT the delivery destination);
    recipient_location – the vendor's address. US rows by state, others by country."""
    parts = []
    for basis, (ccol, scol) in GEO_BASES.items():
        if basis_only and basis != basis_only:
            continue
        if df.empty or ccol not in df or scol not in df:
            continue
        d = df[["transaction_amount", "award_id", "company_id"]].copy()
        d["country"], d["location"] = _locations(df, ccol, scol)
        total = d["transaction_amount"].sum()
        g = (d.groupby(["country", "location"], dropna=False)
             .agg(fy2026_obligations=("transaction_amount", "sum"),
                  actions=("transaction_amount", "size"),
                  award_count=("award_id", "nunique"),
                  supplier_count=("company_id", "nunique")).reset_index())
        g["share_of_selected_dataset_pct"] = g["fy2026_obligations"] / total * 100 if total else np.nan
        g.insert(0, "basis", basis)
        parts.append(g.sort_values("fy2026_obligations", ascending=False))
    cols = ["basis", "country", "location", "fy2026_obligations", "actions", "award_count",
            "supplier_count", "share_of_selected_dataset_pct"]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)


def combine(parts: list[dict]) -> dict[str, pd.DataFrame]:
    names = sorted({n for p in parts for n in p})
    return {n: pd.concat([p[n] for p in parts if n in p], ignore_index=True) for n in names}
