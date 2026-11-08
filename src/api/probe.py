"""Phase 1 – live validation of the public USAspending API for FY2026.

Runs a bounded series of read-only checks against https://api.usaspending.gov
and produces a structured validation report (JSON + Markdown). It never
downloads the full dataset: bulk endpoints are only *listed / counted*.

Each check answers one question the ETL design depends on. Results are real
API data only; nothing is synthesised or estimated.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.api import filters as mfilters
from src.api import schemas
from src.api.client import ApiResponse, CallBudgetExceeded, UsaSpendingClient
from src.config.market import Market, definition_hash
from src.config.settings import PipelineSettings, load_settings
from src.fiscal import (CONTRACT_AWARD_TYPES, FISCAL_YEAR, FY_END, FY_LABEL, FY_START,
                        IDV_AWARD_TYPES, calendar_to_fiscal_month, fiscal_month_range,
                        in_fy2026, parse_date, time_period_filter)

log = logging.getLogger("fci.probe")

PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"

TXN_FIELD_CANDIDATES = [
    "Award ID", "Mod", "Recipient Name", "Recipient UEI", "Action Date",
    "Transaction Amount", "Transaction Description", "Action Type", "Award Type",
    "Awarding Agency", "Awarding Sub Agency", "Funding Agency", "Funding Sub Agency",
    "naics_code", "naics_description", "product_or_service_code",
    "product_or_service_description", "NAICS", "PSC", "Recipient Location",
    "Primary Place of Performance", "generated_internal_id", "internal_id",
    "Last Modified Date",
]
AWARD_FIELD_CANDIDATES = [
    "Award ID", "Recipient Name", "Recipient UEI", "Award Amount", "Total Outlays",
    "Description", "Contract Award Type", "Award Type", "Start Date", "End Date",
    "Base Obligation Date", "Last Modified Date", "Awarding Agency",
    "Awarding Sub Agency", "Funding Agency", "Funding Sub Agency", "NAICS", "PSC",
    "recipient_id", "prime_award_recipient_id", "generated_internal_id",
    "internal_id", "Recipient Location", "Primary Place of Performance",
]
REQUIRED_TXN_FIELDS = ["Award ID", "Action Date", "Transaction Amount"]


# --------------------------------------------------------------------- helpers
@dataclass
class CheckResult:
    check_id: str
    title: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _code(v: Any) -> str | None:
    """NAICS/PSC fields may come back as a string or as {code, description}."""
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("code") or v.get("id")
    s = str(v).strip()
    return s or None


def _first(row: dict, *keys: str) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _filled(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, dict):
        return any(_filled(x) for x in v.values())
    return True


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _valid_values(data: Any) -> list[str]:
    """Extract the API's list of valid field values from a 422 error body."""
    if data is None:
        return []
    text = data if isinstance(data, str) else json.dumps(data)
    m = re.search(r"outside valid values \[(.*?)\]", text)
    if not m:
        return []
    return [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]


def psc_tree_path(code: str) -> list[str]:
    """Best-guess USAspending PSC filter-tree path for a PSC code."""
    if code[0].isdigit():
        return ["Product", code[:2], code]
    if code[0] == "A":
        return ["Research and Development", code[:2], code[:3], code]
    return ["Service", code[0], code[:2], code]


# ----------------------------------------------------------------------- probe
class Phase1Probe:
    def __init__(self, client: UsaSpendingClient, market_key: str, market: Market,
                 scan_max_pages: int = 40, sample_limit: int = 100,
                 settings: PipelineSettings | None = None) -> None:
        self.c = client
        self.market_key = market_key
        self.m = market
        self.scan_max_pages = scan_max_pages
        self.sample_limit = sample_limit
        self.checks: list[CheckResult] = []
        self.facts: dict[str, Any] = {}
        self.code_validation: dict[str, Any] = {}
        self.naics_form = "require"
        self.psc_form = "list"
        self.txn_fields: list[str] = list(REQUIRED_TXN_FIELDS)
        self.award_fields: list[str] = ["Award ID", "Award Amount"]
        self.txn_count: int | None = None
        self.started_at = datetime.now(timezone.utc)
        self.settings = settings or load_settings()
        self.duplicate_inspection: dict[str, Any] = {}
        self.reporting_lag: dict[str, Any] = {}
        self.month_numbering = "fiscal_assumed"

    # ------------------------------------------------------------ filters
    def base_filters(self, award_types: tuple[str, ...] = CONTRACT_AWARD_TYPES,
                     date_type: str | None = None) -> dict[str, Any]:
        f: dict[str, Any] = {"time_period": time_period_filter(date_type)}
        if award_types:
            f["award_type_codes"] = list(award_types)
        agencies = self.m.agency_filters()
        if agencies:
            f["agencies"] = agencies
        return f

    def naics_filter(self, codes: list[str]) -> Any:
        return {"require": codes} if self.naics_form == "require" else codes

    def psc_filter(self, codes: list[str]) -> Any:
        if self.psc_form == "require_tree":
            return {"require": [psc_tree_path(c) for c in codes]}
        return codes

    def segment_filters(self, by: str = "naics", **kw: Any) -> dict[str, Any]:
        f = self.base_filters(**kw)
        if self.m.all_codes:
            return f                      # whole agency scope, no NAICS/PSC filter
        if self.m.has_prefixes():
            # code families: shared builder (NAICS short codes, PSC tree paths, excludes)
            if by == "naics":
                f["naics_codes"] = mfilters.naics_filter_value(self.m)
            else:
                f["psc_codes"] = mfilters.psc_filter_value(self.m)
        elif by == "naics":
            f["naics_codes"] = self.naics_filter(self.m.naics())
        else:
            f["psc_codes"] = self.psc_filter(self.m.psc())
        return f

    # ------------------------------------------------------------ runner
    def run(self) -> dict[str, Any]:
        steps: list[tuple[str, str, Callable[[], CheckResult]]] = [
            ("C01", "API connectivity and data freshness", self.c01_connectivity),
            ("C02", "Agency scope resolves to real toptier agencies", self.c02_agencies),
            ("C03", "NAICS / PSC filter syntax accepted by the API", self.c03_filter_forms),
            ("C04", "Market code validation against FY2026 USAspending data", self.c04_code_scan),
            ("C05", "Record counts and download limits (no download)", self.c05_counts),
            ("C06", "Field availability: transaction and award search", self.c06_fields),
            ("C07", "FY2026 transaction sample: date filter and obligation fields", self.c07_txn_sample),
            ("C08", "Pagination limits and duplicate inspection (transaction ID)", self.c08_pagination),
            ("C09", "Award amount vs FY2026 obligations (date semantics)", self.c09_award_semantics),
            ("C10", "Reconciliation: transaction sum vs API aggregate", self.c10_reconciliation),
            ("C11", "Official bulk files for FY2026 (listing only)", self.c11_bulk_listing),
            ("C12", "Reporting lag and data-through date", self.c12_reporting_lag),
        ]
        abort_reason: str | None = None
        for cid, title, fn in steps:
            if abort_reason:
                self.checks.append(CheckResult(cid, title, SKIP, abort_reason))
                continue
            log.info("Running %s – %s", cid, title)
            try:
                res = fn()
            except CallBudgetExceeded as exc:
                abort_reason = f"Skipped: {exc}"
                res = CheckResult(cid, title, SKIP, abort_reason)
            except Exception as exc:  # keep going; record the failure
                res = CheckResult(cid, title, FAIL, f"Probe error: {exc}",
                                  {"traceback": traceback.format_exc(limit=5)})
            res.check_id, res.title = cid, title
            self.checks.append(res)
            if cid == "C01" and res.status == FAIL:
                abort_reason = "Skipped: API not reachable (see C01)."
        return self.build_report()

    # ------------------------------------------------------------ C01
    def c01_connectivity(self) -> CheckResult:
        r = self.c.get("/api/v2/awards/last_updated/", label="last_updated")
        if not r.ok:
            return CheckResult("", "", FAIL, f"Could not reach the API: {r.error_detail()}",
                               {"status": r.status_code})
        self.facts["source_last_updated"] = (r.data or {}).get("last_updated")
        return CheckResult("", "", PASS,
                           f"API reachable; USAspending reports last update "
                           f"{self.facts['source_last_updated']}.",
                           {"response": r.data, "elapsed_ms": r.elapsed_ms})

    # ------------------------------------------------------------ C02
    def c02_agencies(self) -> CheckResult:
        r = self.c.get("/api/v2/references/toptier_agencies/", label="toptier_agencies")
        if not r.ok:
            return CheckResult("", "", FAIL, f"toptier_agencies failed: {r.error_detail()}")
        rows = (r.data or {}).get("results", [])
        found, missing = [], []
        for a in self.m.agency_scope.toptier_agencies:
            hit = next((x for x in rows if str(x.get("agency_name", "")).lower()
                        == a.name.lower()), None)
            if hit:
                found.append({k: hit.get(k) for k in
                              ("agency_name", "toptier_code", "agency_id", "abbreviation")})
            else:
                missing.append(a.name)
        self.facts["scope_agencies"] = found
        sub_res: dict[str, Any] = {}
        subs = self.m.agency_scope.subtier_agencies
        if subs and found:
            code = found[0]["toptier_code"]
            rs = self.c.get(f"/api/v2/agency/{code}/sub_agency/",
                            params={"fiscal_year": FISCAL_YEAR, "limit": 100},
                            label="sub_agencies")
            listed = (rs.data or {}).get("results", []) if rs.ok else []
            for sa in subs:
                hit = next((x for x in listed if str(x.get("name", "")).casefold()
                            == sa.name.casefold()), None)
                sub_res[sa.name] = ({k: hit.get(k) for k in ("name", "abbreviation",
                                                             "total_obligations",
                                                             "transaction_count")}
                                    if hit else None)
            missing += [n for n, v in sub_res.items() if v is None]
        self.facts["scope_subtier_agencies"] = sub_res or None
        status = FAIL if missing else PASS
        return CheckResult("", "", status,
                           f"{len(found)} toptier"
                           + (f" and {sum(v is not None for v in sub_res.values())} sub-tier"
                              if sub_res else "")
                           + " scope agencies resolved"
                           + (f"; NOT found: {missing}" if missing else "."),
                           {"resolved": found, "resolved_subtier": sub_res, "missing": missing,
                            "toptier_agency_count": len(rows)})

    # ------------------------------------------------------------ C03
    def _category_codes(self, category: str, filters: dict, limit: int = 10) -> ApiResponse:
        return self.c.post(f"/api/v2/search/spending_by_category/{category}/",
                           {"filters": filters, "limit": limit, "page": 1},
                           label=f"category_{category}_filtertest")

    def _try_forms(self, category: str, key: str, codes: list[str],
                   forms: dict[str, Callable[[str], Any]]) -> dict[str, Any]:
        outcome: dict[str, Any] = {}
        for code in codes[:3]:
            for name, make in forms.items():
                f = self.base_filters()
                f[key] = make(code)
                r = self._category_codes(category, f)
                returned = [_code(x.get("code")) for x in (r.data or {}).get("results", [])] if r.ok else []
                outcome.setdefault(code, {})[name] = {
                    "http_status": r.status_code,
                    "returned_codes": returned[:10],
                    "error": None if r.ok else r.error_detail(),
                    "accepted": r.ok and bool(returned) and all(c == code for c in returned),
                }
            if any(v["accepted"] for v in outcome[code].values()):
                break
        return outcome

    def c03_filter_forms(self) -> CheckResult:
        core_naics = self.m.naics(("core",)) or self.m.naics()
        core_psc = [c for c in (self.m.psc(("core",)) or self.m.psc()) if c[0].isdigit()]
        naics_out = self._try_forms("naics", "naics_codes", core_naics, {
            "require": lambda c: {"require": [c]},
            "list": lambda c: [c],
        })
        psc_out = self._try_forms("psc", "psc_codes", core_psc, {
            "list": lambda c: [c],
            "require_tree": lambda c: {"require": [psc_tree_path(c)]},
        })

        def pick(out: dict, order: list[str]) -> str | None:
            for name in order:
                if any(v.get(name, {}).get("accepted") for v in out.values()):
                    return name
            return None

        n = pick(naics_out, ["require", "list"]) if naics_out else "require"
        p = pick(psc_out, ["list", "require_tree"]) if psc_out else "require_tree"
        prefix_tests = self._test_prefix_filters()
        if n:
            self.naics_form = n
        if p:
            self.psc_form = p
        self.facts["naics_filter_form"] = self.naics_form
        self.facts["psc_filter_form"] = self.psc_form
        prefix_ok = all(t["accepted"] for t in prefix_tests.values())
        self.facts["prefix_filters_ok"] = prefix_ok if prefix_tests else None
        status = PASS if (n and p and prefix_ok) else WARN
        return CheckResult("", "", status,
                           f"NAICS filter form: {n or 'UNCONFIRMED'}; PSC filter form: "
                           f"{p or 'UNCONFIRMED'}"
                           + (f"; code-family filters: {'accepted' if prefix_ok else 'NOT confirmed'}"
                              if prefix_tests else "") + ".",
                           {"naics_tests": naics_out, "psc_tests": psc_out,
                            "prefix_tests": prefix_tests})

    def _test_prefix_filters(self) -> dict[str, Any]:
        """For code-family markets: a prefix filter must return only codes in that family."""
        out: dict[str, Any] = {}
        tests = []
        if self.m.naics_prefix_list():
            p = self.m.naics_prefix_list()[0]
            tests.append(("naics", "naics_codes", p, {"require": [p]}))
        if self.m.psc_prefix_list():
            p = self.m.psc_prefix_list()[0]
            tests.append(("psc", "psc_codes", p, {"require": [mfilters.psc_tree_path(p)]}))
        for cat, key, prefix, value in tests:
            f = self.base_filters()
            f[key] = value
            r = self._category_codes(cat, f, limit=20)
            codes = [_code(x.get("code")) for x in (r.data or {}).get("results", [])] if r.ok else []
            out[f"{cat}:{prefix}"] = {"http_status": r.status_code, "returned_codes": codes,
                                      "error": None if r.ok else r.error_detail(),
                                      "accepted": r.ok and bool(codes)
                                      and all(c and c.startswith(prefix) for c in codes)}
        return out

    # ------------------------------------------------------------ C04
    def _scan_category(self, category: str, filters: dict) -> tuple[list[dict], bool, int]:
        rows: list[dict] = []
        page = 0
        complete = False
        while page < self.scan_max_pages:
            page += 1
            r = self.c.post(f"/api/v2/search/spending_by_category/{category}/",
                            {"filters": filters, "limit": 100, "page": page},
                            label=f"scan_{category}_p{page}")
            if not r.ok:
                break
            parsed, err = schemas.validate(schemas.CategoryPage, r.data)
            if err:
                raise ValueError(f"Unexpected {category} response shape: {err}")
            rows.extend(r.data.get("results", []))
            if not (r.data.get("page_metadata") or {}).get("hasNext"):
                complete = True
                break
        return rows, complete, page

    def _naics_reference(self, code: str) -> str | None:
        r = self.c.get(f"/api/v2/references/naics/{code}/", label=f"ref_naics_{code}")
        if not r.ok:
            return None
        for row in (r.data or {}).get("results", []):
            if str(row.get("naics")) == code:
                return row.get("naics_description")
        return None

    def _psc_reference(self, code: str, cache: dict[str, list[dict]]) -> str | None:
        path = psc_tree_path(code)
        parents = [path[:-1]]
        if path[0] == "Service":
            parents.append(path[:2])
        for parent in parents:
            key = "/".join(parent)
            if key not in cache:
                r = self.c.get(f"/api/v2/references/filter_tree/psc/{key}/",
                               label=f"ref_psc_{key}")
                cache[key] = (r.data or {}).get("results", []) if r.ok else []
            for node in cache[key]:
                if str(node.get("id")) == code:
                    return node.get("description")
        return None

    def c04_code_scan(self) -> CheckResult:
        base = self.base_filters()
        naics_rows, naics_done, n_pages = self._scan_category("naics", base)
        psc_rows, psc_done, p_pages = self._scan_category("psc", base)
        naics_map = {_code(r.get("code")): r for r in naics_rows}
        psc_map = {_code(r.get("code")): r for r in psc_rows}
        naics_total = sum(_num(r.get("amount")) or 0 for r in naics_rows)
        psc_total = sum(_num(r.get("amount")) or 0 for r in psc_rows)

        psc_cache: dict[str, list[dict]] = {}

        def evaluate(entries, amap, total, complete, ref_lookup) -> list[dict]:
            out = []
            for e in entries:
                hit = amap.get(e.code)
                amount = _num(hit.get("amount")) if hit else None
                official = hit.get("name") if hit else None
                if hit:
                    status = "active" if amount else "valid_zero_net"
                else:
                    official = ref_lookup(e.code)
                    if official:
                        status = "valid_no_fy2026_activity"
                    else:
                        status = "not_found" if complete else "unverified_scan_incomplete"
                out.append({
                    "code": e.code, "tier": e.tier, "working_label": e.label,
                    "official_description": official, "status": status,
                    "fy2026_obligations_in_scope": amount,
                    "share_of_scope_pct": round(amount / total * 100, 4)
                    if amount and total else None,
                })
            return out

        naics_eval = evaluate(self.m.naics_codes, naics_map, naics_total, naics_done,
                              self._naics_reference)
        psc_eval = evaluate(self.m.psc_codes, psc_map, psc_total, psc_done,
                            lambda c: self._psc_reference(c, psc_cache))

        # Codes already configured OR deliberately excluded are not re-suggested.
        configured_n = {e.code for e in self.m.naics_codes} | self.m.excluded_codes("naics")
        configured_p = {e.code for e in self.m.psc_codes} | self.m.excluded_codes("psc")

        fam_n = [e.prefix for e in self.m.naics_prefixes]
        fam_p = [e.prefix for e in self.m.psc_prefixes]

        def suggestions(rows, configured, prefixes, families) -> list[dict]:
            out = [
                {"code": _code(r.get("code")), "official_description": r.get("name"),
                 "fy2026_obligations_in_scope": _num(r.get("amount"))}
                for r in rows
                if _code(r.get("code")) and _code(r.get("code")) not in configured
                and not any(_code(r.get("code")).startswith(f) for f in families)
                and any(_code(r.get("code")).startswith(p) for p in prefixes)
            ]
            return sorted(out, key=lambda x: -(x["fy2026_obligations_in_scope"] or 0))[:20]

        included = tuple(self.m.included_tiers)

        def basis_sum(rows, tier_fn) -> float:
            return sum(_num(r.get("amount")) or 0 for r in rows
                       if tier_fn(_code(r.get("code"))) in included)

        def families(entries, rows, kind) -> list[dict]:
            out = []
            excluded = self.m.excluded_codes(kind)
            for e in entries:
                members = [r for r in rows if (_code(r.get("code")) or "").startswith(e.prefix)
                           and _code(r.get("code")) not in excluded]
                amount = sum(_num(r.get("amount")) or 0 for r in members)
                top = sorted(members, key=lambda r: -(_num(r.get("amount")) or 0))[:5]
                out.append({"prefix": e.prefix, "tier": e.tier, "working_label": e.label,
                            "codes_with_activity": len(members),
                            "status": "active" if amount else "no_fy2026_activity",
                            "fy2026_obligations_in_scope": round(amount, 2),
                            "share_of_scope_pct": round(amount / (naics_total if kind == "naics"
                                                                   else psc_total) * 100, 4)
                            if amount else None,
                            "top_codes": [f"{_code(r.get('code'))} {r.get('name')}" for r in top]})
            return out

        naics_fam = families(self.m.naics_prefixes, naics_rows, "naics")
        psc_fam = families(self.m.psc_prefixes, psc_rows, "psc")
        self.code_validation = {
            "scope_description": "awarding agency scope + contract types A-D + FY2026 action dates",
            "naics": naics_eval, "psc": psc_eval,
            "naics_prefixes": naics_fam, "psc_prefixes": psc_fam,
            "scan": {
                "naics_codes_with_activity": len(naics_rows), "naics_scan_complete": naics_done,
                "naics_pages": n_pages, "psc_codes_with_activity": len(psc_rows),
                "psc_scan_complete": psc_done, "psc_pages": p_pages,
                "scope_total_by_naics_aggregate": naics_total,
                "scope_total_by_psc_aggregate": psc_total,
            },
            "segment_size_indicators": {
                "included_naics_obligations": round(basis_sum(naics_rows, self.m.naics_tier), 2),
                "included_psc_obligations": round(basis_sum(psc_rows, self.m.psc_tier), 2),
                "note": "NAICS-based and PSC-based totals overlap and must NOT be added; the "
                        "market total is the de-duplicated union computed after extraction.",
            },
            "candidates_for_review": {
                "naics": suggestions(naics_rows, configured_n, self.m.discovery_prefixes.naics, fam_n),
                "psc": suggestions(psc_rows, configured_p, self.m.discovery_prefixes.psc, fam_p),
            },
        }
        everything = naics_eval + psc_eval
        not_found = [e["code"] for e in everything if e["status"] == "not_found"]
        inactive_included = [e["code"] for e in everything
                             if e["tier"] in included and e["status"] != "active"] + [
            f"{f['prefix']}*" for f in naics_fam + psc_fam
            if f["tier"] in included and f["status"] != "active"]
        status = FAIL if not_found else (WARN if inactive_included else PASS)
        return CheckResult("", "", status,
                           f"{sum(e['status'] == 'active' for e in naics_eval)}/{len(naics_eval)} "
                           f"NAICS and {sum(e['status'] == 'active' for e in psc_eval)}/{len(psc_eval)} "
                           f"PSC candidate codes have FY2026 obligations in scope. "
                           f"Codes not found in USAspending: {not_found or 'none'}. "
                           f"Included codes without FY2026 activity: {inactive_included or 'none'}.",
                           {"see": "code_validation section"})

    # ------------------------------------------------------------ C05
    def _count(self, path: str, filters: dict, label: str) -> dict:
        r = self.c.post(path, {"filters": filters}, label=label)
        return {"http_status": r.status_code,
                "response": r.data if r.ok else None,
                "error": None if r.ok else r.error_detail()}

    def c05_counts(self) -> CheckResult:
        out: dict[str, Any] = {}
        for by in ("naics", "psc"):
            seg_all_types = self.segment_filters(by, award_types=())
            seg = self.segment_filters(by)
            out[f"award_count_by_{by}"] = self._count(
                "/api/v2/search/spending_by_award_count/", seg_all_types, f"award_count_{by}")
            out[f"transaction_count_by_{by}"] = self._count(
                "/api/v2/search/spending_by_transaction_count/", seg, f"txn_count_{by}")
            out[f"download_count_by_{by}"] = self._count(
                "/api/v2/download/count/", seg, f"download_count_{by}")
        out["idv_award_count_scope"] = self._count(
            "/api/v2/search/spending_by_award_count/",
            self.base_filters(award_types=IDV_AWARD_TYPES), "idv_count_scope")

        def dig(d: Any, *keys: str) -> Any:
            if not isinstance(d, dict):
                return None
            for k in keys:
                if k in d:
                    return d[k]
            res = d.get("results")
            return dig(res, *keys) if isinstance(res, dict) else None

        def as_int(v: Any) -> int | None:
            n = _num(v)
            return int(n) if n is not None else None

        txn_counts = [as_int(dig(out[f"transaction_count_by_{b}"]["response"], "contracts", "count"))
                      for b in ("naics", "psc")]
        dl = out["download_count_by_naics"]["response"] or {}
        self.txn_count = txn_counts[0]
        self.facts["segment_transaction_count_naics"] = txn_counts[0]
        self.facts["segment_transaction_count_psc"] = txn_counts[1]
        self.facts["download_calculated_transaction_count"] = dl.get("calculated_transaction_count")
        self.facts["download_maximum_transaction_limit"] = dl.get("maximum_transaction_limit")
        self.facts["download_rows_gt_limit"] = dl.get("transaction_rows_gt_limit")
        ok_calls = sum(1 for v in out.values() if v["http_status"] == 200)
        status = PASS if ok_calls == len(out) else WARN
        return CheckResult("", "", status,
                           f"Segment transactions (NAICS basis): {txn_counts[0]}, "
                           f"(PSC basis): {txn_counts[1]}. Download limit reported: "
                           f"{self.facts['download_maximum_transaction_limit']}. "
                           f"{ok_calls}/{len(out)} count endpoints answered.",
                           out)

    # ------------------------------------------------------------ C06
    def _discover(self, path: str, candidates: list[str], filters: dict, sort: str,
                  label: str) -> dict[str, Any]:
        """Find which `fields` an endpoint returns. Exploratory calls use
        retry=False: an invalid field is a deterministic error and must not be
        retried."""
        body = {"filters": filters, "fields": candidates, "limit": 5, "page": 1,
                "sort": sort, "order": "desc"}
        r = self.c.post(path, body, label=f"{label}_fields_batch", retry=False)
        valid = _valid_values(r.data)
        if r.ok and (r.data or {}).get("results"):
            rows = r.data["results"]
            avail = [f for f in candidates if any(f in row for row in rows)]
            return {"mode": "batch", "available": avail,
                    "not_returned": [f for f in candidates if f not in avail],
                    "rejected": {}, "valid_values_reported_by_api": valid,
                    "extra_keys_returned":
                        sorted({k for row in rows for k in row} - set(candidates))}
        result = {"mode": "per_field", "available": [], "not_returned": [], "rejected": {},
                  "batch_error": r.error_detail() if not r.ok else "no results",
                  "valid_values_reported_by_api": valid}
        for f in candidates:
            if valid and f not in valid:
                result["rejected"][f] = "not in API's list of valid field values (not requested)"
                continue
            fields = [sort] if f == sort else [sort, f]
            rr = self.c.post(path, {"filters": filters, "fields": fields, "limit": 1,
                                    "page": 1, "sort": sort, "order": "desc"},
                             label=f"{label}_field", retry=False)
            if not rr.ok:
                result["rejected"][f] = rr.error_detail()
                result["valid_values_reported_by_api"] = (
                    result["valid_values_reported_by_api"] or _valid_values(rr.data))
            elif any(f in row for row in (rr.data or {}).get("results", [])):
                result["available"].append(f)
            else:
                result["not_returned"].append(f)
        return result

    def _probe_transaction_id_field(self, valid: list[str], filters: dict) -> str | None:
        """Look for a transaction-level unique key in transaction search."""
        skip = {"generated_internal_id", "internal_id"}
        cands = [v for v in valid if v not in skip
                 and re.search(r"(transaction.*(key|id))|unique", v, re.I)]
        for f in cands:
            rr = self.c.post("/api/v2/search/spending_by_transaction/",
                             {"filters": filters, "fields": ["Transaction Amount", f],
                              "limit": 3, "page": 1, "sort": "Transaction Amount",
                              "order": "desc"}, label="txn_id_field_probe", retry=False)
            rows = (rr.data or {}).get("results", []) if rr.ok else []
            if rows and all(_filled(x.get(f)) for x in rows):
                return f
        return None

    def c06_fields(self) -> CheckResult:
        seg = self.segment_filters("naics")
        txn = self._discover("/api/v2/search/spending_by_transaction/", TXN_FIELD_CANDIDATES,
                             seg, "Transaction Amount", "txn")
        award = self._discover("/api/v2/search/spending_by_award/", AWARD_FIELD_CANDIDATES,
                               seg, "Award Amount", "award")
        id_field = self._probe_transaction_id_field(txn.get("valid_values_reported_by_api", []), seg)
        self.txn_fields = txn["available"] or list(REQUIRED_TXN_FIELDS)
        if id_field and id_field not in self.txn_fields:
            self.txn_fields.append(id_field)
        self.award_fields = award["available"] or ["Award ID", "Award Amount"]
        missing_required = [f for f in REQUIRED_TXN_FIELDS if f not in txn["available"]]
        self.facts["transaction_fields_available"] = txn["available"]
        self.facts["transaction_search_valid_fields"] = txn.get("valid_values_reported_by_api")
        self.facts["award_fields_available"] = award["available"]
        self.facts["transaction_id_field_in_search"] = id_field

        def has_any(avail, *names):
            return any(n in avail for n in names)

        coverage = {
            "recipient_uei_in_txn_search": has_any(txn["available"], "Recipient UEI"),
            "naics_in_txn_search": has_any(txn["available"], "naics_code", "NAICS"),
            "psc_in_txn_search": has_any(txn["available"], "product_or_service_code", "PSC"),
            "funding_agency_in_txn_search": has_any(txn["available"], "Funding Agency"),
            "transaction_unique_id_in_txn_search": id_field,
            "parent_recipient_in_search": False,
            "note": "Parent recipient UEI/name, contracting office, recipient city, last-modified "
                    "date and contract_transaction_unique_key come from the official prime "
                    "transaction download files (verified from the file header in Phase 2).",
        }
        status = FAIL if missing_required else PASS
        return CheckResult("", "", status,
                           f"Transaction search returns {len(txn['available'])} of "
                           f"{len(TXN_FIELD_CANDIDATES)} candidate fields; award search "
                           f"{len(award['available'])} of {len(AWARD_FIELD_CANDIDATES)}. "
                           f"Transaction unique ID in search: {id_field or 'not available'}."
                           + (f" MISSING required: {missing_required}" if missing_required else ""),
                           {"transaction_search": txn, "award_search": award,
                            "coverage": coverage})

    # ------------------------------------------------------------ C07
    def _txn_page(self, filters: dict, sort: str, page: int = 1, limit: int | None = None,
                  label: str = "txn_sample") -> ApiResponse:
        fields = list(dict.fromkeys(self.txn_fields + [sort]))
        return self.c.post("/api/v2/search/spending_by_transaction/",
                           {"filters": filters, "fields": fields, "page": page,
                            "limit": limit or self.sample_limit, "sort": sort, "order": "desc"},
                           label=label)

    def c07_txn_sample(self) -> CheckResult:
        seg = self.segment_filters("naics")
        rows: list[dict] = []
        shapes = {}
        for sort in ("Transaction Amount", "Action Date"):
            if sort not in self.txn_fields:
                continue
            r = self._txn_page(seg, sort, label=f"txn_sample_by_{sort}")
            if not r.ok:
                return CheckResult("", "", FAIL, f"Sample request failed: {r.error_detail()}")
            _, err = schemas.validate(schemas.SearchPage, r.data)
            shapes[sort] = err or "ok"
            rows.extend(r.data.get("results", []))
        if not rows:
            return CheckResult("", "", WARN, "No transactions returned for the segment.",
                               {"response_shape": shapes})

        dates = [parse_date(r.get("Action Date")) for r in rows]
        out_of_window = [str(d) for d in dates if d and not (FY_START <= d <= FY_END)]
        unparsed = sum(1 for d in dates if d is None)
        amounts = [_num(r.get("Transaction Amount")) for r in rows]
        nums = [a for a in amounts if a is not None]
        completeness = {
            f: round(sum(_filled(r.get(f)) for r in rows) / len(rows) * 100, 1)
            for f in self.txn_fields
        }

        def distinct(*keys):
            return len({_code(_first(r, *keys)) for r in rows if _first(r, *keys)})

        examples = [
            {"Award ID": r.get("Award ID"), "Mod": r.get("Mod"),
             "Recipient": r.get("Recipient Name"), "UEI": r.get("Recipient UEI"),
             "Action Date": r.get("Action Date"),
             "Transaction Amount": _num(r.get("Transaction Amount")),
             "Awarding Sub Agency": r.get("Awarding Sub Agency")}
            for r in rows[:5]
        ]
        details = {
            "rows_examined": len(rows),
            "response_shape_validation": shapes,
            "action_dates_min": str(min(d for d in dates if d)) if any(dates) else None,
            "action_dates_max": str(max(d for d in dates if d)) if any(dates) else None,
            "action_dates_outside_fy2026": out_of_window[:20],
            "action_dates_unparseable": unparsed,
            "amount_numeric_pct": round(len(nums) / len(rows) * 100, 1),
            "negative_amounts": sum(1 for a in nums if a < 0),
            "zero_amounts": sum(1 for a in nums if a == 0),
            "field_completeness_pct": completeness,
            "distinct_recipient_ueis": distinct("Recipient UEI"),
            "distinct_awarding_sub_agencies": distinct("Awarding Sub Agency"),
            "distinct_naics": distinct("naics_code", "NAICS"),
            "distinct_psc": distinct("product_or_service_code", "PSC"),
            "example_rows_real_data": examples,
            "sample_note": "Sample is sorted by amount and by date, so it is NOT "
                           "statistically representative; completeness rates are indicative.",
        }
        self.facts["txn_date_filter_bounds_action_date"] = not out_of_window and unparsed == 0
        if out_of_window or unparsed:
            status, msg = FAIL, (f"{len(out_of_window)} rows outside FY2026, {unparsed} unparseable "
                                 f"dates – time_period does not strictly bound Action Date.")
        else:
            status, msg = PASS, (f"All {len(rows)} sampled transactions have Action Date within "
                                 f"FY2026 ({details['action_dates_min']} – "
                                 f"{details['action_dates_max']}); "
                                 f"{details['negative_amounts']} de-obligations seen.")
        return CheckResult("", "", status, msg, details)

    # ------------------------------------------------------------ C08
    def c08_pagination(self) -> CheckResult:
        seg = self.segment_filters("naics")
        sort = "Transaction Amount"
        details: dict[str, Any] = {}
        p1 = self._txn_page(seg, sort, page=1, label="page_1")
        p2 = self._txn_page(seg, sort, page=2, label="page_2")
        rows1 = (p1.data or {}).get("results", [])
        rows2 = (p2.data or {}).get("results", [])
        details["page1_rows"], details["page2_rows"] = len(rows1), len(rows2)
        details["page1_hasNext"] = ((p1.data or {}).get("page_metadata") or {}).get("hasNext")
        details["page1_page_metadata"] = (p1.data or {}).get("page_metadata")

        over = self._txn_page(seg, sort, page=1, limit=101, label="limit_101")
        details["limit_101"] = {"http_status": over.status_code,
                                "rows": len((over.data or {}).get("results", [])),
                                "error": None if over.ok else over.error_detail()}
        deep_page = 101  # offset 10,000 with limit 100
        if self.txn_count is None or self.txn_count > deep_page * 100:
            deep = self._txn_page(seg, sort, page=deep_page, label="deep_page_101")
            details["deep_page_101_txn"] = {
                "http_status": deep.status_code,
                "rows": len((deep.data or {}).get("results", [])),
                "error": None if deep.ok else deep.error_detail()}
        else:
            details["deep_page_101_txn"] = "not tested: segment smaller than 10,100 rows"
        self.facts["max_page_size_accepted"] = 100 if not over.ok else "> 100"

        dup = self._duplicate_inspection(rows1 + rows2)
        self.duplicate_inspection = dup
        details["duplicate_inspection_summary"] = dup["summary"]

        issues = []
        if dup["summary"]["exact_duplicate_transaction_ids"]:
            issues.append(f"{dup['summary']['exact_duplicate_transaction_ids']} transaction ID(s) "
                          f"returned more than once by search – search rows must be de-duplicated "
                          f"on transaction ID before summing")
        if dup["summary"]["unresolved_groups"]:
            issues.append(f"{dup['summary']['unresolved_groups']} identical-looking group(s) "
                          f"could not be resolved to transaction IDs")
        deep = details.get("deep_page_101_txn")
        if isinstance(deep, dict) and deep["http_status"] != 200:
            issues.append("deep pagination beyond 10,000 rows rejected – use downloads for bulk")
        s = dup["summary"]
        msg = (f"Dedup key = transaction unique ID. {s['rows_examined']} rows examined: "
               f"{s['exact_duplicate_transaction_ids']} exact duplicate transaction ID(s); "
               f"{s['identical_groups_distinct_transactions']} identical-looking group(s) are "
               f"distinct transactions; {s['contract_numbers_with_multiple_transactions']} "
               f"contract number(s) carry multiple distinct transactions.")
        if issues:
            msg += " Issues: " + "; ".join(issues) + "."
        return CheckResult("", "", WARN if issues else PASS, msg, details)

    def _award_transactions(self, gid: str, cache: dict[str, dict]) -> dict:
        if gid in cache:
            return cache[gid]
        txns: list[dict] = []
        complete = False
        for page in range(1, 6):
            t = self.c.post("/api/v2/transactions/",
                            {"award_id": gid, "page": page, "limit": 100,
                             "sort": "action_date", "order": "asc"},
                            label="dup_award_transactions")
            if not t.ok:
                break
            txns.extend(t.data.get("results", []))
            if not (t.data.get("page_metadata") or {}).get("hasNext"):
                complete = True
                break
        cache[gid] = {"transactions": txns, "complete": complete}
        return cache[gid]

    def _duplicate_inspection(self, rows: list[dict], max_groups: int = 20) -> dict[str, Any]:
        """Classify search rows using the transaction unique ID – never by contract
        number, modification, recipient or amount alone."""
        id_field = self.facts.get("transaction_id_field_in_search")
        method = "transaction ID field in search results" if id_field else \
            "transaction IDs resolved via /api/v2/transactions/ for each award"

        def look(r):  # what a person would see as 'the same row'
            return (r.get("Award ID"), r.get("Mod"), str(r.get("Action Date"))[:10],
                    _num(r.get("Transaction Amount")))

        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            groups.setdefault(look(r), []).append(r)
        identical = {k: v for k, v in groups.items() if len(v) > 1}

        exact, distinct, unresolved = [], [], []
        cache: dict[str, dict] = {}
        record_keys: list[str] = []
        for k, members in list(identical.items())[:max_groups]:
            base = {"award_id_piid": k[0], "mod": k[1], "action_date": k[2], "amount": k[3],
                    "rows_returned": len(members),
                    "generated_internal_ids": sorted({str(m.get("generated_internal_id"))
                                                      for m in members}),
                    "recipient": members[0].get("Recipient Name")}
            if id_field:
                ids = [m.get(id_field) for m in members]
                base["transaction_ids"] = ids
                if len(set(ids)) < len(ids):
                    exact.append(base)
                else:
                    distinct.append(dict(base, reason="different transaction IDs"))
                continue
            gids = [g for g in base["generated_internal_ids"] if g and g != "None"]
            matched: list[str] = []
            complete = True
            for g in gids:
                info = self._award_transactions(g, cache)
                complete &= info["complete"]
                for t in info["transactions"]:
                    record_keys = record_keys or sorted(t.keys())
                    tid = t.get("id") or t.get("transaction_unique_id") or \
                        t.get("generated_unique_transaction_id")
                    if (str(t.get("modification_number")) == str(k[1])
                            and str(t.get("action_date"))[:10] == k[2]
                            and _num(t.get("federal_action_obligation")) is not None
                            and k[3] is not None
                            and abs(_num(t.get("federal_action_obligation")) - k[3]) < 0.005):
                        matched.append(str(tid))
            base["transaction_ids_in_source"] = matched
            base["award_transaction_list_complete"] = complete
            if len(gids) > 1:
                distinct.append(dict(base, reason="same contract number (PIID) on different "
                                                  "awards – distinct transactions"))
            elif len(set(matched)) >= len(members):
                distinct.append(dict(base, reason="same contract/mod/date/amount but different "
                                                  "transaction IDs"))
            elif len(set(matched)) == 1:
                exact.append(dict(base, reason="one transaction ID returned "
                                               f"{len(members)} times by search"))
            elif matched:
                exact.append(dict(base, reason=f"{len(members)} rows for {len(set(matched))} "
                                               "distinct transaction IDs (partial duplication)"))
            else:
                unresolved.append(dict(base, reason="no matching transaction found in award "
                                                    "transaction list"))

        # Contract numbers carrying several rows that are NOT the same transaction
        by_piid: dict[str, list[dict]] = {}
        for r in rows:
            by_piid.setdefault(str(r.get("Award ID")), []).append(r)
        shared = []
        for piid, members in by_piid.items():
            looks = {look(m) for m in members}
            if len(looks) > 1:
                shared.append({"award_id_piid": piid, "rows": len(members),
                               "distinct_mod_date_amount": len(looks),
                               "distinct_mods": sorted({str(m.get("Mod")) for m in members}),
                               "distinct_awards": len({str(m.get("generated_internal_id"))
                                                       for m in members})})
        shared.sort(key=lambda x: -x["rows"])
        return {
            "dedup_key": "USAspending transaction unique ID "
                         "(contract_transaction_unique_key in download files)",
            "never_dedup_on": ["contract number (PIID)", "modification number", "recipient",
                               "amount"],
            "method": method,
            "transaction_record_keys_seen": record_keys,
            "summary": {
                "rows_examined": len(rows),
                "identical_looking_groups": len(identical),
                "groups_inspected": min(len(identical), max_groups),
                "exact_duplicate_transaction_ids": len(exact),
                "identical_groups_distinct_transactions": len(distinct),
                "unresolved_groups": len(unresolved),
                "contract_numbers_with_multiple_transactions": len(shared),
            },
            "exact_duplicate_transaction_ids": exact,
            "shared_contract_number_different_transaction_ids": distinct,
            "contract_numbers_with_multiple_distinct_transactions": shared[:25],
            "unresolved": unresolved,
        }

    # ------------------------------------------------------------ C09
    def c09_award_semantics(self) -> CheckResult:
        seg = self.segment_filters("naics")
        fields = list(dict.fromkeys(["Award ID", "Award Amount"] + [
            f for f in ("Recipient Name", "Start Date", "End Date", "Base Obligation Date",
                        "Last Modified Date", "generated_internal_id") if f in self.award_fields]))
        r = self.c.post("/api/v2/search/spending_by_award/",
                        {"filters": seg, "fields": fields, "limit": 100, "page": 1,
                         "sort": "Award Amount", "order": "desc"}, label="award_sample")
        if not r.ok:
            return CheckResult("", "", FAIL, f"spending_by_award failed: {r.error_detail()}")
        awards = r.data.get("results", [])
        starts = [parse_date(a.get("Start Date")) for a in awards]
        started_before = sum(1 for d in starts if d and d < FY_START)

        # date_type variants – counts only
        variants = {}
        for dtp in (None, "action_date", "date_signed", "new_awards_only", "last_modified_date"):
            f = self.segment_filters("naics", award_types=(), date_type=dtp)
            res = self._count("/api/v2/search/spending_by_award_count/", f,
                              f"award_count_datetype_{dtp or 'default'}")
            variants[dtp or "(default)"] = res

        # Deep-dive: lifetime award values vs FY2026 transaction sum
        deep = []
        for a in [a for a in awards if a.get("generated_internal_id")][:3]:
            gid = a["generated_internal_id"]
            det = self.c.get(f"/api/v2/awards/{gid}/", label="award_detail")
            d = det.data if det.ok and isinstance(det.data, dict) else {}
            txns: list[dict] = []
            complete = False
            for page in range(1, 6):
                t = self.c.post("/api/v2/transactions/",
                                {"award_id": gid, "page": page, "limit": 100,
                                 "sort": "action_date", "order": "asc"},
                                label="award_transactions")
                if not t.ok:
                    break
                txns.extend(t.data.get("results", []))
                if not (t.data.get("page_metadata") or {}).get("hasNext"):
                    complete = True
                    break
            fy_sum = sum(_num(x.get("federal_action_obligation")) or 0
                         for x in txns if in_fy2026(x.get("action_date")))
            all_sum = sum(_num(x.get("federal_action_obligation")) or 0 for x in txns)
            deep.append({
                "award_id": a.get("Award ID"), "generated_internal_id": gid,
                "recipient": a.get("Recipient Name"),
                "search_award_amount": _num(a.get("Award Amount")),
                "detail_total_obligation": _num(d.get("total_obligation")),
                "detail_base_exercised_options": _num(d.get("base_exercised_options")),
                "detail_base_and_all_options": _num(d.get("base_and_all_options")),
                "detail_keys": sorted(d.keys())[:60],
                "start_date": a.get("Start Date"),
                "transactions_retrieved": len(txns),
                "transaction_list_complete": complete,
                "fy2026_obligation_sum": round(fy_sum, 2),
                "all_transactions_sum": round(all_sum, 2),
                "fy2026_transactions": sum(1 for x in txns if in_fy2026(x.get("action_date"))),
            })

        diff = [x for x in deep if x["search_award_amount"] is not None
                and abs(x["search_award_amount"] - x["fy2026_obligation_sum"]) > 0.01]
        self.facts["award_amount_is_lifetime"] = bool(diff) or started_before > 0
        details = {
            "awards_sampled": len(awards),
            "awards_started_before_fy2026": started_before,
            "date_type_award_counts": variants,
            "award_vs_fy2026_deep_dive": deep,
            "interpretation": "spending_by_award returns awards with activity in the window, "
                              "but 'Award Amount' is the award's lifetime figure. FY2026 "
                              "obligations must be summed from transactions dated in FY2026.",
        }
        if diff or started_before:
            status = PASS
            msg = (f"Confirmed: {started_before}/{len(awards)} sampled awards began before FY2026 and "
                   f"{len(diff)}/{len(deep)} deep-dived awards have Award Amount ≠ FY2026 "
                   f"transaction sum. Award Amount must not be used as FY2026 obligations.")
        else:
            status = WARN
            msg = "Could not demonstrate a difference between Award Amount and FY2026 sums in sample."
        return CheckResult("", "", status, msg, details)

    # ------------------------------------------------------------ C10
    def c10_reconciliation(self) -> CheckResult:
        seg = self.segment_filters("naics")
        cand_rows: list[dict] = []
        for page in (1, 3):
            r = self.c.post("/api/v2/search/spending_by_category/recipient/",
                            {"filters": seg, "limit": 100, "page": page},
                            label=f"recon_recipients_p{page}")
            if r.ok:
                cand_rows.extend(r.data.get("results", []))
        cands = [x for x in cand_rows if (_num(x.get("amount")) or 0) > 0
                 and (x.get("uei") or x.get("code"))]
        cands.sort(key=lambda x: _num(x.get("amount")) or 0)
        uei_field = "Recipient UEI" in self.txn_fields
        attempts = []
        for cand in cands[:4]:
            uei = cand.get("uei") or cand.get("code")
            f = dict(seg)
            f["recipient_search_text"] = [uei]
            rows: list[dict] = []
            complete = False
            for page in range(1, 6):
                t = self._txn_page(f, "Transaction Amount", page=page, label="recon_txns")
                if not t.ok:
                    break
                rows.extend(t.data.get("results", []))
                if not (t.data.get("page_metadata") or {}).get("hasNext"):
                    complete = True
                    break
            if uei_field:
                rows = [x for x in rows if x.get("Recipient UEI") == uei]
            txn_sum = sum(_num(x.get("Transaction Amount")) or 0 for x in rows)
            agg = _num(cand.get("amount")) or 0
            rel = abs(txn_sum - agg) / abs(agg) if agg else None
            attempt = {"recipient": cand.get("name"), "uei": uei,
                       "aggregate_amount_spending_by_category": agg,
                       "sum_of_transactions": round(txn_sum, 2),
                       "transactions": len(rows), "transaction_pull_complete": complete,
                       "relative_difference": rel}
            attempts.append(attempt)
            if complete and rel is not None and rel <= self.settings.reconciliation.tolerance_ratio:
                self.facts["reconciliation_passed"] = True
                return CheckResult("", "", PASS,
                                   f"Σ transactions for {cand.get('name')} = {_usd(txn_sum)} vs "
                                   f"API aggregate {_usd(agg)} (diff {rel:.4%}). FY2026 obligation "
                                   f"= sum of FY2026 transaction amounts is confirmed.",
                                   {"attempts": attempts, "tolerance": self.settings.reconciliation.tolerance_ratio})
        self.facts["reconciliation_passed"] = False
        return CheckResult("", "", WARN,
                           "No recipient reconciled within tolerance using a bounded pull; "
                           "review attempts (recipient name/UEI matching or pull size).",
                           {"attempts": attempts, "tolerance": self.settings.reconciliation.tolerance_ratio})

    # ------------------------------------------------------------ C11
    def c11_bulk_listing(self) -> CheckResult:
        """List (never download) the official FY2026 contract archive files.
        list_monthly_files needs the bulk-download toptier_agency_id, which is NOT
        the agency_id from /references/toptier_agencies/ – resolve it from
        /bulk_download/list_agencies/ by toptier code."""
        out: dict[str, Any] = {}
        la = self.c.post("/api/v2/bulk_download/list_agencies/", {"type": "award_agencies"},
                         label="bulk_list_agencies")
        scope_codes = {a.get("toptier_code"): a.get("agency_name")
                       for a in self.facts.get("scope_agencies", [])}
        resolved: dict[str, Any] = {}
        if la.ok:
            ag = (la.data or {}).get("agencies", {})
            pools = ag.values() if isinstance(ag, dict) else [ag]
            for pool in pools:
                for a in pool or []:
                    if isinstance(a, dict) and a.get("toptier_code") in scope_codes:
                        resolved[a["toptier_code"]] = a
        out["list_agencies"] = {"http_status": la.status_code,
                                "resolved_scope_agencies": resolved,
                                "error": None if la.ok else la.error_detail()}
        self.facts["bulk_toptier_agency_ids"] = {c: a.get("toptier_agency_id")
                                                 for c, a in resolved.items()}
        bodies = {"all_agencies": {"agency": "all", "fiscal_year": FISCAL_YEAR, "type": "contracts"}}
        for code, a in resolved.items():
            bodies[scope_codes[code]] = {"agency": a.get("toptier_agency_id"),
                                         "fiscal_year": FISCAL_YEAR, "type": "contracts"}
        for name, body in bodies.items():
            r = self.c.post("/api/v2/bulk_download/list_monthly_files/", body,
                            label=f"monthly_files_{name}")
            files = (r.data or {}).get("monthly_files", []) if r.ok else []
            out[name] = {"http_status": r.status_code, "request": body,
                         "files": files[:20], "file_count": len(files),
                         "error": None if r.ok else r.error_detail()}
        missing = [n for c, n in scope_codes.items() if c not in resolved]
        scope_ok = all(out[n]["file_count"] for n in scope_codes.values() if n in out)
        status = PASS if out["all_agencies"]["file_count"] and not missing and scope_ok else WARN
        return CheckResult("", "", status,
                           f"Archive files listed (not downloaded). All-agency files: "
                           f"{out['all_agencies']['file_count']}; scope agency IDs resolved: "
                           f"{self.facts['bulk_toptier_agency_ids'] or 'none'}"
                           + (f"; unresolved: {missing}" if missing else "") + ".", out)

    # ------------------------------------------------------------ C12
    def _month_obligations(self, filters: dict, label: str) -> tuple[dict[int, float | None], dict]:
        r = self.c.post("/api/v2/search/spending_over_time/",
                        {"group": "month", "filters": filters}, label=label)
        out: dict[int, float | None] = {}
        for x in (r.data or {}).get("results", []) if r.ok else []:
            tp = x.get("time_period", {})
            if str(tp.get("fiscal_year")) != str(FISCAL_YEAR):
                continue
            amt = _num(x.get("aggregated_amount"))
            if amt is None:
                amt = _num(x.get("Contract_Obligations"))
            m = int(tp.get("month"))
            fm = m if self.month_numbering != "calendar" else calendar_to_fiscal_month(m)
            out[fm] = amt
        return out, {"http_status": r.status_code, "error": None if r.ok else r.error_detail()}

    def _latest_action_date(self, filters: dict, label: str) -> str | None:
        r = self.c.post("/api/v2/search/spending_by_transaction/",
                        {"filters": filters, "fields": ["Award ID", "Action Date"],
                         "limit": 1, "page": 1, "sort": "Action Date", "order": "desc"},
                        label=label)
        rows = (r.data or {}).get("results", []) if r.ok else []
        d = parse_date(rows[0].get("Action Date")) if rows else None
        return d.isoformat() if d else None

    def c12_reporting_lag(self) -> CheckResult:
        cfg = self.settings.reporting_lag
        thr = cfg.low_ratio_threshold
        # 1. How does spending_over_time number months? Ask for March 2026 only.
        f = self.base_filters()
        f["time_period"] = [{"start_date": "2026-03-01", "end_date": "2026-03-31"}]
        r = self.c.post("/api/v2/search/spending_over_time/", {"group": "month", "filters": f},
                        label="month_numbering_check")
        months_seen = sorted({str(x.get("time_period", {}).get("month"))
                              for x in (r.data or {}).get("results", []) if r.ok
                              and (_num(x.get("aggregated_amount")) or 0) != 0})
        self.month_numbering = ("fiscal" if months_seen == ["6"] else
                                "calendar" if months_seen == ["3"] else "fiscal_assumed")
        self.facts["spending_over_time_month_numbering"] = self.month_numbering

        # 2. Obligations and transaction counts per fiscal month (scope = agency scope, A–D)
        scope_dollars, scope_meta = self._month_obligations(self.base_filters(), "monthly_scope")
        seg_dollars, seg_meta = self._month_obligations(self.segment_filters("naics"),
                                                        "monthly_segment_naics")
        counts: dict[int, int | None] = {}
        for fm in range(1, 13):
            start, end = fiscal_month_range(fm)
            ff = self.base_filters()
            ff["time_period"] = [{"start_date": start.isoformat(), "end_date": end.isoformat()}]
            rc = self.c.post("/api/v2/search/spending_by_transaction_count/", {"filters": ff},
                             label=f"monthly_count_fm{fm}")
            res = (rc.data or {}).get("results", {}) if rc.ok else {}
            counts[fm] = int(res["contracts"]) if isinstance(res, dict) and \
                res.get("contracts") is not None else None

        # 3. Which months are 'most recent'? Those already started at the as-of date.
        as_of = parse_date(self.facts.get("source_last_updated")) or datetime.now(timezone.utc).date()
        lag_cutoff = as_of - timedelta(days=cfg.dod_reporting_lag_days)
        eligible = [fm for fm in range(1, 13) if fiscal_month_range(fm)[0] <= as_of]
        trailing = eligible[-cfg.trailing_months_to_evaluate:]
        baseline = [fm for fm in eligible if fm not in trailing]

        def med(vals):
            vals = [v for v in vals if v is not None]
            return statistics.median(vals) if vals else None

        base_d = med([scope_dollars.get(fm) for fm in baseline])
        base_c = med([counts.get(fm) for fm in baseline])
        table = []
        for fm in range(1, 13):
            start, end = fiscal_month_range(fm)
            d, c = scope_dollars.get(fm), counts.get(fm)
            row = {"fiscal_month": fm, "calendar_month": start.strftime("%Y-%m"),
                   "scope_obligations": d, "scope_transaction_count": c,
                   "segment_obligations": seg_dollars.get(fm),
                   "evaluated": fm in trailing,
                   "within_dod_lag_window": end > lag_cutoff,
                   "dollar_ratio": round(d / base_d, 3) if d is not None and base_d else None,
                   "count_ratio": round(c / base_c, 3) if c is not None and base_c else None,
                   "classification": "baseline" if fm in baseline else
                                     ("not_yet_started" if fm not in eligible else None)}
            if fm in trailing:
                if row["count_ratio"] is None:
                    row["classification"] = ("low_obligations_count_unavailable"
                                             if row["dollar_ratio"] is not None
                                             and row["dollar_ratio"] < thr else
                                             "normal_count_unavailable")
                elif row["count_ratio"] < thr:
                    row["classification"] = "likely_missing_reporting"
                elif row["dollar_ratio"] is not None and row["dollar_ratio"] < thr:
                    row["classification"] = "low_obligations_normal_reporting"
                else:
                    row["classification"] = "normal"
            table.append(row)

        missing = [x for x in table if x["classification"] == "likely_missing_reporting"]
        first_missing = min((x["fiscal_month"] for x in missing), default=None)
        if first_missing:
            normal_through = (fiscal_month_range(first_missing - 1)[1].isoformat()
                              if first_missing > 1 else None)
        elif eligible:
            normal_through = min(fiscal_month_range(eligible[-1])[1], as_of).isoformat()
        else:
            normal_through = None
        latest_scope = self._latest_action_date(self.base_filters(), "latest_action_scope")
        latest_seg = self._latest_action_date(self.segment_filters("naics"), "latest_action_segment")
        self.facts.update({
            "data_through_latest_action_date_scope": latest_scope,
            "data_through_latest_action_date_segment": latest_seg,
            "reporting_volume_normal_through": normal_through,
            "months_likely_missing_reporting": [x["calendar_month"] for x in missing],
            "months_low_obligations_normal_reporting":
                [x["calendar_month"] for x in table
                 if x["classification"] == "low_obligations_normal_reporting"],
            "source_as_of": as_of.isoformat(),
        })
        self.reporting_lag = {
            "settings": cfg.model_dump(), "as_of": as_of.isoformat(),
            "dod_lag_cutoff": lag_cutoff.isoformat(),
            "baseline_months": baseline, "evaluated_months": trailing,
            "baseline_median_obligations": base_d, "baseline_median_transaction_count": base_c,
            "months": table, "month_numbering": self.month_numbering,
            "data_through": {"latest_action_date_scope": latest_scope,
                             "latest_action_date_segment": latest_seg,
                             "reporting_volume_normal_through": normal_through},
            "api_status": {"scope": scope_meta, "segment": seg_meta},
            "policy": "Missing obligations are NOT estimated or extrapolated. Rankings and "
                      "shares are provisional until the scheduled full reload "
                      f"({self.settings.refresh.scheduled_full_reload.isoformat()}).",
        }
        if missing:
            return CheckResult("", "", WARN,
                               f"Latest FY2026 action date available: {latest_scope}. Reporting "
                               f"volume normal through {normal_through}; "
                               f"{', '.join(x['calendar_month'] for x in missing)} show transaction "
                               f"counts below {thr:.0%} of baseline → likely missing reporting "
                               f"(DoD ~{cfg.dod_reporting_lag_days}-day lag). Data is PROVISIONAL; "
                               f"nothing is estimated.", {"see": "reporting_lag section"})
        return CheckResult("", "", PASS,
                           f"No missing-reporting signal in the last {len(trailing)} months. "
                           f"Latest action date: {latest_scope}.", {"see": "reporting_lag section"})

    # ------------------------------------------------------------ report
    def build_report(self) -> dict[str, Any]:
        finished = datetime.now(timezone.utc)
        counts = {s: sum(1 for c in self.checks if c.status == s)
                  for s in (PASS, WARN, FAIL, INFO, SKIP)}
        return {
            "report_type": "phase1_api_validation",
            "report_version": 2,
            "synthetic_data": False,
            "data_source": self.c.base_url,
            "run": {"started_at": self.started_at.isoformat(), "finished_at": finished.isoformat(),
                    "duration_seconds": round((finished - self.started_at).total_seconds(), 1)},
            "reporting_period": {"fiscal_year": FISCAL_YEAR, "start_date": FY_START.isoformat(),
                                 "end_date": FY_END.isoformat(), "label": FY_LABEL},
            "market": {"key": self.market_key, "name": self.m.name, "status": self.m.status,
                       "definition_note": self.m.definition_note,
                       "definition_hash": definition_hash(self.m),
                       "agency_scope": self.m.agency_filters(),
                       "included_tiers": self.m.included_tiers,
                       "included_naics": self.m.naics(), "included_psc": self.m.psc()},
            "settings": self.settings.model_dump(mode="json"),
            "summary": counts,
            "checks": [asdict(c) for c in self.checks],
            "api_facts": self.facts,
            "duplicate_inspection": self.duplicate_inspection,
            "reporting_lag": self.reporting_lag,
            "code_validation": self.code_validation,
            "measure_definitions": MEASURE_DEFINITIONS,
            "recommendations": self._recommendations(),
            "call_stats": asdict(self.c.stats),
        }

    def _recommendations(self) -> list[str]:
        rec = []
        f = self.facts
        if f.get("txn_date_filter_bounds_action_date"):
            rec.append("Use transaction-level data filtered on action_date for all FY2026 "
                       "obligation metrics (C07).")
        if f.get("award_amount_is_lifetime"):
            rec.append("Never sum spending_by_award 'Award Amount' as FY2026 spending; store it "
                       "only as labelled lifetime context (C09).")
        if f.get("reconciliation_passed") is False:
            rec.append("Investigate C10 reconciliation before the full load.")
        dup = (self.duplicate_inspection or {}).get("summary", {})
        if dup.get("exact_duplicate_transaction_ids"):
            rec.append("Search endpoints can return the same transaction ID more than once: never "
                       "sum search rows; load from download files keyed on "
                       "contract_transaction_unique_key (C08).")
        lim = _num(f.get("download_maximum_transaction_limit"))
        cnt = _num(f.get("download_calculated_transaction_count"))
        if lim and cnt:
            rec.append(f"Segment download size {cnt:,.0f} rows vs limit {lim:,.0f}: "
                       + ("single download job per basis (NAICS, PSC) is feasible." if cnt <= lim
                          else "split extraction by awarding sub-agency × fiscal month."))
        if f.get("months_likely_missing_reporting"):
            rec.append(f"Show '{self.settings.refresh.banner_text}' with data-through date "
                       f"{f.get('data_through_latest_action_date_scope')} and reporting volume "
                       f"normal through {f.get('reporting_volume_normal_through')}; full reload "
                       f"scheduled {self.settings.refresh.scheduled_full_reload.isoformat()}. "
                       f"No estimation or extrapolation of missing months.")
        cv = self.code_validation
        if cv:
            for kind in ("naics", "psc"):
                weak = [e["code"] for e in cv.get(kind, []) if e["status"] != "active"
                        and e["tier"] != "review"]
                if weak:
                    rec.append(f"Review included {kind.upper()} codes without FY2026 activity: {weak}.")
            cand = cv.get("candidates_for_review", {})
            if cand.get("naics") or cand.get("psc"):
                rec.append("New active codes in related families appear under "
                           "'candidates_for_review' (documented exclusions are omitted). "
                           "Nothing was added automatically.")
        return rec


MEASURE_DEFINITIONS = [
    {"measure": "FY2026 obligations (primary ranking metric)",
     "definition": "Σ federal_action_obligation of contract transactions (types A–D) with "
                   "action_date 2025-10-01..2026-09-30. Includes de-obligations.",
     "use": "Rankings, market share, all FY2026 KPIs."},
    {"measure": "Individual transaction amount",
     "definition": "federal_action_obligation ('Transaction Amount') of one modification/action.",
     "use": "Transaction lists, drill-down."},
    {"measure": "Current cumulative award value (obligated to date)",
     "definition": "Award-level total_obligation / total_dollars_obligated, all years.",
     "use": "Context only, labelled 'lifetime'. Never summed into FY2026 metrics."},
    {"measure": "Total award amount (ceiling)",
     "definition": "Award-level base_and_all_options / potential_total_value_of_award.",
     "use": "Context only, labelled 'ceiling'. Never summed into FY2026 metrics."},
]


# ------------------------------------------------------------------ markdown
def render_markdown(rep: dict[str, Any]) -> str:
    L: list[str] = []
    a = L.append
    a(f"# Phase 1 API Validation Report — {rep['reporting_period']['label']}")
    a("")
    a(f"- **Data source:** {rep['data_source']} (public API, no credentials; synthetic data: "
      f"{'yes' if rep['synthetic_data'] else 'no'})")
    a(f"- **Run:** {rep['run']['started_at']} → {rep['run']['finished_at']} "
      f"({rep['run']['duration_seconds']} s)")
    a(f"- **USAspending last updated:** {rep['api_facts'].get('source_last_updated', 'n/a')}")
    m = rep["market"]
    a(f"- **Market:** {m['name']} (`{m['key']}`, status: {m['status']}, "
      f"definition hash `{m['definition_hash']}`)")
    a(f"- **Agency scope:** {', '.join(x['name'] for x in m['agency_scope']) or 'all'}")
    s = rep["summary"]
    a(f"- **Result:** {s['PASS']} pass · {s['WARN']} warn · {s['FAIL']} fail · {s['SKIP']} skipped")
    cs = rep["call_stats"]
    a(f"- **API calls:** {cs['calls']} (retries {cs['retries']}, non-200 {cs['failures']})")
    a("")
    a("## Check summary")
    a("")
    a("| ID | Check | Status | Finding |")
    a("|---|---|---|---|")
    for c in rep["checks"]:
        a(f"| {c['check_id']} | {c['title']} | **{c['status']}** | "
          f"{c['summary'].replace('|', '/')} |")
    a("")
    if m.get("definition_note"):
        a(f"> **Market definition note:** {m['definition_note'].strip()}")
        a("")
    lag = rep.get("reporting_lag") or {}
    if lag:
        dt = lag["data_through"]
        a("## Reporting lag and data-through date (C12)")
        a("")
        a(f"- **Latest FY2026 action date available (DoD scope):** {dt['latest_action_date_scope']}")
        a(f"- **Latest action date in segment:** {dt['latest_action_date_segment']}")
        a(f"- **Reporting volume normal through:** {dt['reporting_volume_normal_through']}")
        a(f"- **Source as-of:** {lag['as_of']}; DoD lag cutoff ({lag['settings']['dod_reporting_lag_days']} days): "
          f"{lag['dod_lag_cutoff']}; month numbering: {lag['month_numbering']}")
        a(f"- **Rule:** evaluate last {lag['settings']['trailing_months_to_evaluate']} months; "
          f"threshold {lag['settings']['low_ratio_threshold']:.0%} of baseline median "
          f"(baseline months {lag['baseline_months']}).")
        a(f"- {lag['policy']}")
        a("")
        a("| Fiscal month | Calendar | Scope obligations | Txn count | Dollar ratio | Count ratio | In lag window | Classification |")
        a("|---|---|---|---|---|---|---|---|")
        for x in lag["months"]:
            a(f"| {x['fiscal_month']} | {x['calendar_month']} | {_usd(x['scope_obligations'])} | "
              f"{x['scope_transaction_count'] if x['scope_transaction_count'] is not None else 'n/a'} | "
              f"{x['dollar_ratio'] if x['dollar_ratio'] is not None else '–'} | "
              f"{x['count_ratio'] if x['count_ratio'] is not None else '–'} | "
              f"{'yes' if x['within_dod_lag_window'] else 'no'} | {x['classification']} |")
        a("")
    dup = rep.get("duplicate_inspection") or {}
    if dup:
        sm = dup["summary"]
        a("## C08 duplicate inspection summary")
        a("")
        a(f"- **Dedup key:** {dup['dedup_key']}. Never de-duplicated on: "
          f"{', '.join(dup['never_dedup_on'])}.")
        a(f"- **Method:** {dup['method']}")
        a(f"- Rows examined: {sm['rows_examined']}; identical-looking groups (same PIID, mod, "
          f"date, amount): {sm['identical_looking_groups']} (inspected {sm['groups_inspected']})")
        a(f"- **Exact duplicate transaction IDs:** {sm['exact_duplicate_transaction_ids']}")
        a(f"- **Same contract number, different transaction IDs** (identical-looking): "
          f"{sm['identical_groups_distinct_transactions']}")
        a(f"- Contract numbers carrying several distinct transactions: "
          f"{sm['contract_numbers_with_multiple_transactions']}")
        a(f"- Unresolved: {sm['unresolved_groups']}")
        a("")
        for title, key in (("Exact duplicate transaction IDs", "exact_duplicate_transaction_ids"),
                           ("Shared contract number, different transaction IDs",
                            "shared_contract_number_different_transaction_ids"),
                           ("Unresolved", "unresolved")):
            items = dup.get(key) or []
            if not items:
                continue
            a(f"**{title}**")
            a("")
            a("| PIID | Mod | Date | Amount | Rows | Awards | Transaction IDs | Reason |")
            a("|---|---|---|---|---|---|---|---|")
            for g in items:
                ids = g.get("transaction_ids") or g.get("transaction_ids_in_source") or []
                a(f"| {g['award_id_piid']} | {g['mod']} | {g['action_date']} | {_usd(g['amount'])} | "
                  f"{g['rows_returned']} | {len(g['generated_internal_ids'])} | "
                  f"{', '.join(sorted(set(map(str, ids))))[:160] or '–'} | {g.get('reason', '')} |")
            a("")
    a("## Measure definitions (award amounts vs FY2026 obligations)")
    a("")
    a("| Measure | Definition | Use |")
    a("|---|---|---|")
    for d in rep["measure_definitions"]:
        a(f"| {d['measure']} | {d['definition']} | {d['use']} |")
    a("")
    c09 = next((c for c in rep["checks"] if c["check_id"] == "C09"), None)
    if c09 and c09["details"].get("award_vs_fy2026_deep_dive"):
        a("### Evidence: lifetime award amount vs FY2026 obligations (real awards)")
        a("")
        a("| Award ID | Recipient | Start | Award Amount (lifetime, search) | "
          "Total obligation (detail) | FY2026 Σ transactions | FY2026 txns | Complete |")
        a("|---|---|---|---|---|---|---|---|")
        for x in c09["details"]["award_vs_fy2026_deep_dive"]:
            a(f"| {x['award_id']} | {x['recipient']} | {x['start_date']} | "
              f"{_usd(x['search_award_amount'])} | {_usd(x['detail_total_obligation'])} | "
              f"{_usd(x['fy2026_obligation_sum'])} | {x['fy2026_transactions']} | "
              f"{x['transaction_list_complete']} |")
        a("")
    cv = rep.get("code_validation") or {}
    if cv:
        a("## Market code validation")
        a("")
        a(f"Scope: {cv['scope_description']}. Scan: {json.dumps(cv['scan'])}")
        a("")
        for kind in ("naics", "psc"):
            a(f"### {kind.upper()}")
            a("")
            a("| Code | Tier | Status | Official description (USAspending) | FY2026 obligations in scope | Share of scope |")
            a("|---|---|---|---|---|---|")
            for e in cv[kind]:
                share = f"{e['share_of_scope_pct']:.2f}%" if e["share_of_scope_pct"] else "–"
                a(f"| {e['code']} | {e['tier']} | {e['status']} | "
                  f"{e['official_description'] or '–'} | "
                  f"{_usd(e['fy2026_obligations_in_scope'])} | {share} |")
            a("")
        for kind in ("naics_prefixes", "psc_prefixes"):
            if not cv.get(kind):
                continue
            a(f"### {kind.replace('_', ' ').upper()} (code families)")
            a("")
            a("| Prefix | Tier | Status | Codes active | FY2026 obligations in scope | Share | Largest codes |")
            a("|---|---|---|---|---|---|---|")
            for e in cv[kind]:
                share = f"{e['share_of_scope_pct']:.2f}%" if e["share_of_scope_pct"] else "–"
                a(f"| {e['prefix']}* | {e['tier']} | {e['status']} | {e['codes_with_activity']} | "
                  f"{_usd(e['fy2026_obligations_in_scope'])} | {share} | "
                  f"{'; '.join(e['top_codes'][:3])} |")
            a("")
        ssi = cv["segment_size_indicators"]
        a(f"Included-code obligations — NAICS basis: {_usd(ssi['included_naics_obligations'])}; "
          f"PSC basis: {_usd(ssi['included_psc_obligations'])}. {ssi['note']}")
        a("")
        a("### Candidates for review (active FY2026 codes not in config)")
        a("")
        for kind in ("naics", "psc"):
            rows = cv["candidates_for_review"][kind]
            if not rows:
                continue
            a(f"**{kind.upper()}**")
            a("")
            a("| Code | Description | FY2026 obligations in scope |")
            a("|---|---|---|")
            for r in rows:
                a(f"| {r['code']} | {r['official_description']} | "
                  f"{_usd(r['fy2026_obligations_in_scope'])} |")
            a("")
    a("## Key API facts discovered")
    a("")
    for k, v in rep["api_facts"].items():
        a(f"- `{k}`: {json.dumps(v, default=str)[:400]}")
    a("")
    a("## Recommendations")
    a("")
    for r in rep["recommendations"]:
        a(f"- {r}")
    a("")
    a("## Check details")
    a("")
    for c in rep["checks"]:
        a(f"### {c['check_id']} — {c['title']} ({c['status']})")
        a("")
        a(c["summary"])
        a("")
        if c["details"] and c["check_id"] != "C04":
            a("```json")
            a(json.dumps(c["details"], indent=1, default=str)[:6000])
            a("```")
            a("")
    a("---")
    a("Federal contract obligations are not company revenue or profit. Market share figures "
      "describe the selected federal procurement dataset only, not the commercial market.")
    return "\n".join(L) + "\n"
