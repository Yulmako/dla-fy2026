"""Memory-bounded transform for very large markets (e.g. all DLA contracts,
~3 million FY2026 transactions).

Staging is read and transformed in recipient buckets (by the recipient UEI), so
every recipient – and therefore every recipient-level rule (latest attributes,
parent-UEI conflicts, entity-match candidates) – is evaluated on all of its rows
exactly as in the single-pass transform. Partition results are then merged:

* recipients/companies: companies are rebuilt once over all recipients (a parent
  company's subsidiaries can fall in different buckets); IDs are re-numbered;
* agencies, offices, NAICS, PSC: union, latest description kept;
* awards: one row per award, the most recently updated record kept;
* transactions, segment links, exclusions, row-level data-quality issues: concatenated.

Potential-duplicate groups (same award, modification, date and amount) stay
inside one bucket because copies carry the same recipient UEI (checked in the
Phase 2a profile); a group split across buckets would only be under-flagged,
never wrongly merged. Same rules, same numbers – only the memory profile differs.
"""
from __future__ import annotations

import gc
from collections import Counter
from typing import Any, Callable

import numpy as np
import pandas as pd
from pandas.api.types import union_categoricals

from src.etl.staging import read_staging, union_cats
from src.etl.transform import MarketSpec, TransformResult, build_companies, transform


def bucket_sql(n: int, dialect: str = "sqlite") -> list[str]:
    """SQL predicates splitting staging rows into n buckets by recipient UEI.
    Rows without a UEI all land in bucket 0 (their recipient key is name-based)."""
    code = "unicode" if dialect == "sqlite" else "ascii"
    u = "NULLIF(TRIM(recipient_uei), '')"
    if dialect == "sqlite":
        chars = [f"SUBSTR({u}, {i}, 1)" for i in (1, 2, -2, -1)]
    else:
        chars = [f"SUBSTR({u}, {i}, 1)" for i in (1, 2)] + [f"SUBSTR(RIGHT({u}, {i}), 1, 1)" for i in (2, 1)]
    terms = " + ".join(f"{code}({c}) * {w}" for c, w in zip(chars, (1, 7, 31, 131)))
    expr = f"COALESCE(({terms}) % {n}, 0)"
    return [f"{expr} = {i}" for i in range(n)]


def _dialect(con: Any) -> str:
    d = getattr(con, "dialect", None)
    return getattr(d, "name", "sqlite") if d is not None else "sqlite"


def transform_staged(con: Any, read_markets: list[str], specs: list[MarketSpec], run_id: str,
                     duplicate_treatment: str, ignore_fields: list[str], buckets: int = 8,
                     log: Callable[[str], None] = print) -> TransformResult:
    """Read staging bucket by bucket, transform each, merge. buckets=1 is the
    plain single-pass transform."""
    if buckets <= 1:
        return transform(read_staging(con, read_markets), specs, run_id,
                         duplicate_treatment, ignore_fields)
    parts = []
    for i, where in enumerate(bucket_sql(buckets, _dialect(con))):
        staged = read_staging(con, read_markets, where_sql=where)
        if staged.empty:
            continue
        r = transform(staged, specs, run_id, duplicate_treatment, ignore_fields)
        log(f"  bucket {i + 1}/{buckets}: {len(staged):,} staged rows, "
            f"{r.stats['included_transactions']:,} included")
        del staged
        for name in BIG:
            r.tables[name] = _compact(r.tables[name])
        r.excluded = _compact(r.excluded)
        gc.collect()
        parts.append(r)
    if not parts:
        return transform(read_staging(con, read_markets), specs, run_id,
                         duplicate_treatment, ignore_fields)
    return merge_results(parts, run_id)


BIG = ("fact_transactions", "fact_transaction_segments", "fact_awards")


def _compact(df: pd.DataFrame) -> pd.DataFrame:
    """Object columns -> Categorical while a bucket result waits to be merged
    (repeated values stored once). Reversed by _expand; values never change."""
    for c in df.columns:
        if df[c].dtype == object:
            try:
                cat = pd.Categorical(df[c])
                if cat.categories.dtype != object:
                    cat = cat.set_categories(pd.Index(list(cat.categories), dtype=object))
                df[c] = cat
            except TypeError:                     # unhashable/mixed values: keep as is
                pass
    return df


def _expand(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if isinstance(df[c].dtype, pd.CategoricalDtype):
            lookup = np.array(list(df[c].cat.categories) + [None], dtype=object)
            df[c] = pd.Series(lookup[df[c].cat.codes.to_numpy()], index=df.index, dtype=object)
    return df


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate bucket frames; Categorical columns are unioned so they stay compact."""
    frames = [f for f in frames if f is not None and len(f.columns)]
    if not frames:
        return pd.DataFrame()
    is_cat = lambda f, c: isinstance(f[c].dtype, pd.CategoricalDtype)  # noqa: E731
    if not any(is_cat(f, c) for f in frames for c in f.columns):
        return pd.concat(frames, ignore_index=True)
    data = {}
    for c in frames[0].columns:
        if all(is_cat(f, c) for f in frames):
            data[c] = union_cats([f[c] for f in frames])
        else:
            data[c] = pd.concat([f[c].astype(object) if is_cat(f, c) else f[c] for f in frames],
                                ignore_index=True)
    return pd.DataFrame(data)


def merge_results(parts: list[TransformResult], run_id: str) -> TransformResult:
    T = [p.tables for p in parts]

    # ---- agencies: global ids by agency_key
    ag = _concat([t["dim_agencies"] for t in T]).drop_duplicates("agency_key", keep="last")
    ag = ag.drop(columns="agency_id").reset_index(drop=True)
    ag.insert(0, "agency_id", range(1, len(ag) + 1))
    gid_by_key = dict(zip(ag["agency_key"], ag["agency_id"]))
    amaps = [dict(zip(t["dim_agencies"]["agency_id"], t["dim_agencies"]["agency_key"].map(gid_by_key)))
             for t in T]

    # ---- recipients: keys are unique per bucket; order by latest action, rebuild companies
    for t in T:
        assert t["dim_recipients"]["recipient_key"].is_unique
    rec_local = [t["dim_recipients"] for t in T]
    rec = _concat(rec_local)
    if rec["recipient_key"].duplicated().any():          # defensive: never expected
        raise ValueError("recipient appears in more than one bucket")
    last = _concat([t["fact_transactions"][["recipient_id", "transaction_date", "source_updated_at"]]
                    .assign(_p=i) for i, t in enumerate(T)])
    # map (bucket, local recipient_id) -> recipient_key efficiently
    keymaps = [dict(zip(t["dim_recipients"]["recipient_id"], t["dim_recipients"]["recipient_key"]))
               for t in T]
    last["_key"] = pd.concat([t["fact_transactions"]["recipient_id"].map(keymaps[i])
                              for i, t in enumerate(T)], ignore_index=True)
    last["_d"] = pd.to_datetime(last["transaction_date"], errors="coerce")
    last["_ts"] = last["source_updated_at"].fillna(pd.Timestamp("1900-01-01"))
    order = (last.sort_values(["_d", "_ts"], kind="stable").groupby("_key").tail(1)["_key"]
             .reset_index(drop=True))
    del last
    rank = {k: i for i, k in enumerate(order)}
    rec["_rank"] = rec["recipient_key"].map(rank).fillna(-1)
    rec = rec.sort_values("_rank", kind="stable").drop(columns="_rank")
    comp, rec = build_companies(rec, run_id)
    rid = dict(zip(rec["recipient_key"], rec["recipient_id"]))
    rmaps = [{lid: rid[k] for lid, k in km.items()} for km in keymaps]

    def remap(t_frame: pd.DataFrame, i: int) -> pd.DataFrame:
        f = t_frame.copy()
        if "recipient_id" in f:
            f["recipient_id"] = f["recipient_id"].map(rmaps[i])
        for c in ("agency_id", "funding_agency_id"):
            if c in f:
                f[c] = f[c].map(amaps[i])
        return f

    # ---- awards: latest record per award
    awards = _concat([remap(t["fact_awards"], i) for i, t in enumerate(T)])
    awards["_ts"] = awards["source_updated_at"].fillna(pd.Timestamp("1900-01-01"))
    awards = _expand(awards.sort_values("_ts", kind="stable").drop_duplicates("award_id", keep="last")
                     .drop(columns="_ts").reset_index(drop=True))
    txn = _expand(_concat([remap(t["fact_transactions"], i) for i, t in enumerate(T)]))
    offices = (_concat([remap(t["dim_offices"], i) for i, t in enumerate(T)])
               .drop_duplicates("office_code", keep="last").reset_index(drop=True))
    naics = _concat([t["dim_naics"] for t in T]).drop_duplicates("naics_code", keep="last")
    psc = _concat([t["dim_psc"] for t in T]).drop_duplicates("psc_code", keep="last")
    fts = _expand(_concat([t["fact_transaction_segments"] for t in T]))
    cands = _concat([t["_match_candidates"] for t in T])
    dq = _concat([p.dq_issues for p in parts]).drop_duplicates().reset_index(drop=True)
    excl = _expand(_concat([p.excluded for p in parts]))

    # ---- stats
    S = [p.stats for p in parts]
    summed = ("staged_rows", "unique_transaction_ids", "included_transactions",
              "excluded_transactions", "potential_duplicate_groups",
              "potential_duplicate_records_included", "excluded_flagged_copies")
    stats: dict[str, Any] = {k: sum(s[k] for s in S) for k in summed}
    stats["included_obligations"] = round(sum(s["included_obligations"] for s in S), 2)
    reasons: Counter = Counter()
    for s in S:
        reasons.update(s["excluded_by_reason"])
    stats["excluded_by_reason"] = dict(reasons)
    markets: dict[str, dict] = {}
    for s in S:
        for k, v in s["markets"].items():
            m = markets.setdefault(k, {"segment_id": v["segment_id"]})
            for f, x in v.items():
                if f != "segment_id":
                    m[f] = round(m.get(f, 0) + x, 2) if isinstance(x, float) else m.get(f, 0) + x
    stats["markets"] = markets
    stats["duplicate_treatment"] = S[0]["duplicate_treatment"]
    dts = [s["data_through_date"] for s in S if s["data_through_date"] is not None]
    stats["data_through_date"] = max(dts) if dts else None
    stats.update(companies=int(len(comp)), recipients=int(len(rec)), awards=int(len(awards)),
                 columns_used=S[0]["columns_used"], partitions=len(parts))

    return TransformResult(
        tables={"dim_companies": comp,
                "dim_recipients": rec.drop(columns=["company_key", "company_uei", "parent_status"]),
                "dim_agencies": ag, "dim_offices": offices, "dim_naics": naics, "dim_psc": psc,
                "fact_awards": awards, "fact_transactions": txn,
                "market_segments": T[0]["market_segments"],
                "fact_transaction_segments": fts, "_match_candidates": cands},
        excluded=excl, dq_issues=dq, stats=stats)


def auto_buckets(con: Any, read_markets: list[str], rows_per_bucket: int = 300_000) -> int:
    """Number of recipient buckets so each holds roughly `rows_per_bucket` staged rows."""
    from src.etl.runlog import literal
    from src.etl.staging import STAGING_TABLE
    where = f" WHERE _market IN ({', '.join(literal(m) for m in read_markets)})" if read_markets else ""
    n = int(pd.read_sql(f"SELECT COUNT(*) AS n FROM {STAGING_TABLE}{where}", con)["n"].iloc[0])
    return max(1, -(-n // rows_per_bucket))
