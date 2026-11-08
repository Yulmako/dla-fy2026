#!/usr/bin/env python3
"""Load the staged FY2026 transactions into dims/facts (Phase 2b).

    python scripts/load_fy2026.py

Preconditions (the script refuses to run otherwise):
  * staging filled by scripts/phase2a_stage_and_profile.py
  * config/pipeline.yaml: transaction_counting.potential_duplicate_treatment != undecided
  * staged totals reconcile with the live API (spending_by_category) within tolerance

Reporting-lag status (banner dates) is taken from the latest Phase 1 probe report;
re-run scripts/phase1_api_probe.py first to refresh it.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.client import UsaSpendingClient  # noqa: E402
from src.api.filters import segment_filters  # noqa: E402
from src.config.market import definition_hash, load_market_config  # noqa: E402
from src.config.settings import load_settings  # noqa: E402
from src.database.engine import get_engine, init_db  # noqa: E402
from src.etl import runlog  # noqa: E402
from src.etl.load import load_snapshot  # noqa: E402
from src.etl.period import read_staging_period  # noqa: E402
from src.etl.partitioned import auto_buckets, transform_staged  # noqa: E402
from src.etl.staging import migrate_legacy_staging, staged_markets  # noqa: E402
from src.etl.transform import MarketSpec, TransformError, transform  # noqa: E402
from src.fiscal import parse_date  # noqa: E402


def api_total(client: UsaSpendingClient, category: str, filters: dict) -> float:
    total, page = 0.0, 1
    while True:
        r = client.post(f"/api/v2/search/spending_by_category/{category}/",
                        {"filters": filters, "limit": 100, "page": page},
                        label=f"recon_{category}_p{page}")
        if not r.ok:
            raise RuntimeError(f"reconciliation call failed: {r.error_detail()}")
        total += sum(float(x.get("amount") or 0) for x in r.data.get("results", []))
        if not (r.data.get("page_metadata") or {}).get("hasNext"):
            return round(total, 2)
        page += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-api-reconciliation", action="store_true",
                    help="load without the live control-total check (recorded in etl_log)")
    args = ap.parse_args(argv)
    period = read_staging_period(ROOT / "data" / "processed")
    if period["partial"]:
        print(f"Staging holds a partial TEST extract ({period['start_date']} .. {period['end_date']}). "
              "It is not loaded as FY2026 facts – re-run Phase 2a without --fiscal-months first.")
        return 2
    run_id = "load_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_market_config()
    key = cfg.active_market
    seg_ids = cfg.segment_ids()
    settings = load_settings()
    tc = settings.transaction_counting
    engine = get_engine()
    init_db(engine)

    probe_path = ROOT / "data" / "processed" / "phase1_validation_report_latest.json"
    probe = json.loads(probe_path.read_text()) if probe_path.exists() else {}
    facts = probe.get("api_facts", {})
    lag = probe.get("reporting_lag") or {}

    with engine.begin() as con:
        migrate_legacy_staging(con, key)
        runlog.start_run(con, run_id, "load", key, facts.get("source_last_updated"))
    try:
        with engine.connect() as con:
            staged_keys = [k for k in staged_markets(con) if k in cfg.markets]
            keys = cfg.loadable_keys(staged_keys)
            if not keys:
                raise TransformError("staging is empty – run scripts/phase2a_stage_and_profile.py")
            read_keys = [k for k in keys if k in staged_keys]
            buckets = auto_buckets(con, read_keys)
        specs = [MarketSpec(k, cfg.market(k), seg_ids[k], definition_hash(cfg.market(k)),
                            cfg.market(k).model_dump_json(indent=1)) for k in keys]
        logging.info("Markets: %s", [(s.key, s.segment_id, s.market.status) for s in specs])
        logging.info("Transform in %d recipient bucket(s)", buckets)
        with engine.connect() as con:
            result = transform_staged(con, read_keys, specs, run_id,
                                      tc.potential_duplicate_treatment, tc.ignore_fields,
                                      buckets=buckets, log=logging.info)
        st = result.stats
        logging.info("Transform: %s", {k: v for k, v in st.items() if k != "columns_used"})

        recon = {"skipped": True}
        if not args.skip_api_reconciliation:
            client = UsaSpendingClient(delay_seconds=settings.api.delay_seconds,
                                       max_calls=settings.api.max_calls)
            recon = {}
            for sp in specs:
                for basis in sp.market.bases():
                    f = segment_filters(sp.market, basis)
                    api = api_total(client, "awarding_subagency" if basis == "all" else basis, f)
                    ours = st["markets"][sp.key][f"{basis}_basis_obligations"]
                    rel = abs(ours - api) / abs(api) if api else 0.0
                    recon[f"{sp.key}/{basis}"] = {"api": api, "staged": ours,
                                                  "relative_difference": rel}
                    logging.info("Reconciliation %s/%s: staged %.2f vs API %.2f (%.4f%%)",
                                 sp.key, basis, ours, api, rel * 100)
            bad = {b: v for b, v in recon.items()
                   if v["relative_difference"] > settings.reconciliation.tolerance_ratio}
            if bad:
                raise RuntimeError(f"reconciliation outside tolerance: {bad}. Re-run Phase 2a "
                                   f"staging (data may have changed) before loading.")

        missing_months = facts.get("months_likely_missing_reporting")
        provisional = bool(missing_months) or not probe
        with engine.begin() as con:
            counts = load_snapshot(con, result, run_id)
        with engine.begin() as con:
            runlog.finish_run(
                con, run_id, "success",
                records_extracted=st["staged_rows"], records_inserted=st["included_transactions"],
                records_excluded=st["excluded_transactions"],
                data_through_date=st["data_through_date"],
                reporting_normal_through=parse_date(facts.get("reporting_volume_normal_through")),
                is_provisional=provisional,
                lag_assessment_json=json.dumps({"lag": lag, "reconciliation": recon,
                                                "transform_stats": st}, default=str),
                error_message="API reconciliation skipped" if recon.get("skipped") else None)
        print(f"Loaded: {counts}")
        print(f"Data through {st['data_through_date']}; provisional={provisional}; "
              f"duplicate treatment={tc.potential_duplicate_treatment}")
        return 0
    except TransformError as exc:
        logging.error("%s", exc)
        with engine.begin() as con:
            runlog.finish_run(con, run_id, "failed", error_message=str(exc))
        return 2
    except Exception as exc:
        logging.exception("Load failed – previous snapshot left unchanged")
        with engine.begin() as con:
            runlog.finish_run(con, run_id, "failed", error_message=str(exc)[:2000])
        return 1


if __name__ == "__main__":
    sys.exit(main())
