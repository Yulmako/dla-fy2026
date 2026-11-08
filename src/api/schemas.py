"""Pydantic models used to validate the shape of USAspending API responses.

They are deliberately permissive about extra keys (the API adds fields over
time) but strict about the keys the pipeline depends on.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class PageMetadata(_Loose):
    page: int | None = None
    hasNext: bool | None = None


class SearchPage(_Loose):
    """spending_by_award / spending_by_transaction response."""
    results: list[dict[str, Any]]
    page_metadata: PageMetadata | None = None


class CategoryRow(_Loose):
    amount: float | None = None
    name: str | None = None
    code: str | None = None


class CategoryPage(_Loose):
    results: list[CategoryRow]
    page_metadata: PageMetadata | None = None


class AwardTransactionRow(_Loose):
    action_date: str | None = None
    federal_action_obligation: float | None = None
    modification_number: str | None = None


class AwardTransactionsPage(_Loose):
    results: list[AwardTransactionRow]
    page_metadata: PageMetadata | None = None


def validate(model: type[BaseModel], data: Any) -> tuple[BaseModel | None, str | None]:
    """Return (parsed, None) or (None, error summary)."""
    try:
        return model.model_validate(data), None
    except ValidationError as exc:
        return None, "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
