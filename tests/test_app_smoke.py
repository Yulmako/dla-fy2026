"""Smoke-run every dashboard page against a TEST FIXTURE database.
Skipped automatically where Streamlit/SQLAlchemy/Plotly are not installed."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("sqlalchemy")
pytest.importorskip("plotly")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src.etl import runlog  # noqa: E402
from src.etl.load import load_snapshot  # noqa: E402
from tests.test_transform_load import _run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PAGES = sorted((ROOT / "app" / "pages").glob("[0-9]_*.py"))


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "fixture.sqlite"
    con = sqlite3.connect(path)
    con.executescript((ROOT / "sql" / "schema_sqlite.sql").read_text())
    con.executescript((ROOT / "sql" / "views.sql").read_text())
    runlog.start_run(con, "load_test", "load", "defense_electronics")
    load_snapshot(con, _run(), "load_test")
    runlog.finish_run(con, "load_test", "success", is_provisional=True,
                      data_through_date="2026-05-01", reporting_normal_through="2026-07-31")
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_runs(page, fixture_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{fixture_db}")
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception, at.exception


def test_empty_database_shows_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty.sqlite'}")
    at = AppTest.from_file(str(PAGES[0]), default_timeout=60).run()
    assert not at.exception
    assert any("No FY2026 data is loaded yet" in i.value for i in at.info)
