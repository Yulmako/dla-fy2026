"""Federal Competitor Intelligence – FY2026 (Streamlit entry point).

    streamlit run app/dashboard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Federal Competitor Intelligence – FY2026", page_icon="📊",
                   layout="wide")

pages = [
    st.Page("pages/1_market_overview.py", title="Market Overview", icon="🏛️", default=True),
    st.Page("pages/2_competitor_ranking.py", title="Competitor Ranking", icon="🏆"),
    st.Page("pages/3_competitor_profile.py", title="Competitor Profile", icon="🏢"),
    st.Page("pages/4_agency_intelligence.py", title="Agency Intelligence", icon="🏦"),
    st.Page("pages/5_market_segmentation.py", title="Market Segmentation", icon="🧩"),
    st.Page("pages/6_methodology.py", title="Data Source & Methodology", icon="📘"),
]
st.navigation(pages).run()
