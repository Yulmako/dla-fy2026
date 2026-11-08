"""Which part of FY2026 is currently staged (full year or a partial test extract).

Written by Phase 2a, read by the snapshot export (labels the data) and by the
fact load (refuses partial extracts – they are not FY2026 totals)."""
from __future__ import annotations

import json
from pathlib import Path

from src.fiscal import FY_END, FY_START, fiscal_month_range

FILE = "staging_period.json"


def write_staging_period(out_dir: Path, run_id: str, months: list[int] | None) -> dict:
    months = months or list(range(1, 13))
    info = {"run_id": run_id, "fiscal_months": months,
            "start_date": fiscal_month_range(min(months))[0].isoformat(),
            "end_date": fiscal_month_range(max(months))[1].isoformat(),
            "partial": months != list(range(1, 13))}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def read_staging_period(out_dir: Path) -> dict:
    p = out_dir / FILE
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"fiscal_months": list(range(1, 13)), "start_date": FY_START.isoformat(),
            "end_date": FY_END.isoformat(), "partial": False}


def parse_months(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        a, _, b = part.strip().partition("-")
        out += list(range(int(a), int(b or a) + 1))
    if not out or any(not 1 <= m <= 12 for m in out):
        raise SystemExit(f"--fiscal-months must be within 1..12 (1 = Oct 2025), got {text!r}")
    return sorted(set(out))
