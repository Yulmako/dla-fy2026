#!/usr/bin/env python3
"""Vendor names – top companies in the staged Defense Electronics data, plus a
lookup of named vendors in ALL DoD FY2026 contract obligations (public API).

    python scripts/vendor_lookup.py                       # top 25 in staging
    python scripts/vendor_lookup.py --find "NOBLE SUPPLY" --find "ATLANTIC DIVING"

Writes data/processed/vendor_lookup_latest.md. Read-only: nothing is loaded.
Figures are FY2026, provisional (DoD reporting lag) and shown both with all
records and with one record per probable-duplicate group.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analytics.vendor_lookup import prepare, search, top_companies  # noqa: E402
from src.api.client import UsaSpendingClient  # noqa: E402
from src.api.filters import scope_filters  # noqa: E402
from src.config.market import load_market_config  # noqa: E402
from src.database.engine import get_engine  # noqa: E402
from src.etl.staging import (VENDOR_COLUMNS, as_object, migrate_legacy_staging,  # noqa: E402
                              read_staging, staged_markets)


def usd(v) -> str:
    return "–" if pd.isna(v) else ("-" if v < 0 else "") + f"${abs(v):,.0f}"


def md_table(df: pd.DataFrame, money: list[str]) -> str:
    if df.empty:
        return "_no rows_\n"
    d = df.copy()
    for c in money:
        d[c] = d[c].map(usd)
    cols = list(d.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join(lines) + "\n"


def api_lookup(client: UsaSpendingClient, market, term: str, key: str = "market") -> str:
    out = [f"### `{term}` — all DoD FY2026 contract obligations (any NAICS/PSC)\n"]
    ac = client.post("/api/v2/autocomplete/recipient/", {"search_text": term, "limit": 10},
                     label="autocomplete_recipient")
    cands = (ac.data or {}).get("results", []) if ac.ok else []
    if cands:
        out.append("Recipient records matching the name (pick by UEI – names are not merged):\n")
        out.append(md_table(pd.DataFrame([{k: c.get(k) for k in ("recipient_name", "uei",
                                                                    "recipient_level")}
                                           for c in cands]), []))
    ueis = [c["uei"] for c in cands if c.get("uei")][:5]
    if not ueis:
        out.append("_No recipient found._\n")
        return "\n".join(out)
    for cat, title in (("naics", "NAICS"), ("psc", "PSC")):
        f = scope_filters(market)
        f["recipient_search_text"] = ueis
        r = client.post(f"/api/v2/search/spending_by_category/{cat}/",
                        {"filters": f, "limit": 10, "page": 1}, label=f"vendor_{cat}")
        rows = (r.data or {}).get("results", []) if r.ok else []
        df = pd.DataFrame([{"code": x.get("code"), "description": x.get("name"),
                            "fy2026_obligations": float(x.get("amount") or 0)} for x in rows])
        if not df.empty:
            tier = market.naics_tier if cat == "naics" else market.psc_tier
            df[f"in_{key}"] = df["code"].map(
                lambda c: "yes" if tier(c) in market.included_tiers else "no")
        out.append(f"\nTop {title} codes for these UEIs (DoD, FY2026, USAspending basis):\n")
        out.append(md_table(df, ["fy2026_obligations"]))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", action="append", default=[], help="vendor name to look up")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--market", default=None, help="market key (default: active_market)")
    args = ap.parse_args(argv)
    cfg = load_market_config()
    key = args.market or cfg.active_market
    market = cfg.market(key)
    engine = get_engine()
    with engine.begin() as con:
        migrate_legacy_staging(con, cfg.active_market)
    with engine.connect() as con:
        staged_keys = staged_markets(con)
        source = key if key in staged_keys else cfg.covering(key, staged_keys)
        if source is None:
            print(f"No staged data for {key} – run scripts/phase2a_stage_and_profile.py")
            return 2
        raw = as_object(read_staging(con, [source], VENDOR_COLUMNS))
    if source != key:      # derived segment: keep rows whose scope and codes match it
        keep = [market.in_scope(a, b) and market.match(n if isinstance(n, str) else None,
                                                        p if isinstance(p, str) else None) is not None
                for a, b, n, p in zip(raw["awarding_agency_name"], raw["awarding_sub_agency_name"],
                                      raw["naics_code"], raw["product_or_service_code"])]
        raw = raw[keep]
    df = prepare(raw)

    L = [f"# Vendor lookup — {market.name}, FY2026 (provisional)", "",
         f"Staged {market.name} data (DoD, contracts A–D). Company = reported parent UEI, "
         "else own UEI. `one_per_group` keeps one record per probable-duplicate group; "
         "`all_records` counts every record as USAspending does. Not revenue.", "",
         f"## Top {args.top} companies in the {market.name} segment", "",
         md_table(top_companies(df, args.top),
                  ["fy2026_all_records", "fy2026_one_per_group", "probable_duplicate_amount"])]
    if args.find:
        client = UsaSpendingClient()
        L += ["## Named vendors", ""]
        for term in args.find:
            L += [f"### `{term}` — inside the {market.name} segment (staging)", "",
                  md_table(search(df, term), ["fy2026_all_records", "fy2026_one_per_group"]), ""]
            L.append(api_lookup(client, market, term, key))
    out = ROOT / "data" / "processed" / "vendor_lookup_latest.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:12 + args.top]))
    print(f"\nFull report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
