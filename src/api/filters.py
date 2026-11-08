"""USAspending filter objects for a market (FY2026, contracts A–D).

Filter syntax confirmed live in Phase 1 (C03): NAICS {"require": [...]};
PSC plain list or tree paths {"require": [["Product","58","5840"], ...]}.
Markets with code families use tree paths (PSC) / short NAICS codes, plus an
"exclude" list for documented exclusions that fall inside an included family.
The API combines top-level filters with AND, so a market (NAICS OR PSC) is
extracted as two bases and merged on the transaction ID.
"""
from __future__ import annotations

from typing import Any, Literal

from src.config.market import Market
from src.fiscal import CONTRACT_AWARD_TYPES, time_period_filter

Basis = Literal["naics", "psc"]


def psc_tree_path(code: str) -> list[str]:
    """USAspending PSC filter-tree path for a full code or a prefix."""
    code = code.upper()
    if code[0].isdigit():
        return ["Product", code[:2]] + ([code] if len(code) > 2 else [])
    if code[0] == "A":
        return ["Research and Development", code[:2], code[:3], code][: max(2, len(code))]
    path = ["Service", code[0]]
    if len(code) >= 2:
        path.append(code[:2])
    if len(code) == 4:
        path.append(code)
    elif len(code) == 3:
        path.append(code)
    return path


def scope_filters(market: Market) -> dict[str, Any]:
    f: dict[str, Any] = {"time_period": time_period_filter(),
                         "award_type_codes": list(CONTRACT_AWARD_TYPES)}
    agencies = market.agency_filters()
    if agencies:
        f["agencies"] = agencies
    return f


def _excluded_inside(market: Market, kind: str, prefixes: list[str]) -> list[str]:
    return sorted(c for c in market.excluded_codes(kind) if any(c.startswith(p) for p in prefixes))


def naics_filter_value(market: Market) -> dict[str, Any]:
    prefixes = market.naics_prefix_list()
    v: dict[str, Any] = {"require": market.naics() + prefixes}
    exc = _excluded_inside(market, "naics", prefixes)
    if exc:
        v["exclude"] = exc
    return v


def psc_filter_value(market: Market) -> Any:
    prefixes = market.psc_prefix_list()
    if not prefixes:
        return market.psc()                       # plain list form (validated in C03)
    v: dict[str, Any] = {"require": [psc_tree_path(c) for c in market.psc() + prefixes]}
    exc = _excluded_inside(market, "psc", prefixes)
    if exc:
        v["exclude"] = [psc_tree_path(c) for c in exc]
    return v


def segment_filters(market: Market, basis: str) -> dict[str, Any]:
    """Filters for one extraction basis ('all', 'naics' or 'psc'); {} if the market
    has no codes for that basis."""
    if market.all_codes:
        return scope_filters(market) if basis in ("all", "naics", "psc") else {}
    if basis == "all":
        return {}
    f = scope_filters(market)
    if basis == "naics":
        if market.naics() or market.naics_prefix_list():
            f["naics_codes"] = naics_filter_value(market)
        else:
            return {}
    else:
        if market.psc() or market.psc_prefix_list():
            f["psc_codes"] = psc_filter_value(market)
        else:
            return {}
    return f
