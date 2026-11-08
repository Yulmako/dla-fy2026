import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from app.components import charts, layout  # noqa: E402
from app.components.format import pct, usd  # noqa: E402
from app.pages._common import bootstrap  # noqa: E402
from src.analytics import metrics as M  # noqa: E402

df, df_all, entity, meta = bootstrap("Market Overview")
k = M.market_kpis(df, entity)
layout.kpis([
    ("Total FY2026 obligations", usd(k["fy2026_obligations"]),
     "Σ FY2026 transaction obligations in the selected dataset (provisional)"),
    ("FY2026 awards with activity", f"{k['award_count']:,}",
     "Distinct award IDs with ≥1 FY2026 action (not transaction rows)"),
    ("Competitors", f"{k['company_count']:,}" if entity == "company" else f"{k['recipient_count']:,}",
     "Companies = reported parent UEI; recipients = individual UEIs"),
    ("Awarding agencies", f"{k['agency_count']:,}", "Awarding sub-tier agencies"),
])
layout.kpis([
    ("Top-5 concentration", pct(k["cr5_pct"]), "Share of selected FY2026 obligations held by the top 5"),
    ("Top-10 concentration", pct(k["cr10_pct"]), None),
    ("HHI", f"{k['hhi']:,.0f}" if k["hhi"] is not None else "–",
     "Herfindahl-Hirschman Index over entities with positive FY2026 obligations (0–10,000)"),
    ("Median FY2026 award (positive-net)", usd(k["median_positive_award_fy2026"]),
     "Median net FY2026 obligation over awards with positive net obligation"),
])

ranking = M.entity_summary(df, entity)
c1, c2 = st.columns(2)
with c1:
    st.plotly_chart(charts.hbar(ranking, "fy2026_obligations", "name",
                                f"Top 10 {layout.entity_label(entity).lower()}s – FY2026 obligations",
                                hover=["uei", "award_count"]), use_container_width=True)
with c2:
    ag = M.obligations_by(df, "awarding_sub_agency", entity=entity)
    st.plotly_chart(charts.hbar(ag, "fy2026_obligations", "awarding_sub_agency",
                                "Top awarding agencies", hover=["supplier_count", "top_supplier"]),
                    use_container_width=True)
c3, c4 = st.columns(2)
with c3:
    na = M.obligations_by(df, "naics_code", "naics_description", entity=entity)
    na["label"] = na["naics_code"].astype(str) + " " + na["naics_description"].fillna("").str.slice(0, 40)
    st.plotly_chart(charts.hbar(na, "fy2026_obligations", "label", "Top NAICS categories",
                                hover=["supplier_count"]), use_container_width=True)
with c4:
    ps = M.obligations_by(df, "psc_code", "psc_description", entity=entity)
    ps["label"] = ps["psc_code"].astype(str) + " " + ps["psc_description"].fillna("").str.slice(0, 40)
    st.plotly_chart(charts.hbar(ps, "fy2026_obligations", "label", "Top PSC categories",
                                hover=["supplier_count"]), use_container_width=True)

dist = M.obligation_distribution(df, entity)
c5, c6 = st.columns(2)
with c5:
    st.plotly_chart(charts.vbar(dist, "bucket", "awards",
                                "Distribution of awards by FY2026 net obligation (positive-net awards)",
                                "Awards"), use_container_width=True)
with c6:
    st.plotly_chart(charts.vbar(dist, "bucket", "fy2026_obligations",
                                "FY2026 obligations by award-size bucket", "FY2026 obligations (USD)"),
                    use_container_width=True)

with st.expander("Tables and CSV export"):
    for name, frame in (("top_competitors", ranking.head(20)), ("agencies", ag),
                        ("naics", na.drop(columns="label")), ("psc", ps.drop(columns="label")),
                        ("award_size_distribution", dist)):
        st.markdown(f"**{name.replace('_', ' ').title()}**")
        st.dataframe(frame, use_container_width=True, hide_index=True)
        layout.csv_button(frame, f"overview_{name}")
layout.footer()
