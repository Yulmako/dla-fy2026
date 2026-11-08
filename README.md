# Federal Competitor Intelligence — FY2026

Competitor intelligence for US federal prime contracts, built **only** from public
USAspending.gov data. The reporting period is fixed to **FY2026 (Oct 1, 2025 – Sep 30, 2026)**.
No SAM.gov, no Login.gov, no API keys, no scraping, no synthetic data.

**Current status: Phase 2a (staging + duplicate profiling) and the MVP structure.** Facts are
loaded only after the potential-duplicate treatment has been decided. The dashboard shows an
empty state until then; it never shows demo or estimated data.

## Run order

```bash
pip install -r requirements.txt
python scripts/phase1_api_probe.py                             # API checks + reporting-lag status
python scripts/phase1_api_probe.py --market logistics_supply   # validate a candidate market's codes
python scripts/phase2a_stage_and_profile.py                    # all non-planned markets -> staging + duplicate profiles
python scripts/phase2a_stage_and_profile.py --fiscal-months 9-12   # TEST extract: Jun–Sep 2026 only (labelled partial; not loadable)
#   review data/processed/phase2a_duplicate_profile_<market>_latest.md, then set
#   config/pipeline.yaml -> transaction_counting.potential_duplicate_treatment
python scripts/vendor_lookup.py --market logistics_supply --find "NOBLE SUPPLY"
python scripts/load_fy2026.py                                  # transform, API reconciliation, atomic load
streamlit run app/dashboard.py                                 # dashboard; market selector in the sidebar
pytest -q
```

Markets live in `config/market_config.yaml`. All of them are limited to awarding
sub-agency = Defense Logistics Agency (decision 2026-11-07):

- `dla_all`: every DLA contract action, any NAICS/PSC (`all_codes: true`). This is the base
  market and the only one downloaded.
- `defense_electronics`: validated codes, DLA awards only.
- `logistics_supply` (candidate): code families such as PSC `84*`.

**Hosted dashboard markets (decision 2026-11-07):** the snapshot export uses only the base
market `dla_all`. Its market filter lists "DLA – all contracts" plus every official PSC group
present in the data (products: 2-digit Federal Supply Group, e.g. 59; services: 1-letter
category, e.g. R; R&D: 2 letters), named from USAspending's PSC tree. Hand-made segments
(`defense_electronics`, `logistics_supply`) stay in the config but are not shown separately.

Segments covered by an all-codes market with the same agency scope are built from that
download, so there is no second download. A transaction can belong to several markets;
each market is analysed separately in the dashboard. Large extracts are split into monthly
jobs when a job would exceed the 500k-row limit. Run the probe first
(`python scripts/phase1_api_probe.py`): it reports the DLA row count before anything is
downloaded.

### Large markets (all DLA ≈ 3.0 million FY2026 transactions)

Probe of 2026-11-06: 3,014,118 DLA contract transactions, $51.48B; the largest fiscal
month has 355,160 rows, so 12 monthly download jobs stay under the 500k limit.

- **Disk:** about 20 GB free (raw CSV files ~12 GB, zips, SQLite staging ~3 GB).
- **Memory:** staging is read as pandas Categoricals, and the transform runs in
  recipient-UEI buckets of ~300k rows that are merged afterwards
  (`src/etl/partitioned.py`; same rules and numbers as a single pass, covered by
  `tests/test_partitioned.py`). A 3-million-row synthetic benchmark peaked at about
  4.2 GB RAM; real data with more distinct text needs more. Use a machine with 16 GB RAM
  and close other heavy programs. With 8 GB, the run may fail with out-of-memory.
- **Time:** download 30–90 min (server-side job generation), staging 10–20 min,
  snapshot export about 10–15 min.

## Run the Phase 1 validation (one command)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/phase1_api_probe.py
```

Expect about 150–220 read-only API calls, throttled to ≤2 per second (≈3–6 minutes).
Settings (retries, reporting-lag thresholds, reload date) are in `config/pipeline.yaml`.
Nothing large is downloaded: bulk files are only listed and counted.

Outputs:

| File | Content |
|---|---|
| `data/processed/phase1_validation_report_latest.md` | Human-readable report (**send this for review**) |
| `data/processed/phase1_validation_report_latest.json` | Full structured results |
| `data/processed/phase1_duplicate_inspection_latest.json` | C08 duplicate inspection by transaction ID |
| `data/raw/phase1/<run_id>/` | Every raw request/response, with a SHA-256 checksum, plus an `index.jsonl` |
| `logs/phase1_probe_<run_id>.log` | Request log |

Options: `--max-calls N`, `--delay S`, `--scan-pages 40`, `--market defense_electronics`,
`--settings config/pipeline.yaml`.
Exit code 0 = no failed checks, 1 = at least one FAIL, 2 = configuration error.

## What the probe checks

| ID | Question |
|---|---|
| C01 | Is the API reachable, and when was it last updated? |
| C02 | Does the agency scope (DoD) resolve to a real toptier agency? |
| C03 | Which NAICS/PSC filter syntax does the API accept? |
| C04 | Is each candidate NAICS/PSC code real and active in FY2026 DoD data? Which related active codes are missing from the config? |
| C05 | How many transactions and awards are in the segment, and what is the download row limit? |
| C06 | Which recipient, agency, NAICS and PSC fields do the search endpoints actually return? |
| C07 | Does `time_period` strictly bound **Action Date** to FY2026? Are obligation amounts numeric, and are there de-obligations? |
| C08 | Page size limit and deep-paging cap. Duplicate inspection keyed on the transaction unique ID: exact duplicate IDs vs. shared contract numbers with different transaction IDs |
| C09 | **Award Amount (lifetime) vs FY2026 obligations (Σ FY2026 transactions)** on real awards |
| C10 | Does the transaction sum reconcile with the API's own FY2026 aggregate for a recipient? |
| C11 | Which official FY2026 contract archive files exist (listing only; DoD ID resolved via `bulk_download/list_agencies`)? |
| C12 | Reporting lag over the last N months (transaction counts vs. dollars); latest available action date; "reporting volume normal through" date |

## Tests

```bash
pytest -q
```

Unit tests run offline. The probe tests use a clearly labelled fake HTTP session
(base URL `http://test.invalid`) that only exercises code paths. Real validation
happens only against the live API.

## Layout

```
config/market_config.yaml   market definitions (Defense Electronics = candidate)
src/fiscal.py               the single definition of the FY2026 window
src/api/                    client (retry, throttle, raw capture), probe, response schemas
src/config/market.py        market config model and validation
src/database/               SQLAlchemy models and engine (SQLite now, PostgreSQL via DATABASE_URL)
sql/schema_sqlite.sql       equivalent DDL for review
docs/PHASE1_ARCHITECTURE.md architecture, market criteria, limitations
docs/OBLIGATION_METHODOLOGY.md approved FY2026 aggregation, company and provisional-data rules
config/pipeline.yaml        retry policy, reporting-lag thresholds, scheduled reload
sql/views.sql               FY2026 aggregation views (award, company, share)
scripts/phase1_api_probe.py the Phase 1 command
src/etl/                    download (official files), staging, transform, gated loader, run log
src/quality/duplicates.py   Phase 2a potential-duplicate profiler
src/analytics/metrics.py    all dashboard metrics (cross-checked against sql/views.sql in tests)
app/                        Streamlit dashboard: dashboard.py + pages/ + components/
scripts/                    phase1_api_probe, phase2a_stage_and_profile, load_fy2026, refresh_fy2026
```

Federal contract obligations are not company revenue or profit. Market share is the
share of the selected federal procurement dataset, not of the commercial market.
