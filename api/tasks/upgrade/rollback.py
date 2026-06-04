"""Rollback path for the in-app self-upgrade flow.

Module summary: PATCHes the Container App template back to the
snapshot taken before the upgrade. Allowed from any post-PATCH state
including `rollback_failed` so a transient ACA outage can be retried
without dropping to the escape-hatch shell. When `STRICT_BLUEGREEN`
is on and the blue revision is still warm, a fast traffic-flip path
reverts in seconds without re-pulling from ACR.

Responsibility: Reverse-direction PATCH + audit + state CAS for rollback.
Edit boundaries: All rollback decision logic lives here. The pipeline's
  `_fail_pre`/`_fail_rollout` are NOT reused — rollback failures go
  to `ROLLBACK_FAILED` via this module's own `_fail_rollback`.
Key entry points: `start_rollback_inline`, `RollbackStartRefused`,
  `_fail_rollback`.
Risky contracts: ACR pre-flight refuses when any snapshotted tag was
  retention-purged — without this, the rollback PATCH "succeeds" but
  ACA cannot pull and the new revision crashloops. The blue/green fast
  path only runs when the blue revision is still active (verified via
  `list_revisions`); otherwise it falls back to the re-PATCH path.
Validation: `uv run pytest -q api/tests/test_upgrade_task.py`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from api.services.upgrade import aca_template, acr_inventory, history, revisions, state

LOGGER = logging.getLogger(__name__)


class RollbackStartRefused(RuntimeError):
    """Raised when the rollback CAS cannot proceed."""


def start_rollback_inline(
    *,
    started_by_oid: str,
    reason: str = "",
    aca: object | None = None,
    watcher: object | None = None,
    acr: object | None = None,
    revisions_mod: object | None = None,
) -> state.UpgradeState:
    """PATCH the Container App back to the pre-upgrade snapshot.

    Allowed from `rolling_out`, `confirming`, `succeeded`,
    `failed_rollout`, and `rollback_failed` (retry after a transient
    ACA outage). Refuses when there is no rollback target or when ACR
    no longer carries the snapshotted tags.

    `confirming` is the blue/green confirm window where green serves
    100% and blue is still warm at 0% — the highest-value moment for a
    manual revert, handled by the fast traffic-flip path below.

    When `STRICT_BLUEGREEN` is enabled and the blue revision is still
    warm, traffic is flipped back to blue in seconds (no ACR pull, no
    new revision boot). If blue was already torn down the call falls
    back to the snapshot re-PATCH path below.
    """
    row = state.get_state()
    if row.state not in {
        state.STATE_ROLLING_OUT,
        state.STATE_CONFIRMING,
        state.STATE_SUCCEEDED,
        state.STATE_FAILED_ROLLOUT,
        state.STATE_ROLLBACK_FAILED,
    }:
        raise RollbackStartRefused(
            f"rollback only valid after PATCH was issued (state={row.state})"
        )

    revisions_module = revisions_mod or revisions
    if revisions_module.strict_bluegreen() and row.blue_revision:
        fast = _bluegreen_fast_rollback(row, started_by_oid, reason, revisions_module)
        if fast is not None:
            return fast

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

    # ACR pre-flight — refuse if any snapshotted tag was retention-purged.
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
        reason=reason or "",
    )

    try:
        aca_mod.apply_images(images=target_images, revision_suffix=suffix)
    except aca_template.TemplateError as exc:
        return _fail_rollback(str(exc))

    # Intermediate progress: PATCH accepted but new revision still booting.
    try:
        state.update_state(
            lambda s: (
                setattr(s, "phase_detail", "rollback PATCH accepted; revision booting"),
                setattr(s, "phase_progress", 75),
            )[-1]
        )
    except state.RowEtagMismatch:
        pass

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


def _bluegreen_fast_rollback(
    row: state.UpgradeState,
    started_by_oid: str,
    reason: str,
    revisions_mod: object,
) -> state.UpgradeState | None:
    """Flip traffic back to the still-warm blue revision (seconds).

    Returns the post-flip state on success/failure, or ``None`` when the
    fast path is not applicable (blue no longer active, or revision I/O
    failed) so the caller falls back to the snapshot re-PATCH path. No
    state mutation happens before the applicability decision.
    """
    blue = row.blue_revision
    green = row.green_revision
    try:
        active = {r.name for r in revisions_mod.list_revisions() if r.active}
    except revisions.RevisionsError as exc:
        LOGGER.warning("upgrade.rollback: cannot list revisions for fast path: %s", exc)
        return None
    if blue not in active:
        # Blue was torn down (e.g. post-success GC) → must re-pull from ACR.
        return None

    if not green:
        try:
            green = revisions_mod.serving_revision()
        except revisions.RevisionsError:
            green = ""

    try:
        state.cas_state(
            expected_state=row.state,
            new_state=state.STATE_ROLLING_BACK,
            mutate=lambda s: (
                setattr(s, "phase_detail", f"blue/green flip to {blue}"),
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
        target={"blue_revision": blue, "mode": "bluegreen-flip"},
        reason=reason or "",
    )

    try:
        revisions_mod.flip_traffic(to_revision=blue, from_revision=green)
    except revisions.RevisionsError as exc:
        return _fail_rollback(f"blue/green flip failed: {exc}")

    try:
        after = state.cas_state(
            expected_state=state.STATE_ROLLING_BACK,
            new_state=state.STATE_ROLLED_BACK,
            mutate=lambda s: (
                setattr(s, "phase_detail", f"traffic reverted to blue {blue}"),
                setattr(s, "phase_progress", 100),
                setattr(s, "traffic_serving", blue),
            )[-1],
        )
        history.record_event(
            "rollback_done", job_id=after.job_id, target={"blue_revision": blue}
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
