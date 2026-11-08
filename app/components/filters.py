"""Global sidebar filters (persist across pages via session_state). The
reporting period is fixed to FY2026 – there is deliberately no year control."""
from __future__ import annotations

import pandas as pd
import streamlit as st

ENTITY_OPTIONS = {"Company (reported parent UEI)": "company", "Recipient UEI": "recipient"}


def _opts(df: pd.DataFrame, code: str, label: str | None = None) -> dict[str, str]:
    cols = [code] + ([label] if label else [])
    u = df[cols].dropna(subset=[code]).drop_duplicates(code).sort_values(code)
    return {(f"{r[code]} – {str(r[label])[:50]}" if label else str(r[code])): r[code]
            for _, r in u.iterrows()}


def sidebar(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    st.sidebar.markdown("**Reporting period: FY2026**  \nOct 1, 2025 – Sep 30, 2026 (fixed)")
    entity_label = st.sidebar.radio("Rank by", list(ENTITY_OPTIONS), key="f_entity")
    entity = ENTITY_OPTIONS[entity_label]
    if df.empty:
        return df, entity
    st.sidebar.markdown("### Filters")
    specs = [
        ("f_tier", "Market tier", "match_tier", None),
        ("f_agency", "Awarding sub-agency", "awarding_sub_agency", None),
        ("f_naics", "NAICS", "naics_code", "naics_description"),
        ("f_psc", "PSC", "psc_code", "psc_description"),
        ("f_type", "Award type", "award_type", None),
        ("f_country", "Recipient country", "recipient_country", None),
    ]
    out = df
    for key, title, col, label in specs:
        opts = _opts(df, col, label)
        chosen = st.sidebar.multiselect(title, list(opts), key=key)
        if chosen:
            out = out[out[col].isin([opts[c] for c in chosen])]
    if st.sidebar.button("Clear filters"):
        for key, *_ in specs:
            st.session_state.pop(key, None)
        st.rerun()
    st.sidebar.caption(f"{len(out):,} of {len(df):,} FY2026 actions selected")
    return out, entity
