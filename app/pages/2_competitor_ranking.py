import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from app.components import layout  # noqa: E402
from app.components.format import PARENT_STATUS_LABEL  # noqa: E402
from app.pages._common import bootstrap  # noqa: E402
from src.analytics import metrics as M  # noqa: E402

df, df_all, entity, meta = bootstrap("Competitor Ranking")
ranking = M.entity_summary(df, entity)
st.caption("Sorted by FY2026 obligations (provisional). Click a column header to re-sort. "
           "Market share = share of the selected federal procurement dataset after filters.")

cols = ["rank", "name", "uei", "fy2026_obligations", "award_count", "avg_positive_award_fy2026",
        "median_positive_award_fy2026", "agency_count", "share_of_selected_dataset_pct",
        "positive_net_award_count", "zero_net_award_count", "negative_net_award_count"]
if entity == "company":
    cols += ["parent_status", "recipient_uei_count"]
view = ranking[cols].copy()
if entity == "company":
    view["parent_status"] = view["parent_status"].map(PARENT_STATUS_LABEL)
st.dataframe(
    view, use_container_width=True, hide_index=True, height=560,
    column_config={
        "name": st.column_config.TextColumn(layout.entity_label(entity)),
        "uei": st.column_config.TextColumn("UEI" if entity == "recipient" else "Company UEI (reported parent or own)"),
        "fy2026_obligations": st.column_config.NumberColumn("FY2026 obligations", format="dollar"),
        "award_count": st.column_config.NumberColumn("FY2026 awards with activity"),
        "avg_positive_award_fy2026": st.column_config.NumberColumn(
            "Avg FY2026 obligation per award (positive-net)", format="dollar"),
        "median_positive_award_fy2026": st.column_config.NumberColumn(
            "Median FY2026 obligation per award (positive-net)", format="dollar"),
        "agency_count": st.column_config.NumberColumn("Awarding agencies"),
        "share_of_selected_dataset_pct": st.column_config.NumberColumn(
            "Share of selected dataset (provisional)", format="%.2f%%"),
        "positive_net_award_count": st.column_config.NumberColumn("Positive-net awards"),
        "zero_net_award_count": st.column_config.NumberColumn("Zero-net awards"),
        "negative_net_award_count": st.column_config.NumberColumn("Negative-net awards (net de-obligation)"),
        "parent_status": st.column_config.TextColumn("Parent identification"),
        "recipient_uei_count": st.column_config.NumberColumn("Recipient UEIs"),
    })
layout.csv_button(view, f"ranking_{entity}")

st.subheader("Open a competitor profile")
options = dict(zip(ranking["rank"].astype(str) + ". " + ranking["name"].astype(str), ranking[
    "company_id" if entity == "company" else "recipient_id"]))
pick = st.selectbox("Competitor", list(options), index=0)
if st.button("Open profile", type="primary"):
    st.session_state["profile_entity"] = (entity, options[pick])
    st.switch_page("pages/3_competitor_profile.py")
layout.footer()
