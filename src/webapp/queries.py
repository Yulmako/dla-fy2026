"""Read-only queries of the web-app database (src/webapp/build.py). No Streamlit here,
so they can be tested offline."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

import pandas as pd

BASE = "dla_all"


def q(con: sqlite3.Connection, sql: str, params: tuple | list = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def markets(con) -> pd.DataFrame:
    return q(con, "SELECT market, market_name, market_status, psc_group, fy2026_obligations "
                  "FROM data_status WHERE counting = 'usaspending_basis' ORDER BY rowid")


def status(con, market: str, counting: str) -> dict[str, Any]:
    d = q(con, "SELECT * FROM data_status WHERE market = ? AND counting = ?", (market, counting))
    return d.iloc[0].to_dict() if len(d) else {}


def table(con, name: str, market: str, counting: str, order: str | None = None,
          limit: int | None = None, extra: str = "", params: tuple = ()) -> pd.DataFrame:
    sql = f"SELECT * FROM {name} WHERE market = ? AND counting = ? {extra}"
    if order:
        sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return q(con, sql, (market, counting, *params))


def kpis(con, market: str, counting: str) -> dict[str, Any]:
    d = table(con, "kpis", market, counting)
    return d.iloc[0].to_dict() if len(d) else {}


def psc_group_clause(group: str | None) -> tuple[str, list]:
    if not group:
        return "", []
    return " AND o.psc_group = ?", [group]


_FTS_SAFE = re.compile(r"[^\w]+", re.UNICODE)


def fts_query(text: str) -> str | None:
    """User text -> FTS5 query: every word must match (prefix match on each)."""
    words = [w for w in _FTS_SAFE.split(text or "") if w]
    return " AND ".join(f'"{w}"*' for w in words) if words else None


def search_orders(con, market: str, counting: str, text: str = "", company_id: Any = None,
                  office: str | None = None, loc: tuple[str, str] | None = None,
                  basis: str = "place_of_performance", limit: int = 200) -> tuple[pd.DataFrame, dict]:
    """Orders of the selected market and filters, largest amounts first, plus totals
    (count and sum) over every match – across all actions, not only the shown rows."""
    group = market.split(":", 1)[1] if ":" in market else None
    where, params = ["1=1"], []
    clause, p = psc_group_clause(group if isinstance(group, str) else None)
    if clause:
        where.append(clause[5:]); params += p
    if counting == "probable_duplicates_removed":
        where.append("o.excluded_as_copy = 0")
    if company_id is not None:
        where.append("o.company_id = ?"); params.append(int(company_id))
    if office:
        where.append("o.office_code = ?"); params.append(office)
    if loc:
        cc, sc = (("recipient_country", "recipient_state") if basis == "recipient_location"
                  else ("pop_country_code", "pop_state_code"))
        country, location = loc
        if country == "USA" and location:
            where.append(f"o.{cc} = 'USA' AND substr(o.{sc}, 1, 2) = ?"); params.append(location)
        else:
            where.append(f"o.{cc} = ?"); params.append(country)
    fts = fts_query(text)
    src = "orders_all o"
    if fts:
        src = "orders_fts f JOIN orders_all o ON o.rowid = f.rowid"
        where.append("orders_fts MATCH ?"); params.append(fts)
    w = " AND ".join(where)
    tot = q(con, f"SELECT COUNT(*) AS n, COALESCE(SUM(o.transaction_amount), 0) AS total FROM {src} WHERE {w}", params)
    rows = q(con, f"SELECT o.* FROM {src} WHERE {w} ORDER BY ABS(o.transaction_amount) DESC LIMIT {int(limit)}", params)
    return rows, {"n": int(tot["n"].iloc[0]), "total": float(tot["total"].iloc[0])}
