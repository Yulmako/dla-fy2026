import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.components import charts, layout  # noqa: E402
from app.components.format import PARENT_STATUS_LABEL, pct, usd  # noqa: E402
from app.pages._common import bootstrap  # noqa: E402
from src.analytics import metrics as M  # noqa: E402

df, df_all, entity, meta = bootstrap("Competitor Profile")
eid = "company_id" if entity == "company" else "recipient_id"
ranking = M.entity_summary(df, entity)
labels = dict(zip(ranking[eid], ranking["rank"].astype(str) + ". " + ranking["name"].astype(str)))
ids = list(labels)
preset = st.session_state.get("profile_entity")
default = ids.index(preset[1]) if preset and preset[0] == entity and preset[1] in ids else 0
sel = st.selectbox(layout.entity_label(entity), ids, index=default, format_func=labels.get)
row = ranking.set_index(eid).loc[sel]
sub = df[df[eid] == sel]

st.header(str(row["name"]))
info = [f"**UEI:** {row['uei'] if pd.notna(row['uei']) else '– (no UEI reported)'}"]
if entity == "company":
    status = row["parent_status"]
    info.append(f"**Parent identification:** {PARENT_STATUS_LABEL.get(status, status)}")
    if status == "reported_parent":
        info.append("Parent company = parent UEI as reported in USAspending records (not "
                    "independently verified).")
else:
    parent = sub[["company_name", "company_uei", "parent_status"]].iloc[0]
    info.append(f"**Reported parent:** {parent['company_name'] if parent['parent_status'] == 'reported_parent' else '–'} "
                f"({parent['company_uei'] if pd.notna(parent['company_uei']) else '–'})")
st.markdown(" · ".join(info))
if sub["parent_uei_conflict"].any():
    st.warning("At least one recipient UEI reported more than one parent UEI in FY2026; the most "
               "recent is used. See Data Source & Methodology → data quality.")

layout.kpis([
    ("FY2026 obligations", usd(row["fy2026_obligations"]), "Provisional"),
    ("FY2026 awards with activity", f"{int(row['award_count']):,}", None),
    ("Share of selected dataset", pct(row["share_of_selected_dataset_pct"], 2), "Provisional"),
    ("Awarding agencies", f"{int(row['agency_count'])}", None),
])
layout.kpis([
    ("Avg FY2026 award (positive-net)", usd(row["avg_positive_award_fy2026"]), None),
    ("Median FY2026 award (positive-net)", usd(row["median_positive_award_fy2026"]), None),
    ("Largest FY2026 award obligation", usd(row["largest_award_fy2026"]), None),
    ("Zero-net / negative-net awards",
     f"{int(row['zero_net_award_count'])} / {int(row['negative_net_award_count'])}",
     "Counted separately; excluded from average and median"),
])

c1, c2 = st.columns(2)
with c1:
    ag = M.portfolio(df, sel, "awarding_sub_agency", entity=entity)
    st.plotly_chart(charts.hbar(ag, "fy2026_obligations", "awarding_sub_agency",
                                "Top awarding agencies", hover=["share_of_entity_pct"]),
                    use_container_width=True)
with c2:
    psc = M.portfolio(df, sel, "psc_code", "psc_description", entity=entity)
    psc["label"] = psc["psc_code"].astype(str) + " " + psc["psc_description"].fillna("").str.slice(0, 40)
    st.plotly_chart(charts.hbar(psc, "fy2026_obligations", "label", "PSC distribution",
                                hover=["share_of_entity_pct"]), use_container_width=True)
c3, c4 = st.columns(2)
with c3:
    na = M.portfolio(df, sel, "naics_code", "naics_description", entity=entity)
    na["label"] = na["naics_code"].astype(str) + " " + na["naics_description"].fillna("").str.slice(0, 40)
    st.plotly_chart(charts.hbar(na, "fy2026_obligations", "label", "NAICS distribution",
                                hover=["share_of_entity_pct"]), use_container_width=True)
with c4:
    geo = (sub[sub["pop_country_code"].fillna("USA") == "USA"]
           .groupby("pop_state_code")["transaction_amount"].sum().reset_index())
    if len(geo):
        st.plotly_chart(charts.state_map(geo, "pop_state_code", "transaction_amount",
                                         "Geographic footprint – place of performance (US states)"),
                        use_container_width=True)
    else:
        st.info("No US place-of-performance data for this selection.")

st.subheader("Portfolio diversification")
div = M.diversification(df, sel, entity)
layout.kpis([
    (f"Across {div['agency_id']['members']} agencies", str(div["agency_id"]["diversification_index"]),
     "1 − HHI/10,000 of this competitor's obligations by agency (0 = single agency)"),
    (f"Across {div['naics_code']['members']} NAICS", str(div["naics_code"]["diversification_index"]), None),
    (f"Across {div['psc_code']['members']} PSC", str(div["psc_code"]["diversification_index"]), None),
])

if entity == "company":
    st.subheader("Recipient UEIs (drill-down)")
    uei = (sub.groupby(["recipient_uei", "recipient_name", "recipient_state", "parent_uei_conflict"],
                       dropna=False)["transaction_amount"].sum().reset_index(name="fy2026_obligations")
           .sort_values("fy2026_obligations", ascending=False))
    st.dataframe(uei, use_container_width=True, hide_index=True,
                 column_config=layout.money_cols(["fy2026_obligations"]))

st.subheader("Awards")
awards = M.award_list(df, sel, entity)
st.caption("FY2026 net obligation = Σ FY2026 actions on the award. Lifetime columns are context "
           "only and are never added to FY2026 metrics.")
st.markdown("**Largest FY2026 awards**")
st.dataframe(awards.head(10), use_container_width=True, hide_index=True,
             column_config=layout.money_cols(["fy2026_net_obligation", "lifetime_cumulative_obligation",
                                              "lifetime_ceiling"]))
with st.expander(f"Full award list ({len(awards):,})"):
    st.dataframe(awards, use_container_width=True, hide_index=True,
                 column_config=layout.money_cols(["fy2026_net_obligation",
                                                  "lifetime_cumulative_obligation", "lifetime_ceiling"]))
layout.csv_button(awards, f"profile_awards_{sel}")
if awards["potential_duplicate_records"].sum():
    st.info(f"{int(awards['potential_duplicate_records'].sum())} FY2026 records on these awards share "
            f"award, modification, date and amount with another record (potential source duplicates; "
            f"treatment: {meta['duplicate_treatment']}).")
layout.footer()
