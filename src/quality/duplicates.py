"""Phase 2a – profile potential source duplicates in the official download data.

A *suspicious group* = 2+ records with DIFFERENT transaction unique IDs but the
same award ID (contract_award_unique_key), modification number, action date and
non-zero transaction amount. Nothing is excluded here: the profiler only
reports which fields differ inside each group so a counting rule can be chosen
on evidence.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

ID = "contract_transaction_unique_key"
AWARD = "contract_award_unique_key"
MOD = "modification_number"
DATE = "action_date"
AMOUNT = "federal_action_obligation"
KEY = [AWARD, MOD, DATE, "_amount_norm"]
IGNORE = {ID, "_market", "_basis", "_source_file", "_run_id", "_amount_norm", "_amount"}
PRIORITY_FIELDS = [
    "transaction_number", "action_type_code", "action_type", "last_modified_date",
    "correction_delete_ind", "initial_report_date", "awarding_office_code",
    "funding_office_code", "funding_sub_agency_code", "product_or_service_code",
    "naics_code", "recipient_uei", "transaction_description",
    "foreign_funding", "contract_bundling_code", "type_of_contract_pricing_code",
    "solicitation_identifier", "usaspending_permalink",
]


# Fields that describe the award or the recipient AS OF THE TIME a record was
# reported (award-level cumulative values, executive compensation from the
# entity registration, evaluation metadata, report timestamps). They can differ
# between two reports of the same action without making them different actions.
SNAPSHOT_FIELDS = {
    "transaction_number", "last_modified_date", "initial_report_date",
    "total_dollars_obligated", "base_and_exercised_options_value",
    "current_total_value_of_award", "base_and_all_options_value",
    "potential_total_value_of_award",
    "price_evaluation_adjustment_preference_percent_difference",
} | {f"highly_compensated_officer_{i}_{k}" for i in range(1, 6) for k in ("name", "amount")}


def _norm_amount(v: Any) -> str | None:
    try:
        return str(Decimal(str(v)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _differing_columns(members: pd.DataFrame, cols: list[str]) -> list[str]:
    out = []
    for c in cols:
        vals = members[c].astype("string").fillna("<NULL>")
        if vals.nunique() > 1:
            out.append(c)
    return out


def profile_potential_duplicates(raw: pd.DataFrame, top_n: int = 25,
                                 keys: pd.DataFrame | None = None) -> dict[str, Any]:
    """`raw`: full rows (all, or only awards with multi-record groups).
    `keys`: optional key columns of ALL staged rows, used for totals."""
    missing = [c for c in (ID, AWARD, MOD, DATE, AMOUNT) if c not in raw.columns]
    if missing:
        raise ValueError(f"download file lacks required columns {missing}; "
                         f"header has {list(raw.columns)[:40]}…")

    # 1. Exact-ID merge of the NAICS and PSC extracts (approved de-dup key)
    tot = keys if keys is not None else raw
    in_both = tot.groupby(ID)["_basis"].nunique() if "_basis" in tot else pd.Series(dtype=int)
    tot_u = tot.drop_duplicates(subset=[ID])
    segment_total = float(pd.to_numeric(tot_u[AMOUNT], errors="coerce").sum())
    df = raw.drop_duplicates(subset=[ID]).copy()
    df["_amount_norm"] = df[AMOUNT].map(_norm_amount)
    df["_amount"] = pd.to_numeric(df[AMOUNT], errors="coerce")
    compare_cols = [c for c in df.columns if c not in IGNORE and c != "_market"]

    # 2. Same award / mod / date / amount, different transaction IDs
    keyed = df.dropna(subset=["_amount_norm"])
    sizes = keyed.groupby(KEY, dropna=False)[ID].transform("nunique")
    cand = keyed[sizes > 1]
    groups = []
    for key, members in cand.groupby(KEY, dropna=False):
        amount = float(Decimal(key[3]))
        diff = _differing_columns(members, compare_cols)
        tn = sorted(members["transaction_number"].astype(str).tolist()) \
            if "transaction_number" in members else []
        groups.append({
            "award_id": key[0], "modification_number": key[1], "action_date": key[2],
            "amount": amount, "records": len(members),
            "transaction_ids": members[ID].tolist(),
            "transaction_numbers": tn,
            "differing_fields": diff,
            "differing_priority_fields": [f for f in PRIORITY_FIELDS if f in diff],
            "potential_duplicate_amount": amount * (len(members) - 1),
            "awarding_sub_agency": members.get("awarding_sub_agency_name",
                                               pd.Series([None])).iloc[0],
            "recipient": members.get("recipient_name", pd.Series([None])).iloc[0],
            "recipient_parent": members.get("recipient_parent_name", pd.Series([None])).iloc[0],
        })
    nonzero = [g for g in groups if g["amount"] != 0]
    zero = [g for g in groups if g["amount"] == 0]

    # 3. Comparison baseline: same award / mod / date with DIFFERENT amounts
    #    (shows whether multi-record modifications are normal for this data)
    dkey = [AWARD, MOD, DATE]
    multi = keyed.groupby(dkey, dropna=False).agg(ids=(ID, "nunique"),
                                                  amounts=("_amount_norm", "nunique"))
    multi_diff_amount = multi[(multi.ids > 1) & (multi.amounts > 1)]
    baseline_tn = Counter()
    if "transaction_number" in keyed and len(multi_diff_amount):
        sub = keyed.set_index(dkey).loc[multi_diff_amount.index]
        for _, m in sub.groupby(level=[0, 1, 2]):
            baseline_tn[" | ".join(sorted(m["transaction_number"].astype(str)))] += 1

    signature = Counter(" + ".join(g["differing_fields"]) or "(no field differs)" for g in nonzero)
    per_field = Counter(f for g in nonzero for f in g["differing_fields"])
    tn_pairs = Counter(" | ".join(g["transaction_numbers"]) for g in nonzero)
    only_tn = [g for g in nonzero
               if set(g["differing_fields"]) <= {"transaction_number"}]
    only_tn_or_dates = [g for g in nonzero if set(g["differing_fields"]) <=
                        {"transaction_number", "last_modified_date", "initial_report_date"}]
    snapshot_only = [g for g in nonzero if set(g["differing_fields"]) <= SNAPSHOT_FIELDS]
    by_agency = Counter()
    for g in nonzero:
        by_agency[g["awarding_sub_agency"] or "(blank)"] += g["potential_duplicate_amount"]

    dup_amount = sum(g["potential_duplicate_amount"] for g in nonzero)
    summary = {
        "records_in_download_union": int(len(tot)),
        "unique_transaction_ids": int(len(tot_u)),
        "ids_in_both_naics_and_psc_extracts": int((in_both > 1).sum()) if len(in_both) else 0,
        "segment_obligations_union": round(segment_total, 2),
        "suspicious_groups_nonzero_amount": len(nonzero),
        "affected_records": int(sum(g["records"] for g in nonzero)),
        "potential_duplicate_amount_net": round(dup_amount, 2),
        "potential_duplicate_amount_gross": round(sum(abs(g["potential_duplicate_amount"])
                                                      for g in nonzero), 2),
        "potential_duplicate_share_of_segment_pct":
            round(dup_amount / segment_total * 100, 3) if segment_total else None,
        "zero_amount_groups": len(zero),
        "groups_where_only_transaction_number_differs": len(only_tn),
        "amount_where_only_transaction_number_differs":
            round(sum(g["potential_duplicate_amount"] for g in only_tn), 2),
        "groups_where_only_transaction_number_or_report_dates_differ": len(only_tn_or_dates),
        "baseline_same_award_mod_date_different_amounts": int(len(multi_diff_amount)),
        "groups_differing_only_in_snapshot_fields": len(snapshot_only),
        "amount_differing_only_in_snapshot_fields":
            round(sum(g["potential_duplicate_amount"] for g in snapshot_only), 2),
        "groups_differing_in_transaction_level_fields": len(nonzero) - len(snapshot_only),
    }
    consistency = _award_total_consistency(df, nonzero)
    return {
        "definition": "Suspicious group = >=2 records with different transaction unique IDs "
                      "and identical award ID, modification number, action date and "
                      "non-zero amount. No records are excluded by this profile.",
        "summary": summary,
        "differing_field_frequency": per_field.most_common(),
        "differing_field_signatures": signature.most_common(20),
        "transaction_number_patterns": tn_pairs.most_common(20),
        "baseline_transaction_number_patterns_different_amounts": baseline_tn.most_common(20),
        "potential_duplicate_amount_by_awarding_sub_agency":
            [(k, round(v, 2)) for k, v in by_agency.most_common(15)],
        "largest_groups": sorted(nonzero, key=lambda g: -abs(g["potential_duplicate_amount"]))[:top_n],
        "award_total_consistency": consistency,
        "snapshot_fields": sorted(SNAPSHOT_FIELDS),
        "columns_compared": len(compare_cols),
        "interpretation_hints": _hints(summary, per_field),
    }


def _award_total_consistency(df: pd.DataFrame, nonzero: list[dict]) -> dict[str, Any]:
    """For awards whose whole history lies in FY2026 (base action, mod 0, in the
    window), compare the award's reported cumulative obligation on its most
    recently modified record with (a) the sum of ALL its records and (b) the sum
    with one record per suspicious group. Uses only the downloaded data."""
    if "total_dollars_obligated" not in df.columns or not nonzero:
        return {"tested_awards": 0, "note": "not testable"}
    extra: dict[str, float] = {}
    for g in nonzero:
        extra[g["award_id"]] = extra.get(g["award_id"], 0.0) + g["potential_duplicate_amount"]
    rows = df[df[AWARD].isin(extra)].copy()
    rows["_reported"] = pd.to_numeric(rows["total_dollars_obligated"], errors="coerce")
    rows["_lm"] = pd.to_datetime(rows.get("last_modified_date"), errors="coerce")
    out = Counter()
    amounts = Counter()
    examples = []
    for award, r in rows.groupby(AWARD):
        if not r[MOD].astype(str).str.fullmatch(r"0+").any():
            continue                                   # history starts before FY2026
        latest = r.sort_values(["_lm", "_reported"]).iloc[-1]["_reported"]
        if pd.isna(latest):
            continue
        s_all = round(float(r["_amount"].sum()), 2)
        s_one = round(s_all - extra[award], 2)
        if abs(latest - s_one) < 0.01 and abs(latest - s_all) >= 0.01:
            k = "reported_total_matches_one_copy"
        elif abs(latest - s_all) < 0.01 and abs(latest - s_one) >= 0.01:
            k = "reported_total_matches_both_copies"
        else:
            k = "neither_or_ambiguous"
        out[k] += 1
        amounts[k] += extra[award]
        if len(examples) < 10:
            examples.append({"award_id": award, "reported_cumulative": float(latest),
                             "sum_all_records": s_all, "sum_one_per_group": s_one, "result": k})
    return {"tested_awards": int(sum(out.values())), "results": dict(out),
            "duplicate_amount_by_result": {k: round(v, 2) for k, v in amounts.items()},
            "examples": examples,
            "note": "Only awards with a base action (mod 0) in FY2026 are testable; for older "
                    "awards the pre-FY2026 history is not in this download."}


def _hints(s: dict, per_field: Counter) -> list[str]:
    n = s["suspicious_groups_nonzero_amount"]
    if not n:
        return ["No suspicious groups: no duplicate treatment needed."]
    hints = []
    share_tn = s["groups_where_only_transaction_number_differs"] / n
    share_tn_dates = s["groups_where_only_transaction_number_or_report_dates_differ"] / n
    hints.append(f"{share_tn:.0%} of groups differ ONLY in transaction_number; "
                 f"{share_tn_dates:.0%} differ only in transaction_number and/or report dates.")
    substantive = [f for f in ("funding_office_code", "funding_sub_agency_code",
                               "product_or_service_code", "naics_code", "awarding_office_code",
                               "transaction_description", "foreign_funding", "action_type_code")
                   if per_field.get(f)]
    if substantive:
        hints.append("Some groups differ in substantive fields (" + ", ".join(
            f"{f}: {per_field[f]}" for f in substantive) +
            ") – those records look like separate reporting lines, not copies.")
    if s["baseline_same_award_mod_date_different_amounts"]:
        hints.append(f"{s['baseline_same_award_mod_date_different_amounts']} other award/mod/date "
                     f"groups have several records with DIFFERENT amounts – multi-record "
                     f"modifications do occur in this data; compare their transaction-number "
                     f"patterns with the suspicious groups.")
    return hints


def render_markdown(p: dict[str, Any], run_id: str) -> str:
    s = p["summary"]
    L = [f"# Phase 2a — Potential Duplicate Profile (FY2026, run {run_id})", "",
         p["definition"], "",
         "## Summary", "",
         "| Metric | Value |", "|---|---|"]
    fmt_money = {"segment_obligations_union", "potential_duplicate_amount_net",
                 "potential_duplicate_amount_gross",
                 "amount_where_only_transaction_number_differs"}
    for k, v in s.items():
        L.append(f"| {k.replace('_', ' ')} | {f'${v:,.2f}' if k in fmt_money and v is not None else v} |")
    c = p.get("award_total_consistency") or {}
    if c.get("tested_awards"):
        L += ["", "## Award-total consistency test", "",
              "Awards fully inside FY2026 (base action in the window): the cumulative obligation "
              "reported on the award's latest record vs. the sum of its records.", "",
              "| Result | Awards | Potential duplicate amount |", "|---|---|---|"]
        for k, n in c["results"].items():
            L.append(f"| {k} | {n} | ${c['duplicate_amount_by_result'].get(k, 0):,.2f} |")
        L += ["", "| Award | Reported cumulative | Σ all records | Σ one per group | Result |",
              "|---|---|---|---|---|"]
        for e in c["examples"]:
            L.append(f"| {e['award_id']} | ${e['reported_cumulative']:,.2f} | "
                     f"${e['sum_all_records']:,.2f} | ${e['sum_one_per_group']:,.2f} | {e['result']} |")
        L.append(f"\n{c['note']}")
    L += ["", "## Interpretation hints (not a decision)", ""]
    L += [f"- {h}" for h in p["interpretation_hints"]]
    L += ["", "## Fields that differ inside suspicious groups", "",
          "| Field | Groups |", "|---|---|"]
    L += [f"| {f} | {n} |" for f, n in p["differing_field_frequency"][:30]]
    L += ["", "## Differing-field signatures", "", "| Fields that differ | Groups |", "|---|---|"]
    L += [f"| {sig} | {n} |" for sig, n in p["differing_field_signatures"]]
    L += ["", "## Transaction-number patterns", "",
          "| Suspicious groups (same amount) | Groups |", "|---|---|"]
    L += [f"| {pat} | {n} |" for pat, n in p["transaction_number_patterns"]]
    L += ["", "| Baseline groups (same award/mod/date, different amounts) | Groups |", "|---|---|"]
    L += [f"| {pat} | {n} |" for pat, n in p["baseline_transaction_number_patterns_different_amounts"]]
    L += ["", "## Potential duplicate amount by awarding sub-agency", "",
          "| Sub-agency | Amount |", "|---|---|"]
    L += [f"| {a} | ${v:,.2f} |" for a, v in p["potential_duplicate_amount_by_awarding_sub_agency"]]
    L += ["", "## Largest groups", "",
          "| Award | Mod | Date | Amount | Records | Txn numbers | Differing fields | Recipient |",
          "|---|---|---|---|---|---|---|---|"]
    for g in p["largest_groups"]:
        L.append(f"| {g['award_id']} | {g['modification_number']} | {g['action_date']} | "
                 f"${g['amount']:,.2f} | {g['records']} | {', '.join(g['transaction_numbers'])} | "
                 f"{', '.join(g['differing_fields'][:6]) or '(none)'} | {g['recipient']} |")
    L += ["", "No records were excluded. The counting rule is chosen after review."]
    return "\n".join(L) + "\n"
