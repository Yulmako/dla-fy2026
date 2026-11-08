"""Pipeline settings (config/pipeline.yaml)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from src.config.market import PROJECT_ROOT

DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "pipeline.yaml"


class ReportingLag(BaseModel):
    trailing_months_to_evaluate: int = Field(3, ge=1, le=6)
    low_ratio_threshold: float = Field(0.5, gt=0, lt=1)
    dod_reporting_lag_days: int = Field(90, ge=0)


class Refresh(BaseModel):
    scheduled_full_reload: date
    banner_text: str = "PROVISIONAL — DoD REPORTING LAG"


class ApiSettings(BaseModel):
    delay_seconds: float = 0.5
    timeout_seconds: float = 90
    max_retries: int = Field(4, ge=0, le=8)
    max_retries_on_500: int = Field(1, ge=0, le=3)
    max_calls: int = 400


class Reconciliation(BaseModel):
    tolerance_ratio: float = Field(0.005, gt=0, lt=0.1)


class TransactionCounting(BaseModel):
    potential_duplicate_treatment: Literal["undecided", "count_all_flag",
                                           "exclude_flagged_copies"] = "undecided"
    ignore_fields: list[str] = Field(default_factory=lambda: [
        "transaction_number", "contract_transaction_unique_key", "last_modified_date",
        "initial_report_date"])


class PipelineSettings(BaseModel):
    reporting_lag: ReportingLag = Field(default_factory=ReportingLag)
    refresh: Refresh
    api: ApiSettings = Field(default_factory=ApiSettings)
    reconciliation: Reconciliation = Field(default_factory=Reconciliation)
    transaction_counting: TransactionCounting = Field(default_factory=TransactionCounting)


def load_settings(path: Path | str = DEFAULT_SETTINGS_PATH) -> PipelineSettings:
    return PipelineSettings.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
