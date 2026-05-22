"""Rollout watcher — confirms a new revision became Healthy.

Module summary: Polls the Container App resource until either (a) the
new revision's `runningState` is `Running` and `provisioningState` is
`Provisioned`, or (b) a timeout elapses. Designed to be called from the
new revision itself (the post-PATCH reconciler), but also usable from
the producing revision while it drains.

Responsibility: Polling + status interpretation. No PATCH calls here.
Edit boundaries: Update when ACA revision states change semantics.
Key entry points: `wait_for_revision`, `RevisionUnhealthy`,
  `RevisionTimeout`, `revision_status`.
Risky contracts: The polling loop sleeps between iterations; tests
  inject a synchronous sleeper.
Validation: `uv run pytest -q api/tests/test_upgrade_rollout_watcher.py`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from api.services.upgrade.aca_template import (
    AZURE_RESOURCE_GROUP_ENV,
    CONTAINER_APP_NAME_ENV,
    TemplateError,
    _client,
    _env,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 600  # 10 min
DEFAULT_POLL_INTERVAL_SECONDS = 10


class RevisionUnhealthy(RuntimeError):
    """Raised when the watched revision reports an explicit failure state."""


class RevisionTimeout(RuntimeError):
    """Raised when `wait_for_revision` exceeds its budget."""


@dataclass(frozen=True)
class RevisionStatus:
    name: str
    running_state: str
    provisioning_state: str
    health_state: str


def revision_status(revision_name: str, *, client: Any | None = None) -> RevisionStatus:
    """Return the live status of ``revision_name`` from ARM."""
    rg = _env(AZURE_RESOURCE_GROUP_ENV)
    app = _env(CONTAINER_APP_NAME_ENV)
    cli = client or _client()
    try:
        rev = cli.container_apps_revisions.get_revision(rg, app, revision_name)
    except Exception as exc:
        raise TemplateError(f"failed to read revision {revision_name!r}: {exc}") from exc
    properties = getattr(rev, "properties", rev)
    return RevisionStatus(
        name=str(getattr(rev, "name", revision_name)),
        running_state=str(getattr(properties, "running_state", "") or ""),
        provisioning_state=str(getattr(properties, "provisioning_state", "") or ""),
        health_state=str(getattr(properties, "health_state", "") or ""),
    )


def wait_for_revision(
    revision_name: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    client: Any | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RevisionStatus:
    """Block until ``revision_name`` is Healthy, Unhealthy, or the timeout."""
    deadline = now() + timeout_seconds
    last: RevisionStatus | None = None
    while now() < deadline:
        last = revision_status(revision_name, client=client)
        if _is_healthy(last):
            return last
        if _is_terminal_failure(last):
            raise RevisionUnhealthy(
                f"revision {revision_name} reached terminal failure: {last}"
            )
        sleep(poll_interval_seconds)
    raise RevisionTimeout(
        f"revision {revision_name} did not become healthy within {timeout_seconds}s "
        f"(last status={last})"
    )


def _is_healthy(status: RevisionStatus) -> bool:
    running = status.running_state.lower()
    provisioning = status.provisioning_state.lower()
    return running == "running" and provisioning == "provisioned"


def _is_terminal_failure(status: RevisionStatus) -> bool:
    provisioning = status.provisioning_state.lower()
    if provisioning in {"failed", "canceled"}:
        return True
    running = status.running_state.lower()
    return running in {"failed", "degraded"}
