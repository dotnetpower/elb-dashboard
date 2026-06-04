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
  `ROLLING_OUT_TIMEOUT_SECONDS`, `VALIDATING_TIMEOUT_SECONDS`,
  `CONFIRM_WINDOW_SECONDS`, `CONFIRM_CUTOVER_CONVERGE_GRACE_SECONDS`,
  `validating_timeout_seconds`, `confirm_window_seconds`,
  `_RUNNING_STATE_TERMINAL_FAILURES`.
Risky contracts: This task runs on every revision via beat. It MUST be
  safe to call against an idle/terminal row (no-op). The replica-zero
  guard escalates only when `started_at` is sufficiently in the past
  so a legitimate cold-start is never tripped.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py
  api/tests/test_upgrade_chaos.py`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from celery import shared_task

import api as _api
from api.services.upgrade import aca_template, history, revisions, rollout_watcher, state
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

# Blue/green-only budgets (charter §12a STRICT_BLUEGREEN gate). The green
# revision must reach a healthy running state within this window or the
# rollout aborts (green never took traffic, so blue is untouched).
VALIDATING_TIMEOUT_SECONDS = 15 * 60
# How long green serves 100% before the upgrade is declared succeeded and
# blue is garbage-collected. During this window any green degradation
# triggers an automatic traffic flip back to blue (guaranteed rollback).
CONFIRM_WINDOW_SECONDS = 5 * 60
# Hard cap for the confirm-window "traffic not yet on green" re-cutover
# retry loop. Once green is healthy and the confirm deadline has elapsed
# but `serving_revision()` still does not report green, the reconciler
# re-asserts the cutover each tick. If that never converges within this
# grace beyond the deadline, the row is escalated to `rollback_failed`
# rather than spinning in `confirming` forever (bounded-loop guarantee).
CONFIRM_CUTOVER_CONVERGE_GRACE_SECONDS = 5 * 60


def _env_int(name: str, default: int, *, minimum: int) -> int:
    """Read a positive int from the environment, clamped to ``minimum``.

    Returns ``default`` when unset or unparseable so a typo never silently
    disables a timeout. These knobs are read at call time (not import) so a
    revision can be reconfigured without a code change.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def validating_timeout_seconds() -> int:
    """Green-health window (env: UPGRADE_VALIDATING_TIMEOUT_SECONDS)."""
    return _env_int(
        "UPGRADE_VALIDATING_TIMEOUT_SECONDS", VALIDATING_TIMEOUT_SECONDS, minimum=60
    )


def confirm_window_seconds() -> int:
    """Confirm/bake window (env: UPGRADE_CONFIRM_WINDOW_SECONDS)."""
    return _env_int("UPGRADE_CONFIRM_WINDOW_SECONDS", CONFIRM_WINDOW_SECONDS, minimum=0)


def reconcile_rolling_out_inline(
    *,
    aca: object | None = None,
    watcher: object | None = None,
    now: Callable[[], datetime] | None = None,
    revisions_mod: object | None = None,
    gc: object | None = None,
) -> state.UpgradeState:
    """Drive `rolling_out` to `succeeded`/`failed_rollout` when possible.

    Also fails-out pre-PATCH states (`queued`/`fetching`/`building`/
    `patching`) that have been parked longer than the per-state budget —
    those are the worker-died-mid-task cases where no other code path
    can advance the row. Without this guard the row stayed in
    `queued`/`building` indefinitely and the SPA showed a permanently
    spinning progress bar.

    When `STRICT_BLUEGREEN` is on, the row also passes through
    `validating` and `confirming`. This reconciler drives those: it
    validates the green revision's health, cuts traffic over to green,
    bakes the confirm window, and either finalises to `succeeded` (then
    garbage-collects blue) or — if green degrades while serving — flips
    traffic back to blue (`rolled_back`).
    """
    row = state.get_state()
    clock = now or (lambda: datetime.now(UTC))

    if row.state in PRE_PATCH_STATES:
        stuck = _check_pre_patch_stuck(row, clock)
        if stuck is not None:
            return stuck
        return row

    watcher_mod = watcher or rollout_watcher
    revisions_module = revisions_mod or revisions

    if row.state == state.STATE_VALIDATING:
        return _reconcile_validating(row, clock, watcher_mod, revisions_module)
    if row.state == state.STATE_CONFIRMING:
        return _reconcile_confirming(row, clock, watcher_mod, revisions_module, gc)

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


def _green_health(status: rollout_watcher.RevisionStatus) -> str:
    """Classify a green revision's health: healthy | booting | failed.

    Mirrors the Single-mode success/replica-zero logic: a revision is
    healthy only when Running+Provisioned and not provably replica-zero;
    a provisioning failure or a terminal running_state is a hard failure;
    everything else is still booting.
    """
    running = status.running_state.lower()
    provisioning = status.provisioning_state.lower()
    if provisioning in {"failed", "canceled"}:
        return "failed"
    if running in _RUNNING_STATE_TERMINAL_FAILURES:
        return "failed"
    if running == "running" and provisioning == "provisioned":
        # `replicas == 0 and not active` is a crashed/never-came-up pod;
        # `replicas == -1` (SDK did not report) is treated as not-zero.
        if status.replicas == 0 and not status.active:
            return "failed"
        return "healthy"
    return "booting"


def _terminal_transition(
    row: state.UpgradeState,
    *,
    expected: str,
    new: str,
    detail: str,
    history_kind: str,
    progress: int,
) -> state.UpgradeState:
    """CAS the row to a terminal blue/green state with an audit event.

    Unlike `_fail_rollout` (which is hard-wired to expect
    `rolling_out`), the blue/green aborts originate from `validating` /
    `confirming`, so the caller supplies the expected source state.
    """
    LOGGER.warning("upgrade.reconcile job=%s %s->%s: %s", row.job_id, expected, new, detail)
    history.record_event(history_kind, job_id=row.job_id, detail=detail)
    truncated = detail[:240]
    try:
        return state.cas_state(
            expected_state=expected,
            new_state=new,
            mutate=lambda s, d=truncated: (
                setattr(s, "phase_detail", d),
                setattr(s, "phase_progress", progress),
            )[-1],
        )
    except (state.StateTransitionRefused, state.RowEtagMismatch):
        return state.get_state()


def _reconcile_validating(
    row: state.UpgradeState,
    clock: Callable[[], datetime],
    watcher_mod: object,
    revisions_mod: object,
) -> state.UpgradeState:
    """Validate green health, then cut traffic over and enter `confirming`.

    Green is staged at 0% traffic while blue serves 100%, so any failure
    here is a safe abort: blue never lost traffic, no flip is required.
    """
    green = row.green_revision
    if not green:
        return _terminal_transition(
            row,
            expected=state.STATE_VALIDATING,
            new=state.STATE_FAILED_ROLLOUT,
            detail="validating without a recorded green revision",
            history_kind="failed",
            progress=0,
        )

    # Timeout anchor: the moment the row ENTERED validating (green just
    # created), not `started_at` (which already absorbed clone+build). Fall
    # back to `started_at` only for rows written before this field existed.
    anchor = row.validating_started_at or row.started_at
    if anchor:
        try:
            elapsed = (clock() - datetime.fromisoformat(anchor)).total_seconds()
        except ValueError:
            elapsed = 0.0
        if elapsed > validating_timeout_seconds():
            return _terminal_transition(
                row,
                expected=state.STATE_VALIDATING,
                new=state.STATE_FAILED_ROLLOUT,
                detail=f"green {green} not healthy after {elapsed:.0f}s",
                history_kind="failed",
                progress=0,
            )

    try:
        status = watcher_mod.revision_status(green)
    except aca_template.TemplateError as exc:
        LOGGER.warning("upgrade.reconcile: cannot read green status: %s", exc)
        return row

    health = _green_health(status)
    if health == "failed":
        return _terminal_transition(
            row,
            expected=state.STATE_VALIDATING,
            new=state.STATE_FAILED_ROLLOUT,
            detail=(
                f"green {green} unhealthy "
                f"({status.provisioning_state}/{status.running_state}); "
                "blue still serves 100%"
            ),
            history_kind="failed",
            progress=0,
        )
    if health == "booting":
        try:
            state.update_state(
                lambda s: (
                    setattr(s, "phase_detail", "green booting; awaiting health"),
                    setattr(s, "phase_progress", 94),
                )[-1]
            )
        except state.RowEtagMismatch:
            pass
        return state.get_state()

    # Healthy → cut traffic over to green (green 100% / blue 0% kept warm).
    try:
        revisions_mod.cutover(green_revision=green, blue_revision=row.blue_revision)
    except revisions.RevisionsError as exc:
        return _terminal_transition(
            row,
            expected=state.STATE_VALIDATING,
            new=state.STATE_FAILED_ROLLOUT,
            detail=f"cutover to {green} failed: {exc}",
            history_kind="failed",
            progress=0,
        )
    deadline = (clock() + timedelta(seconds=confirm_window_seconds())).isoformat(
        timespec="seconds"
    )
    try:
        after = state.cas_state(
            expected_state=state.STATE_VALIDATING,
            new_state=state.STATE_CONFIRMING,
            mutate=lambda s: (
                setattr(s, "confirm_deadline", deadline),
                setattr(s, "traffic_serving", green),
                setattr(s, "phase_detail", "green serving 100%; baking confirm window"),
                setattr(s, "phase_progress", 97),
            )[-1],
        )
        history.record_event(
            "cutover", job_id=row.job_id, detail=f"green={green} blue={row.blue_revision}"
        )
        return after
    except (state.StateTransitionRefused, state.RowEtagMismatch):
        return state.get_state()


def _reconcile_confirming(
    row: state.UpgradeState,
    clock: Callable[[], datetime],
    watcher_mod: object,
    revisions_mod: object,
    gc: object | None,
) -> state.UpgradeState:
    """Bake the confirm window; finalise to `succeeded` or auto-rollback.

    Green serves 100% while blue stays warm at 0%. If green degrades the
    traffic flips back to blue (`rolled_back`, guaranteed rollback). Once
    the confirm deadline elapses with green healthy AND verified serving
    100%, the row is `succeeded` and blue is garbage-collected (no
    leftover containers).
    """
    green = row.green_revision
    if not green:
        return _terminal_transition(
            row,
            expected=state.STATE_CONFIRMING,
            new=state.STATE_ROLLBACK_FAILED,
            detail="confirming without a recorded green revision",
            history_kind="failed",
            progress=0,
        )

    try:
        status = watcher_mod.revision_status(green)
    except aca_template.TemplateError as exc:
        LOGGER.warning("upgrade.reconcile: cannot read green status: %s", exc)
        return row

    if _green_health(status) == "failed":
        # Guaranteed rollback: green degraded while serving → flip traffic
        # back to the still-warm blue revision.
        try:
            revisions_mod.flip_traffic(to_revision=row.blue_revision, from_revision=green)
        except revisions.RevisionsError as exc:
            return _terminal_transition(
                row,
                expected=state.STATE_CONFIRMING,
                new=state.STATE_ROLLBACK_FAILED,
                detail=f"green {green} degraded; rollback flip failed: {exc}",
                history_kind="failed",
                progress=0,
            )
        return _terminal_transition(
            row,
            expected=state.STATE_CONFIRMING,
            new=state.STATE_ROLLED_BACK,
            detail=f"green {green} degraded during bake; traffic reverted to blue",
            history_kind="rolled_back",
            progress=100,
        )

    # Still healthy: wait for the confirm deadline before finalising.
    if row.confirm_deadline:
        try:
            deadline = datetime.fromisoformat(row.confirm_deadline)
        except ValueError:
            deadline = clock()
        if clock() < deadline:
            return row

    # Deadline elapsed + green healthy → verify traffic actually sits on
    # green (not __version__) before declaring success.
    try:
        serving = revisions_mod.serving_revision()
    except revisions.RevisionsError as exc:
        LOGGER.warning("upgrade.reconcile: cannot read serving revision: %s", exc)
        return row
    if serving != green:
        # Traffic not (yet) on green — re-assert the cutover and wait, but
        # bound the retry: if the cutover never converges within a grace
        # window past the confirm deadline, the row would otherwise spin in
        # `confirming` forever. Escalate to `rollback_failed` (blue is still
        # the prior serving revision, so traffic stays on a known-good
        # version) instead of looping invisibly.
        if row.confirm_deadline:
            try:
                cap = datetime.fromisoformat(
                    row.confirm_deadline
                ) + timedelta(seconds=CONFIRM_CUTOVER_CONVERGE_GRACE_SECONDS)
            except ValueError:
                cap = None
            if cap is not None and clock() > cap:
                return _terminal_transition(
                    row,
                    expected=state.STATE_CONFIRMING,
                    new=state.STATE_ROLLBACK_FAILED,
                    detail=(
                        f"cutover to {green} never converged "
                        f"(serving={serving}) within grace; manual intervention "
                        "required"
                    ),
                    history_kind="failed",
                    progress=0,
                )
        try:
            revisions_mod.cutover(green_revision=green, blue_revision=row.blue_revision)
        except revisions.RevisionsError as exc:
            LOGGER.warning("upgrade.reconcile: re-cutover failed: %s", exc)
        return row

    try:
        after = state.cas_state(
            expected_state=state.STATE_CONFIRMING,
            new_state=state.STATE_SUCCEEDED,
            mutate=lambda s: (
                setattr(s, "phase_detail", f"green serving v{_api.__version__}"),
                setattr(s, "phase_progress", 100),
                setattr(s, "running_version", _api.__version__),
                setattr(s, "traffic_serving", green),
            )[-1],
        )
    except (state.StateTransitionRefused, state.RowEtagMismatch):
        return state.get_state()

    history.record_event("succeeded", job_id=row.job_id, running_version=_api.__version__)
    # Garbage-collect blue so no leftover container revisions remain.
    _run_gc(gc)
    return after


def _run_gc(gc: object | None) -> None:
    """Best-effort blue/green garbage collection; never blocks success."""
    try:
        gc_mod = gc
        if gc_mod is None:
            from api.tasks.upgrade import revision_gc as gc_mod  # lazy: avoid cycle
        gc_mod.collect_garbage_inline()
    except Exception as exc:
        LOGGER.warning("upgrade.reconcile: blue/green GC failed (non-fatal): %s", exc)


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
