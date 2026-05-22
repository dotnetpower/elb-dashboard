"""Celery tasks for the in-app self-upgrade flow.

Module summary: Hosts the beat-driven and worker-triggered tasks that drive
the upgrade lifecycle. PR1 ships only the read-only periodic check that
populates the `upgradestate` row's ``latest_*`` fields; the build/apply/
rollback tasks are added in later PRs.

Responsibility: Long-running side effects for upgrade discovery and execution.
Edit boundaries: Tasks here own the state-row transitions; routes call into
  these tasks via `.delay()` or directly via the helper functions exposed
  for the synchronous "check now" endpoint.
Key entry points: `check_latest`, `check_latest_inline`.
Risky contracts: `check_latest_inline` writes to the upgrade-state row even on
  failure (so the SPA can show ``latest_checked_at``), but never writes the
  `error` field for transient remote failures — those go to logs only so the
  field stays reserved for upgrade-execution failures (added in PR3).
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py
  api/tests/test_upgrade_state.py`.
"""

from __future__ import annotations

import logging

from celery import shared_task

from api import __version__
from api.services.upgrade import remote_tags, state

LOGGER = logging.getLogger(__name__)


def _record_running_version(s: state.UpgradeState) -> None:
    """Keep the row's running_version in sync with the api's __version__.

    `__version__` is the authoritative source for the api sidecar process
    that is currently serving HTTP. The row's `running_version` mirrors
    this so a freshly booted revision overwrites the previous value
    automatically — no extra plumbing required.
    """
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
    """Run one discovery round and persist the result. Returns the updated row.

    Called from the `/api/upgrade/check` route (synchronous) and from the
    `upgrade.check_latest` beat task (periodic). Either way the row is
    updated even when discovery fails so the SPA can render
    ``latest_checked_at`` honestly.
    """
    from datetime import UTC, datetime

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
    """Beat-scheduled wrapper around :func:`check_latest_inline`.

    Returns the public-shaped dict so the result backend carries a payload
    that is safe to inspect via `/api/health/celery`-style diagnostics.
    """
    updated = check_latest_inline()
    return updated.to_public_dict()
