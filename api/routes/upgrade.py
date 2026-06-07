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
  `upgrade_check`, `upgrade_settings`, `upgrade_start`, `upgrade_build_log`,
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
from api.services.sanitise import sanitise
from api.services.upgrade import (
    acr_inventory,
    build_logs,
    escape_hatch,
    history,
    remote_tags,
    state,
)
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
# Process-local throttle — the api Dockerfile launches 2 uvicorn workers,
# so worst-case the upstream git remote can see one /check per worker per
# `_CHECK_MIN_INTERVAL_SECONDS`. That is still gentle (≤ 8 req/min) and
# avoids needing a Redis-backed coordinator. Distributed throttling is
# only worth wiring if the worker count ever grows or if the beat job
# becomes more frequent than the current 30 minutes.
_check_lock = threading.Lock()
_last_check_at: float = 0.0

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
_COMPONENT_ALLOWED = frozenset({"api", "frontend", "terminal"})


class UpgradeStartRequest(BaseModel):
    target_version: str = Field(
        "",
        pattern=r"^(|\d+\.\d+\.\d+)$",
        description=(
            "Release target semver (e.g. 0.4.0). Required for "
            "target_kind='release'; ignored for 'commit' (the server derives "
            "the commit version string from the running release + target_sha)."
        ),
    )
    target_sha: str = Field("", pattern=r"^(|[0-9a-fA-F]{7,40})$")
    target_kind: str = Field(
        "release",
        pattern=r"^(release|commit)$",
        description=(
            "Which update channel to install: 'release' (a vX.Y.Z tag, the "
            "default) or 'commit' (the latest tracking-branch commit). A "
            "commit upgrade is only honoured when the persisted track_commits "
            "toggle is on, and requires a full 40-hex target_sha."
        ),
    )
    confirm_downtime: bool = Field(
        False,
        description=(
            "The operator has acknowledged that the upgrade involves a "
            "container restart and a short downtime window."
        ),
    )
    reason: str = Field(
        "",
        max_length=280,
        description=(
            "Optional operator-supplied justification (CVE id, ticket #, "
            "feature flag, etc.) recorded verbatim in the start audit event."
        ),
    )
    idempotency_key: str = Field(
        "",
        max_length=64,
        pattern=r"^[A-Za-z0-9._\-]{0,64}$",
        description=(
            "Optional client-supplied retry key. If two POSTs share the "
            "same key + target_version, the second returns the existing "
            "in-flight row instead of 409 — protects double-click / "
            "network-retry scenarios."
        ),
    )


class UpgradeRollbackRequest(BaseModel):
    reason: str = Field(
        "",
        max_length=280,
        description="Optional rollback justification recorded in audit.",
    )


class UpgradeSettingsRequest(BaseModel):
    track_commits: bool = Field(
        ...,
        description=(
            "When true (default for new deployments), the discovery flow also "
            "surfaces new commits on the tracking branch (preview channel). "
            "When false, only release tags are checked."
        ),
    )


def _mask_state(payload: dict[str, Any]) -> dict[str, Any]:
    # Surface the *effective* remote so the SPA never shows "not configured"
    # when a default remote is active but no discovery check has populated
    # the persisted `git_remote` yet (cold-start chicken-and-egg). The
    # persisted row is unchanged; only the response is enriched.
    if not payload.get("git_remote"):
        effective = remote_tags.configured_remote()
        if effective:
            payload["git_remote"] = effective
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


@router.post("/settings")
def upgrade_settings(
    body: UpgradeSettingsRequest,
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Persist the update-channel toggle and return the refreshed state row.

    This only changes which refs the read-only discovery flow surfaces; it
    cannot trigger a deployment (that still requires `/start`, the
    UpgradeAdmin gate, and an explicit `confirm_downtime`). It is therefore
    gated by `require_caller` rather than `require_upgrade_admin` so any
    authenticated operator can pick their update channel.
    """
    track = bool(body.track_commits)
    updated = state.update_state(lambda s: setattr(s, "track_commits", track))
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
    kind = body.target_kind or "release"
    target_version = body.target_version
    target_sha = body.target_sha
    if kind == "commit":
        # Defense in depth: a commit upgrade is only allowed when the operator
        # has opted into the commit channel. The SPA hides the option when the
        # toggle is off, but a direct API call must be rejected too.
        if not state.get_state().track_commits:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="commit channel is off; enable 'Allow updates from new commits' first",
            )
        full_sha = (body.target_sha or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", full_sha):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="commit upgrade requires a full 40-hex target_sha",
            )
        # Derive the commit version string server-side from the running
        # release base so the operator never fabricates a version. The base is
        # the running api version reduced to bare semver (handles re-upgrading
        # from an existing commit build).
        from api import __version__ as running_version
        from api.services.upgrade.version_target import (
            base_release,
            make_commit_version,
        )

        base = base_release(running_version)
        if not re.fullmatch(r"\d+\.\d+\.\d+", base):
            base = "0.0.0"
        try:
            target_version = make_commit_version(base, full_sha)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid commit target: {sanitise(str(exc))[:160]}",
            ) from exc
        target_sha = full_sha
    elif not re.fullmatch(r"\d+\.\d+\.\d+", target_version or ""):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="release upgrade requires a semver target_version (e.g. 0.4.0)",
        )
    try:
        updated = start_upgrade_inline(
            target_version=target_version,
            target_sha=target_sha,
            target_kind=kind,
            started_by_oid=caller.object_id,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
    except UpgradeStartRefused as exc:
        # Audit P1 #7: sanitise + cap exception text.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=sanitise(str(exc))[:200]
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
        # The per-component blob is created only when that component's
        # `az acr build` actually starts. While an upgrade is mid-flight the
        # SPA polls all three components every 3 s, so the not-yet-built ones
        # (e.g. frontend/terminal during the api build) would otherwise return
        # 404 on every poll — observed as ~2k "failed request" rows / day in
        # App Insights plus a browser console error per poll. For the *current*
        # job that 404 is a benign "no log yet" state, so return an empty 200.
        # Reserve 404 for a genuinely unknown job_id (e.g. a pruned old job).
        try:
            current_job = state.get_state().job_id
        except Exception:  # pragma: no cover - state read best-effort
            current_job = ""
        if current_job and job_id == current_job:
            return Response(content=b"", media_type="text/plain; charset=utf-8")
        raise HTTPException(status_code=404, detail="build log not found") from exc
    return Response(content=payload, media_type="text/plain; charset=utf-8")


@router.get("/rollback-preflight")
def upgrade_rollback_preflight(
    _caller: CallerIdentity = Depends(require_upgrade_admin),
) -> dict[str, Any]:
    """Inspect whether the rollback snapshot is still resolvable in ACR.

    Returns per-image existence + creation timestamp so the SPA can warn
    proactively when an upcoming rollback would fail because retention
    purged a tag. Cheap read — used by the rollback card on render.
    """
    row = state.get_state()
    target = row.rollback_target()
    if not target:
        return {"available": False, "reason": "no snapshot recorded", "images": []}
    refs = [target.get("api", ""), target.get("frontend", ""), target.get("terminal", "")]
    refs = [r for r in refs if r]
    if not refs:
        return {"available": False, "reason": "snapshot empty", "images": []}
    try:
        probes = acr_inventory.lookup_images(refs)
    except Exception as exc:
        LOGGER.warning("upgrade.rollback-preflight: %s", exc)
        return {
            "available": False,
            "reason": f"acr probe failed: {exc}",
            "images": [{"image_ref": r, "exists": False, "error": str(exc)} for r in refs],
        }
    missing = [p for p in probes if not p.exists]
    return {
        "available": not missing,
        "reason": "ok" if not missing else "one or more tags missing",
        "images": [
            {
                "image_ref": p.image_ref,
                "exists": p.exists,
                "created_on": p.created_on.isoformat() if p.created_on else None,
                "error": p.error,
            }
            for p in probes
        ],
    }


@router.post("/rollback", status_code=status.HTTP_202_ACCEPTED)
def upgrade_rollback(
    body: UpgradeRollbackRequest | None = None,
    caller: CallerIdentity = Depends(require_upgrade_admin),
) -> dict[str, Any]:
    """Roll the Container App back to the snapshot taken before the upgrade.

    Requires the row to be in `rolling_out`, `succeeded`, or
    `failed_rollout`. The rollback target images must still exist in
    ACR — if retention expired the call fails. Returns the updated
    state row.
    """
    reason = body.reason if body else ""
    try:
        updated = start_rollback_inline(
            started_by_oid=caller.object_id, reason=reason
        )
    except RollbackStartRefused as exc:
        # Audit P1 #7: sanitise + cap exception text.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=sanitise(str(exc))[:200]
        ) from exc
    return _mask_state(updated.to_public_dict())


@router.get("/history")
def upgrade_history(
    limit: int = 50,
    _caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return the tail of the upgrade-history append blob."""
    events = history.tail_events(limit=limit)
    return {
        "events": [
            {"ts": e.ts, "job_id": e.job_id, "event": e.event, **e.detail}
            for e in events
        ]
    }


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
