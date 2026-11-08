"""Fixed reporting period for the whole application: FY2026 only.

Every module that filters, validates or labels dates imports from here, so the
reporting window is defined in exactly one place and cannot drift.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

FISCAL_YEAR: int = 2026
FY_START: date = date(2025, 10, 1)
FY_END: date = date(2026, 9, 30)
FY_LABEL: str = "FY2026 (October 1, 2025 – September 30, 2026)"

# USAspending award type codes for prime contracts (definitive contracts, purchase
# orders, delivery orders, BPA calls). IDVs are excluded from obligation sums
# because obligations are recorded on the orders placed under them.
CONTRACT_AWARD_TYPES: tuple[str, ...] = ("A", "B", "C", "D")
IDV_AWARD_TYPES: tuple[str, ...] = (
    "IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E",
)


def parse_date(value: Any) -> date | None:
    """Parse the date formats USAspending returns. Returns None if unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def federal_fiscal_year(d: date) -> int:
    """US federal fiscal year: Oct 1 starts the next year's FY."""
    return d.year + 1 if d.month >= 10 else d.year


def in_fy2026(value: Any) -> bool:
    d = parse_date(value)
    return d is not None and FY_START <= d <= FY_END


def time_period_filter(date_type: str | None = None) -> list[dict[str, str]]:
    """USAspending `time_period` filter for the fixed FY2026 window."""
    period: dict[str, str] = {
        "start_date": FY_START.isoformat(),
        "end_date": FY_END.isoformat(),
    }
    if date_type:
        period["date_type"] = date_type
    return [period]


def fiscal_month_range(fiscal_month: int) -> tuple[date, date]:
    """Calendar date range of FY2026 fiscal month 1..12 (1 = October 2025)."""
    import calendar
    if not 1 <= fiscal_month <= 12:
        raise ValueError(f"fiscal month must be 1..12, got {fiscal_month}")
    month = (fiscal_month + 8) % 12 + 1
    year = FY_START.year if month >= 10 else FY_END.year
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def calendar_to_fiscal_month(month: int) -> int:
    return (month - 10) % 12 + 1
