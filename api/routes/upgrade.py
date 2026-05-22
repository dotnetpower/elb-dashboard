"""HTTP routes for the in-app self-upgrade flow (read-only surface).

Module summary: Read-only endpoints that let the SPA show "upgrade available"
without performing any build or rollout. Mutating routes (`start`,
`rollback`, build-log streaming, escape-hatch) are added in later PRs.

Responsibility: HTTP validation, auth wiring, and response shaping for the
  upgrade endpoints. No Azure SDK, no Storage I/O — those live in
  `api.services.upgrade.*`.
Edit boundaries: Add new endpoints here; their business logic goes into
  services/tasks. Keep MSAL bearer enforcement on every route.
Key entry points: `router`, `upgrade_status`, `upgrade_candidates`,
  `upgrade_check`.
Risky contracts: Every endpoint goes through `require_caller`. The
  `/check` endpoint is throttled by `_CHECK_MIN_INTERVAL_SECONDS` so a
  malicious or buggy SPA cannot turn it into a remote-traffic amplifier.
  Remote URLs are masked before being returned so credentials added in a
  future PR (PAT-prefixed remotes) never leak to the SPA.
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from api.auth import CallerIdentity, require_caller
from api.services.upgrade import remote_tags, state
from api.tasks.upgrade import check_latest_inline

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upgrade", tags=["upgrade"])

# Throttle the synchronous remote-traffic-amplifying endpoint. Any caller can
# trigger /check, so without a cooldown a misbehaving SPA could DOS the
# upstream git remote. 15 s is plenty given the beat job runs every 30 min.
_CHECK_MIN_INTERVAL_SECONDS = 15.0
_check_lock = threading.Lock()
_last_check_at: float = 0.0


def _mask_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask any credentials embedded in remote URLs before SPA serialisation."""
    if payload.get("git_remote"):
        payload["git_remote"] = remote_tags.mask_remote_url(str(payload["git_remote"]))
    return payload


@router.get("/status")
def upgrade_status(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the persisted upgrade-state row.

    Cheap read used by the SPA header indicator (polled ~30 s).
    """
    return _mask_state(state.get_state().to_public_dict())


@router.get("/candidates")
def upgrade_candidates(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return release tags newer than the currently running version.

    Reads the operator-configured `UPGRADE_GIT_REMOTE` env. When unset
    the endpoint returns an empty candidates list with `configured=false`
    so the SPA can render "set UPGRADE_GIT_REMOTE to enable" guidance.
    """
    persisted = state.get_state()
    remote = remote_tags.configured_remote()
    if not remote:
        return {
            "configured": False,
            "remote": None,
            "running_version": persisted.running_version,
            "candidates": [],
        }
    masked = remote_tags.mask_remote_url(remote)
    try:
        tags = remote_tags.fetch_release_tags(remote)
    except remote_tags.RemoteTagsError as exc:
        LOGGER.warning("upgrade.candidates: %s", exc)
        return {
            "configured": True,
            "remote": masked,
            "running_version": persisted.running_version,
            "candidates": [],
            "error": str(exc),
        }
    running = persisted.running_version or ""
    if running:
        tags = remote_tags.filter_candidates(tags, running_version=running)
    return {
        "configured": True,
        "remote": masked,
        "running_version": running,
        "candidates": [t.as_dict() for t in tags],
    }


@router.post("/check")
def upgrade_check(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Force a single discovery round and return the refreshed state row.

    Throttled: callers must wait `_CHECK_MIN_INTERVAL_SECONDS` between
    requests so the SPA polling layer cannot amplify into an upstream-DOS.
    """
    global _last_check_at
    now = time.monotonic()
    with _check_lock:
        if now - _last_check_at < _CHECK_MIN_INTERVAL_SECONDS:
            retry_after = max(
                1,
                int(_CHECK_MIN_INTERVAL_SECONDS - (now - _last_check_at)) + 1,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"upgrade check throttled; retry in {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
        _last_check_at = now
    updated = check_latest_inline()
    return _mask_state(updated.to_public_dict())


def reset_check_throttle_for_tests() -> None:
    """Reset the /check throttle so tests are isolated from each other."""
    global _last_check_at
    with _check_lock:
        _last_check_at = 0.0
