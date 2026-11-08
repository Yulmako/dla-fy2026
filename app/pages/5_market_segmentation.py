import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from app.components import charts, layout  # noqa: E402
from app.components.format import pct, usd  # noqa: E402
from app.pages._common import bootstrap  # noqa: E402
from src.analytics import metrics as M  # noqa: E402

df, df_all, entity, meta = bootstrap("Market Segmentation")
eid = "company_id" if entity == "company" else "recipient_id"
DIMS = {"NAICS": ("naics_code", "naics_description"), "PSC": ("psc_code", "psc_description"),
        "Awarding agency": ("awarding_sub_agency", None), "Award type": ("award_type", None),
        "Market tier": ("match_tier", None)}
c1, c2 = st.columns([1, 3])
dim_name = c1.radio("Segment by", list(DIMS))
col, lab = DIMS[dim_name]
seg = M.obligations_by(df, col, lab, entity=entity)
seg["label"] = seg[col].astype(str) + ((" " + seg[lab].fillna("").str.slice(0, 50)) if lab else "")
with c2:
    st.plotly_chart(charts.hbar(seg, "fy2026_obligations", "label", f"FY2026 obligations by {dim_name}",
                                n=15, hover=["supplier_count", "top_supplier"]), use_container_width=True)

picked = st.multiselect(f"Select {dim_name} segment(s)", seg["label"].tolist(),
                        default=seg["label"].head(1).tolist())
keys = seg.loc[seg["label"].isin(picked), col].tolist()
sub = df[df[col].isin(keys)] if keys else df.iloc[0:0]
if sub.empty:
    st.info("Select at least one segment.")
    layout.footer()
    st.stop()
k = M.market_kpis(sub, entity)
layout.kpis([
    ("Segment FY2026 obligations", usd(k["fy2026_obligations"]), None),
    ("Share of selected dataset", pct(k["fy2026_obligations"] / df["transaction_amount"].sum() * 100), None),
    ("Suppliers", f"{k['company_count'] if entity == 'company' else k['recipient_count']:,}", None),
    ("Top-5 concentration", pct(k["cr5_pct"]), None),
])
lead = M.entity_summary(sub, entity)
st.plotly_chart(charts.hbar(lead, "fy2026_obligations", "name", "Leading suppliers in segment",
                            hover=["share_of_selected_dataset_pct", "award_count"]),
                use_container_width=True)
view = lead[["rank", "name", "uei", "fy2026_obligations", "share_of_selected_dataset_pct",
             "award_count", "agency_count", "psc_count"]]
st.dataframe(view, use_container_width=True, hide_index=True, column_config={
    **layout.money_cols(["fy2026_obligations"]),
    "share_of_selected_dataset_pct": st.column_config.NumberColumn("Share of segment", format="%.2f%%")})
layout.csv_button(view, "segment_leaders")

st.subheader("Competitor overlap by category")
st.caption("Overlap = Σ over PSC categories of min(share A, share B) of each competitor's positive "
           "FY2026 obligations. 100 = identical category mix, 0 = no shared category.")
top_ids = M.entity_summary(df, entity)[eid].head(10).tolist()
names = dict(zip(df[eid], df["company_name" if entity == "company" else "recipient_name"]))
chosen = st.multiselect("Competitors", top_ids, default=top_ids[:6],
                        format_func=lambda i: str(names.get(i))[:40])
if len(chosen) >= 2:
    mat = M.category_overlap(df, chosen, "psc_code", entity)
    mat.index = [str(names.get(i))[:30] for i in mat.index]
    mat.columns = [str(names.get(i))[:30] for i in mat.columns]
    st.plotly_chart(charts.heatmap(mat, "PSC portfolio overlap (%)"), use_container_width=True)
    layout.csv_button(mat.reset_index(names="competitor"), "overlap_psc")
layout.footer()
