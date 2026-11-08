"""Transform + load tests on the TEST FIXTURE (tests/fixtures.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.config.market import definition_hash, load_market_config
from src.etl import runlog
from src.etl.load import load_snapshot
from src.etl.transform import MarketSpec, TransformError, transform
from tests.fixtures import staged_rows

ROOT = Path(__file__).resolve().parents[1]
IGNORE = ["transaction_number", "contract_transaction_unique_key", "last_modified_date",
          "initial_report_date"]


def _spec(key="defense_electronics", sid=1, dod_wide=None):
    """Market spec for tests. Logic tests use a DoD-wide copy of Defense
    Electronics (the fixture spans Army/Navy); dod_wide=False uses the real
    DLA-only configuration."""
    m = load_market_config().market(key)
    if dod_wide is None:
        dod_wide = key == "defense_electronics"
    if dod_wide:
        m = m.model_copy(update={"agency_scope": m.agency_scope.model_copy(
            update={"subtier_agencies": []})})
    return MarketSpec(key, m, sid, definition_hash(m), "yaml")


def _run(treatment="count_all_flag", staged=None, specs=None):
    return transform(staged if staged is not None else staged_rows(), specs or [_spec()],
                     "load_test", treatment, IGNORE)


def _db():
    con = sqlite3.connect(":memory:")
    con.executescript((ROOT / "sql" / "schema_sqlite.sql").read_text())
    con.executescript((ROOT / "sql" / "views.sql").read_text())
    return con


def test_undecided_refuses():
    with pytest.raises(TransformError, match="undecided"):
        _run("undecided")


def test_exclusions_and_dedup():
    r = _run()
    s = r.stats
    assert s["unique_transaction_ids"] == 19                    # T3 appeared in both extracts
    assert s["excluded_by_reason"] == {"outside_fy2026": 1, "not_contract_type_a_to_d": 1,
                                       "outside_agency_scope": 1, "not_in_market_codes": 1,
                                       "amount_unparseable": 1}
    assert s["included_transactions"] == 14
    assert s["included_obligations"] == 100 + 50 + 10 - 10 - 5 + 30 + 200 + 40 + 5 + 6 + 630 + 140
    assert s["potential_duplicate_groups"] == 2 and s["excluded_flagged_copies"] == 0
    assert str(s["data_through_date"]) == "2026-05-01"


def test_exclude_flagged_copies_only_when_identical():
    r = _run("exclude_flagged_copies")
    assert r.stats["excluded_flagged_copies"] == 1             # T12 only; G1 differs in funding office
    excl = set(r.excluded.loc[r.excluded.exclusion_reason == "excluded_flagged_copy", "transaction_id"])
    assert excl == {"T12"}


def test_companies_from_reported_parent_uei():
    r = _run()
    comp = r.tables["dim_companies"].set_index("company_key")
    assert comp.loc["PARENT:TESTPARENT01", "parent_status"] == "reported_parent"
    assert comp.loc["PARENT:TESTPARENT01", "company_name"] == "TEST PARENT HOLDINGS"
    assert comp.loc["UEI:TESTUEIC0001", "parent_status"] == "no_parent_reported"
    assert any(k.startswith("NOUEI:") for k in comp.index)
    rec = r.tables["dim_recipients"].set_index("recipient_key")
    assert bool(rec.loc["UEI:TESTUEIE0001", "parent_uei_conflict"])
    assert rec.loc["UEI:TESTUEIE0001", "parent_recipient_uei"] == "TESTPARENT02"  # latest action
    cand = r.tables["_match_candidates"]
    assert set(cand["potential_parent_uei"]) == {"TESTPARENT01", "TESTPARENT02"}


def test_segment_tagging():
    fts = _run().tables["fact_transaction_segments"].set_index("transaction_id")
    assert fts.loc["T1", "matched_by"] == "both" and fts.loc["T1", "match_tier"] == "core"
    assert fts.loc["T3", "matched_by"] == "psc"
    assert fts.loc["T6", "match_tier"] == "extended"
    assert fts.loc["T1", "keyword_tags"] == "radar"


def test_load_snapshot_and_views():
    r = _run()
    con = _db()
    runlog.start_run(con, "load_test", "load", "defense_electronics")
    counts = load_snapshot(con, r, "load_test")
    assert counts["fact_transactions"] == 14
    total = con.execute("SELECT fy2026_obligations FROM v_segment_fy2026_totals").fetchone()[0]
    assert total == r.stats["included_obligations"]
    # reload is idempotent (snapshot replace) and keeps reviewed decisions
    con.execute("UPDATE entity_match_candidates SET review_status='confirmed' "
                "WHERE potential_parent_uei='TESTPARENT02'")
    load_snapshot(con, _run(), "load_test")
    assert con.execute("SELECT COUNT(*) FROM fact_transactions").fetchone()[0] == 14
    assert con.execute("SELECT review_status FROM entity_match_candidates "
                       "WHERE potential_parent_uei='TESTPARENT02'").fetchone()[0] == "confirmed"
    flat = pd.read_sql("SELECT * FROM v_market_transactions_fy2026", con)
    assert len(flat) == 14 and flat["company_name"].notna().all()


def test_multi_market_membership():
    from tests.fixtures import row
    import pandas as pd
    extra = pd.DataFrame([
        row("L1", "CONT_AWD_L1", naics_code="423840", product_or_service_code="5305",
            recipient_uei="TESTUEIL0001", recipient_name="TEST LIMA SUPPLY",
            recipient_parent_uei=None, recipient_parent_name=None, federal_action_obligation="80.00",
            awarding_sub_agency_code="97AS", awarding_sub_agency_name="Defense Logistics Agency"),
        row("L2", "CONT_AWD_L2", naics_code="334511", product_or_service_code="8415",
            federal_action_obligation="20.00", awarding_sub_agency_code="97AS",
            awarding_sub_agency_name="Defense Logistics Agency"),                    # in BOTH markets
        row("L3", "CONT_AWD_L3", naics_code="423840", product_or_service_code="5305",
            federal_action_obligation="55.00"),                    # Army, not DLA -> out
    ]).assign(_basis="psc", _source_file="T", _run_id="s")
    staged = pd.concat([staged_rows(), extra], ignore_index=True)
    r = _run(staged=staged, specs=[_spec(), _spec("logistics_supply", 2)])
    fts = r.tables["fact_transaction_segments"]
    seg = fts.groupby("transaction_id")["segment_id"].apply(set)
    assert seg["L1"] == {2} and seg["L2"] == {1, 2} and seg["T1"] == {1}
    assert "L3" not in seg.index                                  # awarding sub-agency filter
    assert r.excluded.set_index("transaction_id").loc["L3", "exclusion_reason"] == "not_in_market_codes"
    assert r.stats["markets"]["logistics_supply"]["obligations"] == 100
    assert r.stats["markets"]["logistics_supply"]["psc_basis_obligations"] == 100
    assert len(r.tables["market_segments"]) == 2
    assert r.tables["fact_transactions"]["transaction_id"].is_unique
    con = _db()
    runlog.start_run(con, "load_test", "load", "x")
    load_snapshot(con, r, "load_test")
    tot = dict(con.execute("SELECT segment_id, fy2026_obligations FROM v_segment_fy2026_totals").fetchall())
    assert tot[2] == 100 and tot[1] == r.stats["markets"]["defense_electronics"]["obligations"]


def test_all_codes_base_market_with_derived_segments():
    from tests.fixtures import row
    import pandas as pd
    dla = dict(awarding_sub_agency_code="97AS", awarding_sub_agency_name="Defense Logistics Agency")
    base_rows = pd.DataFrame([
        row("D1", "CONT_AWD_D1", naics_code="334511", product_or_service_code="5840", **dla),   # DE + base
        row("D2", "CONT_AWD_D2", naics_code="423840", product_or_service_code="5305", **dla),   # logistics + base
        row("D3", "CONT_AWD_D3", naics_code="336413", product_or_service_code="1560", **dla),   # base only
        row("D4", "CONT_AWD_D4", naics_code="334511", product_or_service_code="5840"),          # Army: nowhere
    ]).assign(_basis="all", _source_file="T", _run_id="s", _market="dla_all")
    r = _run(staged=base_rows, specs=[_spec("dla_all", 1), _spec("defense_electronics", 2, dod_wide=False),
                                      _spec("logistics_supply", 5)])
    seg = r.tables["fact_transaction_segments"].groupby("transaction_id")["segment_id"].apply(set)
    assert seg["D1"] == {1, 2} and seg["D2"] == {1, 5} and seg["D3"] == {1}
    assert "D4" not in seg.index
    mb = r.tables["fact_transaction_segments"].set_index(["transaction_id", "segment_id"])["matched_by"]
    assert mb[("D3", 1)] == "all"
    assert r.stats["markets"]["dla_all"]["all_basis_obligations"] == 300
    con = _db()
    runlog.start_run(con, "load_test", "load", "x")
    load_snapshot(con, r, "load_test")                         # 'all' accepted by the schema
    assert con.execute("SELECT COUNT(*) FROM fact_transaction_segments").fetchone()[0] == 5
