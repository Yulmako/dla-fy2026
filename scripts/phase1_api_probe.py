#!/usr/bin/env python3
"""Phase 1 USAspending API validation – single command.

    python scripts/phase1_api_probe.py

Reads config/market_config.yaml (active market), runs bounded read-only checks
against the public USAspending API for FY2026 and writes:

    data/processed/phase1_validation_report_<run_id>.json
    data/processed/phase1_validation_report_<run_id>.md
    data/processed/phase1_validation_report_latest.{json,md}
    data/raw/phase1/<run_id>/NNNN_*.json   (every raw request/response)
    logs/phase1_probe_<run_id>.log

No credentials or API keys are used. The full dataset is NOT downloaded.
Exit code: 0 = no FAIL checks, 1 = at least one FAIL, 2 = could not run.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.api.client import BASE_URL, UsaSpendingClient  # noqa: E402
from src.api.probe import Phase1Probe, render_markdown  # noqa: E402
from src.config.market import DEFAULT_CONFIG_PATH, load_market_config  # noqa: E402
from src.config.settings import DEFAULT_SETTINGS_PATH, load_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--market", default=None, help="market key (default: active_market)")
    ap.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH))
    ap.add_argument("--max-calls", type=int, default=None, help="override api.max_calls")
    ap.add_argument("--delay", type=float, default=None, help="override api.delay_seconds")
    ap.add_argument("--scan-pages", type=int, default=40,
                    help="max pages (x100 codes) when scanning NAICS/PSC activity")
    ap.add_argument("--base-url", default=BASE_URL)
    args = ap.parse_args(argv)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (ROOT / "logs").mkdir(exist_ok=True)
    log_file = ROOT / "logs" / f"phase1_probe_{run_id}.log"
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])

    try:
        cfg = load_market_config(args.config)
        settings = load_settings(args.settings)
        key = args.market or cfg.active_market
        market = cfg.market(key)
    except Exception as exc:
        logging.error("Invalid market config: %s", exc)
        return 2

    raw_dir = ROOT / "data" / "raw" / "phase1" / run_id
    api = settings.api
    client = UsaSpendingClient(base_url=args.base_url,
                               delay_seconds=args.delay if args.delay is not None else api.delay_seconds,
                               timeout_seconds=api.timeout_seconds, max_retries=api.max_retries,
                               max_retries_on_500=api.max_retries_on_500,
                               max_calls=args.max_calls or api.max_calls, raw_dir=raw_dir)
    logging.info("Phase 1 probe %s – market %s – raw responses in %s", run_id, key, raw_dir)
    report = Phase1Probe(client, key, market, scan_max_pages=args.scan_pages,
                         settings=settings).run()
    report["run"]["run_id"] = run_id
    report["run"]["raw_response_dir"] = str(raw_dir.relative_to(ROOT))
    report["run"]["log_file"] = str(log_file.relative_to(ROOT))

    out_dir = ROOT / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / f"phase1_validation_report_{run_id}.json"
    mpath = out_dir / f"phase1_validation_report_{run_id}.md"
    jpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    mpath.write_text(render_markdown(report), encoding="utf-8")
    shutil.copyfile(jpath, out_dir / "phase1_validation_report_latest.json")
    shutil.copyfile(mpath, out_dir / "phase1_validation_report_latest.md")

    s = report["summary"]
    if report.get("duplicate_inspection"):
        dpath = out_dir / f"phase1_duplicate_inspection_{run_id}.json"
        dpath.write_text(json.dumps(report["duplicate_inspection"], indent=2, default=str),
                         encoding="utf-8")
        shutil.copyfile(dpath, out_dir / "phase1_duplicate_inspection_latest.json")
    print(f"\nPASS {s['PASS']}  WARN {s['WARN']}  FAIL {s['FAIL']}  SKIP {s['SKIP']}")
    print(f"Report: {mpath}\n        {jpath}")
    return 1 if s["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
