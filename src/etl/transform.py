"""Staging (official download columns) -> normalized frames for every table.

Pure pandas; no database access. Rules implemented here are documented in
docs/OBLIGATION_METHODOLOGY.md:
  * FY2026 + contracts A–D + agency scope + market codes, else excluded (logged).
  * De-duplication only on the transaction unique ID.
  * Potential source duplicates (same award/mod/date/non-zero amount, different
    IDs) are flagged; treatment comes from config and is never 'undecided' here.
  * Company = reported parent UEI; else own UEI; else no-UEI key. Never names.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd

from src.config.market import Market
from src.fiscal import CONTRACT_AWARD_TYPES, FISCAL_YEAR, FY_END, FY_START

# canonical name -> candidate column names in the download file (first found wins)
COLUMN_MAP: dict[str, list[str]] = {
    "transaction_id": ["contract_transaction_unique_key"],
    "award_id": ["contract_award_unique_key"],
    "piid": ["award_id_piid"],
    "parent_award_piid": ["parent_award_id_piid"],
    "modification_number": ["modification_number"],
    "transaction_number": ["transaction_number"],
    "action_date": ["action_date"],
    "amount": ["federal_action_obligation"],
    "cumulative_obligation": ["total_dollars_obligated"],
    "current_award_value": ["current_total_value_of_award", "base_and_exercised_options_value"],
    "award_amount": ["potential_total_value_of_award", "base_and_all_options_value"],
    "start_date": ["period_of_performance_start_date"],
    "end_date": ["period_of_performance_current_end_date"],
    "toptier_code": ["awarding_agency_code"],
    "toptier_name": ["awarding_agency_name"],
    "sub_code": ["awarding_sub_agency_code"],
    "sub_name": ["awarding_sub_agency_name"],
    "office_code": ["awarding_office_code"],
    "office_name": ["awarding_office_name"],
    "f_toptier_code": ["funding_agency_code"],
    "f_toptier_name": ["funding_agency_name"],
    "f_sub_code": ["funding_sub_agency_code"],
    "f_sub_name": ["funding_sub_agency_name"],
    "recipient_uei": ["recipient_uei"],
    "recipient_duns": ["recipient_duns"],
    "recipient_name": ["recipient_name"],
    "recipient_name_raw": ["recipient_name_raw", "recipient_name"],
    "parent_uei": ["recipient_parent_uei"],
    "parent_name": ["recipient_parent_name"],
    "country": ["recipient_country_code"],
    "state": ["recipient_state_code"],
    "city": ["recipient_city_name"],
    "award_type_code": ["award_type_code"],
    "award_type": ["award_type"],
    "action_type_code": ["action_type_code"],
    "action_type": ["action_type"],
    "transaction_description": ["transaction_description"],
    "award_description": ["prime_award_base_transaction_description", "award_description"],
    "naics_code": ["naics_code"],
    "naics_description": ["naics_description"],
    "psc_code": ["product_or_service_code"],
    "psc_description": ["product_or_service_code_description"],
    "pop_state_code": ["primary_place_of_performance_state_code"],
    "pop_country_code": ["primary_place_of_performance_country_code"],
    "last_modified": ["last_modified_date"],
    "permalink": ["usaspending_permalink"],
    "correction_delete_ind": ["correction_delete_ind"],
}
REQUIRED = ["transaction_id", "award_id", "action_date", "amount", "award_type_code"]
LINEAGE = {"_basis", "_source_file", "_run_id"}


class TransformError(ValueError):
    pass


@dataclass
class TransformResult:
    tables: dict[str, pd.DataFrame]
    excluded: pd.DataFrame
    dq_issues: pd.DataFrame
    stats: dict[str, Any] = field(default_factory=dict)


def _clean(s: pd.Series) -> pd.Series:
    if isinstance(s.dtype, pd.CategoricalDtype):
        # clean each distinct value once, then expand by code (fast for millions of rows)
        cats = [None if str(v).strip() == "" else str(v).strip() for v in s.cat.categories]
        lookup = np.array(cats + [None], dtype=object)       # code -1 (missing) -> None
        return pd.Series(lookup[s.cat.codes.to_numpy()], index=s.index, dtype=object)
    s = s.astype("object").where(s.notna(), None)
    return s.map(lambda v: None if v is None or str(v).strip() == "" else str(v).strip())


def _has(v: Any) -> bool:
    """True for a real value; False for None/NaN/NA/blank."""
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    return str(v).strip() != ""


def normalize_name(name: Any) -> str:
    if name is None or (isinstance(name, float) and np.isnan(name)):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9& ]", " ", str(name).upper())).strip()


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _amount_key(v: Any) -> str | None:
    try:
        return str(Decimal(str(v)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _gid(*parts: Any) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def resolve_columns(staged: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str | None]]:
    used: dict[str, str | None] = {}
    out = pd.DataFrame(index=staged.index)
    for canon, cands in COLUMN_MAP.items():
        col = next((c for c in cands if c in staged.columns), None)
        used[canon] = col
        out[canon] = _clean(staged[col]) if col else None
    missing = [c for c in REQUIRED if used[c] is None]
    if missing:
        raise TransformError(f"download file lacks required columns for {missing}; "
                             f"first columns: {list(staged.columns)[:30]}")
    return out, used


@dataclass
class MarketSpec:
    key: str
    market: Market
    segment_id: int
    definition_hash: str
    definition_yaml: str


def build_companies(rec: pd.DataFrame, run_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Company dimension from recipients (rows in chronological order of each
    recipient's latest action). Company = reported parent UEI; else own UEI;
    else the no-UEI recipient key. Never merged by name. Returns (companies,
    recipients with company_id and a fresh 1..n recipient_id)."""
    rec = rec.drop(columns=[c for c in ("company_key", "company_uei", "parent_status",
                                        "company_id", "recipient_id", "first_seen_run_id",
                                        "last_seen_run_id") if c in rec.columns]).reset_index(drop=True)
    has_p = rec["parent_recipient_uei"].map(_has).astype(bool)
    has_u = rec["recipient_uei"].map(_has).astype(bool)
    rec["company_key"] = np.select(
        [has_p, has_u],
        ["PARENT:" + rec["parent_recipient_uei"].astype(str), "UEI:" + rec["recipient_uei"].astype(str)],
        "NOUEI:" + rec["recipient_key"].astype(str)).astype(object)
    rec["company_uei"] = np.where(has_p, rec["parent_recipient_uei"],
                                  np.where(has_u, rec["recipient_uei"], None)).astype(object)
    rec["parent_status"] = np.select([has_p, has_u], ["reported_parent", "no_parent_reported"],
                                     "no_uei").astype(object)
    uei_name = dict(zip(rec["recipient_uei"], rec["recipient_name"]))

    def cname(g):
        reported = g["parent_recipient_name"].dropna()
        if len(reported):
            return reported.iloc[-1]
        cu = g["company_uei"].iloc[0]
        if cu in uei_name and g["parent_status"].iloc[0] == "reported_parent":
            return uei_name[cu]
        return g["recipient_name"].iloc[0]

    comp = (rec.groupby("company_key", sort=True)
            .apply(lambda g: pd.Series({"company_uei": g["company_uei"].iloc[0],
                                        "company_name": cname(g),
                                        "parent_status": g["parent_status"].iloc[0]}),
                   include_groups=False)
            .reset_index())
    if comp.empty:
        comp = pd.DataFrame(columns=["company_key", "company_uei", "company_name", "parent_status"])
    comp.insert(0, "company_id", range(1, len(comp) + 1))
    comp["last_seen_run_id"] = run_id
    rec = rec.merge(comp[["company_key", "company_id"]], on="company_key", how="left")
    rec.insert(0, "recipient_id", range(1, len(rec) + 1))
    rec["first_seen_run_id"] = run_id
    rec["last_seen_run_id"] = run_id
    return comp, rec


def transform(staged: pd.DataFrame, markets: list[MarketSpec], run_id: str,
              duplicate_treatment: str, ignore_fields: list[str]) -> TransformResult:
    """Normalize staged rows for one or more markets (segments). A transaction is
    loaded once and linked to every market whose scope and codes it matches."""
    if duplicate_treatment == "undecided":
        raise TransformError("transaction_counting.potential_duplicate_treatment is 'undecided' "
                             "in config/pipeline.yaml – choose a treatment after reviewing the "
                             "Phase 2a profile before loading facts.")
    raw = staged.drop_duplicates(subset=[COLUMN_MAP["transaction_id"][0]]).reset_index(drop=True)
    t, used = resolve_columns(raw)
    t["action_date"] = pd.to_datetime(t["action_date"], errors="coerce").dt.date
    t["amount_num"] = _num(t["amount"])
    # grouping key in integer cents (exact for 2-decimal amounts); the text form
    # used in group IDs is built only for flagged rows
    t["amount_cents"] = (t["amount_num"] * 100).round().astype("Int64")
    for c in ("cumulative_obligation", "current_award_value", "award_amount"):
        t[c] = _num(t[c])
    for c in ("start_date", "end_date"):
        t[c] = pd.to_datetime(t[c], errors="coerce").dt.date
    t["last_modified_ts"] = pd.to_datetime(t["last_modified"], errors="coerce")
    # ordering for "latest" attributes: action date, then last-modified time
    t["_sort_date"] = pd.to_datetime(t["action_date"], errors="coerce").fillna(pd.Timestamp(FY_START))
    t["_sort_ts"] = t["last_modified_ts"].fillna(pd.Timestamp("1900-01-01"))

    # ------------------------------------------------------------ exclusions
    reason = pd.Series(None, index=t.index, dtype="object")

    def mark(mask, why):
        reason.loc[mask & reason.isna()] = why

    mark(t["transaction_id"].isna() | t["award_id"].isna(), "missing_identifier")
    mark(~t["action_date"].map(lambda d: (not pd.isna(d)) and FY_START <= d <= FY_END),
         "outside_fy2026")
    mark(t["amount_num"].isna(), "amount_unparseable")
    mark(~t["award_type_code"].isin(CONTRACT_AWARD_TYPES), "not_contract_type_a_to_d")
    mark(t["correction_delete_ind"].fillna("").str.upper().eq("D"), "delete_record")
    scope_any = pd.Series(False, index=t.index)
    member_any = pd.Series(False, index=t.index)
    membership: dict[str, pd.Series] = {}
    basis_masks: dict[str, tuple[pd.Series, pd.Series]] = {}
    for spec in markets:
        m = spec.market
        inc_tiers = set(m.included_tiers)
        pairs = list(zip(t["toptier_name"], t["sub_name"]))
        scope_map = {p: m.in_scope(*p) for p in set(pairs)}
        in_scope = pd.Series([scope_map[p] for p in pairs], index=t.index, dtype=bool)
        if m.all_codes:
            membership[spec.key] = pd.Series([("all", "core") if v else None for v in in_scope],
                                              index=t.index, dtype="object")
            basis_masks[spec.key] = {"all": in_scope}
        else:
            ntier = t["naics_code"].map({c: m.naics_tier(c) for c in t["naics_code"].dropna().unique()})
            ptier = t["psc_code"].map({c: m.psc_tier(c) for c in t["psc_code"].dropna().unique()})
            n_ok = ntier.isin(inc_tiers) & in_scope
            p_ok = ptier.isin(inc_tiers) & in_scope
            by = np.select([n_ok & p_ok, n_ok, p_ok], ["both", "naics", "psc"], None)
            core = (n_ok & ntier.eq("core")) | (p_ok & ptier.eq("core"))
            tier = np.where(core, "core", "extended")
            membership[spec.key] = pd.Series([(b_, t_) if b_ is not None else None
                                              for b_, t_ in zip(by, tier)], index=t.index, dtype="object")
            basis_masks[spec.key] = {"naics": n_ok, "psc": p_ok}
        scope_any |= in_scope
        member_any |= membership[spec.key].notna()
    mark(~scope_any, "outside_agency_scope")
    mark(~member_any, "not_in_market_codes")

    # ------------------------------------------------ potential duplicates
    ok = reason.isna()
    nz = ok & t["amount_num"].ne(0)
    grp_cols = ["award_id", "modification_number", "action_date", "amount_cents"]
    grp_size = t.loc[nz, grp_cols + ["transaction_id"]].groupby(
        grp_cols, dropna=False)["transaction_id"].transform("nunique")
    flagged_idx = grp_size[grp_size > 1].index
    t["potential_duplicate_group"] = None
    fk = t.loc[flagged_idx, ["award_id", "modification_number", "action_date", "amount"]]
    t.loc[flagged_idx, "potential_duplicate_group"] = [
        _gid(a, m_, d, _amount_key(v)) for a, m_, d, v in fk.itertuples(index=False)]
    dup_groups = t.loc[flagged_idx].groupby("potential_duplicate_group")
    excluded_copies = 0
    if duplicate_treatment == "exclude_flagged_copies" and len(flagged_idx):
        ignore = set(ignore_fields) | LINEAGE
        compare = [c for c in raw.columns if c not in ignore]
        # convert the flagged rows once (per-group conversion of Categorical columns
        # re-converted every category each time and took hours on large markets)
        flagged_raw = raw.loc[flagged_idx, compare]
        flagged_raw = pd.DataFrame({c: flagged_raw[c].astype(object).where(flagged_raw[c].notna(), "<NULL>")
                                    .map(str) for c in compare}, index=flagged_raw.index)
        for _, members in dup_groups:
            rawm = flagged_raw.loc[members.index]
            if (rawm.nunique() > 1).any():
                continue                          # not field-identical -> keep all
            tn = pd.to_numeric(members["transaction_number"], errors="coerce").fillna(np.inf)
            keep = tn.idxmin()
            drop = [i for i in members.index if i != keep]
            reason.loc[drop] = "excluded_flagged_copy"
            excluded_copies += len(drop)

    t["exclusion_reason"] = reason
    basis_totals = {sp.key: {f"{basis}_basis_obligations": round(float(t.loc[ok & mask, "amount_num"].sum()), 2)
                             for basis, mask in basis_masks[sp.key].items()} for sp in markets}
    excl = t[reason.notna()][["transaction_id", "award_id", "action_date", "amount_num",
                              "exclusion_reason"]].copy()
    inc = t[reason.isna()].drop(columns=["amount_cents", "exclusion_reason"])
    del t, basis_masks, scope_any, member_any            # free memory before dimensions are built

    # ------------------------------------------------------------ recipients
    def rkey(r):
        if _has(r["recipient_uei"]):
            return f"UEI:{r['recipient_uei']}"
        c = r["country"] if _has(r["country"]) else ""
        st = r["state"] if _has(r["state"]) else ""
        return f"NOUEI:{normalize_name(r['recipient_name'])}|{c}|{st}"

    has_uei = inc["recipient_uei"].map(_has).astype(bool)
    inc["recipient_key"] = None
    inc.loc[has_uei, "recipient_key"] = "UEI:" + inc.loc[has_uei, "recipient_uei"].astype(str)
    if (~has_uei).any():
        inc.loc[~has_uei, "recipient_key"] = inc.loc[~has_uei].apply(rkey, axis=1)
    inc = inc.sort_values(["_sort_date", "_sort_ts"], kind="stable")
    latest = inc.groupby("recipient_key").tail(1).set_index("recipient_key")
    parents = inc.dropna(subset=["parent_uei"]).groupby("recipient_key")["parent_uei"].nunique()
    rec = pd.DataFrame({
        "recipient_key": latest.index,
        "recipient_uei": latest["recipient_uei"].values,
        "recipient_duns": latest["recipient_duns"].values,
        "recipient_name": latest["recipient_name"].fillna(latest["recipient_name_raw"]).fillna("(blank)").values,
        "parent_recipient_name": latest["parent_name"].values,
        "parent_recipient_uei": latest["parent_uei"].values,
        "country": latest["country"].values,
        "state": latest["state"].map(lambda v: str(v)[:2] if _has(v) else None).values,
        "city": latest["city"].values,
    })
    rec["recipient_name_normalized"] = rec["recipient_name"].map(normalize_name)
    rec["parent_uei_conflict"] = rec["recipient_key"].map(parents).fillna(0).gt(1)

    # ------------------------------------------------------------ companies
    comp, rec = build_companies(rec, run_id)

    # ------------------------------------------------------------ agencies
    aw = inc[["toptier_code", "toptier_name", "sub_code", "sub_name"]]
    fu = inc[["f_toptier_code", "f_toptier_name", "f_sub_code", "f_sub_name"]].set_axis(aw.columns, axis=1)
    ag = pd.concat([aw, fu]).dropna(subset=["toptier_code"])
    ag["agency_key"] = ag["toptier_code"] + "|" + ag["sub_code"].fillna("-")
    ag = ag.drop_duplicates("agency_key", keep="last").reset_index(drop=True)
    ag.insert(0, "agency_id", range(1, len(ag) + 1))
    agency = pd.DataFrame({"agency_id": ag["agency_id"], "agency_key": ag["agency_key"],
                           "toptier_code": ag["toptier_code"],
                           "parent_agency_name": ag["toptier_name"].fillna(ag["toptier_code"]),
                           "agency_code": ag["sub_code"], "agency_name": ag["sub_name"]})
    akey = dict(zip(agency["agency_key"], agency["agency_id"]))
    inc["agency_id"] = (inc["toptier_code"] + "|" + inc["sub_code"].fillna("-")).map(akey)
    inc["funding_agency_id"] = (inc["f_toptier_code"] + "|" + inc["f_sub_code"].fillna("-")).map(akey)

    offices = (inc.dropna(subset=["office_code"]).groupby("office_code").tail(1)
               [["office_code", "office_name", "agency_id"]].reset_index(drop=True))

    # ------------------------------------------------------------ codes
    dq_rows: list[dict] = []
    valid_naics = inc["naics_code"].fillna("").str.fullmatch(r"\d{6}")
    valid_psc = inc["psc_code"].fillna("").str.fullmatch(r"[A-Z0-9]{4}")
    for col, valid, name in (("naics_code", valid_naics, "missing_or_invalid_naics"),
                             ("psc_code", valid_psc, "missing_or_invalid_psc")):
        for tid in inc.loc[~valid, "transaction_id"]:
            dq_rows.append({"entity_type": "transaction", "entity_key": tid,
                            "check_name": name, "severity": "warning", "detail": None})
        inc.loc[~valid, col] = None
    naics = (inc.dropna(subset=["naics_code"]).groupby("naics_code").tail(1)
             [["naics_code", "naics_description"]].reset_index(drop=True))
    naics["sector"] = naics["naics_code"].str[:2]
    psc = (inc.dropna(subset=["psc_code"]).groupby("psc_code").tail(1)
           [["psc_code", "psc_description"]].reset_index(drop=True))
    psc["category"] = psc["psc_code"].map(
        lambda c: "product" if c[0].isdigit() else ("rnd" if c[0] == "A" else "service"))
    psc["psc_group"] = psc["psc_code"].str[:2]

    # ------------------------------------------------------------ awards
    rmap = dict(zip(rec["recipient_key"], rec["recipient_id"]))
    inc["recipient_id"] = inc["recipient_key"].map(rmap)
    al = inc.groupby("award_id").tail(1)
    awards = pd.DataFrame({
        "award_id": al["award_id"], "piid": al["piid"], "parent_award_piid": al["parent_award_piid"],
        "recipient_id": al["recipient_id"], "agency_id": al["agency_id"],
        "funding_agency_id": al["funding_agency_id"], "naics_code": al["naics_code"],
        "psc_code": al["psc_code"], "award_type_code": al["award_type_code"],
        "award_type": al["award_type"], "award_description": al["award_description"],
        "award_amount": al["award_amount"], "current_award_value": al["current_award_value"],
        "cumulative_obligation": al["cumulative_obligation"], "start_date": al["start_date"],
        "end_date": al["end_date"], "source_updated_at": al["last_modified_ts"],
        "usaspending_permalink": al["permalink"], "first_seen_run_id": run_id,
        "last_seen_run_id": run_id}).reset_index(drop=True)

    # ------------------------------------------------------------ transactions
    txn = pd.DataFrame({
        "transaction_id": inc["transaction_id"], "award_id": inc["award_id"],
        "recipient_id": inc["recipient_id"], "agency_id": inc["agency_id"],
        "funding_agency_id": inc["funding_agency_id"], "office_code": inc["office_code"],
        "naics_code": inc["naics_code"], "psc_code": inc["psc_code"],
        "modification_number": inc["modification_number"],
        "transaction_number": inc["transaction_number"],
        "transaction_date": inc["action_date"], "transaction_amount": inc["amount_num"].round(2),
        "transaction_type": inc["action_type"].fillna(inc["action_type_code"]),
        "transaction_description": inc["transaction_description"],
        "recipient_name_raw": inc["recipient_name_raw"],
        "parent_recipient_uei_raw": inc["parent_uei"], "parent_recipient_name_raw": inc["parent_name"],
        "pop_state_code": inc["pop_state_code"].map(lambda v: str(v)[:2] if _has(v) else None),
        "pop_country_code": inc["pop_country_code"].map(lambda v: str(v)[:3] if _has(v) else None),
        "potential_duplicate_group": inc["potential_duplicate_group"],
        "fiscal_year": FISCAL_YEAR, "source_updated_at": inc["last_modified_ts"],
        "run_id": run_id}).reset_index(drop=True)

    # ------------------------------------------------------------ segments
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    segment = pd.DataFrame([{"segment_id": sp.segment_id, "market_key": sp.key,
                             "name": sp.market.name, "status": sp.market.status,
                             "definition_hash": sp.definition_hash,
                             "definition_yaml": sp.definition_yaml, "created_at": now}
                            for sp in markets])
    text = (inc["transaction_description"].fillna("") + " " +
            inc["award_description"].fillna("")).str.lower()
    fts_parts = []
    for sp in markets:
        mem = membership[sp.key].loc[inc.index]
        rows = inc.loc[mem.notna()]
        if rows.empty:
            continue
        terms = [x.lower() for x in sp.market.keywords.terms]
        fts_parts.append(pd.DataFrame({
            "transaction_id": rows["transaction_id"].values, "segment_id": sp.segment_id,
            "matched_by": [v[0] for v in mem.loc[rows.index]],
            "match_tier": [v[1] for v in mem.loc[rows.index]],
            "keyword_tags": text.loc[rows.index].map(
                lambda s: ";".join(x for x in terms if x in s) or None).values}))
    fts = (pd.concat(fts_parts, ignore_index=True) if fts_parts else
           pd.DataFrame(columns=["transaction_id", "segment_id", "matched_by", "match_tier",
                                 "keyword_tags"]))

    # ------------------------------------------------------------ data quality
    for _, r in rec.iterrows():
        if not _has(r["recipient_uei"]):
            dq_rows.append({"entity_type": "recipient", "entity_key": r["recipient_key"],
                            "check_name": "missing_uei", "severity": "warning", "detail": None})
        if r["parent_uei_conflict"]:
            dq_rows.append({"entity_type": "recipient", "entity_key": r["recipient_key"],
                            "check_name": "parent_uei_conflict", "severity": "warning",
                            "detail": "more than one parent UEI reported in FY2026; latest used"})
    for _, c in comp[comp["parent_status"] != "reported_parent"].iterrows():
        dq_rows.append({"entity_type": "company", "entity_key": c["company_key"],
                        "check_name": "no_reported_parent_uei", "severity": "info", "detail": None})
    for gid, members in dup_groups:
        dq_rows.append({"entity_type": "transaction_group", "entity_key": gid,
                        "check_name": "potential_source_duplicate", "severity": "warning",
                        "detail": f"{len(members)} records, amount {members['amount_num'].iloc[0]:.2f}, "
                                  f"treatment={duplicate_treatment}"})
    for _, e in excl.iterrows():
        dq_rows.append({"entity_type": "transaction", "entity_key": e["transaction_id"],
                        "check_name": f"excluded_{e['exclusion_reason']}",
                        "severity": "info" if e["exclusion_reason"] in
                        ("not_in_market_codes", "excluded_flagged_copy") else "error",
                        "detail": None})
    dq = pd.DataFrame(dq_rows, columns=["entity_type", "entity_key", "check_name", "severity", "detail"])

    # parent conflicts -> review candidates (never applied automatically)
    cand_rows = []
    for rk in rec.loc[rec["parent_uei_conflict"], "recipient_key"]:
        rows = inc[(inc["recipient_key"] == rk) & inc["parent_uei"].notna()]
        share = rows.groupby("parent_uei").size() / len(rows)
        names = rows.groupby("parent_uei")["parent_name"].last()
        for puei, sh in share.items():
            cand_rows.append({"recipient_key": rk, "original_name": rows["recipient_name"].iloc[-1],
                              "potential_parent_name": names.get(puei),
                              "potential_parent_uei": puei,
                              "matching_identifier": "reported_parent_uei",
                              "match_confidence": round(float(sh), 4)})
    candidates = pd.DataFrame(cand_rows, columns=["recipient_key", "original_name",
                                                  "potential_parent_name", "potential_parent_uei",
                                                  "matching_identifier", "match_confidence"])

    flagged = inc[inc["potential_duplicate_group"].notna()]
    stats = {
        "staged_rows": int(len(staged)), "unique_transaction_ids": int(len(raw)),
        "included_transactions": int(len(inc)), "excluded_transactions": int(len(excl)),
        "excluded_by_reason": excl["exclusion_reason"].value_counts().to_dict(),
        "included_obligations": round(float(inc["amount_num"].sum()), 2),
        "markets": {sp.key: {
            "segment_id": sp.segment_id,
            **basis_totals[sp.key],
            "transactions": int(membership[sp.key].loc[inc.index].notna().sum()),
            "obligations": round(float(inc.loc[membership[sp.key].loc[inc.index].notna(),
                                               "amount_num"].sum()), 2)} for sp in markets},
        "potential_duplicate_groups": int(dup_groups.ngroups),
        "potential_duplicate_records_included": int(len(flagged)),
        "excluded_flagged_copies": excluded_copies,
        "duplicate_treatment": duplicate_treatment,
        "data_through_date": max(inc["action_date"]) if len(inc) else None,
        "companies": int(len(comp)), "recipients": int(len(rec)), "awards": int(len(awards)),
        "columns_used": used,
    }
    return TransformResult(
        tables={"dim_companies": comp, "dim_recipients": rec.drop(columns=["company_key", "company_uei", "parent_status"]),
                "dim_agencies": agency, "dim_offices": offices, "dim_naics": naics, "dim_psc": psc,
                "fact_awards": awards, "fact_transactions": txn, "market_segments": segment,
                "fact_transaction_segments": fts, "_match_candidates": candidates},
        excluded=excl, dq_issues=dq, stats=stats)
