"""Shared NCBI E-utilities HTTP helpers (identity, byte cap, rate limit).

Responsibility: Centralise the EUTILS base URL, identity params, pooled
``httpx.Client`` slot, byte-capped JSON/bytes fetchers, and a single
shared token bucket so dashboard callers cannot exceed the NCBI 3 req/s
(no key) / 10 req/s (with key) policy as a group.
Edit boundaries: Only HTTP plumbing + rate limiting + identity. Record-type
parsing lives in `nuccore.py` / future siblings; taxonomy has its own copy.
Key entry points: `EUTILS_BASE_URL`, `NcbiServiceUnavailable`,
`request_json`, `request_bytes`, `ncbi_identity_params`.
Risky contracts: The token bucket prefers a Redis-backed bucket
(`OPS_REDIS_URL` / broker Redis) so api + worker sidecars share the
same budget over the egress SNAT IP. When Redis is unreachable each
sidecar falls back to its own in-process bucket — acceptable because
the dashboard's response caching dominates real NCBI traffic.
Validation: `uv run pytest -q api/tests/test_ncbi_nuccore.py`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_TIMEOUT_SECONDS = 8.0
# efetch (GenBank XML / FASTA) is generated server-side and, for large viral /
# organelle / draft-genome records, legitimately takes 10-20 s — well above the
# 8 s budget that is fine for the tiny esummary JSON header. With the short
# timeout a slow-but-healthy record turns into a retry storm that ALWAYS fails:
# every attempt times out at 8 s, the record is never faster, and the user
# waits ~26 s (8 + 0.5 + 8 + 1.5 + 8) only to get a 503. Give the
# byte-streaming path its own larger budget so these records load on the first
# attempt. Override with ``NCBI_EFETCH_HTTP_TIMEOUT`` (floored at the JSON
# timeout so it can never be made shorter than the esummary call).
_DEFAULT_EFETCH_TIMEOUT_SECONDS = 30.0


def _efetch_timeout_seconds() -> float:
    """Return the byte-streaming (efetch) request timeout in seconds.

    Read at call time (like ``_rate_capacity`` / ``ncbi_identity_params``) so
    tests and dev can override via env after import. Values below
    ``DEFAULT_TIMEOUT_SECONDS`` are ignored — the efetch path must never be
    faster-failing than the cheap esummary call.
    """
    raw = os.environ.get("NCBI_EFETCH_HTTP_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _DEFAULT_EFETCH_TIMEOUT_SECONDS
        if value >= DEFAULT_TIMEOUT_SECONDS:
            return value
    return _DEFAULT_EFETCH_TIMEOUT_SECONDS


class NcbiServiceUnavailable(RuntimeError):
    """Raised when an NCBI E-utilities call cannot complete cleanly."""


class NcbiResponseTooLarge(NcbiServiceUnavailable):
    """Raised when an NCBI response exceeds the configured byte cap.

    Distinct from generic ``NcbiServiceUnavailable`` so callers (notably the
    BLAST submit accession resolver) can map this to a 422 user-fixable error
    rather than a 503 retryable one — the same accession will always blow
    the cap, retrying does not help, but supplying a sub-range does.
    """


class NcbiRateLimited(NcbiServiceUnavailable):
    """Raised when the dashboard's own token bucket is exhausted.

    Distinct from generic ``NcbiServiceUnavailable`` so callers can map this
    to a 429 with a short ``Retry-After`` (NCBI itself is healthy; only our
    in-process rate limiter is saturated). 503 with a 30 s retry would
    mislead the user into thinking NCBI is down.
    """


# Transient HTTP error statuses worth a single retry. 5xx only — 4xx are
# client-side and the same request will keep failing.
_RETRY_STATUSES = frozenset({500, 502, 503, 504})
# Short, deterministic backoff: 0.5 s then 1.5 s. Total worst-case added
# latency 2 s on a single retry, comfortably under the 8 s request timeout
# so the FastAPI worker is not held longer than necessary.
_RETRY_DELAYS = (0.5, 1.5)


def _is_transient_http_error(exc: httpx.HTTPError) -> bool:
    """Return True when the error class is worth one short retry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_STATUSES
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
        ),
    )


# ---------------------------------------------------------------------------
# Rate limit — a simple token bucket scoped to the process. We deliberately
# under-allocate when no API key is configured (NCBI policy: 3 req/s).
# ---------------------------------------------------------------------------
_RATE_LOCK = threading.Lock()
_RATE_TOKENS: float = 0.0
_RATE_LAST: float = 0.0
_RATE_CAPACITY: float = 0.0
_RATE_RATE: float = 0.0


def _rate_capacity() -> tuple[float, float]:
    """Return (capacity, refill_per_sec) honouring NCBI policy.

    Without an API key: 3 req/s sustained, burst 3. With ``NCBI_API_KEY``: 10
    req/s sustained, burst 10. Both can be overridden via
    ``NCBI_EUTILS_RATE_PER_SEC`` for tests / dev.
    """
    if os.environ.get("NCBI_API_KEY", "").strip():
        default_rate = 10.0
    else:
        default_rate = 3.0
    override = os.environ.get("NCBI_EUTILS_RATE_PER_SEC", "").strip()
    if override:
        try:
            rate = float(override)
            if rate > 0:
                default_rate = rate
        except ValueError:
            pass
    return default_rate, default_rate


def _consume_token(timeout_seconds: float = 5.0) -> None:
    """Block until a token is available or raise NcbiServiceUnavailable.

    Tries the cross-sidecar Redis bucket first; on any Redis failure
    falls back to the in-process bucket so a Redis outage does not
    break NCBI fetches outright. The caller's ``timeout_seconds`` is
    the *total* budget for both paths combined — the fallback only
    gets whatever time the Redis attempt did not consume so we never
    silently double the caller's wait.
    """
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    try:
        if _consume_token_redis_until(deadline):
            return
        # Redis returned 'rate limited' — honour it cooperatively.
        raise NcbiRateLimited(
            "NCBI rate limit: too many concurrent requests"
        )
    except _RedisBucketUnavailable:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Redis ate the whole budget on its own — don't punish the
            # caller with a second full window.
            raise NcbiRateLimited(
                "NCBI rate limit: redis path exhausted timeout"
            ) from None
        _consume_token_in_process_until(deadline)


def _consume_token_in_process(timeout_seconds: float = 5.0) -> None:
    """Compatibility shim — absolute-deadline variant is preferred internally."""
    _consume_token_in_process_until(time.monotonic() + max(0.1, timeout_seconds))


def _consume_token_in_process_until(deadline: float) -> None:
    """In-process token bucket fallback, deadline-driven.

    `capacity` and `rate` are read once before the busy-wait loop so
    every iteration is a cheap arithmetic check, not a re-read of two
    environment variables.
    """
    global _RATE_TOKENS, _RATE_LAST, _RATE_CAPACITY, _RATE_RATE
    capacity, rate = _rate_capacity()
    while True:
        with _RATE_LOCK:
            now = time.monotonic()
            if capacity != _RATE_CAPACITY or rate != _RATE_RATE:
                _RATE_CAPACITY = capacity
                _RATE_RATE = rate
                _RATE_TOKENS = min(capacity, _RATE_TOKENS or capacity)
                _RATE_LAST = _RATE_LAST or now
            elapsed = max(0.0, now - (_RATE_LAST or now))
            _RATE_TOKENS = min(_RATE_CAPACITY, _RATE_TOKENS + elapsed * _RATE_RATE)
            _RATE_LAST = now
            if _RATE_TOKENS >= 1.0:
                _RATE_TOKENS -= 1.0
                return
            wait_for = (1.0 - _RATE_TOKENS) / max(0.001, _RATE_RATE)
        if time.monotonic() + wait_for > deadline:
            raise NcbiRateLimited(
                "NCBI rate limit: too many concurrent requests"
            )
        time.sleep(min(wait_for, 0.25))


# ---------------------------------------------------------------------------
# Redis token bucket — shared across api + worker sidecars
# ---------------------------------------------------------------------------
_REDIS_BUCKET_KEY = "elb:ncbi:eutils:bucket"
# TTL is sized to keep the bucket alive across short idle periods so a
# burst that follows N seconds of silence does not see a fresh
# `capacity`-token bucket (which would briefly violate the NCBI
# policy). 10 minutes covers normal idle windows; the bucket is still
# bounded by `capacity` so memory cost is constant.
_REDIS_BUCKET_TTL_SECONDS = 600
# Refill-elapsed safety cap. Clamps both clock-skew negatives and the
# "woke up after TTL expired" jumps so a single caller cannot suddenly
# bank more than `capacity` tokens worth of credit.
_REDIS_BUCKET_MAX_ELAPSED_SECONDS = 5.0
# Hard cap on the polling sleep so an aggressive `wait_ms` from the
# bucket cannot stall a worker for seconds at a time.
_REDIS_BUCKET_MAX_SLEEP_SECONDS = 0.25
# Short circuit breaker so we don't pay the import + try/except every call
# when Redis is down.
_REDIS_BUCKET_BREAKER_WINDOW = 1.0
_REDIS_BUCKET_LAST_FAILURE: float = 0.0
_REDIS_BUCKET_LOCK = threading.Lock()

# Lua: atomically refill + try-deduct.
#
# The script prefers `redis.call('TIME')` (server-side clock, identical
# across api + worker sidecars regardless of container ntp drift). The
# caller-supplied `now_ms` (ARGV[3]) is only used when `redis.call`
# would not yield a deterministic value — e.g. a future Redis cluster
# mode that disables `TIME` for replication. Elapsed is clamped to
# `[0, max_elapsed_s]` so neither a clock jump back nor a long idle
# period (TTL ran out, key recreated) can mint more than `capacity`
# tokens in a single call.
#
# The first call against a missing key seeds tokens at `capacity - 1`
# rather than `capacity` so a post-eviction first call still costs
# something — important when Redis is configured with `allkeys-lru`
# and the bucket can disappear between bursts without TTL expiry.
# This avoids a hot-cache eviction silently allowing a full burst.
#
# `redis.replicate_commands()` makes this script compatible with Redis
# 5/6 (script-replication default rejects writes after a non-deterministic
# `TIME` call). It is a no-op on Redis 7+ where effects-replication is
# the default.
#
# Returns:
#   {1, 0}     — acquired; no wait needed
#   {0, wait}  — not enough tokens; wait `wait` ms before retrying
_REDIS_BUCKET_LUA = """
if redis.replicate_commands then redis.replicate_commands() end
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local fallback_now_ms = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local max_elapsed_s = tonumber(ARGV[5])
local t = redis.call('TIME')
local now_ms
if t and t[1] then
    now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
else
    now_ms = fallback_now_ms
end
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last = tonumber(data[2])
if tokens == nil then tokens = math.max(0, capacity - 1) end
if last == nil then last = now_ms end
local elapsed_s = (now_ms - last) / 1000.0
if elapsed_s < 0 then elapsed_s = 0 end
if elapsed_s > max_elapsed_s then elapsed_s = max_elapsed_s end
tokens = math.min(capacity, tokens + elapsed_s * rate)
if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now_ms)
    redis.call('EXPIRE', key, ttl)
    return {1, 0}
else
    local need_s = (1 - tokens) / rate
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now_ms)
    redis.call('EXPIRE', key, ttl)
    return {0, math.ceil(need_s * 1000)}
end
"""
_REDIS_BUCKET_LUA_SHA1 = hashlib.sha1(  # noqa: S324 - SHA1 is the Redis EVALSHA contract
    _REDIS_BUCKET_LUA.encode("utf-8")
).hexdigest()


def _is_noscript_error(exc: BaseException) -> bool:
    """Return True when ``exc`` represents a Redis NOSCRIPT response.

    redis-py 5.x raises ``NoScriptError`` whose ``str()`` is just
    ``"No matching script. Please use [E]VAL."`` (the "NOSCRIPT" prefix
    is stripped during parsing), so substring checks on the message
    silently miss the case. Match by type first, then fall back to the
    legacy substring check for older clients / hand-rolled fakes that
    raise ``RuntimeError("NOSCRIPT ...")``.
    """
    try:
        from redis.exceptions import NoScriptError
    except ImportError:  # pragma: no cover - redis is a hard dep
        NoScriptError = ()  # type: ignore[assignment]
    if isinstance(exc, NoScriptError):
        return True
    return "NOSCRIPT" in str(exc).upper()


class _RedisBucketUnavailable(Exception):
    """Internal sentinel — Redis bucket cannot be consulted right now."""


def _redis_bucket_client() -> Any | None:
    """Return a Redis client or ``None`` when unreachable."""
    global _REDIS_BUCKET_LAST_FAILURE
    now = time.monotonic()
    with _REDIS_BUCKET_LOCK:
        if (
            _REDIS_BUCKET_LAST_FAILURE
            and now - _REDIS_BUCKET_LAST_FAILURE < _REDIS_BUCKET_BREAKER_WINDOW
        ):
            return None
    if os.environ.get("NCBI_REDIS_BUCKET_DISABLED", "").lower() == "true":
        # Test / dev knob — force the in-process fallback (matches the
        # behaviour callers got before this module learned the Redis
        # path).
        return None
    try:
        # Prefer ops Redis (the durable hot cache) since it survives
        # broker rebalancing; fall back to broker Redis on import error.
        from api.services.redis_clients import (
            get_broker_redis_client,
            get_ops_redis_client,
        )

        try:
            return get_ops_redis_client(socket_timeout=0.5)
        except Exception:
            return get_broker_redis_client(socket_timeout=0.5)
    except Exception as exc:
        LOGGER.info(
            "ncbi rate-limit: redis client unavailable (%s) — using in-process bucket",
            type(exc).__name__,
        )
        with _REDIS_BUCKET_LOCK:
            _REDIS_BUCKET_LAST_FAILURE = time.monotonic()
        return None


def _consume_token_redis_until(deadline: float) -> bool:
    """Try the shared Redis bucket. Returns True when a token was issued,
    False when the bucket signalled rate-limit before ``deadline``,
    raises ``_RedisBucketUnavailable`` when Redis cannot be consulted.
    """
    global _REDIS_BUCKET_LAST_FAILURE
    client = _redis_bucket_client()
    if client is None:
        raise _RedisBucketUnavailable
    capacity, rate = _rate_capacity()
    while True:
        now_ms = int(time.time() * 1000)
        try:
            try:
                result = client.evalsha(
                    _REDIS_BUCKET_LUA_SHA1,
                    1,
                    _REDIS_BUCKET_KEY,
                    capacity,
                    rate,
                    now_ms,
                    _REDIS_BUCKET_TTL_SECONDS,
                    _REDIS_BUCKET_MAX_ELAPSED_SECONDS,
                )
            except AttributeError:
                # Stripped fake / older client — fall through to EVAL.
                result = client.eval(
                    _REDIS_BUCKET_LUA,
                    1,
                    _REDIS_BUCKET_KEY,
                    capacity,
                    rate,
                    now_ms,
                    _REDIS_BUCKET_TTL_SECONDS,
                    _REDIS_BUCKET_MAX_ELAPSED_SECONDS,
                )
            except Exception as exc:
                if not _is_noscript_error(exc):
                    raise
                # Script was evicted (e.g. Redis restart / SCRIPT FLUSH).
                # redis-py strips the "NOSCRIPT" prefix when constructing
                # ``NoScriptError`` so a substring check on ``str(exc)``
                # silently misses; classify by type instead.
                result = client.eval(
                    _REDIS_BUCKET_LUA,
                    1,
                    _REDIS_BUCKET_KEY,
                    capacity,
                    rate,
                    now_ms,
                    _REDIS_BUCKET_TTL_SECONDS,
                    _REDIS_BUCKET_MAX_ELAPSED_SECONDS,
                )
        except Exception as exc:
            LOGGER.warning(
                "ncbi rate-limit: redis EVAL failed (%s) — falling back to in-process bucket",
                type(exc).__name__,
            )
            with _REDIS_BUCKET_LOCK:
                _REDIS_BUCKET_LAST_FAILURE = time.monotonic()
            raise _RedisBucketUnavailable from exc
        try:
            acquired = int(result[0])
            wait_ms = int(result[1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "ncbi rate-limit: redis EVAL returned unexpected shape %r",
                result,
            )
            raise _RedisBucketUnavailable from exc
        if acquired:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Cap the poll sleep so a multi-second `wait_ms` (e.g. when
        # capacity is exhausted by a sibling sidecar's burst) cannot
        # stall a worker for the full window.
        time.sleep(
            min(
                remaining,
                _REDIS_BUCKET_MAX_SLEEP_SECONDS,
                max(0.005, wait_ms / 1000.0),
            )
        )


def _consume_token_redis(timeout_seconds: float) -> bool:
    """Compatibility shim — delegates to the deadline-driven variant."""
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    return _consume_token_redis_until(deadline)


def reset_rate_limiter() -> None:
    """Reset internal bucket. Tests use this to avoid bleed-over."""
    global _RATE_TOKENS, _RATE_LAST, _RATE_CAPACITY, _RATE_RATE
    global _REDIS_BUCKET_LAST_FAILURE
    with _RATE_LOCK:
        _RATE_TOKENS = 0.0
        _RATE_LAST = 0.0
        _RATE_CAPACITY = 0.0
        _RATE_RATE = 0.0
    with _REDIS_BUCKET_LOCK:
        _REDIS_BUCKET_LAST_FAILURE = 0.0


def ncbi_identity_params() -> dict[str, str]:
    """Return the standard NCBI identity query params from env."""
    params: dict[str, str] = {}
    for env_name, param_name in (
        ("NCBI_TOOL", "tool"),
        ("NCBI_EMAIL", "email"),
        ("NCBI_API_KEY", "api_key"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            params[param_name] = value
    return params


def _pooled_client(slot: str) -> httpx.Client:
    """Return the slot's pooled client.

    ``Accept`` is intentionally NOT baked into the pool client because the
    same client can be re-used for JSON and bytes calls — callers pass
    ``headers={"Accept": ...}`` per request instead. Only the User-Agent
    (stable across calls) lives on the pool client.
    """
    from api.services.httpx_pool import get_pooled_client

    return get_pooled_client(
        slot,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        base_url=EUTILS_BASE_URL,
        headers={"User-Agent": "elb-dashboard/1.0"},
    )


def request_json(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    """GET an NCBI E-utilities endpoint that returns JSON.

    Retries once on transient 5xx / network errors (0.5 s then 1.5 s backoff).
    A short retry catches NCBI's frequent intermittent 502/503 blips without
    holding the FastAPI worker longer than the request timeout itself.
    """
    _consume_token()
    client = _pooled_client("ncbi-eutils-json")
    full_params = {**params, **ncbi_identity_params()}
    last_exc: httpx.HTTPError | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            response = client.get(
                endpoint,
                params=full_params,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS) and _is_transient_http_error(exc):
                LOGGER.warning(
                    "ncbi_eutils_retry endpoint=%s attempt=%d error=%s",
                    endpoint,
                    attempt + 1,
                    exc.__class__.__name__,
                )
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise NcbiServiceUnavailable(
                "NCBI E-utilities service is unavailable"
            ) from exc
        except ValueError as exc:
            raise NcbiServiceUnavailable(
                "NCBI E-utilities response was not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise NcbiServiceUnavailable(
                "NCBI E-utilities response was not an object"
            )
        return data
    # Unreachable — the loop either returns or raises — but keep mypy happy.
    raise NcbiServiceUnavailable("NCBI E-utilities service is unavailable") from last_exc


def request_bytes(
    endpoint: str,
    params: dict[str, str],
    *,
    max_bytes: int,
    accept: str = "application/xml",
) -> bytes:
    """GET an NCBI E-utilities endpoint that returns bytes (XML / text / FASTA).

    Streams the response and raises ``NcbiResponseTooLarge`` when the body
    exceeds ``max_bytes``. The cap protects against accidentally fetching a
    chromosome-sized record into the api sidecar.

    Overflow handling: when the size cap is hit we abort the stream early via
    ``response.close()`` so the pooled connection is not returned to the pool
    in a half-read state, then raise ``NcbiResponseTooLarge``. A
    ``Content-Length`` short-circuit makes the cheap path even cheaper.

    Retries once on transient 5xx / network errors (0.5 s then 1.5 s
    backoff). ``NcbiResponseTooLarge`` is **not** retryable — the same
    accession will always blow the cap; the caller must supply a sub-range.

    Uses the longer ``_efetch_timeout_seconds()`` budget (not the 8 s
    esummary timeout): large viral / genome records are generated slowly
    server-side (10-20 s observed) and would otherwise time out on every
    attempt and fail with a misleading "service unavailable".
    """
    _consume_token()
    client = _pooled_client("ncbi-eutils-bytes")
    full_params = {**params, **ncbi_identity_params()}
    last_exc: httpx.HTTPError | None = None
    timeout = _efetch_timeout_seconds()
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            with client.stream(
                "GET",
                endpoint,
                params=full_params,
                headers={"Accept": accept},
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    response.close()
                    raise NcbiResponseTooLarge(
                        f"NCBI response declared {declared} bytes (cap={max_bytes})"
                    )
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        response.close()
                        raise NcbiResponseTooLarge(
                            f"NCBI response exceeded {max_bytes} byte limit"
                        )
                return bytes(buffer)
        except NcbiResponseTooLarge:
            # Deterministic cap hit — bypass the retry path.
            raise
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS) and _is_transient_http_error(exc):
                LOGGER.warning(
                    "ncbi_eutils_retry endpoint=%s attempt=%d error=%s",
                    endpoint,
                    attempt + 1,
                    exc.__class__.__name__,
                )
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise NcbiServiceUnavailable(
                "NCBI E-utilities service is unavailable"
            ) from exc
    # Unreachable — the loop either returns or raises — but keep mypy happy.
    raise NcbiServiceUnavailable("NCBI E-utilities service is unavailable") from last_exc
