"""Reconciler for the in-app self-upgrade flow.

Module summary: Beat-driven task that walks the persisted upgrade state
row and drives it forward (or aborts it) when the producing worker
cannot. Hosts the per-state budget guard that escalates dead-worker
scenarios, the new-revision pre-warm probe, the fast-fail for PATCH
that never landed, and the replica-zero/degraded crash-loop detection.

Responsibility: Out-of-band recovery + finalisation of the upgrade flow.
Edit boundaries: All reconciler decision logic lives here; no business
  state machine writes outside the existing CAS helpers in
  `api.services.upgrade.state`. Failure transitions delegate to the
  pipeline's `_fail_pre` / `_fail_rollout` so the audit trail stays
  consistent.
Key entry points: `reconcile_rolling_out_inline`, `reconcile_rolling_out`,
  `PRE_PATCH_BUDGET_SECONDS`, `PATCH_NEVER_LANDED_GRACE_SECONDS`,
  `ROLLING_OUT_TIMEOUT_SECONDS`, `_RUNNING_STATE_TERMINAL_FAILURES`.
Risky contracts: This task runs on every revision via beat. It MUST be
  safe to call against an idle/terminal row (no-op). The replica-zero
  guard escalates only when `started_at` is sufficiently in the past
  so a legitimate cold-start is never tripped.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py
  api/tests/test_upgrade_chaos.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from celery import shared_task

import api as _api
from api.services.upgrade import aca_template, history, rollout_watcher, state
from api.tasks.upgrade.pipeline import _fail_pre, _fail_rollout

LOGGER = logging.getLogger(__name__)


# Stuck guard for reconcile_rolling_out_inline. Reasonably generous;
# ACA's own startup-probe retries plus image pull can extend several
# minutes. The escape-hatch / rollback paths are always available so
# the operator is never trapped.
ROLLING_OUT_TIMEOUT_SECONDS = 15 * 60
# Fast-fail when the row says "rolling_out" but the ACA template still
# carries the old image after this many seconds (the new revision should
# at least appear in the latest_revision_name by then). 120 s is generous
# vs the typical begin_update -> revision-created lag of < 30 s.
PATCH_NEVER_LANDED_GRACE_SECONDS = 120

# Pre-PATCH stuck guard. Per-state budget so a fast operation (worker
# pickup) does not get the same generous ceiling as a slow one (3-way
# `az acr build`). The reconciler escalates a row whose elapsed time
# in its current state exceeds the per-state budget. Each budget is
# generous vs the observed P99 of that step so a legitimate slow run
# is never tripped. The reconciler runs every 60 s so worst-case
# dead-row latency is `budget + 60 s`.
PRE_PATCH_TIMEOUT_SECONDS = 35 * 60  # legacy aggregate budget (kept for callers)
PRE_PATCH_BUDGET_SECONDS: dict[str, int] = {
    state.STATE_QUEUED: 5 * 60,  # worker pickup (typical: <5 s)
    state.STATE_FETCHING: 10 * 60,  # git clone (typical: 10-60 s)
    state.STATE_BUILDING: 30 * 60,  # 3x az acr build (typical: 5-15 min total)
    state.STATE_PATCHING: 5 * 60,  # ACR snapshot + ARM PATCH prep (typical: <30 s)
}
PRE_PATCH_STATES = tuple(PRE_PATCH_BUDGET_SECONDS.keys())

# A revision whose `runningState` is one of these has crash-looped or
# been killed; the rollout watcher's `provisioning_state` check alone
# misses this because ACA reports `provisioning_state=Provisioned`
# while the container is in CrashLoopBackOff.
_RUNNING_STATE_TERMINAL_FAILURES = frozenset(
    {"degraded", "unhealthy", "failed", "deactivating"}
)


def reconcile_rolling_out_inline(
    *,
    aca: object | None = None,
    watcher: object | None = None,
    now: Callable[[], datetime] | None = None,
) -> state.UpgradeState:
    """Drive `rolling_out` to `succeeded`/`failed_rollout` when possible.

    Also fails-out pre-PATCH states (`queued`/`fetching`/`building`/
    `patching`) that have been parked longer than the per-state budget —
    those are the worker-died-mid-task cases where no other code path
    can advance the row. Without this guard the row stayed in
    `queued`/`building` indefinitely and the SPA showed a permanently
    spinning progress bar.
    """
    row = state.get_state()
    clock = now or (lambda: datetime.now(UTC))

    if row.state in PRE_PATCH_STATES:
        stuck = _check_pre_patch_stuck(row, clock)
        if stuck is not None:
            return stuck
        return row

    if row.state != state.STATE_ROLLING_OUT:
        if row.running_version != _api.__version__:
            try:
                row = state.update_state(
                    lambda s: setattr(s, "running_version", _api.__version__)
                )
            except state.RowEtagMismatch:
                pass
        return row

    aca_mod = aca or aca_template
    watcher_mod = watcher or rollout_watcher

    # Stuck guard: rolling_out > 15 min → fail_rollout so the operator
    # can rollback or use the escape-hatch.
    if row.started_at:
        try:
            started = datetime.fromisoformat(row.started_at)
            elapsed = (clock() - started).total_seconds()
            if elapsed > ROLLING_OUT_TIMEOUT_SECONDS:
                return _fail_rollout(
                    row.job_id,
                    f"rolling_out exceeded budget ({elapsed:.0f}s); "
                    "rollback or escape-hatch",
                )
        except ValueError:
            pass

    # Pre-warm gate: api `__version__` matching `target_version` plus
    # the ACA revision being Running+Provisioned avoids a brief window
    # where the SPA flips to `succeeded` while other sidecars are
    # still rebooting.
    if row.target_version and _api.__version__ == row.target_version:
        ready = _new_revision_is_ready(row, aca_mod, watcher_mod)
        if not ready:
            try:
                state.update_state(
                    lambda s: (
                        setattr(
                            s,
                            "phase_detail",
                            "new revision booting; awaiting readiness probe",
                        ),
                        setattr(s, "phase_progress", 95),
                    )[-1]
                )
            except state.RowEtagMismatch:
                pass
            return state.get_state()
        try:
            after = state.cas_state(
                expected_state=state.STATE_ROLLING_OUT,
                new_state=state.STATE_SUCCEEDED,
                mutate=lambda s: (
                    setattr(s, "phase_detail", f"new revision running v{_api.__version__}"),
                    setattr(s, "phase_progress", 100),
                    setattr(s, "running_version", _api.__version__),
                )[-1],
            )
            history.record_event(
                "succeeded",
                job_id=row.job_id,
                running_version=_api.__version__,
            )
            return after
        except (state.StateTransitionRefused, state.RowEtagMismatch):
            return state.get_state()

    # Fast-fail when the ARM PATCH evidently never landed.
    if row.target_version and row.started_at:
        try:
            started = datetime.fromisoformat(row.started_at)
            elapsed = (clock() - started).total_seconds()
        except ValueError:
            elapsed = 0
        if elapsed > PATCH_NEVER_LANDED_GRACE_SECONDS:
            try:
                deployed = aca_mod.read_current_images()
                target_in_template = _image_matches_version(
                    deployed.api, row.target_version
                )
            except aca_template.TemplateError:
                target_in_template = True  # don't escalate on a transient SDK glitch
            if not target_in_template:
                return _fail_rollout(
                    row.job_id,
                    f"ARM PATCH evidently never landed ({elapsed:.0f}s elapsed)",
                )

    try:
        latest = aca_mod.latest_revision_name()
    except aca_template.TemplateError as exc:
        LOGGER.warning("upgrade.reconcile: cannot read latest revision: %s", exc)
        return row
    try:
        status = watcher_mod.revision_status(latest)
    except aca_template.TemplateError as exc:
        LOGGER.warning("upgrade.reconcile: cannot read revision status: %s", exc)
        return row
    if (
        status.running_state.lower() == "running"
        and status.provisioning_state.lower() == "provisioned"
    ):
        # Replica-zero guard: Running+Provisioned but pods crashed
        # before becoming ready. Escalate after grace.
        if status.replicas == 0 and not status.active and row.started_at:
            try:
                started = datetime.fromisoformat(row.started_at)
                elapsed = (clock() - started).total_seconds()
            except ValueError:
                elapsed = 0
            if elapsed > PATCH_NEVER_LANDED_GRACE_SECONDS:
                return _fail_rollout(
                    row.job_id,
                    f"revision {latest} has 0 replicas after {elapsed:.0f}s; "
                    "new pods never came up",
                )
        return row
    if status.provisioning_state.lower() in {"failed", "canceled"}:
        return _fail_rollout(
            row.job_id,
            f"revision {latest} provisioning {status.provisioning_state}",
        )
    if status.running_state.lower() in _RUNNING_STATE_TERMINAL_FAILURES:
        return _fail_rollout(
            row.job_id,
            f"revision {latest} running_state={status.running_state}",
        )
    return row


def _check_pre_patch_stuck(
    row: state.UpgradeState, clock: Callable[[], datetime]
) -> state.UpgradeState | None:
    """Return the post-fail snapshot when ``row`` exceeds its per-state budget.

    Returns ``None`` when within budget or `started_at` is malformed —
    we never escalate on a parse error; the next tick retries.
    """
    if not row.started_at:
        return None
    try:
        started = datetime.fromisoformat(row.started_at)
    except ValueError:
        return None
    elapsed = (clock() - started).total_seconds()
    budget = PRE_PATCH_BUDGET_SECONDS.get(row.state, PRE_PATCH_TIMEOUT_SECONDS)
    if elapsed <= budget:
        return None
    return _fail_pre(
        row.job_id,
        f"stuck in {row.state} for {elapsed:.0f}s (budget {budget}s); "
        "producing worker likely died",
    )


def _new_revision_is_ready(
    row: state.UpgradeState, aca_mod: object, watcher_mod: object
) -> bool:
    """Best-effort pre-warm gate before declaring `succeeded`.

    Open-fail: when the gate cannot read ARM, return True so a transient
    ARM glitch doesn't block forever — the stuck-guard remains the
    upper bound.
    """
    try:
        latest = aca_mod.latest_revision_name()  # type: ignore[attr-defined]
        status = watcher_mod.revision_status(latest)  # type: ignore[attr-defined]
    except Exception as exc:
        LOGGER.warning(
            "upgrade.reconcile: pre-warm probe failed (%s); declaring ready", exc
        )
        return True
    running_ok = status.running_state.lower() in {"running", ""}
    provisioning_ok = status.provisioning_state.lower() in {"provisioned", ""}
    replicas_ok = status.replicas != 0  # -1 (unknown) or >=1 both pass
    return running_ok and provisioning_ok and replicas_ok


def _image_matches_version(image_ref: str, target_version: str) -> bool:
    """True iff ``image_ref`` is tagged exactly ``v<target_version>``.

    Compares the parsed tag for equality so `0.3` does NOT match
    `0.3.0-alpha` via substring search. Digest-pinned refs are not
    produced by the in-app build pipeline and are not supported here.
    """
    if not image_ref or not target_version:
        return False
    try:
        from api.services.upgrade.acr_inventory import parse_image_ref

        _endpoint, _repo, tag = parse_image_ref(image_ref)
    except ValueError:
        return False
    return tag == f"v{target_version}"


@shared_task(name="api.tasks.upgrade.reconcile_rolling_out")
def reconcile_rolling_out() -> dict:
    return reconcile_rolling_out_inline().to_public_dict()
