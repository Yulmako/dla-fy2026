"""Cached data access for the dashboard. Reads only the loaded FY2026 snapshot
(v_market_transactions_fy2026) – never staging, never synthetic data."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from src.config.market import load_market_config
from src.config.settings import load_settings
from src.database.engine import get_engine, init_db


@st.cache_resource
def engine():
    eng = get_engine()
    init_db(eng)          # idempotent: tables + views exist even before the first load
    return eng


@st.cache_data(ttl=600)
def segments() -> pd.DataFrame:
    """Loaded market segments (one per market in the latest snapshot)."""
    return pd.read_sql("SELECT segment_id, market_key, name, status FROM market_segments "
                       "ORDER BY segment_id", engine())


@st.cache_data(ttl=600, show_spinner="Loading FY2026 data…")
def market_frame(segment_id: int | None) -> pd.DataFrame:
    """Transactions of ONE segment – a transaction can belong to several segments,
    so frames are never mixed across segments."""
    if segment_id is None:
        return pd.read_sql("SELECT * FROM v_market_transactions_fy2026 WHERE 1 = 0", engine())
    df = pd.read_sql(f"SELECT * FROM v_market_transactions_fy2026 "
                     f"WHERE segment_id = {int(segment_id)}", engine())
    if not df.empty:
        df["transaction_date"] = pd.to_datetime(df["transaction_date"]).dt.date
        df["parent_uei_conflict"] = df["parent_uei_conflict"].fillna(0).astype(bool)
    return df


@st.cache_data(ttl=600)
def meta(market_key: str | None = None) -> dict:
    settings = load_settings()
    cfg = load_market_config()
    market = cfg.market(market_key if market_key in cfg.markets else None)
    m = {"banner_text": settings.refresh.banner_text,
         "scheduled_full_reload": settings.refresh.scheduled_full_reload.isoformat(),
         "dod_lag_days": settings.reporting_lag.dod_reporting_lag_days,
         "duplicate_treatment": settings.transaction_counting.potential_duplicate_treatment,
         "market_name": market.name, "market_status": market.status,
         "definition_note": market.definition_note, "load": None, "lag": {}}
    runs = pd.read_sql("SELECT * FROM etl_log WHERE run_type = 'load' AND status = 'success' "
                       "ORDER BY finished_at DESC LIMIT 1", engine())
    if not runs.empty:
        r = runs.iloc[0].to_dict()
        m["load"] = r
        try:
            m["lag"] = json.loads(r.get("lag_assessment_json") or "{}")
        except ValueError:
            m["lag"] = {}
    return m


@st.cache_data(ttl=600)
def etl_history() -> pd.DataFrame:
    return pd.read_sql("SELECT run_id, run_type, status, started_at, finished_at, "
                       "records_extracted, records_inserted, records_excluded, data_through_date, "
                       "is_provisional, error_message FROM etl_log ORDER BY started_at DESC LIMIT 15",
                       engine())


@st.cache_data(ttl=600)
def dq_counts() -> pd.DataFrame:
    return pd.read_sql("SELECT check_name, severity, COUNT(*) AS issues FROM dq_issues "
                       "WHERE run_id = (SELECT run_id FROM etl_log WHERE run_type='load' AND "
                       "status='success' ORDER BY finished_at DESC LIMIT 1) "
                       "GROUP BY check_name, severity ORDER BY issues DESC", engine())


def clear_caches() -> None:
    segments.clear()
    market_frame.clear()
    meta.clear()
    etl_history.clear()
    dq_counts.clear()
