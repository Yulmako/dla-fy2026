"""Page chrome: header with fixed FY2026 period, provisional banner, KPI row,
CSV export, empty state and footer disclaimer."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.components.format import usd


def banner(meta: dict) -> None:
    load = meta.get("load") or {}
    provisional = bool(load.get("is_provisional", True))
    if not provisional:
        return
    normal = load.get("reporting_normal_through") or "n/a"
    latest = load.get("data_through_date") or "n/a"
    refreshed = str(load.get("finished_at") or "n/a")[:16].replace("T", " ")
    st.warning(
        f"**{meta['banner_text']}**  \n"
        f"Reporting volume normal through: **{normal}** · "
        f"Latest action date in loaded data: **{latest}** · "
        f"Last successful data refresh: **{refreshed} UTC**  \n"
        f"DoD contract actions appear in FPDS/USAspending about {meta['dod_lag_days']} days after "
        f"they occur, so late-FY2026 DoD months are still incomplete. **All FY2026 rankings and "
        f"shares are provisional.** Missing obligations are not estimated or extrapolated. "
        f"Full reload scheduled: {meta['scheduled_full_reload']}.",
        icon="⚠️")


def header(title: str, meta: dict) -> None:
    st.title(title)
    st.caption(f"**FY2026** (Oct 1, 2025 – Sep 30, 2026) · {meta['market_name']} – "
               f"working market segment · Source: USAspending.gov public API · "
               f"Federal prime contract obligations (types A–D)")
    banner(meta)


def empty_state() -> None:
    st.info("No FY2026 data is loaded yet. Run `scripts/phase2a_stage_and_profile.py`, set the "
            "duplicate treatment in `config/pipeline.yaml`, then run `scripts/load_fy2026.py`. "
            "The dashboard never shows demo or estimated data.")
    footer()
    st.stop()


def kpis(items: list[tuple[str, str, str | None]]) -> None:
    cols = st.columns(len(items))
    for c, (label, value, help_) in zip(cols, items):
        c.metric(label, value, help=help_)


def csv_button(df: pd.DataFrame, name: str, label: str = "Download CSV") -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8"),
                       file_name=f"fy2026_{name}.csv", mime="text/csv", key=f"dl_{name}")


def money_cols(cols: list[str]) -> dict:
    return {c: st.column_config.NumberColumn(c.replace("_", " "), format="dollar") for c in cols}


def footer() -> None:
    st.divider()
    st.caption("Federal contract obligations are not company revenue or profit. Shares describe "
               "the selected federal procurement dataset, not the commercial market. "
               "Lifetime award values (ceiling, cumulative obligation) are never counted as "
               "FY2026 obligations.")


def entity_label(entity: str) -> str:
    return "Company" if entity == "company" else "Recipient (UEI)"


__all__ = ["banner", "header", "empty_state", "kpis", "csv_button", "money_cols", "footer",
           "entity_label", "usd"]
