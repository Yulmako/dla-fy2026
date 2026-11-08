"""Vendor lookup on the TEST FIXTURE."""
from src.analytics.vendor_lookup import prepare, search, top_companies
from tests.fixtures import staged_rows


def test_top_and_search():
    df = prepare(staged_rows())
    top = top_companies(df).set_index("company_key")
    p = top.loc["TESTPARENT01"]
    # F1 (315 x2) and G1 (70 x2) are probable-duplicate groups -> one copy each removed
    assert p["probable_duplicate_amount"] == 385
    assert p["fy2026_all_records"] - p["fy2026_one_per_group"] == 385
    hits = search(df, "bravo")
    assert list(hits["recipient_uei"]) == ["TESTUEIB0001"]
    assert search(df, "no such vendor").empty
