"""Staging: raw download CSVs -> stg_prime_transactions (schema-on-read, all TEXT).

* Only the columns the pipeline uses are kept (transform mapping, duplicate
  profiling, snapshot fields) – the full ~280-column files stay in data/raw/.
* CSVs are streamed in chunks so large markets do not need to fit in memory.
* One table for all markets; `_market` identifies the market extract, and a
  restage of one market replaces only that market's rows.
Staging is transient and is never read by the dashboard.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from pandas.api.types import union_categoricals

from src.etl.runlog import execute, literal
from src.etl.transform import COLUMN_MAP
from src.quality.duplicates import PRIORITY_FIELDS, SNAPSHOT_FIELDS

STAGING_TABLE = "stg_prime_transactions"
LINEAGE_COLS = ["_market", "_basis", "_source_file", "_run_id"]
EXTRA_COLS = ["primary_place_of_performance_city_name", "primary_place_of_performance_zip_4",
              "primary_place_of_performance_county_name", "recipient_zip_4_code",
              "recipient_county_name", "prime_award_transaction_place_of_performance_county_fips_code",
              "prime_award_transaction_recipient_county_fips_code"]
KEEP_COLUMNS: list[str] = list(dict.fromkeys(
    [c for cands in COLUMN_MAP.values() for c in cands] + PRIORITY_FIELDS
    + sorted(SNAPSHOT_FIELDS) + EXTRA_COLS))
TABLE_COLUMNS = KEEP_COLUMNS + LINEAGE_COLS


def _frame(chunk: pd.DataFrame, market: str, basis: str, source: str, run_id: str) -> pd.DataFrame:
    out = chunk.reindex(columns=KEEP_COLUMNS)          # stable schema; absent columns -> NULL
    out["_market"], out["_basis"], out["_source_file"], out["_run_id"] = market, basis, source, run_id
    return out


def read_download_csvs(paths: Iterable[Path], basis: str, run_id: str,
                       market: str = "market") -> pd.DataFrame:
    """Read whole files into memory (small extracts / tests)."""
    frames = []
    for p in paths:
        df = pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""], low_memory=False,
                         encoding="utf-8")
        frames.append(_frame(df, market, basis, Path(p).name, run_id))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=TABLE_COLUMNS)


def _columns(con: Any) -> set[str] | None:
    try:
        return set(pd.read_sql(f"SELECT * FROM {STAGING_TABLE} LIMIT 0", con).columns)
    except Exception:
        return None


def _table_ok(con: Any) -> bool:
    cols = _columns(con)
    return cols is not None and set(TABLE_COLUMNS) <= cols


def migrate_legacy_staging(con: Any, legacy_market: str) -> bool:
    """Upgrade a staging table written before multi-market support (no `_market`
    column) in place: tag its rows with `legacy_market` and add missing columns.
    Returns True if a migration happened."""
    cols = _columns(con)
    if cols is None or "_market" in cols:
        return False
    for c in TABLE_COLUMNS:
        if c not in cols:
            execute(con, f'ALTER TABLE {STAGING_TABLE} ADD COLUMN "{c}" TEXT')
    execute(con, f"UPDATE {STAGING_TABLE} SET _market = {literal(legacy_market)}")
    return True


def reset_markets(con: Any, markets: list[str]) -> None:
    """Remove previous rows of these markets (or the whole table if its schema is old)."""
    cols = _columns(con)
    if cols is not None and "_market" in cols and not _table_ok(con):
        for c in TABLE_COLUMNS:                    # newer column list: extend in place
            if c not in cols:
                execute(con, f'ALTER TABLE {STAGING_TABLE} ADD COLUMN "{c}" TEXT')
    if not _table_ok(con):
        try:
            execute(con, f"DROP TABLE IF EXISTS {STAGING_TABLE}")
        except Exception:
            pass
        pd.DataFrame(columns=TABLE_COLUMNS).to_sql(STAGING_TABLE, con, index=False)
        return
    execute(con, f"DELETE FROM {STAGING_TABLE} WHERE _market IN "
                 f"({', '.join(literal(m) for m in markets)})")


def stage_csvs(con: Any, paths: Iterable[Path], basis: str, run_id: str, market: str,
               chunksize: int = 50_000) -> int:
    """Stream CSVs into the staging table; returns rows written."""
    n = 0
    for p in paths:
        header = pd.read_csv(p, nrows=0, encoding="utf-8").columns
        use = [c for c in KEEP_COLUMNS if c in header]
        for chunk in pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""],
                                 usecols=use, chunksize=chunksize, encoding="utf-8"):
            _frame(chunk, market, basis, Path(p).name, run_id).to_sql(
                STAGING_TABLE, con, if_exists="append", index=False, chunksize=10_000)
            n += len(chunk)
    return n


def write_staging(con: Any, df: pd.DataFrame) -> int:
    """Replace staging with an in-memory frame (tests / small runs)."""
    df.reindex(columns=TABLE_COLUMNS).to_sql(STAGING_TABLE, con, if_exists="replace", index=False)
    return len(df)


def as_category(s: pd.Series) -> pd.Categorical:
    """Categorical whose categories are always plain object dtype. A chunk where a
    column is entirely empty would otherwise get float categories, and combining
    chunks fails with 'dtype of categories must be the same' (older pandas)."""
    vals = s.astype(object).where(s.notna(), None)
    c = pd.Categorical(vals)
    if c.categories.dtype != object:
        c = c.set_categories(pd.Index(list(c.categories), dtype=object))
    return c


def union_cats(cats: list) -> pd.Categorical:
    """union_categoricals after normalising every input to object categories."""
    norm = []
    for c in cats:
        c = c.array if isinstance(c, pd.Series) else c
        if c.categories.dtype != object:
            c = c.set_categories(pd.Index(list(c.categories), dtype=object))
        norm.append(c)
    return union_categoricals(norm, ignore_order=True)


def read_staging(con: Any, markets: list[str] | None = None,
                 columns: list[str] | None = None, chunksize: int = 50_000,
                 categorical: bool = True, where_sql: str | None = None) -> pd.DataFrame:
    """Read staged rows in chunks. With `categorical` (default) every column is
    stored as a pandas Categorical, so repeated text (agency, recipient, NAICS,
    PSC, award IDs…) is held once in memory – needed for multi-million-row
    markets such as all DLA contracts. Values are unchanged."""
    cols = ", ".join(columns) if columns else "*"
    conds = ([f"_market IN ({', '.join(literal(m) for m in markets)})"] if markets else []) + \
        ([f"({where_sql})"] if where_sql else [])
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    sql = f"SELECT {cols} FROM {STAGING_TABLE}{where}"
    if not categorical:
        return pd.read_sql(sql, con)
    parts = [pd.DataFrame({c: as_category(ch[c]) for c in ch.columns})
             for ch in pd.read_sql(sql, con, chunksize=chunksize)]
    if not parts:
        return pd.read_sql(sql + " LIMIT 0", con)
    if len(parts) == 1:
        return parts[0].reset_index(drop=True)
    names = list(parts[0].columns)
    data = {}
    for c in names:
        data[c] = union_cats([p[c] for p in parts])
        for p in parts:                      # release chunk memory column by column
            del p[c]
    return pd.DataFrame(data)


def as_object(df: pd.DataFrame) -> pd.DataFrame:
    """Categorical columns back to plain object columns (values shared, cheap).
    Use before groupby on several columns: older pandas would otherwise build
    every category combination (observed=False)."""
    for c in df.columns:
        if isinstance(df[c].dtype, pd.CategoricalDtype):
            df[c] = df[c].astype(object).where(df[c].notna(), None)
    return df


VENDOR_COLUMNS = ["contract_transaction_unique_key", "contract_award_unique_key",
                  "modification_number", "action_date", "federal_action_obligation",
                  "recipient_uei", "recipient_name", "recipient_parent_uei",
                  "recipient_parent_name", "awarding_agency_name", "awarding_sub_agency_name",
                  "naics_code", "product_or_service_code"]


def staged_markets(con: Any) -> list[str]:
    try:
        return pd.read_sql(f"SELECT DISTINCT _market FROM {STAGING_TABLE}", con)["_market"].tolist()
    except Exception:
        return []


def read_for_profile(con: Any, market: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(keys of every staged row, full rows of awards that have any multi-record
    award/mod/date group). Group analysis only needs the second frame."""
    key_cols = ["contract_transaction_unique_key", "contract_award_unique_key",
                "modification_number", "action_date", "federal_action_obligation",
                "transaction_number", "_basis"]
    keys = as_object(read_staging(con, [market], key_cols))
    k = keys.drop_duplicates("contract_transaction_unique_key")
    multi = k.groupby(["contract_award_unique_key", "modification_number", "action_date"],
                      dropna=False)["contract_transaction_unique_key"].transform("count") > 1
    awards = sorted(k.loc[multi, "contract_award_unique_key"].dropna().astype(str).unique())
    del k, multi
    # an index keeps the per-award lookups fast on multi-million-row staging
    execute(con, f"CREATE INDEX IF NOT EXISTS ix_stg_award ON {STAGING_TABLE} "
                 f"(contract_award_unique_key)")
    parts = []
    for i in range(0, len(awards), 2000):
        batch = ", ".join(literal(a) for a in awards[i:i + 2000])
        parts.append(read_staging(con, [market], where_sql=f"contract_award_unique_key IN ({batch})"))
    detail = (pd.concat([p.astype(object) for p in parts], ignore_index=True) if parts
              else keys.iloc[0:0])
    return keys, detail
