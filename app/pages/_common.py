"""Shared page bootstrap: header + banner, data, global filters, empty states."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.components import data, filters, layout  # noqa: E402


def select_segment() -> tuple[int | None, str | None]:
    """Sidebar market selector; the choice persists across pages."""
    segs = data.segments()
    if segs.empty:
        return None, None
    labels = {f"{r['name']}" + (" (candidate)" if r["status"] == "candidate" else ""): (
        int(r["segment_id"]), r["market_key"]) for _, r in segs.iterrows()}
    choice = st.sidebar.selectbox("Market segment", list(labels), key="f_segment")
    return labels[choice]


def bootstrap(title: str):
    segment_id, market_key = select_segment()
    meta = data.meta(market_key)
    layout.header(title, meta)
    df_all = data.market_frame(segment_id)
    df, entity = filters.sidebar(df_all)
    if df_all.empty:
        layout.empty_state()
    if df.empty:
        st.info("No FY2026 actions match the current filters.")
        layout.footer()
        st.stop()
    return df, df_all, entity, meta
