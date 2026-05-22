"""HTTP routes for the in-app self-upgrade flow.

Module summary: Read-only discovery endpoints (PR1) plus the PR2 build
pipeline trigger and log streamer. The ARM PATCH that swaps Container App
images is intentionally deferred to PR3; PR2 stops once `az acr build`
has produced the new images and the state row is `succeeded`.

Responsibility: HTTP validation, auth wiring, and response shaping for the
  upgrade endpoints. No Azure SDK, no Storage I/O — those live in
  `api.services.upgrade.*`.
Edit boundaries: Add new endpoints here; their business logic goes into
  services/tasks. Keep MSAL bearer enforcement on every route and the
  upgrade-admin gate on every mutating route.
Key entry points: `router`, `upgrade_status`, `upgrade_candidates`,
  `upgrade_check`, `upgrade_start`, `upgrade_build_log`,
  `reset_check_throttle_for_tests`.
Risky contracts: `/check` is throttled to avoid amplifying an upstream-git
  DOS. `/start` requires the UpgradeAdmin gate plus an explicit
  `confirm_downtime` body flag; without it the request 422s so an
  accidental click cannot kick off a downtime window. Remote URLs are
  masked before SPA serialisation.
Validation: `uv run pytest -q api/tests/test_upgrade_routes.py`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services.upgrade import build_logs, escape_hatch, remote_tags, state
from api.services.upgrade.aca_template import SidecarImages
from api.services.upgrade.auth import require_upgrade_admin
from api.tasks.upgrade import (
    RollbackStartRefused,
    UpgradeStartRefused,
    check_latest_inline,
    start_rollback_inline,
    start_upgrade_inline,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upgrade", tags=["upgrade"])

_CHECK_MIN_INTERVAL_SECONDS = 15.0
_check_lock = threading.Lock()
_last_check_at: float = 0.0

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
_COMPONENT_ALLOWED = frozenset({"api", "frontend", "terminal"})


class UpgradeStartRequest(BaseModel):
    target_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    target_sha: str = Field("", pattern=r"^(|[0-9a-fA-F]{7,40})$")
    confirm_downtime: bool = Field(
        False,
        description=(
            "The operator has acknowledged that the upgrade involves a "
            "container restart and a short downtime window."
        ),
    )


def _mask_state(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("git_remote"):
        payload["git_remote"] = remote_tags.mask_remote_url(str(payload["git_remote"]))
    return payload


@router.get("/status")
def upgrade_status(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the persisted upgrade-state row (cheap read for SPA polling)."""
    return _mask_state(state.get_state().to_public_dict())


@router.get("/candidates")
def upgrade_candidates(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return release tags newer than the currently running version."""
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
    """Force a single discovery round and return the refreshed state row."""
    global _last_check_at
    now = time.monotonic()
    with _check_lock:
        if now - _last_check_at < _CHECK_MIN_INTERVAL_SECONDS:
            retry_after = max(
                1, int(_CHECK_MIN_INTERVAL_SECONDS - (now - _last_check_at)) + 1
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"upgrade check throttled; retry in {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
        _last_check_at = now
    updated = check_latest_inline()
    return _mask_state(updated.to_public_dict())


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
def upgrade_start(
    body: UpgradeStartRequest,
    caller: CallerIdentity = Depends(require_upgrade_admin),
) -> dict[str, Any]:
    """Queue an upgrade execution. PR2 runs git clone + `az acr build`.

    The ARM PATCH that swaps the Container App template is deferred to
    PR3, so a successful PR2 start ends in `state=succeeded` after the
    three images are built and pushed to ACR — no traffic impact yet.
    Operators verify the build evidence via
    `GET /api/upgrade/jobs/{job_id}/build-log/{component}` before
    cutting over to the new images in PR3.
    """
    if not body.confirm_downtime:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confirm_downtime must be true to start an upgrade",
        )
    try:
        updated = start_upgrade_inline(
            target_version=body.target_version,
            target_sha=body.target_sha,
            started_by_oid=caller.object_id,
        )
    except UpgradeStartRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return _mask_state(updated.to_public_dict())


@router.get(
    "/jobs/{job_id}/build-log/{component}",
    response_class=Response,
)
def upgrade_build_log(
    job_id: str,
    component: str,
    _caller: CallerIdentity = Depends(require_upgrade_admin),
) -> Response:
    """Stream the per-component build log blob for a given job."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="invalid job_id")
    if component not in _COMPONENT_ALLOWED:
        raise HTTPException(status_code=400, detail="invalid component")
    try:
        payload = build_logs.read_blob(job_id, component)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="build log not found") from exc
    return Response(content=payload, media_type="text/plain; charset=utf-8")


@router.post("/rollback", status_code=status.HTTP_202_ACCEPTED)
def upgrade_rollback(
    caller: CallerIdentity = Depends(require_upgrade_admin),
) -> dict[str, Any]:
    """Roll the Container App back to the snapshot taken before the upgrade.

    Requires the row to be in `rolling_out`, `succeeded`, or
    `failed_rollout`. The rollback target images must still exist in
    ACR — if retention expired the call fails. Returns the updated
    state row.
    """
    try:
        updated = start_rollback_inline(started_by_oid=caller.object_id)
    except RollbackStartRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return _mask_state(updated.to_public_dict())


@router.get("/escape-hatch")
def upgrade_escape_hatch(
    _caller: CallerIdentity = Depends(require_upgrade_admin),
) -> dict[str, Any]:
    """Return copy-pasteable recovery commands using the recorded snapshot.

    Used when the new revision fails to come up and the in-app rollback
    path is unreachable. The operator runs the commands from an outside
    `az login` shell.
    """
    row = state.get_state()
    target_dict = row.rollback_target()
    if not target_dict:
        raise HTTPException(
            status_code=404,
            detail="no rollback snapshot recorded; nothing to escape to",
        )
    try:
        images = SidecarImages(
            api=target_dict["api"],
            frontend=target_dict["frontend"],
            terminal=target_dict["terminal"],
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=500, detail=f"snapshot missing key: {exc}"
        ) from exc
    plan = escape_hatch.build_plan(images)
    return {
        "container_app": plan.container_app,
        "subscription_id": plan.subscription_id,
        "resource_group": plan.resource_group,
        "target_images": plan.target_images,
        "commands": plan.commands,
    }


def reset_check_throttle_for_tests() -> None:
    """Reset the /check throttle so tests are isolated from each other."""
    global _last_check_at
    with _check_lock:
        _last_check_at = 0.0
