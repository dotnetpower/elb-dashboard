"""Per-cluster budget threshold preference (single Azure Table row per cluster).

Responsibility: Persist and read a per-cluster monthly USD budget threshold used
by the cost card to render an over-budget warning. One row per
(subscription, resource_group, cluster) under a deterministic partition key.
Edit boundaries: Azure-Tables access for the ``budgetpref`` table lives here. No
HTTP shaping, no cost math (that is ``cost/estimate.py``).
Key entry points: ``get_budget``, ``save_budget``, ``BudgetPreference``.
Risky contracts: Best-effort read (a storage fault degrades to "no budget set",
never raises); the route layer passes the authenticated identity for the audit
fields but the key is cluster-scoped (deployment-wide), not per-user. A budget of
0 / unset means "no threshold" (no warning).
Validation: ``uv run pytest -q api/tests/test_cost_budget_pref.py``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient, UpdateMode

from api.services import get_credential
from api.services.state.table_pool import (
    _ENSURED_TABLES,
    _ENSURED_TABLES_LOCK,
    _TABLE_ENDPOINT_ENV,
    _PooledTableClient,
)

LOGGER = logging.getLogger(__name__)

_TABLE_NAME = "budgetpref"
_ROW_KEY = "current"
_MAX_BUDGET_USD = 10_000_000.0

_TABLE_POOL: _PooledTableClient | None = None
_TABLE_POOL_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def preference_key(subscription_id: str, resource_group: str, cluster_name: str) -> str:
    raw = f"{subscription_id}:{resource_group}:{cluster_name}"
    return "budget:" + re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)


def _table_client() -> TableClient:
    global _TABLE_POOL
    pool = _TABLE_POOL
    if pool is not None:
        return pool  # type: ignore[return-value]
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    with _TABLE_POOL_LOCK:
        if _TABLE_POOL is None:
            _TABLE_POOL = _PooledTableClient(
                TableClient(
                    endpoint=endpoint,
                    table_name=_TABLE_NAME,
                    credential=get_credential(),
                )
            )
        return _TABLE_POOL  # type: ignore[return-value]


def _reset_table_pool() -> None:
    """Test hook + credential-reset safety valve."""
    global _TABLE_POOL
    with _TABLE_POOL_LOCK:
        pool = _TABLE_POOL
        _TABLE_POOL = None
    if pool is not None:
        close = getattr(pool, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: S110 - close races are not fatal
                pass


def _ensure_table() -> None:
    endpoint = os.environ[_TABLE_ENDPOINT_ENV]
    cache_key = (endpoint, _TABLE_NAME)
    if cache_key in _ENSURED_TABLES:
        return
    with _ENSURED_TABLES_LOCK:
        if cache_key in _ENSURED_TABLES:
            return
        with TableServiceClient(endpoint=endpoint, credential=get_credential()) as service:
            try:
                service.create_table_if_not_exists(_TABLE_NAME)
            except AttributeError:
                try:
                    service.create_table(_TABLE_NAME)
                except ResourceExistsError:
                    pass
        _ENSURED_TABLES.add(cache_key)


@dataclass(frozen=True)
class BudgetPreference:
    subscription_id: str
    resource_group: str
    cluster_name: str
    monthly_budget_usd: float
    owner_oid: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "cluster_name": self.cluster_name,
            "monthly_budget_usd": self.monthly_budget_usd,
            "updated_at": self.updated_at,
        }


def normalise_budget(value: Any) -> float:
    """Clamp a budget to ``[0, _MAX_BUDGET_USD]``; 0 means "no threshold"."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if amount != amount or amount < 0:  # NaN or negative
        return 0.0
    return min(amount, _MAX_BUDGET_USD)


def get_budget(
    subscription_id: str, resource_group: str, cluster_name: str
) -> BudgetPreference | None:
    """Return the stored budget for a cluster, or ``None`` when unset / on fault."""
    try:
        _ensure_table()
        key = preference_key(subscription_id, resource_group, cluster_name)
        with _table_client() as table:
            try:
                entity = dict(table.get_entity(partition_key=key, row_key=_ROW_KEY))
            except ResourceNotFoundError:
                return None
        return BudgetPreference(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
            monthly_budget_usd=normalise_budget(entity.get("monthly_budget_usd")),
            owner_oid=str(entity.get("owner_oid") or ""),
            updated_at=str(entity.get("updated_at") or ""),
        )
    except Exception as exc:
        LOGGER.warning("budget read failed: %s", type(exc).__name__)
        return None


def save_budget(pref: BudgetPreference) -> BudgetPreference:
    """Upsert the cluster budget (last-writer-wins)."""
    _ensure_table()
    key = preference_key(pref.subscription_id, pref.resource_group, pref.cluster_name)
    now = _now_iso()
    amount = normalise_budget(pref.monthly_budget_usd)
    entity = {
        "PartitionKey": key,
        "RowKey": _ROW_KEY,
        "subscription_id": pref.subscription_id,
        "resource_group": pref.resource_group,
        "cluster_name": pref.cluster_name,
        "monthly_budget_usd": amount,
        "owner_oid": pref.owner_oid or "",
        "updated_at": now,
    }
    with _table_client() as table:
        table.upsert_entity(entity, mode=UpdateMode.REPLACE)
    return BudgetPreference(
        subscription_id=pref.subscription_id,
        resource_group=pref.resource_group,
        cluster_name=pref.cluster_name,
        monthly_budget_usd=amount,
        owner_oid=pref.owner_oid or "",
        updated_at=now,
    )
