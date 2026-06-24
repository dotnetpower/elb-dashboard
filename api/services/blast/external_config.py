"""Config-snapshot + region + sibling-stat enrichment for external/queue BLAST jobs.

Module summary: a Service Bus (queue) or external-API BLAST job is submitted to
the sibling ``/v1/jobs``, which does NOT echo the BLAST options (outfmt, evalue,
word_size, …) or the AKS region back on its job record. The dashboard therefore
rendered an empty ``config_snapshot`` and "Region: —" on the job detail. This
helper rebuilds a flat ``config_snapshot`` from the options the dashboard itself
received at submit time, resolves the cluster region (short-TTL cached), and
keeps an ephemeral remember-store so a direct API submit (whose durable row is
created later by the discovery poll) can still carry its options through.

Responsibility: build the flat config_snapshot, resolve cluster→region (cached),
  and remember/recall a submit's options keyed by openapi job id.
Edit boundaries: pure snapshot construction + one cached Azure region lookup +
  best-effort OPS-Redis remember; no FastAPI, no Table writes.
Key entry points: ``build_external_config_snapshot``, ``resolve_cluster_region``,
  ``remember_config_snapshot``, ``recall_config_snapshot``.
Risky contracts: the snapshot keys must match what the frontend reads
  (``config_snapshot.{outfmt,evalue,word_size,dust,max_target_seqs,...}``); a
  region/remember failure must degrade to "" / no-op, never raise into a submit
  or the jobs-list projection.
Validation: ``uv run pytest -q api/tests/test_external_config.py``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

LOGGER = logging.getLogger(__name__)

# BLAST option keys surfaced on the job detail. Flat layout — matches the
# frontend's ``config_snapshot.<key>`` reads and the dashboard-submitted job's
# own ``config_snapshot`` shape, so a queue/API job renders the same metadata.
_SNAPSHOT_OPTION_KEYS: tuple[str, ...] = (
    "outfmt",
    "evalue",
    "word_size",
    "dust",
    "max_target_seqs",
    "additional_options",
    "extra",
    "taxid",
    "taxids",
    "negative_taxids",
    "is_inclusive",
    "db_effective_search_space",
    "sharding_mode",
    "machine_type",
    "num_nodes",
    "perc_identity",
    "qcov_hsp_perc",
    "gap_open",
    "gap_extend",
    "matrix",
    "soft_masking",
    "lcase_masking",
    "culling_limit",
    "window_size",
    "best_hit_overhang",
    "best_hit_score_edge",
    "num_alignments",
    "num_descriptions",
)

# Hardening round 4: cap free-form option strings (additional_options / extra)
# so a hostile/huge producer value cannot bloat the durable row or the topic
# message. BLAST option strings are short; 1 KiB is generous.
_FREE_FORM_OPTION_KEYS = frozenset({"additional_options", "extra"})
_FREE_FORM_MAX_LEN = 1024


def build_external_config_snapshot(options: dict[str, Any] | None) -> dict[str, Any]:
    """Build a flat ``config_snapshot`` from a submit options dict.

    Copies only the recognised BLAST option keys (so an unexpected/oversized
    producer field cannot bloat the row) and drops ``None`` / empty values so
    the detail shows "—" for a genuinely-absent option rather than a misleading
    blank. Free-form option strings are length-capped. Returns ``{}`` for a
    non-dict / empty input.
    """
    if not isinstance(options, dict):
        return {}
    snapshot: dict[str, Any] = {}
    for key in _SNAPSHOT_OPTION_KEYS:
        value = options.get(key)
        if value in (None, ""):
            continue
        if key in _FREE_FORM_OPTION_KEYS:
            value = str(value)[:_FREE_FORM_MAX_LEN]
        snapshot[key] = value
    return snapshot


# --- cluster -> region (short-TTL cached; region is constant per cluster) ---

_REGION_CACHE: dict[str, tuple[float, str]] = {}
_REGION_CACHE_LOCK = threading.Lock()
_REGION_TTL_SECONDS = 3600.0
# Hardening round 2: cache a FAILED resolve only briefly so a transiently-down
# AKS / RBAC blip recovers within a minute instead of showing "—" for an hour.
_REGION_NEGATIVE_TTL_SECONDS = 60.0
# Hardening round 1: bound the cache so a pathological number of distinct
# cluster keys cannot grow it without limit (clusters are few in practice).
_REGION_CACHE_MAX = 256


def resolve_cluster_region(
    subscription_id: str, resource_group: str, cluster_name: str
) -> str:
    """Resolve an AKS cluster's Azure region (cached, best-effort ``""``).

    Region never changes for a cluster, so a resolved value is cached for 1 hour
    and a failure for 60 s (so a transient AKS/RBAC blip recovers quickly). Any
    failure returns ``""`` so the detail shows "—" instead of erroring. Never
    raises into the caller. The cache is size-bounded.
    """
    sub = str(subscription_id or "").strip()
    rg = str(resource_group or "").strip()
    name = str(cluster_name or "").strip()
    if not (sub and rg and name):
        return ""
    key = f"{sub}/{rg}/{name}"
    now = time.monotonic()
    with _REGION_CACHE_LOCK:
        cached = _REGION_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
    region = ""
    try:
        from api.services import get_credential, monitoring

        snapshot = monitoring.get_aks_cluster_snapshot(get_credential(), sub, rg, name)
        region = str((snapshot or {}).get("region") or "").strip()
    except Exception:  # best-effort — a region we cannot resolve is just "—"
        LOGGER.debug("cluster region resolve failed cluster=%s", name, exc_info=True)
        region = ""
    ttl = _REGION_TTL_SECONDS if region else _REGION_NEGATIVE_TTL_SECONDS
    with _REGION_CACHE_LOCK:
        if len(_REGION_CACHE) >= _REGION_CACHE_MAX and key not in _REGION_CACHE:
            # Simple bound: drop the oldest-expiring entry to make room.
            oldest = min(_REGION_CACHE, key=lambda k: _REGION_CACHE[k][0])
            _REGION_CACHE.pop(oldest, None)
        _REGION_CACHE[key] = (now + ttl, region)
    return region


def reset_region_cache_for_test() -> None:
    """Clear the region cache (pytest hook)."""
    with _REGION_CACHE_LOCK:
        _REGION_CACHE.clear()


# --- remember a submit's config_snapshot (API path; ephemeral OPS Redis) ---

_REMEMBER_KEY_PREFIX = "elb:extcfg:"
_REMEMBER_TTL_SECONDS = 7 * 24 * 3600
_REMEMBER_MAX_BYTES = 4096


def remember_config_snapshot(job_id: str, snapshot: dict[str, Any] | None) -> None:
    """Best-effort: stash a job's ``config_snapshot`` in OPS Redis with a TTL.

    A direct ``POST /v1/elastic-blast/submit`` does not create the durable
    jobstate row (the discovery poll does, later), so the options it received are
    remembered here and re-attached by ``_sync_external_jobs_to_table`` when the
    row is first persisted. Never raises — this is a display-only side effect on
    an already-accepted submit. Oversized snapshots are dropped (the cap guards
    against a hostile/huge options blob).
    """
    if not job_id or not isinstance(snapshot, dict) or not snapshot:
        return
    try:
        blob = json.dumps(snapshot, default=str)
        if len(blob) > _REMEMBER_MAX_BYTES:
            return
        from api.services.redis_clients import get_ops_redis_client

        get_ops_redis_client().set(
            _REMEMBER_KEY_PREFIX + job_id, blob, ex=_REMEMBER_TTL_SECONDS
        )
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug(
            "remember_config_snapshot skipped job_id=%s: %s", job_id, type(exc).__name__
        )


def recall_config_snapshot(job_id: str) -> dict[str, Any]:
    """Best-effort: return the remembered ``config_snapshot`` (``{}`` if none)."""
    if not job_id:
        return {}
    try:
        from api.services.redis_clients import get_ops_redis_client

        value = get_ops_redis_client().get(_REMEMBER_KEY_PREFIX + job_id)
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug(
            "recall_config_snapshot skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        return {}
    if value is None:
        return {}
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- remember a completed external job's sibling stats (detail enrichment) ---

_STATS_KEY_PREFIX = "elb:extstats:"
_STATS_TTL_SECONDS = 7 * 24 * 3600


def remember_sibling_stats(job_id: str, stats: dict[str, Any] | None) -> None:
    """Best-effort: cache a completed external job's sibling stats in OPS Redis.

    The detail view fetches ``db_version`` / ``blast_version`` / ``run_seconds``
    from the live sibling for a completed external job that has none stored.
    Caching the result bounds that fetch to once per TTL (so a stopped-cluster
    job's detail does not re-pay a 10 s timeout on every open). Never raises.
    """
    if not job_id or not isinstance(stats, dict) or not stats:
        return
    try:
        blob = json.dumps(stats, default=str)
        if len(blob) > _REMEMBER_MAX_BYTES:
            return
        from api.services.redis_clients import get_ops_redis_client

        get_ops_redis_client().set(
            _STATS_KEY_PREFIX + job_id, blob, ex=_STATS_TTL_SECONDS
        )
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug(
            "remember_sibling_stats skipped job_id=%s: %s", job_id, type(exc).__name__
        )


def recall_sibling_stats(job_id: str) -> dict[str, Any]:
    """Best-effort: return the cached sibling stats for ``job_id`` (``{}`` if none)."""
    if not job_id:
        return {}
    try:
        from api.services.redis_clients import get_ops_redis_client

        value = get_ops_redis_client().get(_STATS_KEY_PREFIX + job_id)
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug(
            "recall_sibling_stats skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        return {}
    if value is None:
        return {}
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
