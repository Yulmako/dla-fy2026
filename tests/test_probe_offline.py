"""Offline tests for the Phase 1 probe and API client.

The fake session below is a TEST FIXTURE that exercises code paths only. Its
values are obviously artificial (TEST-ONLY names, base URL http://test.invalid)
and are never written to data/ – real validation happens only by running
scripts/phase1_api_probe.py against the live public API.
"""
from __future__ import annotations

import json

import pytest

from src.api.client import CallBudgetExceeded, UsaSpendingClient
from src.api.probe import Phase1Probe, _valid_values, psc_tree_path, render_markdown
from src.config.market import load_market_config

VALID_TXN_FIELDS = ["Action Date", "Action Type", "Award ID", "Award Type", "Awarding Agency",
                    "Awarding Sub Agency", "Funding Agency", "Funding Sub Agency",
                    "generated_internal_id", "internal_id", "Mod", "naics_code",
                    "naics_description", "product_or_service_code",
                    "product_or_service_description", "NAICS", "PSC", "Recipient Name",
                    "Recipient UEI", "Recipient Location", "Primary Place of Performance",
                    "Transaction Amount", "Transaction Description"]


class FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


def _row(fields, **kw):
    base = {"Award ID": "TEST-1", "Mod": "P00001", "Action Date": "2026-03-01",
            "Transaction Amount": 100.0, "Recipient UEI": "TESTUEI00001",
            "Recipient Name": "TEST-ONLY RECIPIENT", "generated_internal_id": "CONT_AWD_G1"}
    base.update(kw)
    return {f: base.get(f) for f in fields}


class FakeSession:
    """Routes requests to canned responses shaped like the documented API."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def request(self, method, url, json=None, params=None, timeout=None):
        path = url.replace("http://test.invalid", "")
        self.calls.append((method, path, json))
        body = json or {}
        f = body.get("filters", {})
        if path == "/api/v2/awards/last_updated/":
            return FakeResp(200, {"last_updated": "11/06/2026"})
        if path == "/api/v2/references/toptier_agencies/":
            return FakeResp(200, {"results": [{"agency_name": "Department of Defense",
                                               "toptier_code": "097", "agency_id": 1173}]})
        if path.startswith("/api/v2/agency/097/sub_agency"):
            return FakeResp(200, {"results": [{"name": "Defense Logistics Agency",
                                               "abbreviation": "DLA", "total_obligations": 1.0,
                                               "transaction_count": 1}]})
        if path.startswith("/api/v2/search/spending_by_category/"):
            cat = path.rstrip("/").split("/")[-1]
            if cat in ("naics", "psc"):
                key = "naics_codes" if cat == "naics" else "psc_codes"
                if key in f:
                    v = f[key]
                    code = v["require"][0] if isinstance(v, dict) else v[0]
                    code = code[-1] if isinstance(code, list) else code
                    return FakeResp(200, {"results": [{"code": code, "name": "TEST-ONLY",
                                                       "amount": 10.0}],
                                          "page_metadata": {"page": 1, "hasNext": False}})
                codes = (["334511", "334220", "334419", "334519", "541330", "334510"]
                         if cat == "naics" else ["5840", "5865", "5998", "J015", "5930"])
                return FakeResp(200, {"results": [{"code": c, "name": f"TEST-ONLY {c}",
                                                   "amount": 100.0 * (i + 1)}
                                                  for i, c in enumerate(codes)],
                                      "page_metadata": {"page": 1, "hasNext": False}})
            if cat == "recipient":
                return FakeResp(200, {"results": [{"name": "TEST-ONLY RECIPIENT",
                                                   "uei": "TESTUEI00001", "amount": 150.0}],
                                      "page_metadata": {"page": 1, "hasNext": False}})
        if path.startswith("/api/v2/references/naics/"):
            code = path.rstrip("/").split("/")[-1]
            return FakeResp(200, {"results": [{"naics": code, "naics_description": "TEST-ONLY"}]})
        if path.startswith("/api/v2/references/filter_tree/psc/"):
            return FakeResp(200, {"results": [{"id": "5810", "description": "TEST-ONLY"}]})
        if path == "/api/v2/download/count/":
            return FakeResp(200, {"calculated_transaction_count": 1234,
                                  "maximum_transaction_limit": 500000,
                                  "transaction_rows_gt_limit": False})
        if path == "/api/v2/search/spending_by_transaction_count/":
            start = (f.get("time_period") or [{}])[0].get("start_date", "")
            n = 20 if start[:7] in ("2026-08", "2026-09") else 1000
            return FakeResp(200, {"results": {"contracts": n}})
        if path.endswith("_count/"):
            return FakeResp(200, {"results": {"contracts": 1234, "idvs": 5}})
        if path == "/api/v2/search/spending_by_transaction/":
            fields = body["fields"]
            bad = [x for x in fields if x not in VALID_TXN_FIELDS]
            if bad:
                return FakeResp(422, {"detail": "Field 'fields' is outside valid values "
                                                f"{VALID_TXN_FIELDS}".replace('"', "'")})
            if body.get("limit", 0) > 100:
                return FakeResp(422, {"detail": "Field 'limit' value '101' is above max '100'"})
            if "recipient_search_text" in f:
                rows = [_row(fields), _row(fields, Mod="P00002", **{"Transaction Amount": 50.0,
                                                                    "Action Date": "2026-04-01"})]
            elif body.get("page", 1) == 1:
                rows = [
                    _row(fields),                                   # \ same transaction ID twice
                    _row(fields),                                   # /
                    _row(fields, **{"Award ID": "TEST-2", "generated_internal_id": "CONT_AWD_G2"}),
                    _row(fields, **{"Award ID": "TEST-2", "generated_internal_id": "CONT_AWD_G3"}),
                    _row(fields, Mod="P00009", **{"Transaction Amount": -5.0}),
                ]
            else:
                rows = []
            return FakeResp(200, {"results": rows,
                                  "page_metadata": {"page": body.get("page"), "hasNext": False}})
        if path == "/api/v2/search/spending_by_award/":
            fields = body["fields"]
            if "internal_id" in fields:
                return FakeResp(500, {"detail": "server error"})
            row = {"Award ID": "TEST-1", "Award Amount": 9999.0, "Start Date": "2021-01-01",
                   "Recipient Name": "TEST-ONLY RECIPIENT", "generated_internal_id": "CONT_AWD_TEST"}
            return FakeResp(200, {"results": [{k: row.get(k) for k in fields}],
                                  "page_metadata": {"page": 1, "hasNext": False}})
        if path.startswith("/api/v2/awards/CONT_AWD_TEST"):
            return FakeResp(200, {"total_obligation": 9999.0, "base_and_all_options": 20000.0})
        if path == "/api/v2/transactions/":
            aid = body.get("award_id")
            if aid == "CONT_AWD_G1":
                res = [{"id": "CONT_TX_1", "modification_number": "P00001",
                        "action_date": "2026-03-01", "federal_action_obligation": 100.0}]
            elif aid in ("CONT_AWD_G2", "CONT_AWD_G3"):
                res = [{"id": f"CONT_TX_{aid}", "modification_number": "P00001",
                        "action_date": "2026-03-01", "federal_action_obligation": 100.0}]
            else:
                res = [{"id": "CONT_TX_A", "action_date": "2021-01-01",
                        "federal_action_obligation": 9849.0},
                       {"id": "CONT_TX_B", "action_date": "2026-03-01",
                        "federal_action_obligation": 150.0}]
            return FakeResp(200, {"results": res, "page_metadata": {"page": 1, "hasNext": False}})
        if path == "/api/v2/bulk_download/list_agencies/":
            return FakeResp(200, {"agencies": {"cfo_agencies": [
                {"name": "Department of Defense", "toptier_code": "097",
                 "toptier_agency_id": 126}], "other_agencies": []}})
        if path == "/api/v2/bulk_download/list_monthly_files/":
            if body.get("agency") not in ("all", 126):
                return FakeResp(400, {"detail": f"{body.get('agency')} agency not found"})
            return FakeResp(200, {"monthly_files": [{"file_name": "FY2026_TEST.zip"}]})
        if path == "/api/v2/search/spending_over_time/":
            tp = (f.get("time_period") or [{}])[0]
            if tp.get("start_date") == "2026-03-01":
                return FakeResp(200, {"results": [{"time_period": {"fiscal_year": "2026",
                                                                   "month": "6"},
                                                   "aggregated_amount": 100.0}]})
            amounts = {1: 20.0, 10: 10.0, 11: 1.0, 12: 0.5}
            return FakeResp(200, {"results": [
                {"time_period": {"fiscal_year": "2026", "month": str(m)},
                 "aggregated_amount": amounts.get(m, 100.0)} for m in range(1, 13)]})
        return FakeResp(404, {"detail": f"unrouted {path}"})


def _client(session=None, **kw):
    kw.setdefault("delay_seconds", 0)
    kw.setdefault("sleep", lambda s: None)
    return UsaSpendingClient(base_url="http://test.invalid", session=session or FakeSession(), **kw)


@pytest.fixture()
def report(tmp_path):
    cfg = load_market_config()
    client = _client(raw_dir=tmp_path / "raw", max_calls=500)
    return Phase1Probe(client, "defense_electronics", cfg.market("defense_electronics")).run()


def _check(report, cid):
    return next(c for c in report["checks"] if c["check_id"] == cid)


def test_all_checks_run_without_probe_errors(report):
    assert len(report["checks"]) == 12
    errors = [c for c in report["checks"] if c["summary"].startswith("Probe error")]
    assert not errors, errors[0]["details"].get("traceback")


def test_key_semantics_detected(report):
    assert _check(report, "C07")["status"] == "PASS"
    assert _check(report, "C09")["status"] == "PASS"
    assert _check(report, "C10")["status"] == "PASS"
    assert report["synthetic_data"] is False


def test_c06_uses_api_valid_field_list_without_retrying(report):
    txn = _check(report, "C06")["details"]["transaction_search"]
    assert "Last Modified Date" in txn["rejected"]
    assert "Action Date" in report["api_facts"]["transaction_search_valid_fields"]


def test_c08_dedups_on_transaction_id(report):
    dup = report["duplicate_inspection"]
    s = dup["summary"]
    assert s["exact_duplicate_transaction_ids"] == 1         # TEST-1 P00001 twice, one ID
    assert s["identical_groups_distinct_transactions"] == 1  # TEST-2 on two different awards
    exact = dup["exact_duplicate_transaction_ids"][0]
    assert exact["transaction_ids_in_source"] == ["CONT_TX_1"]
    assert "contract number (PIID)" in dup["never_dedup_on"]
    assert _check(report, "C08")["status"] == "WARN"


def test_c11_resolves_bulk_agency_id(report):
    c11 = _check(report, "C11")
    assert c11["status"] == "PASS"
    assert report["api_facts"]["bulk_toptier_agency_ids"] == {"097": 126}


def test_c12_trailing_months_counts_vs_dollars(report):
    lag = report["reporting_lag"]
    by = {m["calendar_month"]: m["classification"] for m in lag["months"]}
    assert by["2025-10"] == "baseline"                      # low October never flagged
    assert by["2026-07"] == "low_obligations_normal_reporting"
    assert by["2026-08"] == by["2026-09"] == "likely_missing_reporting"
    assert lag["month_numbering"] == "fiscal"
    assert report["api_facts"]["reporting_volume_normal_through"] == "2026-07-31"
    assert report["api_facts"]["data_through_latest_action_date_scope"] == "2026-03-01"
    assert _check(report, "C12")["status"] == "WARN"


def test_candidates_skip_documented_exclusions(report):
    cand = {x["code"] for x in report["code_validation"]["candidates_for_review"]["naics"]}
    assert "334510" not in cand                              # documented exclusion
    psc = {x["code"] for x in report["code_validation"]["candidates_for_review"]["psc"]}
    assert "J015" not in psc


def test_markdown_renders(report):
    md = render_markdown(report)
    for s in ("FY2026", "Check summary", "not company revenue", "C08 duplicate inspection",
              "Reporting lag and data-through", "Market definition note"):
        assert s in md


def test_raw_responses_saved(tmp_path):
    client = _client(raw_dir=tmp_path)
    client.get("/api/v2/awards/last_updated/", label="x")
    files = list(tmp_path.glob("0001_*.json"))
    assert files and (tmp_path / "index.jsonl").exists()
    rec = json.loads(files[0].read_text())
    assert rec["status_code"] == 200 and rec["response_sha256"]


def test_call_budget_enforced():
    client = _client(max_calls=1)
    client.get("/api/v2/awards/last_updated/")
    with pytest.raises(CallBudgetExceeded):
        client.get("/api/v2/awards/last_updated/")


class Scripted(FakeSession):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)
        self.n = 0

    def request(self, *a, **k):
        self.n += 1
        if self.statuses:
            return FakeResp(self.statuses.pop(0), {"detail": "x"})
        return super().request(*a, **k)


@pytest.mark.parametrize("statuses,expect_attempts,expect_ok", [
    ([429, 503], 3, True),           # transient -> retried, then success
    ([500, 500], 2, False),          # 500 retried once only
    ([422], 1, False),               # 4xx never retried
    ([400], 1, False),
    ([503] * 10, 5, False),          # bounded: 1 + max_retries(4)
])
def test_retry_policy(statuses, expect_attempts, expect_ok):
    s = Scripted(statuses)
    r = _client(s).get("/api/v2/awards/last_updated/")
    assert r.attempts == expect_attempts and r.ok is expect_ok


def test_retry_false_makes_single_attempt():
    s = Scripted([503, 503])
    r = _client(s).post("/api/v2/search/spending_by_award/", {}, retry=False)
    assert r.attempts == 1 and not r.ok


def test_non_retryable_counted():
    c = _client(Scripted([422]))
    c.get("/api/v2/awards/last_updated/")
    assert c.stats.non_retryable_errors == 1 and c.stats.retries == 0


def test_valid_values_parser():
    msg = {"detail": "Field 'fields' is outside valid values ['Action Date', 'Mod']"}
    assert _valid_values(msg) == ["Action Date", "Mod"]


def test_psc_tree_path():
    assert psc_tree_path("5840") == ["Product", "58", "5840"]
    assert psc_tree_path("J058") == ["Service", "J", "J0", "J058"]


def test_probe_resolves_subtier_scope():
    cfg = load_market_config()
    r = Phase1Probe(_client(max_calls=600), "logistics_supply", cfg.market("logistics_supply")).run()
    c02 = _check(r, "C02")
    assert c02["status"] == "PASS"
    assert r["api_facts"]["scope_subtier_agencies"]["Defense Logistics Agency"]["abbreviation"] == "DLA"
    assert r["market"]["agency_scope"][0]["tier"] == "subtier"


def test_probe_all_codes_market_runs():
    cfg = load_market_config()
    r = Phase1Probe(_client(max_calls=600), "dla_all", cfg.market("dla_all")).run()
    errors = [c for c in r["checks"] if c["summary"].startswith("Probe error")]
    assert not errors, errors[0]["details"].get("traceback")
    assert r["market"]["included_naics"] == [] and _check(r, "C02")["status"] == "PASS"
