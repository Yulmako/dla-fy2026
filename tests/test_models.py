"""SQLAlchemy model tests + parity with sql/schema_sqlite.sql.

Skipped automatically if SQLAlchemy is not installed.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")

from src.database.engine import get_engine, init_db, session_factory  # noqa: E402
from src.database.models import Base, EtlLog, FactAward, FactTransaction  # noqa: E402

DDL = Path(__file__).resolve().parents[1] / "sql" / "schema_sqlite.sql"


@pytest.fixture()
def engine():
    eng = get_engine("sqlite://")
    init_db(eng)
    return eng


def test_models_match_sql_file(engine):
    con = sqlite3.connect(":memory:")
    con.executescript(DDL.read_text())
    sql_tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert sql_tables == set(Base.metadata.tables)
    for t in sql_tables:
        sql_cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        orm_cols = set(Base.metadata.tables[t].columns.keys())
        assert sql_cols == orm_cols, t


def test_fy_guard_enforced_by_orm_schema(engine):
    Session = session_factory(engine)
    with Session() as s:
        s.add(EtlLog(run_id="r1", run_type="load", started_at=datetime(2026, 11, 6), status="running"))
        s.add(FactAward(award_id="CONT_AWD_X"))
        s.commit()
        s.add(FactTransaction(transaction_id="T1", award_id="CONT_AWD_X",
                              transaction_date=date(2026, 3, 1),
                              transaction_amount=Decimal("10.00"), fiscal_year=2026, run_id="r1"))
        s.commit()
        s.add(FactTransaction(transaction_id="T2", award_id="CONT_AWD_X",
                              transaction_date=date(2025, 9, 30),
                              transaction_amount=Decimal("10.00"), fiscal_year=2026, run_id="r1"))
        with pytest.raises(sa.exc.IntegrityError):
            s.commit()


def test_init_db_creates_aggregation_views(engine):
    names = set(sa.inspect(engine).get_view_names())
    assert {"v_award_company_fy2026", "v_award_recipient_fy2026", "v_company_fy2026_summary",
            "v_segment_fy2026_totals", "v_company_fy2026_share"} <= names


def test_postgres_view_dialect():
    from src.database.engine import view_statements
    stmts = view_statements("postgresql")
    assert len(stmts) == 6 and all(s.startswith("CREATE OR REPLACE VIEW") for s in stmts)
