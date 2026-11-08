"""TEST FIXTURE ONLY – artificial staged rows shaped like the official download.

Names are prefixed TEST and UEIs are fake. Used to exercise transform, load,
analytics and the app in tests; never written to data/ and never shown as real.
"""
from __future__ import annotations

import pandas as pd

BASE = {
    "award_id_piid": None, "parent_award_id_piid": None, "modification_number": "0",
    "transaction_number": "0", "action_date": "2026-03-01", "federal_action_obligation": "100.00",
    "total_dollars_obligated": "999999.00", "current_total_value_of_award": "1500000.00",
    "potential_total_value_of_award": "2000000.00",
    "period_of_performance_start_date": "2024-01-01",
    "period_of_performance_current_end_date": "2027-01-01",
    "awarding_agency_code": "097", "awarding_agency_name": "Department of Defense",
    "awarding_sub_agency_code": "2100", "awarding_sub_agency_name": "Department of the Army",
    "awarding_office_code": "W1", "awarding_office_name": "TEST OFFICE",
    "funding_agency_code": "097", "funding_agency_name": "Department of Defense",
    "funding_sub_agency_code": "2100", "funding_sub_agency_name": "Department of the Army",
    "funding_office_code": "F1", "recipient_uei": "TESTUEIA0001", "recipient_duns": None,
    "recipient_name": "TEST ALPHA SYSTEMS", "recipient_name_raw": "TEST ALPHA SYSTEMS",
    "recipient_parent_uei": "TESTPARENT01", "recipient_parent_name": "TEST PARENT HOLDINGS",
    "recipient_country_code": "USA", "recipient_state_code": "VA", "recipient_city_name": "TESTVILLE",
    "award_type_code": "D", "award_type": "DEFINITIVE CONTRACT", "action_type_code": "A",
    "action_type": "NEW", "transaction_description": "TEST RADAR SUSTAINMENT",
    "prime_award_base_transaction_description": "TEST RADAR PROGRAM",
    "naics_code": "334511", "naics_description": "TEST NAICS 334511",
    "product_or_service_code": "5840", "product_or_service_code_description": "TEST PSC 5840",
    "primary_place_of_performance_state_code": "VA",
    "primary_place_of_performance_country_code": "USA",
    "last_modified_date": "2026-03-02 10:00:00", "usaspending_permalink": "https://example.invalid",
}


def row(tid: str, award: str, **kw) -> dict:
    r = dict(BASE, contract_transaction_unique_key=tid, contract_award_unique_key=award)
    r["award_id_piid"] = award.replace("CONT_AWD_", "")
    r.update(kw)
    return r


def staged_rows() -> pd.DataFrame:
    rows = [
        # Company PARENT01: recipients A and B
        row("T1", "CONT_AWD_A1", federal_action_obligation="100.00"),
        row("T2", "CONT_AWD_A1", modification_number="P00001", federal_action_obligation="50.00",
            action_date="2026-04-01"),
        row("T3", "CONT_AWD_A2", federal_action_obligation="10.00", naics_code="541330",
            product_or_service_code="5840"),                     # PSC-only match
        row("T4", "CONT_AWD_A2", modification_number="P00001", federal_action_obligation="-10.00"),
        row("T5", "CONT_AWD_A3", federal_action_obligation="-5.00",
            awarding_sub_agency_code="1700", awarding_sub_agency_name="Department of the Navy"),
        row("T6", "CONT_AWD_B1", recipient_uei="TESTUEIB0001", recipient_name="TEST BRAVO",
            federal_action_obligation="30.00", product_or_service_code="J059",
            naics_code="811210", recipient_state_code="CA"),       # extended tier
        # Company without parent (own UEI)
        row("T7", "CONT_AWD_C1", recipient_uei="TESTUEIC0001", recipient_name="TEST CHARLIE",
            recipient_parent_uei=None, recipient_parent_name=None, federal_action_obligation="200.00"),
        # No UEI at all
        row("T8", "CONT_AWD_D1", recipient_uei=None, recipient_name="TEST DELTA LLC",
            recipient_parent_uei=None, recipient_parent_name=None, federal_action_obligation="40.00"),
        # Parent conflict: recipient E reports two different parents
        row("T9", "CONT_AWD_E1", recipient_uei="TESTUEIE0001", recipient_name="TEST ECHO",
            recipient_parent_uei="TESTPARENT01", federal_action_obligation="5.00",
            action_date="2026-01-01"),
        row("T10", "CONT_AWD_E1", recipient_uei="TESTUEIE0001", recipient_name="TEST ECHO",
            recipient_parent_uei="TESTPARENT02", recipient_parent_name="TEST OTHER PARENT",
            modification_number="P00001", federal_action_obligation="6.00", action_date="2026-05-01"),
        # Potential duplicates: identical except transaction number
        row("T11", "CONT_AWD_F1", federal_action_obligation="315.00", transaction_number="0"),
        row("T12", "CONT_AWD_F1", federal_action_obligation="315.00", transaction_number="1"),
        # Same key but different funding office (not field-identical)
        row("T13", "CONT_AWD_G1", federal_action_obligation="70.00", transaction_number="0"),
        row("T14", "CONT_AWD_G1", federal_action_obligation="70.00", transaction_number="16",
            funding_office_code="F2"),
        # Exclusions
        row("X1", "CONT_AWD_X1", action_date="2025-09-30"),                  # FY2025
        row("X2", "CONT_AWD_X2", award_type_code="IDV_B"),                    # IDV
        row("X3", "CONT_AWD_X3", awarding_agency_name="Department of Energy"),  # scope
        row("X4", "CONT_AWD_X4", naics_code="541330", product_or_service_code="R425"),  # codes
        row("X5", "CONT_AWD_X5", federal_action_obligation="abc"),            # amount
    ]
    df = pd.DataFrame(rows)
    df["_basis"] = "naics"
    dup = df[df["contract_transaction_unique_key"] == "T3"].assign(_basis="psc")
    df = pd.concat([df, dup], ignore_index=True)                    # same ID in both extracts
    df["_source_file"] = "TEST.csv"
    df["_run_id"] = "stage_test"
    return df
