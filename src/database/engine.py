"""Engine factory. SQLite by default; set DATABASE_URL for PostgreSQL later, e.g.
postgresql+psycopg://user:pass@host:5432/fci – no application code changes needed.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_URL = f"sqlite:///{PROJECT_ROOT / 'data' / 'processed' / 'fci.sqlite'}"


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)
    engine = create_engine(url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA journal_mode = WAL")
            cur.close()
    return engine


VIEWS_SQL = PROJECT_ROOT / "sql" / "views.sql"


def view_statements(dialect: str = "sqlite") -> list[str]:
    """CREATE VIEW statements from sql/views.sql (comments stripped). The file is
    written for SQLite; PostgreSQL gets CREATE OR REPLACE VIEW instead."""
    lines = [ln for ln in VIEWS_SQL.read_text(encoding="utf-8").splitlines()
             if not ln.strip().startswith("--")]
    stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    if dialect == "postgresql":
        stmts = [s.replace("CREATE VIEW IF NOT EXISTS", "CREATE OR REPLACE VIEW") for s in stmts]
    return stmts


def _migrate_sqlite(engine: Engine) -> None:
    """fact_transaction_segments gained matched_by='all'. It is a snapshot table
    (rebuilt by every load), so an old SQLite copy is dropped and recreated."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        row = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' "
                                   "AND name='fact_transaction_segments'").fetchone()
        if row and "'all'" not in (row[0] or ""):
            conn.exec_driver_sql("DROP TABLE fact_transaction_segments")
        # raw_download_files from the single-market version was unique on (run_id, basis),
        # which refuses several monthly files per run: keep it aside, create the new table
        row = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' "
                                   "AND name='raw_download_files'").fetchone()
        sql = (row[0] or "") if row else ""
        if row and "job_file_name" not in sql.split("UNIQUE", 1)[-1]:
            legacy = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name='raw_download_files_legacy'").fetchone()
            if not legacy:
                conn.exec_driver_sql("ALTER TABLE raw_download_files RENAME TO raw_download_files_legacy")
                for (ix,) in conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
                        "AND tbl_name='raw_download_files_legacy'").fetchall():
                    conn.exec_driver_sql(f'DROP INDEX "{ix}"')   # names would clash with the new table


def _add_missing_columns_sqlite(engine: Engine) -> list[str]:
    """Tables created by an older version keep their old column list
    (create_all never alters). Add any column the models now have, as a
    nullable column – existing rows get NULL. Returns 'table.column' added."""
    if engine.dialect.name != "sqlite":
        return []
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            info = conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")').fetchall()
            if not info:
                continue                                   # created fresh by create_all
            have = {r[1] for r in info}
            for col in table.columns:
                if col.name not in have:
                    ctype = col.type.compile(dialect=engine.dialect)
                    conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ctype}')
                    added.append(f"{table.name}.{col.name}")
    return added


def init_db(engine: Engine) -> None:
    """Create all tables and FY2026 aggregation views (idempotent).
    Alembic migrations are introduced together with PostgreSQL."""
    _migrate_sqlite(engine)
    Base.metadata.create_all(engine)
    _add_missing_columns_sqlite(engine)
    with engine.begin() as conn:
        for stmt in view_statements(engine.dialect.name):
            conn.exec_driver_sql(stmt)


def session_factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
