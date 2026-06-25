"""Tests for the per-cluster budget preference store.

Responsibility: Cover save/get round-trip + budget normalisation with an in-memory
fake table.
Edit boundaries: Test-only; monkeypatches the table client.
Key entry points: pytest test functions.
Risky contracts: negative / NaN budgets clamp to 0 (= no threshold); cap enforced.
Validation: ``uv run pytest -q api/tests/test_cost_budget_pref.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services.cost import budget_pref as bp
from azure.core.exceptions import ResourceNotFoundError


class FakeTable:
    def __init__(self, store: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.store = store

    def __enter__(self) -> FakeTable:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def get_entity(self, partition_key: str, row_key: str) -> dict[str, Any]:
        key = (partition_key, row_key)
        if key not in self.store:
            raise ResourceNotFoundError("missing")
        return dict(self.store[key])

    def upsert_entity(self, entity: dict[str, Any], mode: Any = None) -> None:
        del mode
        self.store[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], dict[str, Any]]:
    data: dict[tuple[str, str], dict[str, Any]] = {}
    monkeypatch.setattr(bp, "_ensure_table", lambda: None)
    monkeypatch.setattr(bp, "_table_client", lambda: FakeTable(data))
    return data


def test_save_and_get(store: dict) -> None:
    saved = bp.save_budget(
        bp.BudgetPreference("sub", "rg", "clu", monthly_budget_usd=1000.0, owner_oid="oid")
    )
    assert saved.monthly_budget_usd == 1000.0
    got = bp.get_budget("sub", "rg", "clu")
    assert got is not None
    assert got.monthly_budget_usd == 1000.0


def test_get_unset_returns_none(store: dict) -> None:
    assert bp.get_budget("sub", "rg", "missing") is None


def test_normalise_budget() -> None:
    assert bp.normalise_budget(-5) == 0.0
    assert bp.normalise_budget(float("nan")) == 0.0
    assert bp.normalise_budget("not a number") == 0.0
    assert bp.normalise_budget(123.45) == 123.45
    assert bp.normalise_budget(10**12) == bp._MAX_BUDGET_USD


def test_save_clamps_negative(store: dict) -> None:
    saved = bp.save_budget(bp.BudgetPreference("s", "r", "c", monthly_budget_usd=-99.0))
    assert saved.monthly_budget_usd == 0.0
