"""Compact read-only database for the public web app (Streamlit Community Cloud).

Contents (one SQLite file, no personal data beyond what USAspending publishes):
  * every table of the dashboard snapshot (aggregates, rankings, breakdowns)
  * orders_all – every FY2026 DLA contract action with the few columns the app
    shows, plus a full-text index (FTS5) on description / company / contract / PSC,
    so the app can search all ~3 million actions without loading them into memory.
The working database (staging, facts) never leaves the user's computer.
"""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pandas as pd

from src.analytics.psc_groups import group_of

ORDER_COLS = ["transaction_id", "transaction_date", "transaction_amount", "company_id", "company_name",
              "recipient_name", "piid", "modification_number", "psc_code", "psc_description",
              "naics_code", "office_code", "pop_state_code", "pop_country_code",
              "recipient_state", "recipient_country", "description", "potential_duplicate"]


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    return con


def slim_orders(df: pd.DataFrame, desc_chars: int = 160) -> pd.DataFrame:
    d = pd.DataFrame(index=df.index)
    for c in ORDER_COLS:
        if c == "description":
            desc = df["transaction_description"].fillna(df.get("award_description")).fillna("")
            d[c] = desc.astype(str).str.replace(r"\s+", " ", regex=True).str.slice(0, desc_chars)
        elif c == "potential_duplicate":
            d[c] = df["potential_duplicate_group"].notna().astype(int)
        else:
            d[c] = df[c] if c in df else None
    d["transaction_date"] = d["transaction_date"].astype(str).str.slice(0, 10)
    d["transaction_amount"] = pd.to_numeric(d["transaction_amount"], errors="coerce").round(2)
    codes = d["psc_code"].astype(object)
    d["psc_group"] = [group_of(c) or "none" for c in codes]
    d["excluded_as_copy"] = 0
    return d


def write_orders(con: sqlite3.Connection, df: pd.DataFrame, chunk: int = 200_000,
                 desc_chars: int = 160) -> int:
    """Append all actions of the base market (all-records basis)."""
    n = 0
    for i in range(0, len(df), chunk):
        part = slim_orders(df.iloc[i:i + chunk], desc_chars)
        part.to_sql("orders_all", con, if_exists="append", index=False)
        n += len(part)
    return n


def finalize_orders(con: sqlite3.Connection, copy_ids: set) -> None:
    """Mark copies removed under the default counting rule, then index for search."""
    con.execute("CREATE TEMP TABLE _copies (transaction_id TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO _copies VALUES (?)", [(i,) for i in copy_ids])
    con.execute("CREATE INDEX ix_orders_tid ON orders_all(transaction_id)")
    con.execute("UPDATE orders_all SET excluded_as_copy = 1 WHERE transaction_id IN (SELECT transaction_id FROM _copies)")
    for col in ("company_id", "office_code", "psc_code", "pop_country_code", "recipient_country", "psc_group"):
        con.execute(f"CREATE INDEX ix_orders_{col} ON orders_all({col})")
    con.execute("CREATE VIRTUAL TABLE orders_fts USING fts5(description, company_name, piid, psc_code, "
                "content='orders_all', content_rowid='rowid', tokenize='unicode61')")
    con.execute("INSERT INTO orders_fts(rowid, description, company_name, piid, psc_code) "
                "SELECT rowid, description, company_name, piid, psc_code FROM orders_all")
    con.commit()


def write_tables(con: sqlite3.Connection, tables: dict[str, pd.DataFrame]) -> None:
    for name, df in tables.items():
        if name == "orders":  # the app searches orders_all instead
            continue
        df.to_sql(name, con, if_exists="replace", index=False)
        for col in ("market", "counting"):
            if col in df.columns:
                con.execute(f"CREATE INDEX IF NOT EXISTS ix_{name}_{col} ON {name}({col})")
    con.commit()


def zip_db(path: Path) -> Path:
    out = path.with_suffix(path.suffix + ".zip")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(path, arcname=path.name)
    return out
