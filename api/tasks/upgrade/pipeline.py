"""Pipeline tasks for the in-app self-upgrade flow.

Module summary: The "happy path" + failure handlers — start, execute,
and the `_fail_pre`/`_fail_rollout` CAS transitions. Also hosts the
discovery (`check_latest`) wrapper because both flows pivot off the
same `state.UpgradeState` schema.

Responsibility: Operator-triggered start + worker-side build/PATCH
  execution + the failure-state transitions called from both this
  module and the reconciler.
Edit boundaries: All state-writing pipeline logic lives here. The
  reconciler imports `_fail_pre`/`_fail_rollout` from this module so
  the audit trail and progress-zeroing stay consistent.
Key entry points: `start_upgrade_inline`, `execute_upgrade_inline`,
  `execute_upgrade`, `check_latest_inline`, `check_latest`,
  `_fail_pre`, `_fail_rollout`, `UpgradeStartRefused`,
  `STATE_TRANSITION_TIMELINE`.
Risky contracts: `start_upgrade_inline` and the pipeline are the single
  funnel through which concurrent operators are serialised via state
  CAS. `execute_upgrade_inline` commits `state=rolling_out` BEFORE the
  ARM PATCH so the row survives the producing revision being torn
  down — the reconciler on the freshly booted revision then finalises
  the state.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py`.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from celery import shared_task

import api as _api
from api.services.upgrade import (
    aca_template,
    acr_inventory,
    build_logs,
    escape_hatch,
    git_workspace,
    history,
    image_builder,
    remote_tags,
    revisions,
    state,
)

LOGGER = logging.getLogger(__name__)


class UpgradeStartRefused(RuntimeError):
    """Raised when the upgrade-start CAS cannot proceed (already in progress)."""


STATE_TRANSITION_TIMELINE = (
    state.STATE_IDLE,
    state.STATE_QUEUED,
    state.STATE_FETCHING,
    state.STATE_BUILDING,
    state.STATE_PATCHING,
    state.STATE_ROLLING_OUT,
    # Blue/green-only intermediate states (entered when STRICT_BLUEGREEN is
    # on). The Single-mode happy path skips straight from rolling_out to
    # succeeded; the blue/green path inserts validating + confirming.
    state.STATE_VALIDATING,
    state.STATE_CONFIRMING,
    state.STATE_SUCCEEDED,
)
# Invariant enforced by tests: every state in STATE_TRANSITION_TIMELINE
# except the terminal must be the `expected_state` of exactly one
# `cas_state` call in this module — the next entry is the corresponding
# `new_state`. Catches drift when a new intermediate state is added to
# `state.VALID_STATES` but the happy path skips it.


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Discovery (beat-driven + sync from the SPA's "Check remote" button).
# ---------------------------------------------------------------------------


def _record_running_version(s: state.UpgradeState) -> None:
    """Keep the row's running_version in sync with the api's __version__."""
    if s.running_version != _api.__version__:
        s.running_version = _api.__version__


def _set_latest(
    s: state.UpgradeState,
    remote: str,
    latest: remote_tags.RemoteTag,
    checked_at: str,
    *,
    commit_sha: str = "",
) -> None:
    s.git_remote = remote
    s.latest_version = latest.name
    s.latest_sha = latest.commit_sha
    s.latest_commit_sha = commit_sha
    s.latest_checked_at = checked_at
    _record_running_version(s)


def _clear_latest(s: state.UpgradeState, remote: str, checked_at: str) -> None:
    s.git_remote = remote
    s.latest_version = ""
    s.latest_sha = ""
    s.latest_commit_sha = ""
    s.latest_checked_at = checked_at
    _record_running_version(s)


def check_latest_inline() -> state.UpgradeState:
    """Run one discovery round and persist the result.

    Channel-aware: the newest release tag is always discovered (the primary,
    well-tested path). When the persisted row has ``track_commits`` on (the
    default), the tracking-branch HEAD commit is additionally discovered
    (best-effort) so the SPA can surface a new commit even between releases.
    A branch-head discovery failure never fails the release check — it just
    leaves ``latest_commit_sha`` empty. When the channel is off, the commit
    sha is cleared.
    """
    track = bool(state.get_state().track_commits)
    remote = remote_tags.configured_remote()
    checked_at = datetime.now(UTC).isoformat(timespec="seconds")

    if not remote:
        return state.update_state(lambda s: _clear_latest(s, "", checked_at))

    try:
        tags = remote_tags.fetch_release_tags(remote)
    except remote_tags.RemoteTagsError as exc:
        LOGGER.warning("upgrade.check_latest: remote %s failed: %s", remote, exc)
        return state.update_state(lambda s: _clear_latest(s, remote, checked_at))

    head = ""
    if track:
        try:
            head = remote_tags.fetch_branch_head(
                remote, branch=remote_tags.DEFAULT_TRACK_BRANCH
            )
        except remote_tags.RemoteTagsError as exc:
            # Best-effort: a branch-head failure must not sink the release
            # check. Leave the commit indicator empty for this round.
            LOGGER.warning(
                "upgrade.check_latest: branch-head discovery failed for %s: %s",
                remote,
                exc,
            )
            head = ""

    latest = tags[0] if tags else None

    def mutate(s: state.UpgradeState) -> None:
        s.git_remote = remote
        s.latest_checked_at = checked_at
        s.latest_version = latest.name if latest else ""
        s.latest_sha = latest.commit_sha if latest else ""
        s.latest_commit_sha = head
        _record_running_version(s)

    return state.update_state(mutate)


@shared_task(name="api.tasks.upgrade.check_latest")
def check_latest() -> dict:
    """Beat-scheduled wrapper around :func:`check_latest_inline`."""
    return check_latest_inline().to_public_dict()


# ---------------------------------------------------------------------------
# Start (CAS idle -> queued, enqueue worker task).
# ---------------------------------------------------------------------------


def start_upgrade_inline(
    *,
    target_version: str,
    target_sha: str,
    started_by_oid: str,
    reason: str = "",
    idempotency_key: str = "",
    enqueue: Callable[[str, str, str, str], object] | None = None,
) -> state.UpgradeState:
    """CAS the row from idle -> queued and enqueue the Celery task.

    ``idempotency_key``: same key + same target_version → return the
    existing row (no 409). Protects double-click / browser-retry.
    """
    if not target_version:
        raise UpgradeStartRefused("target_version required")
    if idempotency_key:
        existing = state.get_state()
        if (
            existing.idempotency_key == idempotency_key
            and existing.target_version == target_version
        ):
            LOGGER.info(
                "upgrade.start: idempotent retry recognised (key=%s, state=%s)",
                idempotency_key,
                existing.state,
            )
            return existing
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
        s.idempotency_key = idempotency_key or ""

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
        reason=reason or "",
    )

    submit = enqueue or _default_enqueue
    try:
        submit(target_version, target_sha or "", started_by_oid or "", job_id)
    except Exception as enqueue_exc:
        LOGGER.exception("upgrade.start: enqueue failed")
        # Sanitise so a `redis://:pw@host` broker URL embedded in the
        # exception repr does not leak into the SPA-visible field.
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
            return _fail_pre(
                job_id,
                f"az acr build {component} failed: {exc}",
                orphan_image_refs=[r.image_ref for r in built],
            )
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
    bluegreen = revisions.strict_bluegreen()
    blue_revision = ""
    green_revision = ""
    if bluegreen:
        # Blue/green: pin 100% of traffic to the currently-serving (blue)
        # revision BEFORE the swap so the green revision `swap_images`
        # creates starts at 0% traffic. Without this pin, Multiple mode
        # routes 100% to the newest (green) revision the moment it is
        # created, defeating the validation gate.
        try:
            app_name = aca_template._env(aca_template.CONTAINER_APP_NAME_ENV)
            green_revision = f"{app_name}--{revision_suffix}"
            blue_revision = revisions.serving_revision()
            revisions.pin_traffic(
                revision_name=blue_revision, label=revisions.BLUE_LABEL
            )
        except (revisions.RevisionsError, aca_template.TemplateError) as exc:
            return _fail_rollout(job_id, f"blue/green pin failed: {exc}")

    try:
        aca_mod.swap_images(
            target_version=target_version, revision_suffix=revision_suffix
        )
    except aca_template.TemplateError as exc:
        LOGGER.exception("upgrade.execute: begin_update failed; row stays in rolling_out")
        return _fail_rollout(job_id, f"begin_update failed: {exc}")

    if bluegreen:
        # Hand off to the reconciler (which runs on the still-alive blue
        # revision via beat): it validates green health, cuts traffic over,
        # bakes the confirm window, then marks succeeded + GCs blue. The
        # worker task returns promptly rather than blocking on the bake.
        try:
            entered = _utc_now()
            return state.cas_state(
                expected_state=state.STATE_ROLLING_OUT,
                new_state=state.STATE_VALIDATING,
                mutate=lambda s: (
                    setattr(s, "green_revision", green_revision),
                    setattr(s, "blue_revision", blue_revision),
                    # Anchor the green-health timeout to the moment green was
                    # created, NOT `started_at` (which already absorbed the
                    # clone+build minutes) — otherwise a healthy-but-booting
                    # green would false-abort immediately.
                    setattr(s, "validating_started_at", entered),
                    setattr(
                        s,
                        "phase_detail",
                        "green revision created; validating health",
                    ),
                    setattr(s, "phase_progress", 92),
                )[-1],
            )
        except (state.StateTransitionRefused, state.RowEtagMismatch):
            return state.get_state()
    return state.get_state()


# ---------------------------------------------------------------------------
# Failure transitions (called from this module AND from the reconciler).
# ---------------------------------------------------------------------------


def _fail_pre(
    job_id: str,
    detail: str,
    *,
    orphan_image_refs: list[str] | None = None,
) -> state.UpgradeState:
    """Move the row to `failed_pre` from any pre-PATCH stage via CAS.

    When ``orphan_image_refs`` is supplied, attempt best-effort ACR
    `delete_tag` (typically succeeds when MI has `acrDelete`) and
    record the outcome in audit so the daily purge task can retry
    later if the MI gained the role meanwhile.
    """
    LOGGER.warning("upgrade.execute job=%s failed_pre: %s", job_id, detail)
    history.record_event("failed", job_id=job_id, stage="pre", detail=detail)
    if orphan_image_refs:
        delete_results: dict[str, str] = {}
        for ref in orphan_image_refs:
            deleted, reason = acr_inventory.delete_tag_best_effort(ref)
            delete_results[ref] = "deleted" if deleted else f"orphaned ({reason})"
        deleted_count = sum(1 for v in delete_results.values() if v == "deleted")
        LOGGER.warning(
            "upgrade.execute job=%s orphan ACR tag cleanup: %d/%d deleted",
            job_id,
            deleted_count,
            len(orphan_image_refs),
        )
        history.record_event(
            "orphan_acr_tags",
            job_id=job_id,
            image_refs=list(orphan_image_refs),
            cleanup_results=delete_results,
            note=(
                "images were pushed to ACR before the upgrade failed; "
                "best-effort delete attempted via the MI's `acrDelete` role."
            ),
        )
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
