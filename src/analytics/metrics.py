"""FY2026 competitor analytics on the flat transaction frame
(v_market_transactions_fy2026). Pure pandas; same rules as sql/views.sql:

* Money = Σ transaction_amount (FY2026 only). Lifetime award values never summed.
* Award metrics aggregate transactions by (award_id, entity) first.
* Avg / median / largest award use positive-net awards only; zero-net and
  negative-net awards are counted separately.
* Shares are of the SELECTED dataset (after filters), not of any commercial market.
* Concentration (CR5/CR10/HHI) uses entities with positive FY2026 obligations.
"""
from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

Entity = Literal["company", "recipient"]
ENTITY_COLS = {
    "company": ("company_id", "company_name", "company_uei"),
    "recipient": ("recipient_id", "recipient_name", "recipient_uei"),
}


def _ids(entity: Entity) -> tuple[str, str, str]:
    return ENTITY_COLS[entity]


def award_level(df: pd.DataFrame, entity: Entity = "company") -> pd.DataFrame:
    """One row per (award, entity): FY2026 net obligation and its sign class."""
    eid = _ids(entity)[0]
    a = (df.groupby(["award_id", eid], dropna=False)
         .agg(fy2026_net_obligation=("transaction_amount", "sum"),
              fy2026_action_count=("transaction_id", "count"))
         .reset_index())
    a["fy2026_net_obligation"] = a["fy2026_net_obligation"].round(2)
    a["net_obligation_class"] = np.select(
        [a["fy2026_net_obligation"] > 0, a["fy2026_net_obligation"] < 0],
        ["positive", "negative"], "zero")
    return a


def entity_summary(df: pd.DataFrame, entity: Entity = "company") -> pd.DataFrame:
    """Ranking table: one row per entity, sorted by FY2026 obligations."""
    eid, ename, euei = _ids(entity)
    if df.empty:
        return pd.DataFrame()
    aw = award_level(df, entity)
    pos = aw[aw["net_obligation_class"] == "positive"]
    by_award = aw.groupby(eid).agg(
        award_count=("award_id", "nunique"),
        zero_net_award_count=("net_obligation_class", lambda s: int((s == "zero").sum())),
        negative_net_award_count=("net_obligation_class", lambda s: int((s == "negative").sum())))
    by_pos = pos.groupby(eid)["fy2026_net_obligation"].agg(
        positive_net_award_count="count", avg_positive_award_fy2026="mean",
        median_positive_award_fy2026="median", largest_award_fy2026="max")
    base = df.groupby(eid, dropna=False).agg(
        name=(ename, "first"), uei=(euei, "first"),
        fy2026_obligations=("transaction_amount", "sum"),
        fy2026_action_count=("transaction_id", "count"),
        agency_count=("agency_id", "nunique"), naics_count=("naics_code", "nunique"),
        psc_count=("psc_code", "nunique"))
    if entity == "company":
        base = base.join(df.groupby(eid).agg(parent_status=("parent_status", "first"),
                                             recipient_uei_count=("recipient_id", "nunique")))
    out = base.join(by_award).join(by_pos)
    out["positive_net_award_count"] = out["positive_net_award_count"].fillna(0).astype(int)
    total = df["transaction_amount"].sum()
    out["share_of_selected_dataset_pct"] = (out["fy2026_obligations"] / total * 100
                                            if total else np.nan)
    out["fy2026_obligations"] = out["fy2026_obligations"].round(2)
    out = out.sort_values("fy2026_obligations", ascending=False).reset_index()
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def concentration(values: pd.Series) -> dict[str, float | None]:
    """CR5, CR10 and HHI (0–10,000) over positive entity totals."""
    v = values[values > 0].sort_values(ascending=False)
    total = v.sum()
    if not total:
        return {"cr5_pct": None, "cr10_pct": None, "hhi": None, "entities": 0}
    shares = v / total * 100
    return {"cr5_pct": round(float(shares.head(5).sum()), 2),
            "cr10_pct": round(float(shares.head(10).sum()), 2),
            "hhi": round(float((shares ** 2).sum()), 1), "entities": int(len(v))}


def market_kpis(df: pd.DataFrame, entity: Entity = "company") -> dict:
    if df.empty:
        return {}
    eid = _ids(entity)[0]
    totals = df.groupby(eid)["transaction_amount"].sum()
    aw = award_level(df, entity)
    pos = aw.loc[aw["net_obligation_class"] == "positive", "fy2026_net_obligation"]
    return {
        "fy2026_obligations": round(float(df["transaction_amount"].sum()), 2),
        "award_count": int(df["award_id"].nunique()),
        "action_count": int(len(df)),
        "company_count": int(df["company_id"].nunique()),
        "recipient_count": int(df["recipient_id"].nunique()),
        "agency_count": int(df["agency_id"].nunique()),
        "median_positive_award_fy2026": float(pos.median()) if len(pos) else None,
        "data_through_date": str(df["transaction_date"].max()),
        **concentration(totals),
    }


def obligations_by(df: pd.DataFrame, dim: str, label: str | None = None,
                   entity: Entity = "company") -> pd.DataFrame:
    """FY2026 obligations per dimension member with supplier count and concentration."""
    if df.empty:
        return pd.DataFrame()
    eid, ename, _ = _ids(entity)
    keys = [dim] + ([label] if label and label != dim else [])
    g = df.groupby(keys, dropna=False)
    out = g.agg(fy2026_obligations=("transaction_amount", "sum"),
                supplier_count=(eid, "nunique"), award_count=("award_id", "nunique")).reset_index()
    total = df["transaction_amount"].sum()
    out["share_of_selected_dataset_pct"] = out["fy2026_obligations"] / total * 100 if total else np.nan
    per = df.groupby([dim, eid, ename], dropna=False)["transaction_amount"].sum().reset_index()
    top, conc = {}, {}
    for k, grp in per.groupby(dim, dropna=False):
        grp = grp.sort_values("transaction_amount", ascending=False)
        top[k] = grp[ename].iloc[0]
        c = concentration(grp["transaction_amount"])
        conc[k] = (c["cr5_pct"], c["hhi"])
    out["top_supplier"] = out[dim].map(top)
    out["top5_supplier_share_pct"] = out[dim].map(lambda k: conc.get(k, (None, None))[0])
    out["supplier_hhi"] = out[dim].map(lambda k: conc.get(k, (None, None))[1])
    return out.sort_values("fy2026_obligations", ascending=False).reset_index(drop=True)


def top_suppliers(df: pd.DataFrame, n: int = 10, entity: Entity = "company") -> pd.DataFrame:
    return entity_summary(df, entity).head(n)


def portfolio(df: pd.DataFrame, entity_id, dim: str, label: str | None = None,
              entity: Entity = "company") -> pd.DataFrame:
    """Share of one entity's FY2026 obligations by a dimension (agency, NAICS, PSC…)."""
    eid = _ids(entity)[0]
    sub = df[df[eid] == entity_id]
    if sub.empty:
        return pd.DataFrame()
    keys = [dim] + ([label] if label and label != dim else [])
    out = sub.groupby(keys, dropna=False)["transaction_amount"].sum().reset_index(
        name="fy2026_obligations")
    tot = out["fy2026_obligations"].sum()
    out["share_of_entity_pct"] = out["fy2026_obligations"] / tot * 100 if tot else np.nan
    return out.sort_values("fy2026_obligations", ascending=False).reset_index(drop=True)


def diversification(df: pd.DataFrame, entity_id, entity: Entity = "company") -> dict:
    """HHI of an entity's own obligations across agencies / NAICS / PSC.
    Index = 1 − HHI/10,000 (0 = single customer/category, →1 = spread)."""
    out = {}
    for dim in ("agency_id", "naics_code", "psc_code"):
        p = portfolio(df, entity_id, dim, entity=entity)
        v = p["fy2026_obligations"] if len(p) else pd.Series(dtype=float)
        c = concentration(v)
        out[dim] = {"members": int(len(p)), "hhi": c["hhi"],
                    "diversification_index": None if c["hhi"] is None
                    else round(1 - c["hhi"] / 10000, 3)}
    return out


def category_overlap(df: pd.DataFrame, entity_ids: list, dim: str = "psc_code",
                     entity: Entity = "company") -> pd.DataFrame:
    """Pairwise portfolio overlap = Σ_c min(share_a(c), share_b(c)) in %, over
    positive obligations by category (100 = identical mix, 0 = no shared category)."""
    eid = _ids(entity)[0]
    sub = df[df[eid].isin(entity_ids)]
    piv = sub.pivot_table(index=eid, columns=dim, values="transaction_amount",
                          aggfunc="sum", fill_value=0).clip(lower=0)
    shares = piv.div(piv.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    ids = [i for i in entity_ids if i in shares.index]
    mat = pd.DataFrame(index=ids, columns=ids, dtype=float)
    for a in ids:
        for b in ids:
            mat.loc[a, b] = round(float(np.minimum(shares.loc[a], shares.loc[b]).sum() * 100), 1)
    return mat


def award_list(df: pd.DataFrame, entity_id, entity: Entity = "company") -> pd.DataFrame:
    """Awards of one entity with FY2026 net obligation (and labelled lifetime context)."""
    eid = _ids(entity)[0]
    sub = df[df[eid] == entity_id]
    if sub.empty:
        return pd.DataFrame()
    g = sub.groupby("award_id").agg(
        piid=("piid", "first"), description=("award_description", "first"),
        awarding_sub_agency=("awarding_sub_agency", "first"),
        naics_code=("naics_code", "last"), psc_code=("psc_code", "last"),
        recipient_uei=("recipient_uei", "first"),
        fy2026_net_obligation=("transaction_amount", "sum"),
        fy2026_actions=("transaction_id", "count"),
        last_action_date=("transaction_date", "max"),
        lifetime_cumulative_obligation=("award_cumulative_obligation_lifetime", "first"),
        lifetime_ceiling=("award_ceiling_lifetime", "first"),
        potential_duplicate_records=("potential_duplicate_group", lambda s: int(s.notna().sum())))
    g["fy2026_net_obligation"] = g["fy2026_net_obligation"].round(2)
    return g.sort_values("fy2026_net_obligation", ascending=False).reset_index()


def obligation_distribution(df: pd.DataFrame, entity: Entity = "company") -> pd.DataFrame:
    """Award-level FY2026 net obligations bucketed (positive-net awards)."""
    aw = award_level(df, entity)
    pos = aw[aw["net_obligation_class"] == "positive"]["fy2026_net_obligation"]
    bins = [0, 1e5, 1e6, 1e7, 1e8, 1e9, np.inf]
    labels = ["< $100K", "$100K–1M", "$1M–10M", "$10M–100M", "$100M–1B", "≥ $1B"]
    cut = pd.cut(pos, bins=bins, labels=labels, right=False)
    out = pd.DataFrame({"bucket": cut, "amount": pos}).groupby("bucket", observed=False).agg(
        awards=("amount", "count"), fy2026_obligations=("amount", "sum")).reset_index()
    out["bucket"] = out["bucket"].astype(str)
    return out


def data_quality_summary(df: pd.DataFrame) -> dict:
    return {
        "actions": int(len(df)),
        "missing_recipient_uei": int(df["recipient_uei"].isna().sum()),
        "companies_without_reported_parent":
            int(df.loc[df["parent_status"] != "reported_parent", "company_id"].nunique()),
        "recipients_with_parent_conflict":
            int(df.loc[df["parent_uei_conflict"].astype(bool), "recipient_id"].nunique()),
        "missing_naics": int(df["naics_code"].isna().sum()),
        "missing_psc": int(df["psc_code"].isna().sum()),
        "potential_duplicate_records": int(df["potential_duplicate_group"].notna().sum()),
        "potential_duplicate_groups": int(df["potential_duplicate_group"].nunique()),
        "potential_duplicate_amount": round(float(
            df[df["potential_duplicate_group"].notna()]
            .groupby("potential_duplicate_group")["transaction_amount"]
            .agg(lambda s: s.iloc[0] * (len(s) - 1)).sum()), 2),
    }
