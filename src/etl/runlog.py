"""etl_log helpers that work with a sqlite3 connection or a SQLAlchemy Connection."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd


def execute(con: Any, sql: str) -> None:
    """Run a parameter-free statement on sqlite3 or SQLAlchemy connections."""
    if hasattr(con, "exec_driver_sql"):
        con.exec_driver_sql(sql)
    else:
        con.execute(sql)


def literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (date, datetime)):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def start_run(con: Any, run_id: str, run_type: str, market_key: str | None,
              source_last_updated: str | None = None) -> None:
    pd.DataFrame([{"run_id": run_id, "run_type": run_type, "market_key": market_key,
                   "started_at": now(), "status": "running",
                   "records_extracted": 0, "records_inserted": 0, "records_updated": 0,
                   "records_excluded": 0, "is_provisional": False,
                   "source_last_updated": source_last_updated}]
                 ).to_sql("etl_log", con, if_exists="append", index=False)


def finish_run(con: Any, run_id: str, status: str, **fields: Any) -> None:
    fields = {"status": status, "finished_at": now(), **fields}
    sets = ", ".join(f"{k} = {literal(v)}" for k, v in fields.items())
    execute(con, f"UPDATE etl_log SET {sets} WHERE run_id = {literal(run_id)}")


def last_successful_load(con: Any) -> dict | None:
    df = pd.read_sql("SELECT * FROM etl_log WHERE run_type = 'load' AND status = 'success' "
                     "ORDER BY finished_at DESC LIMIT 1", con)
    return None if df.empty else df.iloc[0].to_dict()
