"""Vendor views on STAGED download data (before the fact load).

Both totals are shown side by side because the duplicate rule is not decided:
  * all_records        – every staged record (USAspending basis)
  * one_per_group      – one record per suspicious duplicate group
Name search is for FINDING vendors only; results stay per UEI / reported
parent UEI and nothing is merged by name.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd

ID, AWARD, MOD, DATE, AMT = ("contract_transaction_unique_key", "contract_award_unique_key",
                             "modification_number", "action_date", "federal_action_obligation")


def _key_amount(v) -> str | None:
    try:
        return str(Decimal(str(v)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError, TypeError):
        return None


def prepare(staged: pd.DataFrame) -> pd.DataFrame:
    df = staged.drop_duplicates(subset=[ID]).copy()
    df["_amount"] = pd.to_numeric(df[AMT], errors="coerce").fillna(0.0)
    df["_k"] = df[AMT].map(_key_amount)
    nz = df["_amount"] != 0
    rank = df[nz].groupby([AWARD, MOD, DATE, "_k"], dropna=False).cumcount()
    df["_extra_copy"] = False
    df.loc[rank[rank > 0].index, "_extra_copy"] = True
    df["company_key"] = df["recipient_parent_uei"].where(
        df["recipient_parent_uei"].notna(), df["recipient_uei"])
    df["company_name"] = df["recipient_parent_name"].where(
        df["recipient_parent_uei"].notna(), df["recipient_name"])
    return df


def top_companies(df: pd.DataFrame, n: int = 25) -> pd.DataFrame:
    g = df.groupby("company_key", dropna=False)
    out = pd.DataFrame({
        "company_name": g["company_name"].last(),
        "fy2026_all_records": g["_amount"].sum(),
        "fy2026_one_per_group": g.apply(lambda x: x.loc[~x["_extra_copy"], "_amount"].sum(),
                                        include_groups=False),
        "awards": g[AWARD].nunique(),
        "recipient_ueis": g["recipient_uei"].nunique(),
        "parent_reported": g["recipient_parent_uei"].apply(lambda s: s.notna().any()),
    }).reset_index()
    out["probable_duplicate_amount"] = out["fy2026_all_records"] - out["fy2026_one_per_group"]
    return out.sort_values("fy2026_one_per_group", ascending=False).head(n).reset_index(drop=True)


def search(df: pd.DataFrame, term: str) -> pd.DataFrame:
    t = term.upper()
    hit = (df["recipient_name"].fillna("").str.upper().str.contains(t, regex=False) |
           df["recipient_parent_name"].fillna("").str.upper().str.contains(t, regex=False))
    sub = df[hit]
    if sub.empty:
        return pd.DataFrame()
    g = sub.groupby(["recipient_uei", "recipient_name", "recipient_parent_name"], dropna=False)
    return pd.DataFrame({
        "fy2026_all_records": g["_amount"].sum(),
        "fy2026_one_per_group": g.apply(lambda x: x.loc[~x["_extra_copy"], "_amount"].sum(),
                                        include_groups=False),
        "transactions": g[ID].count(),
        "top_naics": g["naics_code"].agg(lambda s: ", ".join(s.value_counts().index[:3].astype(str))),
        "top_psc": g["product_or_service_code"].agg(lambda s: ", ".join(s.value_counts().index[:3].astype(str))),
        "agencies": g["awarding_sub_agency_name"].agg(lambda s: ", ".join(s.value_counts().index[:3].astype(str))),
    }).reset_index().sort_values("fy2026_one_per_group", ascending=False)
