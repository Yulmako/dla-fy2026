import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from app.components import charts, layout  # noqa: E402
from app.components.format import pct, usd  # noqa: E402
from app.pages._common import bootstrap  # noqa: E402
from src.analytics import metrics as M  # noqa: E402

df, df_all, entity, meta = bootstrap("Agency Intelligence")
eid = "company_id" if entity == "company" else "recipient_id"
ag = M.obligations_by(df, "awarding_sub_agency", entity=entity)
st.plotly_chart(charts.hbar(ag, "fy2026_obligations", "awarding_sub_agency",
                            "FY2026 obligations by awarding agency", n=15,
                            hover=["supplier_count", "top_supplier", "top5_supplier_share_pct"]),
                use_container_width=True)
st.markdown("**Supplier reliance by agency** – how concentrated each agency's FY2026 obligations "
            "are among its suppliers")
st.dataframe(ag, use_container_width=True, hide_index=True, column_config={
    **layout.money_cols(["fy2026_obligations"]),
    "share_of_selected_dataset_pct": st.column_config.NumberColumn("Share of selected dataset", format="%.2f%%"),
    "top5_supplier_share_pct": st.column_config.NumberColumn("Top-5 supplier share", format="%.1f%%"),
    "supplier_hhi": st.column_config.NumberColumn("Supplier HHI", format="%.0f")})
layout.csv_button(ag, "agencies")

st.divider()
agency = st.selectbox("Drill into agency", ag["awarding_sub_agency"].tolist())
sub = df[df["awarding_sub_agency"] == agency]
k = M.market_kpis(sub, entity)
layout.kpis([
    ("FY2026 obligations", usd(k["fy2026_obligations"]), None),
    ("Suppliers", f"{k['company_count'] if entity == 'company' else k['recipient_count']:,}", None),
    ("Top-5 supplier concentration", pct(k["cr5_pct"]), None),
    ("Supplier HHI", f"{k['hhi']:,.0f}" if k["hhi"] is not None else "–", None),
])
c1, c2 = st.columns(2)
top = M.entity_summary(sub, entity)
with c1:
    st.plotly_chart(charts.hbar(top, "fy2026_obligations", "name", f"Top suppliers – {agency}",
                                hover=["share_of_selected_dataset_pct"]), use_container_width=True)
with c2:
    cat = M.obligations_by(sub, "psc_code", "psc_description", entity=entity)
    cat["label"] = cat["psc_code"].astype(str) + " " + cat["psc_description"].fillna("").str.slice(0, 40)
    st.plotly_chart(charts.hbar(cat, "fy2026_obligations", "label", "Category breakdown (PSC)",
                                hover=["supplier_count", "top_supplier"]), use_container_width=True)

st.markdown("**Competitor ranking within this agency** and each competitor's exposure to it "
            "(share of the competitor's total FY2026 obligations in the selected dataset that "
            "comes from this agency)")
totals = df.groupby(eid)["transaction_amount"].sum()
top["exposure_to_agency_pct"] = top[eid].map(
    lambda i: (sub.loc[sub[eid] == i, "transaction_amount"].sum() / totals[i] * 100) if totals.get(i) else None)
view = top[["rank", "name", "uei", "fy2026_obligations", "share_of_selected_dataset_pct",
            "award_count", "exposure_to_agency_pct"]]
st.dataframe(view, use_container_width=True, hide_index=True, column_config={
    **layout.money_cols(["fy2026_obligations"]),
    "share_of_selected_dataset_pct": st.column_config.NumberColumn("Share within agency", format="%.2f%%"),
    "exposure_to_agency_pct": st.column_config.NumberColumn("Competitor exposure to agency", format="%.1f%%")})
layout.csv_button(view, "agency_ranking")
layout.footer()
