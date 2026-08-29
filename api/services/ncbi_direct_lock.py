"""Deployment-wide ownership lock for NCBI Direct transfers.

Responsibility: Serialize large Direct transfers across databases with an
    owner-checked, expiring Redis key.
Edit boundaries: Redis lock operations only; dispatch and task state live in
    their owning modules.
Key entry points: `acquire_direct_lock`, `claim_or_refresh_direct_lock`,
    `refresh_direct_lock`, `release_direct_lock`.
Risky contracts: Redis failures fail closed at acquisition; refresh/release use
    Lua compare-and-act so an expired lock acquired by another task is never
    extended or deleted by the old owner.
Validation: `uv run pytest -q api/tests/test_ncbi_direct_lock.py`.
"""

from __future__ import annotations

import os
from typing import Any

_LOCK_KEY = "elb:ncbi-direct:transfer-lock:v1"
_DEFAULT_TTL_SECONDS = 10 * 60 * 60
_REFRESH_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_CLAIM_OR_REFRESH_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if not current then
    local claimed = redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX')
    if claimed then return 1 end
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def direct_lock_ttl_seconds() -> int:
    try:
        value = int(os.environ.get("PREPARE_DB_NCBI_DIRECT_LOCK_TTL_SECONDS", ""))
    except ValueError:
        value = _DEFAULT_TTL_SECONDS
    return max(3600, min(value or _DEFAULT_TTL_SECONDS, 24 * 60 * 60))


def _client() -> Any:
    from api.services.redis_clients import get_broker_redis_client

    return get_broker_redis_client(socket_connect_timeout=2.0, socket_timeout=2.0)


def acquire_direct_lock(owner: str) -> bool:
    """Claim the global transfer slot; Redis errors intentionally propagate."""
    if not owner:
        raise ValueError("Direct lock owner is required")
    return bool(_client().set(_LOCK_KEY, owner, nx=True, ex=direct_lock_ttl_seconds()))


def refresh_direct_lock(owner: str) -> bool:
    """Extend only the matching owner's live lock."""
    return bool(
        _client().eval(
            _REFRESH_SCRIPT,
            1,
            _LOCK_KEY,
            owner,
            direct_lock_ttl_seconds(),
        )
    )


def claim_or_refresh_direct_lock(owner: str) -> bool:
    """Refresh the same owner or reclaim an absent lock after Redis restart."""
    if not owner:
        raise ValueError("Direct lock owner is required")
    return bool(
        _client().eval(
            _CLAIM_OR_REFRESH_SCRIPT,
            1,
            _LOCK_KEY,
            owner,
            direct_lock_ttl_seconds(),
        )
    )


def release_direct_lock(owner: str) -> bool:
    """Delete only the matching owner's live lock."""
    return bool(_client().eval(_RELEASE_SCRIPT, 1, _LOCK_KEY, owner))


__all__ = [
    "acquire_direct_lock",
    "claim_or_refresh_direct_lock",
    "direct_lock_ttl_seconds",
    "refresh_direct_lock",
    "release_direct_lock",
]
