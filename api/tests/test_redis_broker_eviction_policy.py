"""Guard the in-revision Redis sidecar against a key-evicting policy.

Responsibility: Assert the Container App Redis sidecar runs with
`--maxmemory-policy noeviction` (never an `allkeys-*` / `volatile-*` policy).
Edit boundaries: Assertion-only; reads the bicep text, does not build/deploy.
Key entry points: `test_redis_sidecar_uses_noeviction_policy`,
`test_redis_sidecar_keeps_maxmemory_cap`.
Risky contracts: The single Redis instance is the Celery broker (db0) + result
backend (db1) + ops/durable cache (db2). An evicting policy drops broker queue
lists / unacked-task hashes / durable OpenAPI config under memory pressure, so
enqueued jobs silently vanish. The cap must stay so memory pressure fails writes
loudly instead of growing unbounded.
Validation: `uv run pytest -q api/tests/test_redis_broker_eviction_policy.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTROL_BICEP = _REPO_ROOT / "infra" / "modules" / "containerAppControl.bicep"

# Any of these silently drop keys under memory pressure — fatal for a broker.
_EVICTING_POLICIES = (
    "allkeys-lru",
    "allkeys-lfu",
    "allkeys-random",
    "volatile-lru",
    "volatile-lfu",
    "volatile-random",
    "volatile-ttl",
)


def _bicep_text() -> str:
    return _CONTROL_BICEP.read_text(encoding="utf-8")


def test_redis_sidecar_uses_noeviction_policy() -> None:
    """The broker Redis must never use a key-evicting maxmemory policy.

    With any `allkeys-*` / `volatile-*` policy Redis evicts the broker's queue
    lists and unacked-task hashes under memory pressure, so enqueued BLAST / ACR
    / AKS jobs silently disappear. Keep this `noeviction`.
    """
    text = _bicep_text()
    policies = re.findall(r"--maxmemory-policy['\"]?\s*[,:]?\s*['\"]([\w-]+)['\"]", text)
    assert policies, "containerAppControl.bicep no longer pins --maxmemory-policy"
    for policy in policies:
        assert policy not in _EVICTING_POLICIES, (
            f"Redis sidecar uses evicting maxmemory-policy {policy!r}; the single "
            "Redis instance is the Celery broker and an evicting policy drops "
            "queued tasks under memory pressure. Use 'noeviction'."
        )
        assert policy == "noeviction", (
            f"Unexpected Redis maxmemory-policy {policy!r}; only 'noeviction' is "
            "safe for the broker. Update this guard if the contract changes."
        )


def test_redis_sidecar_keeps_maxmemory_cap() -> None:
    """`noeviction` without a cap would let the broker grow until OOM-killed.

    The `--maxmemory` guardrail must stay so memory pressure fails writes loudly
    (and visibly via the sidecar metrics card) instead of growing unbounded.
    """
    text = _bicep_text()
    assert re.search(r"--maxmemory['\"]?\s*[,:]?\s*['\"]\d+\w*['\"]", text), (
        "containerAppControl.bicep dropped the Redis --maxmemory cap; keep it so "
        "noeviction fails writes loudly instead of OOM-killing the replica."
    )
