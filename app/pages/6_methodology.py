import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.components import data, layout  # noqa: E402
from app.pages._common import select_segment  # noqa: E402
from src.analytics import metrics as M  # noqa: E402
from src.api.probe import MEASURE_DEFINITIONS  # noqa: E402
from src.config.market import load_market_config  # noqa: E402

segment_id, market_key = select_segment()
meta = data.meta(market_key)
layout.header("Data Source & Methodology", meta)
_cfg = load_market_config()
market = _cfg.market(market_key if market_key in _cfg.markets else None)

st.markdown("""
**Source.** Only public data from USAspending.gov: the official API (`api.usaspending.gov`) and
the official download files (`files.usaspending.gov`). No SAM.gov, no credentials, no scraping,
no private or personal data.

**Reporting period.** FY2026 only (October 1, 2025 – September 30, 2026). There is no
fiscal-year selector, no year-over-year metric and no multi-year chart.

**Primary metric.** FY2026 obligations = Σ `federal_action_obligation` of contract
transactions (types A–D) dated in FY2026, including de-obligations. Lifetime award values
(ceiling, cumulative obligation) are shown only as labelled context.
""")
st.subheader("Measure definitions")
st.table(pd.DataFrame(MEASURE_DEFINITIONS))
st.subheader("Award-level and company rules")
st.markdown("""
- **FY2026 awards with activity**: distinct award IDs with at least one FY2026 action. Transaction rows are never counted as awards.
- Transactions are aggregated **by award first**. **Average / median / largest award** use awards with **positive net FY2026 obligation** only; **zero-net** and **negative-net** awards are counted separately.
- **Company** = the parent UEI **reported** in USAspending records. A recipient without a reported parent is shown as its own company and flagged; recipients without a UEI are flagged as unverified. **Companies are never merged by name similarity.**
- **Share** = company FY2026 obligations ÷ FY2026 obligations of the selected dataset × 100 – a share of the selected federal procurement dataset, not of the commercial market.
- **Concentration**: CR5/CR10 and HHI over entities with positive FY2026 obligations.
- **De-duplication** uses only the USAspending transaction unique ID.
""")
st.markdown(f"**Potential source duplicates** (same award, modification, action date and amount; "
            f"different transaction IDs): current treatment = `{meta['duplicate_treatment']}`.")

st.subheader(f"Market definition – {market.name} ({market.status})")
st.info(market.definition_note)
codes = pd.DataFrame([{"type": "NAICS", **e.model_dump()} for e in market.naics_codes] +
                     [{"type": "PSC", **e.model_dump()} for e in market.psc_codes] +
                     [{"type": "NAICS family", "code": e.prefix + "*", **e.model_dump(exclude={"prefix"})}
                      for e in market.naics_prefixes] +
                     [{"type": "PSC family", "code": e.prefix + "*", **e.model_dump(exclude={"prefix"})}
                      for e in market.psc_prefixes])
codes["included"] = codes["tier"].isin(market.included_tiers)
st.dataframe(codes[["type", "code", "tier", "included", "label", "rationale"]],
             use_container_width=True, hide_index=True)
st.markdown("**Documented exclusions**")
st.dataframe(pd.DataFrame([e.model_dump() for e in market.documented_exclusions]),
             use_container_width=True, hide_index=True)
st.caption("Inclusion: a transaction is in the market if its NAICS or PSC code (exact code or "
           "code family) is in an included tier and the awarding agency is in scope (Department "
           "of Defense). Keywords only tag records. No single code represents the industry. A "
           "transaction can belong to several markets; each market is analysed separately.")

st.subheader("Known limitations")
st.markdown(f"""
- **DoD reporting lag (~{meta['dod_lag_days']} days):** late-FY2026 DoD months are incomplete; all results are provisional until the full reload ({meta['scheduled_full_reload']}). Missing obligations are not estimated.
- Parent company comes from reported parent UEIs; it can be missing, stale after mergers, or conflicting between records.
- Some recipients lack a UEI; DUNS is legacy.
- NAICS/PSC are assigned per action and can change between modifications; classified or redacted awards are not published.
- Electronics embedded in platform contracts, non-DoD purchases and subcontracts are outside this market definition.
- Obligations are not outlays, revenue or profit.
""")

st.subheader("Data quality (latest load)")
df = data.market_frame(segment_id)
if df.empty:
    st.info("No data loaded yet.")
else:
    dq = M.data_quality_summary(df)
    st.dataframe(pd.DataFrame([dq]).T.rename(columns={0: "value"}), use_container_width=True)
    st.dataframe(data.dq_counts(), use_container_width=True, hide_index=True)

st.subheader("Refresh & load history")
st.caption("A refresh re-pulls the fixed FY2026 window from the public API (probe → staging → "
           "load). The reporting period never changes. The previous snapshot stays live until "
           "the new load passes reconciliation.")
if st.button("Refresh FY2026 data from USAspending"):
    subprocess.Popen([sys.executable, str(ROOT / "scripts" / "refresh_fy2026.py")], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    st.success("Refresh started in the background (several minutes). Reload this page to see "
               "its status below.")
if st.button("Reload dashboard data"):
    data.clear_caches()
    st.rerun()
st.dataframe(data.etl_history(), use_container_width=True, hide_index=True)
layout.footer()
