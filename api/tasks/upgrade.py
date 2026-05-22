"""Celery tasks for the in-app self-upgrade flow.

Module summary: Hosts the beat-driven discovery task and the
worker-triggered upgrade pipeline that the SPA initiates via
`POST /api/upgrade/start`. PR1 shipped the read-only `check_latest`
helper; PR2 adds the `execute_upgrade` task that clones the requested
release tag and runs `az acr build` for each control-plane sidecar.
The ARM PATCH that swaps the Container App template to the new images
arrives in PR3 — PR2 stops at `STATE_SUCCEEDED` immediately after the
last successful build.

Responsibility: Long-running side effects for upgrade discovery and execution.
Edit boundaries: Tasks here own the state-row transitions; routes call
  into these tasks via `.delay()` or directly via the helper functions
  exposed for the synchronous "check now" / "start" endpoints.
Key entry points: `check_latest`, `check_latest_inline`, `execute_upgrade`,
  `start_upgrade_inline`, `STATE_TRANSITION_TIMELINE`.
Risky contracts: `start_upgrade_inline` performs the CAS that gates an
  upgrade — it is the single funnel through which concurrent operators
  are serialised. The task itself is idempotent on `(target_version,
  target_sha)`: re-invoking with the same arguments while a previous
  attempt is still in flight returns the existing job rather than
  starting a new one.
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py
  api/tests/test_upgrade_task.py api/tests/test_upgrade_state.py`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from celery import shared_task

from api import __version__
from api.services.upgrade import build_logs, git_workspace, image_builder, remote_tags, state

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
# PR2: upgrade execution pipeline (fetch + build; PATCH lands in PR3).
# ---------------------------------------------------------------------------


class UpgradeStartRefused(RuntimeError):
    """Raised by `start_upgrade_inline` when the request cannot be queued.

    Routes translate this into a 409 so the SPA can render "an upgrade is
    already in progress" rather than retrying.
    """


# Documented for tests + hardening — the routes follow this order so
# anything that diverges (auto-rollback, retry, etc.) is visible.
STATE_TRANSITION_TIMELINE = (
    state.STATE_IDLE,
    state.STATE_QUEUED,
    state.STATE_FETCHING,
    state.STATE_BUILDING,
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
    """CAS the row from idle -> queued and enqueue the Celery task.

    ``enqueue`` is injected so unit tests can run the gating logic
    without spinning up a Celery worker. Production callers omit it; the
    default value resolves to ``execute_upgrade.delay``.
    """
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

    submit = enqueue or _default_enqueue
    try:
        submit(target_version, target_sha or "", started_by_oid or "", job_id)
    except Exception as enqueue_exc:
        # Roll the row back to idle so a broken broker doesn't strand the
        # state machine in `queued` forever.
        LOGGER.exception("upgrade.start: enqueue failed")
        detail = f"enqueue_failed: {enqueue_exc}"[:240]
        try:
            state.cas_state(
                expected_state=state.STATE_QUEUED,
                new_state=state.STATE_IDLE,
                mutate=lambda s: setattr(s, "phase_detail", detail),
            )
        except state.StateTransitionRefused:
            # If the row already advanced past queued (worker raced us)
            # there's nothing to undo; the worker owns the transitions.
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
    """Worker-side upgrade pipeline. PR2 ends at STATE_SUCCEEDED post-build."""
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
) -> state.UpgradeState:
    """Run the upgrade pipeline end-to-end. Exposed for tests."""
    from api.services import terminal_exec as _exec

    runner = runner or _exec
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
        progress = 30 + int(60 * (idx / len(components)))
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

    # 3. PR2 stops here. PR3 will continue with patching/rolling_out.
    final_detail = (
        "build complete: "
        + ", ".join(f"{r.component}={r.image_ref.rsplit('/', 1)[-1]}" for r in built)
    )
    try:
        return state.cas_state(
            expected_state=state.STATE_BUILDING,
            new_state=state.STATE_SUCCEEDED,
            mutate=lambda s: (
                setattr(s, "phase_detail", final_detail),
                setattr(s, "phase_progress", 100),
            )[-1],
        )
    except state.StateTransitionRefused as exc:
        LOGGER.warning("upgrade.execute: finalise refused (%s)", exc.current)
        return state.get_state()


def _fail_pre(job_id: str, detail: str) -> state.UpgradeState:
    """Drive the row into a terminal `failed_pre` state from any pre-PATCH stage.

    `failed_pre` semantics: nothing customer-facing changed (no ARM PATCH
    was issued). The row is moved back into a non-`idle` state so the SPA
    can render the failure reason; an operator-initiated `/check` or the
    next `start_upgrade_inline` resets it.

    Uses CAS so we cannot overwrite a state another worker has already
    advanced past (e.g. `succeeded`). When the CAS refuses we just
    surface the current row — whatever state it landed in is the truth.
    """
    LOGGER.warning("upgrade.execute job=%s failed_pre: %s", job_id, detail)
    truncated = detail[:240]
    for expected in (
        state.STATE_QUEUED,
        state.STATE_FETCHING,
        state.STATE_BUILDING,
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
