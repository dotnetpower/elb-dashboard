"""Celery tasks for the in-app self-upgrade flow.

Module summary: Hosts the beat-driven discovery task, the upgrade
pipeline triggered from the SPA, and the post-rollout reconciler that
runs on the newly booted revision. PR1 shipped read-only discovery, PR2
added clone + build, PR3 (this) wires the ARM PATCH, rollout watcher,
rollback path, and reconciler that finalises `succeeded` /
`failed_rollout` after the new revision boots.

Responsibility: Long-running side effects for upgrade discovery and execution.
Edit boundaries: Tasks here own the state-row transitions; routes call
  into these tasks via `.delay()` or directly via the helper functions
  exposed for the synchronous "check now" / "start" / "rollback" endpoints.
Key entry points: `check_latest`, `check_latest_inline`, `execute_upgrade`,
  `start_upgrade_inline`, `start_rollback_inline`, `reconcile_rolling_out`,
  `STATE_TRANSITION_TIMELINE`.
Risky contracts: `start_upgrade_inline` and `start_rollback_inline` are
  the single funnels through which concurrent operators are serialised
  via state CAS. `execute_upgrade_inline` commits the
  `state=rolling_out` row BEFORE the ARM PATCH so the row survives the
  producing revision being torn down — the reconciler on the freshly
  booted revision then finalises the state.
Validation: `uv run pytest -q api/tests/test_upgrade_*.py`.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from celery import shared_task

from api import __version__
from api.services.upgrade import (
    aca_template,
    acr_inventory,
    build_logs,
    escape_hatch,
    git_workspace,
    history,
    image_builder,
    remote_tags,
    rollout_watcher,
    state,
)

LOGGER = logging.getLogger(__name__)


def _record_running_version(s: state.UpgradeState) -> None:
    """Keep the row's running_version in sync with the api's __version__."""
    if s.running_version != __version__:
        s.running_version = __version__


def _set_latest(
    s: state.UpgradeState, remote: str, latest: remote_tags.RemoteTag, checked_at: str
) -> None:
    s.git_remote = remote
    s.latest_version = latest.name
    s.latest_sha = latest.commit_sha
    s.latest_checked_at = checked_at
    _record_running_version(s)


def _clear_latest(s: state.UpgradeState, remote: str, checked_at: str) -> None:
    s.git_remote = remote
    s.latest_version = ""
    s.latest_sha = ""
    s.latest_checked_at = checked_at
    _record_running_version(s)


def check_latest_inline() -> state.UpgradeState:
    """Run one discovery round and persist the result. Returns the updated row."""
    remote = remote_tags.configured_remote()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")

    if not remote:
        return state.update_state(lambda s: _clear_latest(s, "", checked_at))

    try:
        tags = remote_tags.fetch_release_tags(remote)
    except remote_tags.RemoteTagsError as exc:
        LOGGER.warning("upgrade.check_latest: remote %s failed: %s", remote, exc)
        return state.update_state(lambda s: _clear_latest(s, remote, checked_at))

    if not tags:
        return state.update_state(lambda s: _clear_latest(s, remote, checked_at))

    latest = tags[0]
    return state.update_state(lambda s: _set_latest(s, remote, latest, checked_at))


@shared_task(name="api.tasks.upgrade.check_latest")
def check_latest() -> dict:
    """Beat-scheduled wrapper around :func:`check_latest_inline`."""
    updated = check_latest_inline()
    return updated.to_public_dict()


# ---------------------------------------------------------------------------
# PR2/PR3: upgrade execution pipeline.
# ---------------------------------------------------------------------------


class UpgradeStartRefused(RuntimeError):
    """Raised when the upgrade-start CAS cannot proceed (already in progress)."""


class RollbackStartRefused(RuntimeError):
    """Raised when the rollback CAS cannot proceed."""


STATE_TRANSITION_TIMELINE = (
    state.STATE_IDLE,
    state.STATE_QUEUED,
    state.STATE_FETCHING,
    state.STATE_BUILDING,
    state.STATE_PATCHING,
    state.STATE_ROLLING_OUT,
    state.STATE_SUCCEEDED,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def start_upgrade_inline(
    *,
    target_version: str,
    target_sha: str,
    started_by_oid: str,
    enqueue: Callable[[str, str, str, str], object] | None = None,
) -> state.UpgradeState:
    """CAS the row from idle -> queued and enqueue the Celery task."""
    if not target_version:
        raise UpgradeStartRefused("target_version required")
    job_id = uuid.uuid4().hex[:16]
    now = _utc_now()

    def mutate(s: state.UpgradeState) -> None:
        s.target_version = target_version
        s.target_sha = target_sha or ""
        s.job_id = job_id
        s.started_by_oid = started_by_oid or ""
        s.started_at = now
        s.phase_detail = "queued"
        s.phase_progress = 1
        s.build_log_blob = ""
        s.rollback_target_json = ""

    try:
        updated = state.cas_state(
            expected_state=state.STATE_IDLE,
            new_state=state.STATE_QUEUED,
            mutate=mutate,
        )
    except state.StateTransitionRefused as exc:
        raise UpgradeStartRefused(
            f"upgrade already in progress (state={exc.current})"
        ) from exc

    history.record_event(
        "start",
        job_id=job_id,
        target_version=target_version,
        target_sha=target_sha or "",
        started_by_oid=started_by_oid or "",
    )

    submit = enqueue or _default_enqueue
    try:
        submit(target_version, target_sha or "", started_by_oid or "", job_id)
    except Exception as enqueue_exc:
        LOGGER.exception("upgrade.start: enqueue failed")
        # Sanitise: a Celery broker exception may carry the broker URL
        # (which includes a password component on `redis://:pw@host`).
        # We surface only the exception type to the SPA-visible field;
        # the full traceback stays in the api log via LOGGER.exception.
        from api.services.sanitise import sanitise

        detail = sanitise(f"enqueue_failed: {type(enqueue_exc).__name__}")[:240]
        try:
            state.cas_state(
                expected_state=state.STATE_QUEUED,
                new_state=state.STATE_IDLE,
                mutate=lambda s: setattr(s, "phase_detail", detail),
            )
        except state.StateTransitionRefused:
            pass
        raise
    return updated


def _default_enqueue(
    target_version: str, target_sha: str, started_by_oid: str, job_id: str
) -> object:
    return execute_upgrade.delay(target_version, target_sha, started_by_oid, job_id)


@shared_task(name="api.tasks.upgrade.execute_upgrade")
def execute_upgrade(
    target_version: str,
    target_sha: str,
    started_by_oid: str,
    job_id: str,
) -> dict:
    """Worker-side upgrade pipeline."""
    return execute_upgrade_inline(
        target_version=target_version,
        target_sha=target_sha,
        started_by_oid=started_by_oid,
        job_id=job_id,
    ).to_public_dict()


def execute_upgrade_inline(
    *,
    target_version: str,
    target_sha: str,
    started_by_oid: str,
    job_id: str,
    runner: object | None = None,
    aca: object | None = None,
) -> state.UpgradeState:
    """Run the upgrade pipeline end-to-end.

    Parameters ``runner`` (terminal_exec) and ``aca`` (an object with the
    same surface as `api.services.upgrade.aca_template`) are injected so
    tests can drive the full pipeline without a terminal sidecar or ARM.
    """
    from api.services import terminal_exec as _exec

    runner = runner or _exec
    aca_mod = aca or aca_template
    remote = remote_tags.configured_remote()
    if not remote:
        return _fail_pre(job_id, "UPGRADE_GIT_REMOTE is not set")

    # 1. fetching
    try:
        state.cas_state(
            expected_state=state.STATE_QUEUED,
            new_state=state.STATE_FETCHING,
            mutate=lambda s: (
                setattr(s, "phase_detail", f"git clone v{target_version}"),
                setattr(s, "phase_progress", 10),
            )[-1],
        )
    except state.StateTransitionRefused as exc:
        LOGGER.warning("upgrade.execute: row not queued (%s); aborting", exc.current)
        return state.get_state()

    try:
        workspace = git_workspace.clone(
            git_remote=remote,
            target_version=target_version,
            job_id=job_id,
            runner=runner,
        )
    except git_workspace.WorkspaceError as exc:
        return _fail_pre(job_id, f"git clone failed: {exc}")

    # 2. building (sequential per component)
    try:
        state.cas_state(
            expected_state=state.STATE_FETCHING,
            new_state=state.STATE_BUILDING,
            mutate=lambda s: (
                setattr(s, "phase_detail", "az acr build api"),
                setattr(s, "phase_progress", 30),
            )[-1],
        )
    except state.StateTransitionRefused as exc:
        return _fail_pre(job_id, f"state moved during fetch: {exc.current}")

    built: list[image_builder.ImageBuildResult] = []
    components = ("api", "frontend", "terminal")
    for idx, component in enumerate(components):
        progress = 30 + int(40 * (idx / len(components)))
        try:
            state.update_state(
                lambda s, c=component, p=progress: (
                    setattr(s, "phase_detail", f"az acr build {c}"),
                    setattr(s, "phase_progress", p),
                    setattr(s, "build_log_blob", build_logs.blob_name(job_id, c)),
                )[-1]
            )
        except state.RowEtagMismatch:
            LOGGER.warning("upgrade.execute: stale etag on progress write; continuing")
        try:
            result = image_builder.build(
                component=component,
                target_version=target_version,
                source_dir=workspace.target_dir,
                job_id=job_id,
                runner=runner,
            )
        except image_builder.ImageBuilderError as exc:
            return _fail_pre(job_id, f"az acr build {component} failed: {exc}")
        built.append(result)

    # 3. patching — snapshot rollback target, swap template, commit
    #    state=rolling_out BEFORE the ARM PATCH so the row survives this
    #    revision being torn down by ACA.
    try:
        state.cas_state(
            expected_state=state.STATE_BUILDING,
            new_state=state.STATE_PATCHING,
            mutate=lambda s: (
                setattr(s, "phase_detail", "snapshot rollback target"),
                setattr(s, "phase_progress", 80),
            )[-1],
        )
    except state.StateTransitionRefused as exc:
        return _fail_pre(job_id, f"state moved during build: {exc.current}")

    try:
        previous_images = aca_mod.read_current_images()
    except aca_template.TemplateError as exc:
        return _fail_pre(job_id, f"read current template failed: {exc}")

    # Pre-record the escape-hatch + rollback snapshot so even if the api
    # sidecar dies mid-PATCH the operator can still recover from the
    # persisted row + audit history.
    target_images = aca_template._compute_target_images(target_version)
    plan = escape_hatch.build_plan(previous_images)
    try:
        state.update_state(
            lambda s: (
                setattr(s, "rollback_target_json", json.dumps(previous_images.as_dict())),
                setattr(s, "current_images_json", json.dumps(target_images.as_dict())),
                setattr(s, "phase_detail", "begin_update"),
                setattr(s, "phase_progress", 85),
            )[-1]
        )
    except state.RowEtagMismatch:
        LOGGER.warning("upgrade.execute: stale etag on snapshot write; continuing")
    LOGGER.info(
        "upgrade.execute: escape_hatch_commands=%s", json.dumps(plan.commands)
    )
    history.record_event(
        "escape_hatch",
        job_id=job_id,
        commands=plan.commands,
        snapshot=previous_images.as_dict(),
        target=target_images.as_dict(),
    )

    # Move to rolling_out BEFORE issuing PATCH — if this sidecar dies
    # during the swap the next revision sees the row in rolling_out and
    # the reconciler decides the final state.
    try:
        state.cas_state(
            expected_state=state.STATE_PATCHING,
            new_state=state.STATE_ROLLING_OUT,
            mutate=lambda s: (
                setattr(s, "phase_detail", "ARM PATCH submitted"),
                setattr(s, "phase_progress", 90),
            )[-1],
        )
    except state.StateTransitionRefused as exc:
        return _fail_pre(job_id, f"state moved during patching: {exc.current}")

    revision_suffix = f"v{target_version.replace('.', '-')}-{job_id[:6]}"
    try:
        aca_mod.swap_images(
            target_version=target_version, revision_suffix=revision_suffix
        )
    except aca_template.TemplateError as exc:
        LOGGER.exception("upgrade.execute: begin_update failed; row stays in rolling_out")
        return _fail_rollout(job_id, f"begin_update failed: {exc}")
    return state.get_state()


def _fail_pre(job_id: str, detail: str) -> state.UpgradeState:
    """Move the row to `failed_pre` from any pre-PATCH stage via CAS."""
    LOGGER.warning("upgrade.execute job=%s failed_pre: %s", job_id, detail)
    history.record_event("failed", job_id=job_id, stage="pre", detail=detail)
    truncated = detail[:240]
    for expected in (
        state.STATE_QUEUED,
        state.STATE_FETCHING,
        state.STATE_BUILDING,
        state.STATE_PATCHING,
    ):
        try:
            return state.cas_state(
                expected_state=expected,
                new_state=state.STATE_FAILED_PRE,
                mutate=lambda s, d=truncated: (
                    setattr(s, "phase_detail", d),
                    setattr(s, "phase_progress", 0),
                )[-1],
            )
        except state.StateTransitionRefused:
            continue
        except state.RowEtagMismatch:
            continue
    return state.get_state()


def _fail_rollout(job_id: str, detail: str) -> state.UpgradeState:
    """Move the row to `failed_rollout` (post-PATCH failure)."""
    LOGGER.warning("upgrade.execute job=%s failed_rollout: %s", job_id, detail)
    history.record_event("failed", job_id=job_id, stage="rollout", detail=detail)
    truncated = detail[:240]
    try:
        return state.cas_state(
            expected_state=state.STATE_ROLLING_OUT,
            new_state=state.STATE_FAILED_ROLLOUT,
            mutate=lambda s, d=truncated: (
                setattr(s, "phase_detail", d),
                setattr(s, "phase_progress", 0),
            )[-1],
        )
    except (state.StateTransitionRefused, state.RowEtagMismatch):
        return state.get_state()


# ---------------------------------------------------------------------------
# Reconciler — runs on every revision via beat. Finalises rolling_out.
# ---------------------------------------------------------------------------


def reconcile_rolling_out_inline(
    *,
    aca: object | None = None,
    watcher: object | None = None,
    now: Callable[[], datetime] | None = None,
) -> state.UpgradeState:
    """Drive `rolling_out` to `succeeded`/`failed_rollout` when possible."""
    row = state.get_state()
    if row.state != state.STATE_ROLLING_OUT:
        if row.running_version != __version__:
            try:
                row = state.update_state(
                    lambda s: setattr(s, "running_version", __version__)
                )
            except state.RowEtagMismatch:
                pass
        return row

    aca_mod = aca or aca_template
    watcher_mod = watcher or rollout_watcher
    clock = now or (lambda: datetime.now(UTC))

    # Stuck guard: if rolling_out has been the state longer than the
    # rollout budget we mark failed_rollout so the operator can rollback
    # or escape-hatch. ACA's own startup probe retries don't have a
    # natural ceiling, but we cap our row at 15 minutes.
    if row.started_at:
        try:
            started = datetime.fromisoformat(row.started_at)
            elapsed = (clock() - started).total_seconds()
            if elapsed > ROLLING_OUT_TIMEOUT_SECONDS:
                return _fail_rollout(
                    row.job_id,
                    f"rolling_out exceeded budget ({elapsed:.0f}s); rollback or escape-hatch",
                )
        except ValueError:
            pass

    # The simplest reliable signal: the running api version matches the
    # target version. If so, the new revision is up and we are it.
    if row.target_version and __version__ == row.target_version:
        try:
            after = state.cas_state(
                expected_state=state.STATE_ROLLING_OUT,
                new_state=state.STATE_SUCCEEDED,
                mutate=lambda s: (
                    setattr(s, "phase_detail", f"new revision running v{__version__}"),
                    setattr(s, "phase_progress", 100),
                    setattr(s, "running_version", __version__),
                )[-1],
            )
            history.record_event(
                "succeeded",
                job_id=row.job_id,
                running_version=__version__,
            )
            return after
        except (state.StateTransitionRefused, state.RowEtagMismatch):
            return state.get_state()

    # Fast-fail when the ARM PATCH evidently never landed: if the row has
    # been `rolling_out` for >2 minutes but the deployed template still
    # carries the OLD image refs (i.e. target_version is missing), the
    # producing worker died after the CAS commit but before begin_update
    # completed. Short-circuit the 15-min stuck guard so the operator can
    # restart sooner.
    if row.target_version and row.started_at:
        try:
            started = datetime.fromisoformat(row.started_at)
            elapsed = (clock() - started).total_seconds()
        except ValueError:
            elapsed = 0
        if elapsed > PATCH_NEVER_LANDED_GRACE_SECONDS:
            try:
                deployed = aca_mod.read_current_images()
                target_in_template = f":v{row.target_version}" in deployed.api
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
        # We are likely the old revision still draining; let the new
        # revision's reconciler finalise. No state change here.
        return row
    if status.provisioning_state.lower() in {"failed", "canceled"}:
        return _fail_rollout(
            row.job_id,
            f"revision {latest} provisioning {status.provisioning_state}",
        )
    return row


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


@shared_task(name="api.tasks.upgrade.reconcile_rolling_out")
def reconcile_rolling_out() -> dict:
    return reconcile_rolling_out_inline().to_public_dict()


# ---------------------------------------------------------------------------
# Rollback.
# ---------------------------------------------------------------------------


def start_rollback_inline(
    *,
    started_by_oid: str,
    aca: object | None = None,
    watcher: object | None = None,
    acr: object | None = None,
) -> state.UpgradeState:
    """PATCH the Container App back to the snapshot taken before the upgrade.

    Allowed from any post-PATCH state (`rolling_out`, `succeeded`,
    `failed_rollout`). Refuses when there is no rollback target, when
    the row is mid-upgrade pre-PATCH, or when ACR no longer carries the
    snapshotted tags (verified via the pre-flight before the CAS).
    """
    row = state.get_state()
    if row.state not in {
        state.STATE_ROLLING_OUT,
        state.STATE_SUCCEEDED,
        state.STATE_FAILED_ROLLOUT,
    }:
        raise RollbackStartRefused(
            f"rollback only valid after PATCH was issued (state={row.state})"
        )
    target_dict = row.rollback_target()
    if not target_dict:
        raise RollbackStartRefused("no rollback target snapshot is recorded")

    aca_mod = aca or aca_template
    acr_mod = acr or acr_inventory
    try:
        target_images = aca_template.SidecarImages(
            api=target_dict["api"],
            frontend=target_dict["frontend"],
            terminal=target_dict["terminal"],
        )
    except KeyError as exc:
        raise RollbackStartRefused(f"snapshot missing key: {exc}") from exc

    # ACR pre-flight — refuse the rollback when any snapshotted tag has
    # already been retention-purged. Catches the silent-failure path
    # where the rollback PATCH succeeds but ACA cannot pull and the
    # new revision crashloops.
    refs = [target_images.api, target_images.frontend, target_images.terminal]
    missing: list[str] = []
    try:
        probes = acr_mod.lookup_images(refs)
    except Exception as exc:
        LOGGER.warning("upgrade.rollback: ACR pre-flight failed: %s", exc)
        probes = []
    if probes:
        missing = [p.image_ref for p in probes if not p.exists]
    if missing:
        raise RollbackStartRefused(
            "ACR no longer carries the snapshotted tags: " + ", ".join(missing)
        )

    suffix = f"rb-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    try:
        state.cas_state(
            expected_state=row.state,
            new_state=state.STATE_ROLLING_BACK,
            mutate=lambda s: (
                setattr(s, "phase_detail", f"rollback PATCH suffix={suffix}"),
                setattr(s, "phase_progress", 50),
                setattr(s, "started_by_oid", started_by_oid or s.started_by_oid),
            )[-1],
        )
    except state.StateTransitionRefused as exc:
        raise RollbackStartRefused(
            f"row moved before rollback could start (state={exc.current})"
        ) from exc

    history.record_event(
        "rollback_start",
        job_id=row.job_id,
        started_by_oid=started_by_oid or "",
        target=target_images.as_dict(),
    )

    try:
        aca_mod.apply_images(images=target_images, revision_suffix=suffix)
    except aca_template.TemplateError as exc:
        # rollback PATCH itself failed; mark the row and surface to UI.
        return _fail_rollback(str(exc))

    try:
        after = state.cas_state(
            expected_state=state.STATE_ROLLING_BACK,
            new_state=state.STATE_ROLLED_BACK,
            mutate=lambda s: (
                setattr(s, "phase_detail", "rollback PATCH submitted"),
                setattr(s, "phase_progress", 100),
                setattr(s, "current_images_json", json.dumps(target_images.as_dict())),
            )[-1],
        )
        history.record_event(
            "rollback_done", job_id=after.job_id, target=target_images.as_dict()
        )
        return after
    except (state.StateTransitionRefused, state.RowEtagMismatch):
        return state.get_state()


def _fail_rollback(detail: str) -> state.UpgradeState:
    truncated = detail[:240]
    try:
        return state.cas_state(
            expected_state=state.STATE_ROLLING_BACK,
            new_state=state.STATE_ROLLBACK_FAILED,
            mutate=lambda s, d=truncated: (
                setattr(s, "phase_detail", d),
                setattr(s, "phase_progress", 0),
            )[-1],
        )
    except (state.StateTransitionRefused, state.RowEtagMismatch):
        return state.get_state()
