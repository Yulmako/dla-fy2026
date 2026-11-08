from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.config.market import Market, load_market_config
from src.fiscal import (FY_END, FY_START, federal_fiscal_year, in_fy2026, parse_date,
                        time_period_filter)


def test_fy_window():
    assert (FY_START, FY_END) == (date(2025, 10, 1), date(2026, 9, 30))
    assert time_period_filter() == [{"start_date": "2025-10-01", "end_date": "2026-09-30"}]
    assert federal_fiscal_year(date(2025, 10, 1)) == 2026
    assert federal_fiscal_year(date(2025, 9, 30)) == 2025
    assert in_fy2026("2026-09-30") and not in_fy2026("2026-10-01")
    assert parse_date("03/15/2026") == date(2026, 3, 15)
    assert parse_date("garbage") is None and not in_fy2026(None)


def test_defense_electronics_market():
    cfg = load_market_config()
    m = cfg.market("defense_electronics")
    assert m.name == "Defense Electronics" and m.status == "validated"
    assert "not an exhaustive definition" in m.definition_note
    for code in ("811219", "811213", "334416"):
        assert code in m.naics()                      # approved additions (extended)
    assert "1290" in m.psc()
    for code in ("334519", "334210"):
        assert code in m.naics(("review",)) and code not in m.naics()
    for code in ("5930", "5945", "5965", "5975", "J015", "6640"):
        assert code not in m.psc()
    assert {"J015", "6640"} <= m.excluded_codes("psc")
    assert "334511" in m.naics() and "5865" in m.psc()
    assert "541330" not in m.naics()          # review tier never included
    assert len(m.naics()) > 1 and len(m.psc()) > 1   # no single-code market
    assert m.agency_filters() == [{"type": "awarding", "tier": "subtier",
                                   "name": "Defense Logistics Agency",
                                   "toptier_name": "Department of Defense"}]


def _market(**kw):
    base = dict(name="X", status="candidate",
                naics_codes=[{"code": "334511", "tier": "core"}], psc_codes=[])
    base.update(kw)
    return Market.model_validate(base)


def test_invalid_codes_rejected():
    with pytest.raises(ValidationError):
        _market(naics_codes=[{"code": "3345", "tier": "core"}])
    with pytest.raises(ValidationError):
        _market(psc_codes=[{"code": "58400", "tier": "core"}])
    with pytest.raises(ValidationError):
        _market(naics_codes=[{"code": "334511", "tier": "core"},
                             {"code": "334511", "tier": "extended"}])
    with pytest.raises(ValidationError):
        _market(included_tiers=["core", "review"])
    with pytest.raises(ValidationError):
        _market(naics_codes=[])


def test_excluded_code_cannot_be_included():
    with pytest.raises(ValidationError):
        _market(documented_exclusions=[{"kind": "naics", "codes": ["334511"], "reason": "x"}])


def test_validated_market_requires_definition_note():
    with pytest.raises(ValidationError):
        _market(status="validated")


def test_fiscal_months():
    from src.fiscal import calendar_to_fiscal_month, fiscal_month_range
    assert fiscal_month_range(1) == (date(2025, 10, 1), date(2025, 10, 31))
    assert fiscal_month_range(12) == (date(2026, 9, 1), date(2026, 9, 30))
    assert [calendar_to_fiscal_month(m) for m in (10, 3, 9)] == [1, 6, 12]


def test_logistics_market_families():
    m = load_market_config().market("logistics_supply")
    assert m.status == "candidate" and m.has_prefixes()
    assert m.match("423840", None) == ("naics", "core")
    assert m.match(None, "8415") == ("psc", "core")          # PSC family 84
    assert m.match(None, "4730") == ("psc", "extended")      # PSC family 47
    assert m.match("424210", "6505") is None                 # review-tier families only
    assert m.naics_tier("336413") is None                    # documented exclusion
    from src.api.filters import segment_filters
    f = segment_filters(m, "psc")["psc_codes"]
    assert ["Product", "84"] in f["require"] and ["Service", "R", "R7", "R706"] in f["require"]


def test_subtier_scope():
    m = load_market_config().market("logistics_supply")
    assert m.agency_filters() == [{"type": "awarding", "tier": "subtier",
                                   "name": "Defense Logistics Agency",
                                   "toptier_name": "Department of Defense"}]
    assert m.in_scope("Department of Defense", "Defense Logistics Agency")
    assert not m.in_scope("Department of Defense", "Department of the Army")
    assert not m.in_scope("Department of Energy", "Defense Logistics Agency")
    with pytest.raises(ValidationError):
        Market.model_validate({"name": "X", "status": "candidate",
                               "naics_codes": [{"code": "423840", "tier": "core"}],
                               "agency_scope": {"subtier_agencies": [{"name": "Defense Logistics Agency"}]}})


def test_all_dla_base_market_and_coverage():
    cfg = load_market_config()
    base = cfg.market("dla_all")
    assert cfg.active_market == "dla_all" and base.all_codes and base.bases() == ["all"]
    assert base.match(None, None) == ("all", "core") and base.match("999999", "ZZZZ") == ("all", "core")
    keys = cfg.active_keys()
    assert set(keys) == {"dla_all", "defense_electronics", "logistics_supply"}
    assert cfg.download_keys(keys) == ["dla_all"]                 # segments derived from the base
    assert cfg.covering("logistics_supply", keys) == "dla_all"
    assert set(cfg.loadable_keys(["dla_all"])) == set(keys)
    assert cfg.loadable_keys([]) == []
    from src.api.filters import segment_filters
    f = segment_filters(base, "all")
    assert "naics_codes" not in f and "psc_codes" not in f and f["agencies"][0]["tier"] == "subtier"
