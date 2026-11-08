"""Atomic snapshot load of transformed frames into dims/facts.

Works with a SQLAlchemy Connection inside `engine.begin()` (production:
pandas joins the open transaction, so the whole snapshot commits or rolls back)
or a sqlite3 connection (tests). Every load replaces the previous FY2026
snapshot; lineage tables (etl_log, raw_*, dq_issues) are append-only.
Reviewed entity-match decisions survive reloads (re-keyed by recipient_key).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.etl.runlog import execute, now
from src.etl.transform import TransformResult

# delete children before parents
SNAPSHOT_TABLES = ["fact_transaction_segments", "fact_transactions", "fact_awards",
                   "entity_match_candidates", "dim_recipients", "dim_companies", "dim_offices",
                   "dim_agencies", "dim_naics", "dim_psc", "market_segments"]
INSERT_ORDER = ["market_segments", "dim_companies", "dim_recipients", "dim_agencies",
                "dim_offices", "dim_naics", "dim_psc", "fact_awards", "fact_transactions",
                "fact_transaction_segments"]


def _reviewed_candidates(con: Any) -> pd.DataFrame:
    try:
        return pd.read_sql(
            "SELECT r.recipient_key, c.potential_parent_uei, c.review_status, c.review_note, "
            "c.reviewed_at FROM entity_match_candidates c JOIN dim_recipients r "
            "ON r.recipient_id = c.recipient_id WHERE c.review_status <> 'pending'", con)
    except Exception:
        return pd.DataFrame(columns=["recipient_key", "potential_parent_uei", "review_status",
                                     "review_note", "reviewed_at"])


def load_snapshot(con: Any, result: TransformResult, run_id: str) -> dict[str, int]:
    reviewed = _reviewed_candidates(con)
    for table in SNAPSHOT_TABLES:
        execute(con, f"DELETE FROM {table}")
    counts: dict[str, int] = {}
    for table in INSERT_ORDER:
        df = result.tables[table]
        df.to_sql(table, con, if_exists="append", index=False, chunksize=5000)
        counts[table] = len(df)

    cand = result.tables["_match_candidates"]
    if len(cand):
        rmap = result.tables["dim_recipients"].set_index("recipient_key")["recipient_id"]
        cand = cand.assign(recipient_id=cand["recipient_key"].map(rmap),
                           review_status="pending", review_note=None, reviewed_at=None,
                           created_at=now())
        if len(reviewed):
            prev = reviewed.set_index(["recipient_key", "potential_parent_uei"])
            for i, r in cand.iterrows():
                k = (r["recipient_key"], r["potential_parent_uei"])
                if k in prev.index:
                    p = prev.loc[k]
                    cand.loc[i, ["review_status", "review_note", "reviewed_at"]] = (
                        p["review_status"], p["review_note"], p["reviewed_at"])
        cand.drop(columns=["recipient_key"]).to_sql("entity_match_candidates", con,
                                                     if_exists="append", index=False)
    counts["entity_match_candidates"] = len(cand)

    dq = result.dq_issues.assign(run_id=run_id, created_at=now())
    dq.to_sql("dq_issues", con, if_exists="append", index=False, chunksize=5000)
    counts["dq_issues"] = len(dq)
    return counts
