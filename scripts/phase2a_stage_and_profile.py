#!/usr/bin/env python3
"""Phase 2a – download FY2026 market transactions into STAGING only and profile
potential source duplicates per market.

    python scripts/phase2a_stage_and_profile.py                 # all non-planned markets
    python scripts/phase2a_stage_and_profile.py --market logistics_supply
    python scripts/phase2a_stage_and_profile.py --profile-only  # re-profile staging, no download

* Per market: NAICS-basis and PSC-basis download jobs (split by fiscal month if a
  job exceeds the 500k-row limit). All jobs are submitted first, so USAspending
  builds them in parallel.
* Raw zips: data/raw/phase2a/<run_id>/ (SHA-256 in raw_download_files).
* Staging: stg_prime_transactions (kept columns only). NOT the fact tables.
* Profiles: data/processed/phase2a_duplicate_profile_<market>_latest.{md,json}.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.client import BASE_URL, UsaSpendingClient  # noqa: E402
from src.api.filters import segment_filters  # noqa: E402
from src.config.market import load_market_config  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.database.engine import get_engine, init_db  # noqa: E402
from src.etl import runlog  # noqa: E402
from src.etl.download import plan_jobs, run_jobs, split_plan  # noqa: E402
from src.etl.period import parse_months, write_staging_period  # noqa: E402
from src.etl.staging import (migrate_legacy_staging, read_for_profile, reset_markets,  # noqa: E402
                             stage_csvs, staged_markets)
from src.quality.duplicates import profile_potential_duplicates, render_markdown  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", action="append", help="market key (repeatable); default: all non-planned")
    ap.add_argument("--profile-only", action="store_true")
    ap.add_argument("--poll-seconds", type=float, default=15)
    ap.add_argument("--fiscal-months", metavar="A-B",
                    help="TEST EXTRACT: only these FY2026 fiscal months, e.g. 9-12 (= Jun–Sep 2026). "
                         "Results are labelled partial and cannot be loaded as FY2026 facts.")
    ap.add_argument("--refresh-months", metavar="A-B",
                    help="Re-download only these fiscal months (e.g. 6 = March 2026) and replace "
                         "just their staged rows; the rest of the staged year is kept. Each month "
                         "is requested in two halves so USAspending builds new files.")
    args = ap.parse_args(argv)
    if args.fiscal_months and args.refresh_months:
        ap.error("use either --fiscal-months or --refresh-months")
    refresh = parse_months(args.refresh_months) if args.refresh_months else None
    months = parse_months(args.fiscal_months) if args.fiscal_months else refresh

    run_id = "stage_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / f"{run_id}.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    cfg = load_market_config()
    keys = args.market or cfg.active_keys()
    downloads = cfg.download_keys(keys)
    for k in keys:
        if k not in downloads:
            print(f"{k}: no own download – derived from {cfg.covering(k, keys)}")
    settings = load_settings()
    engine = get_engine()
    init_db(engine)
    with engine.begin() as con:
        if migrate_legacy_staging(con, cfg.active_market):
            logging.info("Upgraded existing staging table (rows tagged %s)", cfg.active_market)
        runlog.start_run(con, run_id, "stage", ",".join(keys))
    try:
        total_rows = 0
        if not args.profile_only:
            raw_dir = ROOT / "data" / "raw" / "phase2a" / run_id
            api = settings.api
            client = UsaSpendingClient(base_url=BASE_URL, delay_seconds=api.delay_seconds,
                                       timeout_seconds=api.timeout_seconds, max_retries=api.max_retries,
                                       max_retries_on_500=api.max_retries_on_500,
                                       max_calls=api.max_calls, raw_dir=raw_dir / "api")
            plans = []
            for key in downloads:
                for basis in cfg.market(key).bases():
                    plans += plan_jobs(client, segment_filters(cfg.market(key), basis), basis, key,
                                       fiscal_months=months, halves=bool(refresh))
            logging.info("Download plan: %s", [(p.label, p.expected_rows) for p in plans])
            files, incomplete = [], []
            pending, round_no = plans, 0
            while pending:
                results = run_jobs(client, pending, raw_dir, poll_seconds=args.poll_seconds)
                retry: list = []
                with engine.begin() as con:
                    if round_no == 0 and refresh:
                        # only the refreshed months are replaced (by action date)
                        from src.fiscal import fiscal_month_range
                        for fm in refresh:
                            a, b = fiscal_month_range(fm)
                            runlog.execute(con, f"DELETE FROM stg_prime_transactions WHERE _market IN "
                                                f"({', '.join(runlog.literal(k) for k in downloads)}) "
                                                f"AND substr(action_date, 1, 10) BETWEEN '{a}' AND '{b}'")
                        logging.info("Replacing staged rows of fiscal months %s only", refresh)
                    elif round_no == 0:
                        # covered markets' old rows are dropped too: they are rebuilt from the base download
                        reset_markets(con, keys)
                    for res in results:
                        market = res.status_payload["market"]
                        label = res.status_payload["label"]
                        n = stage_csvs(con, res.csv_paths, res.basis, run_id, market)
                        logging.info("%s: %d rows staged (API expected %s)", label, n, res.expected_rows)
                        short = bool(res.expected_rows) and n < res.expected_rows * 0.999
                        subs = split_plan(client, res.plan) if short and round_no < 8 else []
                        if short and subs:
                            # USAspending cut this file short: drop its rows, fetch the range in halves
                            names = ", ".join(runlog.literal(p.name) for p in res.csv_paths)
                            runlog.execute(con, f"DELETE FROM stg_prime_transactions WHERE _run_id = "
                                                f"{runlog.literal(run_id)} AND _source_file IN ({names})")
                            logging.warning("%s truncated by the server (%d of %s rows) – retrying as %s",
                                            label, n, res.expected_rows, [x.label for x in subs])
                            retry += subs
                            continue
                        total_rows += n
                        if short:
                            msg = (f"INCOMPLETE {label}: {n} rows read vs {res.expected_rows} expected; "
                                   f"CSV files: {', '.join(p.name for p in res.csv_paths)}")
                            logging.error(msg)
                            incomplete.append(msg)
                        files.append({"run_id": run_id, "market_key": market, "basis": res.basis,
                                      "job_file_name": res.file_name, "file_url": res.file_url,
                                      "zip_path": str(res.zip_path.relative_to(ROOT)),
                                      "sha256": res.sha256, "bytes": res.bytes,
                                      "expected_rows": res.expected_rows, "rows_read": n,
                                      "downloaded_at": runlog.now()})
                pending, round_no = retry, round_no + 1
            # file lineage in its own transaction: a logging problem never undoes staging
            try:
                with engine.begin() as con:
                    # USAspending may hand back the same generated file to a repeat request;
                    # older databases allow each file name once, so keep the newest record
                    for f in files:
                        runlog.execute(con, "DELETE FROM raw_download_files WHERE job_file_name = "
                                            + runlog.literal(f["job_file_name"]))
                    pd.DataFrame(files).to_sql("raw_download_files", con, if_exists="append",
                                               index=False)
            except Exception as exc:
                logging.warning("Could not record raw_download_files (%s); staging is kept", exc)
            if not refresh:
                write_staging_period(ROOT / "data" / "processed", run_id, months)
            if incomplete:
                print("\n!! Some downloads look incomplete – re-run those months with "
                      "--refresh-months:\n  " + "\n  ".join(incomplete))

        out = ROOT / "data" / "processed"
        out.mkdir(parents=True, exist_ok=True)
        with engine.connect() as con:
            available = staged_markets(con)
            for key in [k for k in downloads if k in available]:
                staged_keys, detail = read_for_profile(con, key)
                profile = profile_potential_duplicates(detail, keys=staged_keys)
                profile.update({"run_id": run_id, "market": key})
                jpath = out / f"phase2a_duplicate_profile_{key}_{run_id}.json"
                mpath = out / f"phase2a_duplicate_profile_{key}_{run_id}.md"
                jpath.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
                mpath.write_text(render_markdown(profile, f"{key} / {run_id}"), encoding="utf-8")
                shutil.copyfile(jpath, out / f"phase2a_duplicate_profile_{key}_latest.json")
                shutil.copyfile(mpath, out / f"phase2a_duplicate_profile_{key}_latest.md")
                s = profile["summary"]
                print(f"\n[{key}] staged {s['records_in_download_union']} rows "
                      f"({s['unique_transaction_ids']} unique IDs); suspicious groups "
                      f"{s['suspicious_groups_nonzero_amount']}; potential duplicate amount "
                      f"${s['potential_duplicate_amount_net']:,.2f}\n  report: {mpath}")
        with engine.begin() as con:
            runlog.finish_run(con, run_id, "success", records_extracted=total_rows)
        return 0
    except Exception as exc:
        logging.exception("Phase 2a failed")
        with engine.begin() as con:
            runlog.finish_run(con, run_id, "failed", error_message=str(exc)[:2000])
        return 1


if __name__ == "__main__":
    sys.exit(main())
