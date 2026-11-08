"""Display formatting (USD, %, labels)."""
from __future__ import annotations

import math

import pandas as pd


def usd(v, compact: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
        return "–"
    v = float(v)
    if not compact:
        return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
    a = abs(v)
    sign = "-" if v < 0 else ""
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:,.2f}{suf}"
    return f"{sign}${a:,.0f}"


def pct(v, digits: int = 1) -> str:
    return "–" if v is None or pd.isna(v) else f"{float(v):.{digits}f}%"


PARENT_STATUS_LABEL = {
    "reported_parent": "Reported parent UEI",
    "no_parent_reported": "No reported parent UEI – shown as its own company",
    "no_uei": "No UEI – identity unverified",
}
