"""Cross-path BLAST submit coordination config, constants, and startup invariants.

Responsibility: Own the single source of truth for the ``k8s`` coordination
backend's tunables (run-concurrency ceiling, Lease TTL, wait deadlines, clock-skew
margin, the finalizer marker selector + grace window) and the startup invariant
assertion that keeps the Lease-release ordering a *checked* contract rather than a
coincidence. This module holds configuration + invariant logic only — no
Kubernetes API calls, no Celery, no Azure SDK.
Edit boundaries: Pure config resolution + validation. The Lease HTTP primitives
live in ``api.services.k8s.submit_lease``; the cluster job count lives in
``api.services.k8s.blast_status``; the admission decision that combines the two
gates lives in ``api.services.blast.k8s_gate``. Keep the env-key names and
defaults identical to the sibling ``elastic-blast-azure`` repo (charter §13).
Key entry points: ``coordination_backend``, ``max_run_concurrency``,
``submit_lease_ttl_seconds``, ``capacity_wait_max_seconds``,
``submit_slot_wait_max_seconds``, ``lease_clock_skew_seconds``,
``finalizer_grace_seconds``, ``FINALIZER_LABEL_SELECTOR``,
``SUBMIT_COORDINATION_NAMESPACE``,
``SUBMIT_EXEC_TIMEOUT_SECONDS``, ``assert_coordination_invariants``.
Risky contracts: ``BLAST_COORD_BACKEND=k8s`` *wins* over ``BLAST_GATE_ENABLED``
(§2a precedence). The ordering invariant
``submit_exec < soft < hard`` and ``submit_exec < lease_ttl`` is load-bearing: a
hard Celery SIGKILL skips the ``finally`` that releases the Lease, so the submit
subprocess MUST be guaranteed to fail gracefully first. Defaults are the
charter §12a Rule 4 default-OFF values — flipping the backend is a deliberate
rollout flag.
Validation: ``uv run pytest -q api/tests/test_blast_coordination.py``.
"""

from __future__ import annotations

import os

# The marker Gate B counts. ``elastic-blast submit`` deploys exactly one
# ``app=finalizer`` Job per submit, synchronously, in both the direct and the
# Azure ``cloud_job_submission`` paths, and it stays non-terminal for the whole
# search lifecycle. ``app=blast`` is NOT a safe marker (N-per-submit and, on
# Azure, created asynchronously after submit returns) — see the design note
# docs/research/blast-submit-coordination.md §5.1. Both repos MUST pin this
# exact selector and dedup by ``elb-job-id`` or they disagree on "3".
FINALIZER_LABEL_SELECTOR = "app=finalizer"
FINALIZER_JOB_ID_LABEL = "elb-job-id"

# The single namespace BOTH gates (Gate A Lease + Gate B count) AND both submit
# code paths (regular submit_task + split fan-out) coordinate in. It MUST be one
# value: if the regular path locked the ``default`` Lease while the split path
# locked a ``<other>`` Lease, the two mutexes would be split-brained and never
# exclude each other (design I1). ElasticBLAST deploys into ``default`` today;
# keep this the single source of truth rather than re-typing the literal at each
# call site (critique round-3 M-A).
SUBMIT_COORDINATION_NAMESPACE = "default"

# Companion markers that prove a finalizer's ``elb-job-id`` is doing live work.
# A lone finalizer with none of these and past the grace window is a phantom
# slot (a submit that failed after applying the finalizer) and is NOT counted.
FINALIZER_COMPANION_SELECTORS = ("app=submit", "app=blast")

# The submit subprocess's own hard cap, mirrored from
# ``api.tasks.blast.submit_runtime._stream_submit_command`` and
# ``api.tasks.blast.split_pipeline._dispatch_split_child_submits``
# (both pass ``timeout_seconds=600``). Kept here as the single value the
# startup invariant asserts against the Lease TTL and the Celery limits.
SUBMIT_EXEC_TIMEOUT_SECONDS = 600

_DEFAULT_MAX_RUN_CONCURRENCY = 3
_DEFAULT_LEASE_TTL_SECONDS = 900
_DEFAULT_CAPACITY_WAIT_MAX_SECONDS = 1800
_DEFAULT_SUBMIT_SLOT_WAIT_MAX_SECONDS = 1800
_DEFAULT_SPLIT_CHILD_GATE_WAIT_MAX_SECONDS = 300
_DEFAULT_SPLIT_PARENT_GATE_BUDGET_SECONDS = 1800
_DEFAULT_LEASE_CLOCK_SKEW_SECONDS = 30
_DEFAULT_FINALIZER_GRACE_SECONDS = 300


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 86_400) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _raw_int_env(name: str, default: int) -> int:
    """Parse an int env var the way ``api.celery_app`` does — no clamp.

    Used only by :func:`assert_coordination_invariants` so it checks the exact
    values the Celery worker was configured with. A malformed value falls back
    to ``default`` (celery_app would raise on a bad value at its own import, so
    this never masks a genuinely broken config the worker would accept).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def coordination_backend() -> str:
    """Resolve ``BLAST_COORD_BACKEND`` — ``"redis"`` (default) or ``"k8s"``.

    Any value other than ``k8s`` (case-insensitive) resolves to ``redis`` so a
    typo can never silently disable coordination — it falls back to today's
    behaviour. Per charter §12a Rule 4 the default is OFF (``redis``).
    """
    raw = os.environ.get("BLAST_COORD_BACKEND", "").strip().lower()
    return "k8s" if raw == "k8s" else "redis"


def is_k8s_backend() -> bool:
    return coordination_backend() == "k8s"


def max_run_concurrency() -> int:
    """Gate B ceiling — distinct active submits allowed cluster-wide (default 3)."""
    return _int_env("BLAST_MAX_RUN_CONCURRENCY", _DEFAULT_MAX_RUN_CONCURRENCY, minimum=1)


def submit_lease_ttl_seconds() -> int:
    """Gate A Lease validity (default 900, matches today's Redis lock TTL)."""
    return _int_env("BLAST_SUBMIT_LEASE_TTL_SECONDS", _DEFAULT_LEASE_TTL_SECONDS, minimum=1)


def capacity_wait_max_seconds() -> int:
    """Deadline bounding the ``waiting_for_capacity`` (Gate B) requeue loop."""
    return _int_env(
        "BLAST_CAPACITY_WAIT_MAX_SECONDS", _DEFAULT_CAPACITY_WAIT_MAX_SECONDS, minimum=1
    )


def submit_slot_wait_max_seconds() -> int:
    """Deadline bounding the ``waiting_for_submit_slot`` (Gate A) requeue loop.

    Gate A contention was historically unbounded (re-enqueue every 30s without
    consuming ``max_retries``). A stuck/crashed Lease holder would otherwise
    block every other submitter forever and invisibly, so the wait gets its own
    deadline mirroring ``warmup_wait_deadline_ts`` — see the design §6/§9.
    """
    return _int_env(
        "BLAST_SUBMIT_SLOT_WAIT_MAX_SECONDS",
        _DEFAULT_SUBMIT_SLOT_WAIT_MAX_SECONDS,
        minimum=1,
    )


def split_child_gate_wait_max_seconds() -> int:
    """Per-child INLINE gate wait cap for the split fan-out (default 300).

    Unlike the regular submit path (which re-enqueues via Celery and frees the
    single worker between attempts), split children are dispatched sequentially
    inside one parent task and wait inline — so a child blocked on a full
    cluster monopolises the only worker. This cap is intentionally MUCH smaller
    than ``submit_slot_wait_max_seconds`` (the requeue-path budget) to bound
    head-of-line blocking: a child that cannot get a slot within this window is
    marked failed (parent reports partial failure) rather than starving every
    other task. The whole fan-out is additionally bounded by
    ``split_parent_gate_budget_seconds`` (design §7.1.4 / critique H4-H5).
    """
    return _int_env(
        "BLAST_SPLIT_CHILD_GATE_WAIT_MAX_SECONDS",
        _DEFAULT_SPLIT_CHILD_GATE_WAIT_MAX_SECONDS,
        minimum=1,
    )


def split_parent_gate_budget_seconds() -> int:
    """Overall wall-clock budget for ALL child gate waits in one fan-out.

    Computed ONCE before the dispatch loop so a Celery retry of the parent task
    cannot reset each child's wait budget and multiply the worker monopoly
    (critique H5). Once the budget is exhausted the remaining children are
    marked failed immediately without waiting.
    """
    return _int_env(
        "BLAST_SPLIT_PARENT_GATE_BUDGET_SECONDS",
        _DEFAULT_SPLIT_PARENT_GATE_BUDGET_SECONDS,
        minimum=1,
    )


def lease_clock_skew_seconds() -> int:
    """Margin added before treating a Lease as expired (default 30).

    Expiry is judged on the *caller's* wall clock, but the two submit paths
    (dashboard MI host vs in-cluster ``elb-openapi`` pod) have different clocks.
    A clock-ahead caller could judge a still-valid Lease expired and take it
    over → two concurrent submits. The skew margin makes a premature takeover
    require more than this many seconds of skew (design §4.2 / §9 clock-skew).
    """
    return _int_env(
        "BLAST_LEASE_CLOCK_SKEW_SECONDS",
        _DEFAULT_LEASE_CLOCK_SKEW_SECONDS,
        minimum=0,
    )


def finalizer_grace_seconds() -> int:
    """How long a lone (companion-less) finalizer still counts as a live slot.

    On Azure ``cloud_job_submission`` the ``app=blast`` batch Jobs are created
    asynchronously *after* submit returns, so a healthy submit is briefly a
    finalizer with no companion Jobs. This grace window covers that lag; past
    it, a companion-less finalizer is treated as a phantom slot and NOT counted
    (reconciled out-of-band, never silently auto-failed) — design §5.1.
    """
    return _int_env(
        "BLAST_FINALIZER_GRACE_SECONDS",
        _DEFAULT_FINALIZER_GRACE_SECONDS,
        minimum=0,
    )


def assert_coordination_invariants() -> None:
    """Fail fast if the Lease-release ordering chain is misconfigured.

    The conditional Lease release lives in a ``finally`` that runs on a normal
    return or a *catchable* exception, but NOT on a ``SIGKILL``. Celery's hard
    ``task_time_limit`` SIGKILLs the worker child, skipping the ``finally`` and
    orphaning the Lease until its TTL — blocking every other submitter. The full
    chain that guarantees a graceful (``finally``-running) failure first is:

        submit_exec_timeout < CELERY_TASK_SOFT_TIME_LIMIT
                            < CELERY_TASK_TIME_LIMIT
        submit_exec_timeout < lease_ttl

    The soft limit raises a catchable ``SoftTimeLimitExceeded`` (``finally`` still
    runs); only the hard limit skips it. Keeping the submit subprocess's own
    timeout below the soft limit is what makes the release reliable. This is only
    asserted when the ``k8s`` backend is active so a default-OFF deployment is
    never blocked by an unrelated Celery tuning choice (design §4.3).
    """
    if not is_k8s_backend():
        return
    submit_exec = SUBMIT_EXEC_TIMEOUT_SECONDS
    # Parse the Celery limits EXACTLY as ``api.celery_app`` does — a bare
    # ``int(os.environ.get(...))`` with no min/max clamp — so the invariant
    # validates the SAME numbers the worker is actually configured with. Using
    # ``_int_env`` here (which clamps at maximum=86_400) would let a worker run
    # with a larger hard limit than the value this assertion checked, silently
    # passing on a divergent config (critique M18).
    soft = _raw_int_env("CELERY_TASK_SOFT_TIME_LIMIT", 3300)
    hard = _raw_int_env("CELERY_TASK_TIME_LIMIT", 3600)
    lease_ttl = submit_lease_ttl_seconds()
    if not (submit_exec < soft < hard):
        raise ValueError(
            "BLAST_COORD_BACKEND=k8s requires "
            f"submit_exec({submit_exec}) < CELERY_TASK_SOFT_TIME_LIMIT({soft}) "
            f"< CELERY_TASK_TIME_LIMIT({hard}); otherwise a hard SIGKILL skips "
            "the finally that releases the submit Lease (design §4.3)."
        )
    if not (submit_exec < lease_ttl):
        raise ValueError(
            "BLAST_COORD_BACKEND=k8s requires "
            f"submit_exec({submit_exec}) < BLAST_SUBMIT_LEASE_TTL_SECONDS({lease_ttl}); "
            "otherwise an overrunning submit could be reclaimed by a second "
            "holder before it finishes → concurrent submit (design §4.3)."
        )
