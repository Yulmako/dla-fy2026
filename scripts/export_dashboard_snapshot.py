#!/usr/bin/env python3
"""Export FY2026 aggregates for the hosted dashboard page (variant 3).

    python scripts/export_dashboard_snapshot.py

Base market: all DLA contract actions (`dla_all`, an all-codes market). The
dashboard's market filter lists that base market plus every official PSC group
found in it (e.g. 59 Electrical and Electronic Equipment Components, 84 Clothing,
R Support Services) – one "market" per group, named from USAspending's PSC tree.
Hand-made segments in config/market_config.yaml are not exported here.

Reads STAGED data (run phase2a first), applies the same rules and metrics as the
app in both counting modes, writes CSV tables to data/processed/dashboard_snapshot/
plus dashboard_snapshot.zip – send that zip. Aggregates and a capped order list only.
"""
from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analytics.psc_groups import fetch_group_names, group_of, kind_of, label  # noqa: E402
from src.analytics.snapshot import MODES, combine, flat_frame, market_tables, orders  # noqa: E402
from src.api.client import BASE_URL, UsaSpendingClient  # noqa: E402
from src.config.market import definition_hash, load_market_config  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.database.engine import get_engine, init_db  # noqa: E402
from src.etl.partitioned import auto_buckets, transform_staged  # noqa: E402
from src.etl.period import read_staging_period  # noqa: E402
from src.etl.staging import migrate_legacy_staging, staged_markets  # noqa: E402
from src.etl.transform import MarketSpec  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-market", default="dla_all", help="all-codes market to export (default dla_all)")
    ap.add_argument("--group-companies", type=int, default=200,
                    help="companies kept per PSC group in the ranking table (ranks use the full group)")
    ap.add_argument("--group-profiles", type=int, default=10,
                    help="companies per PSC group with agency/PSC/NAICS/location breakdowns")
    ap.add_argument("--app-db", action="store_true",
                    help="also build data/processed/app_data.sqlite(.zip) for the public web app: "
                         "these tables plus every order with a full-text index")
    ap.add_argument("--app-desc-chars", type=int, default=160,
                    help="characters of each order description kept in the web-app database (smaller file)")
    args = ap.parse_args(argv)

    cfg = load_market_config()
    settings = load_settings()
    base = args.base_market
    if base not in cfg.markets or not cfg.market(base).all_codes:
        print(f"{base} is not an all-codes market in config/market_config.yaml")
        return 2
    market = cfg.market(base)
    engine = get_engine()
    init_db(engine)
    with engine.begin() as con:
        migrate_legacy_staging(con, cfg.active_market)
    with engine.connect() as con:
        if base not in staged_markets(con):
            print(f"No staged rows for {base}. Run first, e.g.:\n"
                  f"  python scripts/phase2a_stage_and_profile.py --fiscal-months 9-12   (test)\n"
                  f"  python scripts/phase2a_stage_and_profile.py                        (full FY2026)")
            return 2
        buckets = auto_buckets(con, [base])
    spec = MarketSpec(base, market, cfg.segment_ids()[base], definition_hash(market), "")

    processed = ROOT / "data" / "processed"
    probe_path = processed / "phase1_validation_report_latest.json"
    facts = json.loads(probe_path.read_text()).get("api_facts", {}) if probe_path.exists() else {}
    api = settings.api
    client = UsaSpendingClient(base_url=BASE_URL, delay_seconds=api.delay_seconds,
                               timeout_seconds=api.timeout_seconds, max_retries=api.max_retries,
                               max_retries_on_500=api.max_retries_on_500, max_calls=20)
    try:
        names = fetch_group_names(client, processed / "psc_group_names.json")
    except Exception as exc:                      # names are labels only
        print(f"PSC group names unavailable ({exc}); codes are shown instead.")
        names = {}

    period = read_staging_period(processed)
    if period["partial"]:
        print(f"TEST EXTRACT – partial period {period['start_date']} .. {period['end_date']} "
              f"(fiscal months {period['fiscal_months']}); figures are NOT FY2026 totals.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    scope = ", ".join(a["name"] for a in market.agency_filters())
    parts, status_rows, order_parts, copy_ids = [], [], [], set()
    app_con = None
    if args.app_db:
        from src.webapp.build import open_db
        app_path = processed / "app_data.sqlite"
        app_con = open_db(app_path)
    group_ids: dict[str, set] = {}          # all-records transaction ids per PSC group

    def status(df, key, name, kind, group, note, mode, excluded):
        dup = df[df["potential_duplicate_group"].notna()]
        return {"market": key, "counting": mode, "market_name": name, "market_status": kind,
                "psc_group": group, "agency_scope": scope, "definition_note": note,
                "fy2026_obligations": round(float(df["transaction_amount"].sum()), 2),
                "actions": int(len(df)), "latest_action_date_in_data": str(df["transaction_date"].max()),
                "reporting_normal_through": facts.get("reporting_volume_normal_through"),
                "source_last_updated": facts.get("source_last_updated"),
                "potential_duplicate_groups": int(dup["potential_duplicate_group"].nunique()),
                "excluded_probable_copies": excluded, "generated_at": now,
                "scheduled_full_reload": settings.refresh.scheduled_full_reload.isoformat(),
                "period_start": period["start_date"], "period_end": period["end_date"],
                "partial_period": bool(period["partial"])}

    for mode, treatment in MODES.items():
        print(f"Computing {mode} ({buckets} recipient bucket(s)) …")
        with engine.connect() as con:
            r = transform_staged(con, [base], [spec], "snapshot", treatment,
                                 settings.transaction_counting.ignore_fields, buckets=buckets)
        if treatment == "exclude_flagged_copies":
            ex = r.excluded
            copy_ids.update(ex.loc[ex["exclusion_reason"] == "excluded_flagged_copy", "transaction_id"])
        df = flat_frame(r, spec.segment_id)
        del r
        gc.collect()
        if df.empty:
            continue
        df["psc_group"] = df["psc_code"].map(group_of)
        excl_ids = copy_ids if treatment == "exclude_flagged_copies" else set()
        parts.append(market_tables(df, base, mode))
        status_rows.append(status(df, base, market.name, "all", None,
                                  " ".join(market.definition_note.split()), mode, len(excl_ids)))
        if mode == "usaspending_basis":
            order_parts.append(orders(df, base))
            if app_con is not None:
                from src.webapp.build import write_orders
                print(f"  app database: {write_orders(app_con, df, desc_chars=args.app_desc_chars):,} orders written")
        totals = df.groupby("psc_group", dropna=False)["transaction_amount"].sum().sort_values(ascending=False)
        for g in totals.index:
            sub = df[df["psc_group"].isna()] if pd.isna(g) else df[df["psc_group"] == g]
            g = g if isinstance(g, str) else "none"
            key = f"{base}:{g}"
            t = market_tables(sub, key, mode, top_profiles=args.group_profiles)
            t["companies"] = t["companies"][t["companies"]["rank"] <= args.group_companies]
            parts.append(t)
            name = label(g, names) if g != "none" else "No valid PSC reported"
            note = (f"PSC group {g} ({kind_of(g) if g != 'none' else 'none'}) inside {market.name}: "
                    f"every FY2026 DLA contract action whose Product and Service Code belongs to this "
                    f"official group (USAspending PSC tree, tier 1). Not an industry definition.")
            if mode == "usaspending_basis":
                group_ids[g] = set(sub["transaction_id"])
            n_ex = len(group_ids.get(g, set()) & excl_ids) if excl_ids else 0
            status_rows.append(status(sub, key, name, "psc_group", g, note, mode, n_ex))
        print(f"  {len(totals)} PSC groups")
        del df
        gc.collect()

    tables = combine(parts)
    tables["data_status"] = pd.DataFrame(status_rows)
    if order_parts:
        od = pd.concat(order_parts, ignore_index=True)
        od["excluded_as_copy"] = od["transaction_id"].isin(copy_ids)
        tables["orders"] = od

    out = processed / "dashboard_snapshot"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, df in tables.items():
        num = df.select_dtypes("number").columns
        df[num] = df[num].round(4)
        df.to_csv(out / f"{name}.csv", index=False)
        print(f"  {name}.csv  {len(df):,} rows")
    archive = shutil.make_archive(str(out.parent / "dashboard_snapshot"), "zip", out)
    print(f"\nSend this file: {archive}")
    if app_con is not None:
        from src.webapp.build import finalize_orders, write_tables, zip_db
        print("Building the web-app database (search index) …")
        finalize_orders(app_con, copy_ids)
        write_tables(app_con, tables)
        app_con.execute("VACUUM")
        app_con.close()
        z = zip_db(app_path)
        print(f"Web-app data: {z} ({z.stat().st_size / 1e6:,.0f} MB) – upload it as a GitHub release "
              f"asset, see DEPLOY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
