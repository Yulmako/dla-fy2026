"""Phase 2a tests (offline). All rows below are TEST FIXTURES, not real data."""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from src.api.client import UsaSpendingClient
from src.etl import runlog
from src.etl.download import DownloadError, download_basis, fetch_file
from src.etl.staging import read_download_csvs, read_staging, write_staging
from src.quality.duplicates import profile_potential_duplicates, render_markdown

ROOT = Path(__file__).resolve().parents[1]


def _rows():
    base = dict(contract_award_unique_key="CONT_AWD_A", modification_number="0",
                action_date="2026-06-30", federal_action_obligation="315000000.00",
                transaction_number="0", action_type_code="", last_modified_date="2026-07-01",
                awarding_sub_agency_name="TEST ARMY", recipient_name="TEST CO",
                funding_office_code="F1", product_or_service_code="5840")
    return [
        dict(base, contract_transaction_unique_key="TX_A_0"),
        dict(base, contract_transaction_unique_key="TX_A_1", transaction_number="1"),   # tn only
        dict(base, contract_transaction_unique_key="TX_B_0", contract_award_unique_key="CONT_AWD_B",
             federal_action_obligation="50.00"),
        dict(base, contract_transaction_unique_key="TX_B_16", contract_award_unique_key="CONT_AWD_B",
             federal_action_obligation="50.00", transaction_number="16", funding_office_code="F2"),
        dict(base, contract_transaction_unique_key="TX_C_0", contract_award_unique_key="CONT_AWD_C",
             federal_action_obligation="0.00"),
        dict(base, contract_transaction_unique_key="TX_C_1", contract_award_unique_key="CONT_AWD_C",
             federal_action_obligation="0.00", transaction_number="1"),                 # zero group
        dict(base, contract_transaction_unique_key="TX_D_0", contract_award_unique_key="CONT_AWD_D",
             federal_action_obligation="10.00"),
        dict(base, contract_transaction_unique_key="TX_D_1", contract_award_unique_key="CONT_AWD_D",
             federal_action_obligation="20.00", transaction_number="1"),                # baseline
        dict(base, contract_transaction_unique_key="TX_E_0", contract_award_unique_key="CONT_AWD_E",
             federal_action_obligation="7.00"),                                          # single
    ]


def _frame():
    df = pd.DataFrame(_rows())
    df["_basis"] = "naics"
    extra = df.iloc[[0]].copy()
    extra["_basis"] = "psc"                       # same transaction ID in both extracts
    return pd.concat([df, extra], ignore_index=True)


def test_profile_counts_and_fields():
    p = profile_potential_duplicates(_frame())
    s = p["summary"]
    assert s["unique_transaction_ids"] == 9 and s["ids_in_both_naics_and_psc_extracts"] == 1
    assert s["suspicious_groups_nonzero_amount"] == 2           # A and B, not zero-amount C
    assert s["affected_records"] == 4
    assert s["potential_duplicate_amount_net"] == 315000050.0
    assert s["zero_amount_groups"] == 1
    assert s["groups_where_only_transaction_number_differs"] == 1
    assert s["baseline_same_award_mod_date_different_amounts"] == 1
    freq = dict(p["differing_field_frequency"])
    assert freq["transaction_number"] == 2 and freq["funding_office_code"] == 1
    assert any("substantive" in h or "separate reporting" in h for h in p["interpretation_hints"])
    md = render_markdown(p, "test")
    assert "No records were excluded" in md and "$315,000,050.00" in md


def test_profile_requires_columns():
    with pytest.raises(ValueError):
        profile_potential_duplicates(pd.DataFrame([{"x": 1}]))


def test_staging_roundtrip(tmp_path):
    csv = tmp_path / "Contracts_PrimeTransactions_1.csv"
    pd.DataFrame(_rows()).to_csv(csv, index=False)
    df = read_download_csvs([csv], "naics", "run1")
    assert df["action_type_code"].isna().all()                  # blanks preserved as NULL
    con = sqlite3.connect(":memory:")
    assert write_staging(con, df) == 9
    back = read_staging(con)
    assert list(back["_basis"].unique()) == ["naics"] and len(back) == 9


class _Resp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._p = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.headers = {}
        self._content = content

    def json(self):
        if self._p is None:
            raise ValueError
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Contracts_PrimeTransactions_2026-11-06_1.csv",
                    pd.DataFrame(_rows()).to_csv(index=False))
        zf.writestr("Data_Dictionary.txt", "x")
    return buf.getvalue()


class _ApiSession:
    def __init__(self):
        self.headers = {}
        self.polls = 0

    def request(self, method, url, json=None, params=None, timeout=None):
        if url.endswith("/download/count/"):
            return _Resp(200, {"calculated_transaction_count": 9, "maximum_transaction_limit": 500000})
        if url.endswith("/download/transactions/"):
            return _Resp(200, {"file_name": "job.zip", "status_url": "x"})
        if url.endswith("/download/status/"):
            self.polls += 1
            if self.polls < 2:
                return _Resp(200, {"status": "running"})
            return _Resp(200, {"status": "finished",
                               "file_url": "https://files.usaspending.gov/generated_downloads/job.zip"})
        return _Resp(404, {"detail": "unrouted"})


class _FileSession:
    def get(self, url, stream=True, timeout=None):
        return _Resp(200, content=_zip_bytes())


def test_download_flow(tmp_path):
    client = UsaSpendingClient(base_url="http://test.invalid", session=_ApiSession(),
                               delay_seconds=0, sleep=lambda s: None)
    res = download_basis(client, {"x": 1}, "naics", tmp_path, session=_FileSession(),
                         poll_seconds=0, sleep=lambda s: None)
    assert res.expected_rows == 9 and len(res.sha256) == 64
    assert [p.name for p in res.csv_paths] == ["Contracts_PrimeTransactions_2026-11-06_1.csv"]


def test_fetch_refuses_unofficial_host(tmp_path):
    with pytest.raises(DownloadError):
        fetch_file("https://example.com/x.zip", tmp_path, session=_FileSession())


def test_runlog_sqlite():
    con = sqlite3.connect(":memory:")
    con.executescript((ROOT / "sql" / "schema_sqlite.sql").read_text())
    runlog.start_run(con, "r1", "stage", "defense_electronics")
    runlog.finish_run(con, "r1", "success", records_extracted=9, error_message="it's ok")
    row = con.execute("SELECT status, records_extracted, error_message FROM etl_log").fetchone()
    assert row == ("success", 9, "it's ok")


def test_award_total_consistency_and_snapshot_rule():
    rows = _rows()[:2]                                         # A: same action, tn 0 and 1
    rows[0]["total_dollars_obligated"] = "0.00"
    rows[1]["total_dollars_obligated"] = "315000000.00"        # latest report: one copy
    rows[1]["last_modified_date"] = "2026-07-05"
    rows[1]["highly_compensated_officer_1_name"] = "TEST NAME"  # snapshot field
    df = pd.DataFrame(rows).assign(_basis="naics")
    p = profile_potential_duplicates(df)
    c = p["award_total_consistency"]
    assert c["results"] == {"reported_total_matches_one_copy": 1}
    assert p["summary"]["groups_differing_only_in_snapshot_fields"] == 1
    assert "Award-total consistency test" in render_markdown(p, "t")


def test_multi_market_staging_and_legacy_migration(tmp_path):
    from src.etl.staging import (migrate_legacy_staging, read_for_profile, read_staging,
                                 reset_markets, stage_csvs, staged_markets)
    con = sqlite3.connect(":memory:")
    # legacy table: written before multi-market support (no _market column)
    legacy = pd.DataFrame(_rows()).assign(_basis="naics", _source_file="x", _run_id="old")
    legacy.to_sql("stg_prime_transactions", con, index=False)
    assert migrate_legacy_staging(con, "defense_electronics")
    assert staged_markets(con) == ["defense_electronics"]
    # stage a second market from CSV without touching the first
    csv = tmp_path / "Contracts_PrimeTransactions_1.csv"
    pd.DataFrame(_rows()[:3]).to_csv(csv, index=False)
    reset_markets(con, ["logistics_supply"])
    assert stage_csvs(con, [csv], "psc", "new", "logistics_supply", chunksize=2) == 3
    assert sorted(staged_markets(con)) == ["defense_electronics", "logistics_supply"]
    assert len(read_staging(con, ["defense_electronics"])) == 9
    keys, detail = read_for_profile(con, "defense_electronics")
    p = profile_potential_duplicates(detail, keys=keys)
    assert p["summary"]["unique_transaction_ids"] == 9
    assert p["summary"]["suspicious_groups_nonzero_amount"] == 2
    # restaging a market replaces only its rows
    reset_markets(con, ["logistics_supply"])
    assert staged_markets(con) == ["defense_electronics"]


def test_plan_jobs_splits_by_month():
    from src.etl.download import plan_jobs

    class S(_ApiSession):
        def request(self, method, url, json=None, params=None, timeout=None):
            if url.endswith("/download/count/"):
                tp = (json["filters"].get("time_period") or [{}])[0]
                whole = tp.get("start_date") == "2025-10-01" and tp.get("end_date") == "2026-09-30"
                n = 900000 if whole else 75000
                return _Resp(200, {"calculated_transaction_count": n,
                                   "maximum_transaction_limit": 500000})
            return super().request(method, url, json, params, timeout)

    client = UsaSpendingClient(base_url="http://test.invalid", session=S(), delay_seconds=0,
                               sleep=lambda s: None)
    plans = plan_jobs(client, {"time_period": [{"start_date": "2025-10-01",
                                                "end_date": "2026-09-30"}]}, "psc", "logistics_supply")
    assert len(plans) == 12 and plans[0].label == "logistics_supply_psc_fm01"
    assert plans[11].filters["time_period"][0] == {"start_date": "2026-09-01", "end_date": "2026-09-30"}


def test_plan_jobs_partial_months_and_period_file(tmp_path):
    from src.etl.download import plan_jobs
    from src.etl.period import read_staging_period, write_staging_period
    from src.etl.period import parse_months

    class C:
        calls = []
        def post(self, path, body, **kw):
            self.calls.append(body["filters"]["time_period"][0])
            return {"calculated_transaction_count": 1000, "maximum_transaction_limit": 500000}
    months = parse_months("9-12")
    assert months == [9, 10, 11, 12]
    c = C()
    import src.etl.download as D
    orig = D._count
    D._count = lambda client, f: (1000, 500000)
    try:
        plans = plan_jobs(c, {"time_period": [{"start_date": "2025-10-01", "end_date": "2026-09-30"}]},
                          "all", "dla_all", fiscal_months=months)
    finally:
        D._count = orig
    assert [p.filters["time_period"][0]["start_date"] for p in plans] == \
        ["2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"]
    assert plans[-1].filters["time_period"][0]["end_date"] == "2026-09-30"
    info = write_staging_period(tmp_path, "r", months)
    assert info["partial"] and read_staging_period(tmp_path)["start_date"] == "2026-06-01"
    assert read_staging_period(tmp_path / "none")["partial"] is False


def test_extract_skips_subaward_file(tmp_path):
    import zipfile
    from src.etl.download import extract_contract_csvs
    z = tmp_path / "PrimeTransactionsAndSubawards_x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Contracts_PrimeTransactions_2026-11-07_1.csv", "a\n1\n")
        zf.writestr("Contracts_Subawards_2026-11-07_1.csv", "b\n2\n")
    out = extract_contract_csvs(z, tmp_path / "x")
    assert [p.name for p in out] == ["Contracts_PrimeTransactions_2026-11-07_1.csv"]


def test_plan_jobs_halves():
    import src.etl.download as D
    orig = D._count
    D._count = lambda client, f: (1000, 500000)
    try:
        plans = D.plan_jobs(None, {"time_period": []}, "all", "dla_all", fiscal_months=[6], halves=True)
    finally:
        D._count = orig
    assert [(p.filters["time_period"][0]["start_date"], p.filters["time_period"][0]["end_date"]) for p in plans] == \
        [("2026-03-01", "2026-03-15"), ("2026-03-16", "2026-03-31")]
    assert [p.label for p in plans] == ["dla_all_all_fm06a", "dla_all_all_fm06b"]


def test_fetch_file_retries_broken_connection(tmp_path):
    import io
    import zipfile
    import requests
    from src.etl.download import fetch_file
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Contracts_PrimeTransactions_1.csv", "a\n1\n")
    payload = buf.getvalue()

    class Resp:
        def __init__(self, fail):
            self.fail = fail
            self.headers = {"Content-Length": str(len(payload))}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            yield payload[:10]
            if self.fail:
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            yield payload[10:]

    class Sess:
        calls = 0
        def get(self, url, stream, timeout):
            Sess.calls += 1
            return Resp(fail=Sess.calls == 1)

    path, sha, size = fetch_file("https://files.usaspending.gov/generated_downloads/x.zip", tmp_path,
                                 session=Sess(), sleep=lambda s: None)
    assert Sess.calls == 2 and size == len(payload)
    assert zipfile.ZipFile(path).namelist() == ["Contracts_PrimeTransactions_1.csv"]


def test_split_plan_halves_and_single_day():
    import src.etl.download as D
    orig = D._count
    D._count = lambda client, f: (10, 500000)
    try:
        p = D.JobPlan("dla_all_all_fm06b", "all", "dla_all",
                      {"time_period": [{"start_date": "2026-03-16", "end_date": "2026-03-31"}]}, 188162)
        subs = D.split_plan(None, p)
        assert [D.plan_range(x) for x in subs] == [
            (D.date(2026, 3, 16), D.date(2026, 3, 23)), (D.date(2026, 3, 24), D.date(2026, 3, 31))]
        one = D.JobPlan("d", "all", "dla_all", {"time_period": [{"start_date": "2026-03-20", "end_date": "2026-03-20"}]}, 5)
        assert D.split_plan(None, one) == []
    finally:
        D._count = orig
