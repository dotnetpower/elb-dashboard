"""Keep-N revision + orphan ACR tag garbage collection for blue/green.

Module summary: After a blue/green upgrade is confirmed succeeded, the
previously-serving blue revision is kept warm at 0% traffic only long
enough to guarantee instant rollback during the confirm window. Once the
window passes (state row left ``confirming``), this task deactivates the
stale revisions beyond the keep-N most-recent and deletes ACR tags that
no retained revision references — so a successful upgrade leaves no
garbage running container and no unbounded tag accumulation.

Responsibility: Idempotent, best-effort cleanup of stale Container App
  revisions and their orphaned ACR tags. No state-machine writes.
Edit boundaries: GC policy (keep-N, which revisions are protected, which
  tags are safe to delete) lives here. Revision/traffic ARM I/O is
  delegated to `api.services.upgrade.revisions`; tag deletion to
  `api.services.upgrade.acr_inventory`.
Key entry points: `collect_garbage_inline`, `collect_garbage`,
  `GcResult`, `KEEP_N_REVISIONS`.
Risky contracts: NEVER deactivates the serving revision or the row's
  recorded blue/green revisions, and NEVER deletes a tag still
  referenced by a retained revision — both guards protect the
  rollback path. Every Azure call is best-effort: a single failure is
  logged and the sweep continues so one stuck revision cannot block the
  rest of the cleanup. Safe to call against an idle row (no-op).
Validation: `uv run pytest -q api/tests/test_upgrade_revision_gc.py`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from celery import shared_task

from api.services.upgrade import acr_inventory, history, revisions, state

LOGGER = logging.getLogger(__name__)

# Number of most-recently-created ACTIVE revisions to retain in addition
# to the always-protected set (serving + recorded blue/green). 2 keeps
# the current serving revision plus one prior so a manual operator
# rollback target survives even after the automatic blue is GC'd.
KEEP_N_REVISIONS = 2


def keep_n_revisions() -> int:
    """Resolve keep-N from UPGRADE_REVISION_KEEP_N (default KEEP_N_REVISIONS).

    Read at call time so a revision can be reconfigured without redeploy.
    Clamped to >= 1 so the serving revision is never the only thing kept
    by accident (the always-protected set still applies on top).
    """
    raw = os.environ.get("UPGRADE_REVISION_KEEP_N")
    if raw is None:
        return KEEP_N_REVISIONS
    try:
        return max(1, int(raw))
    except ValueError:
        return KEEP_N_REVISIONS


@dataclass
class GcResult:
    """Outcome of one GC sweep (for audit + tests)."""

    deactivated: list[str] = field(default_factory=list)
    deactivate_failed: list[str] = field(default_factory=list)
    deleted_tags: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "deactivated": list(self.deactivated),
            "deactivate_failed": list(self.deactivate_failed),
            "deleted_tags": list(self.deleted_tags),
            "retained": list(self.retained),
            "skipped_reason": self.skipped_reason,
        }


def _protected_revisions(row: state.UpgradeState, serving: str) -> set[str]:
    """Revisions that must never be deactivated regardless of keep-N.

    The serving revision (taking live traffic) and the row's recorded
    blue/green names (the rollback handles) are always protected.
    """
    protected = {serving, row.green_revision, row.blue_revision, row.running_revision}
    return {name for name in protected if name}


def collect_garbage_inline(
    *,
    keep_n: int | None = None,
    revisions_mod: object | None = None,
    acr_mod: object | None = None,
) -> GcResult:
    """Deactivate stale revisions and delete orphaned ACR tags (idempotent).

    Retain policy: the serving revision + the row's recorded blue/green
    revisions are always kept; on top of that the ``keep_n`` most-recently
    created ACTIVE revisions survive. Every other ACTIVE revision is
    deactivated (0 replicas → no compute). An ACR tag is deleted only when
    NO retained revision still references it.

    ``revisions_mod`` / ``acr_mod`` are injected so tests drive the sweep
    without ARM or a real registry.
    """
    rev_mod = revisions_mod or revisions
    registry = acr_mod or acr_inventory
    result = GcResult()
    effective_keep_n = keep_n_revisions() if keep_n is None else keep_n

    try:
        all_revs = rev_mod.list_revisions()
    except Exception as exc:
        LOGGER.warning("upgrade.gc: list_revisions failed; skipping sweep: %s", exc)
        result.skipped_reason = f"list_revisions failed: {type(exc).__name__}"
        return result

    try:
        serving = rev_mod.serving_revision()
    except Exception as exc:
        LOGGER.warning("upgrade.gc: serving_revision failed; skipping sweep: %s", exc)
        result.skipped_reason = f"serving_revision failed: {type(exc).__name__}"
        return result

    row = state.get_state()
    protected = _protected_revisions(row, serving)

    active = [r for r in all_revs if r.active]
    # Newest first; revisions without a created_on sort last (treated as
    # oldest so they are GC candidates rather than accidentally retained).
    active.sort(key=lambda r: (r.created_on is not None, r.created_on), reverse=True)

    # Retain the ``keep_n`` most-recently-created active revisions, then
    # force-add the always-protected set (serving + recorded blue/green)
    # even when they are older than the keep-N window.
    retained: set[str] = {rev.name for rev in active[:effective_keep_n]}
    retained |= protected

    to_deactivate = [r for r in active if r.name not in retained]
    for rev in to_deactivate:
        ok = rev_mod.deactivate_revision(revision_name=rev.name)
        if ok:
            result.deactivated.append(rev.name)
        else:
            result.deactivate_failed.append(rev.name)

    result.retained = sorted(retained)
    _gc_orphan_tags(rev_mod, registry, retained, result)

    if result.deactivated or result.deleted_tags or result.deactivate_failed:
        history.record_event(
            "revision_gc",
            job_id=row.job_id,
            deactivated=result.deactivated,
            deactivate_failed=result.deactivate_failed,
            deleted_tags=result.deleted_tags,
            retained=result.retained,
        )
    return result


def _is_platform_acr_ref(image_ref: str) -> bool:
    """True iff ``image_ref`` lives in the platform ACR.

    Guards the tag GC so a base image pulled from Docker Hub
    (``redis:7-alpine``) or any non-platform registry is never a deletion
    candidate — we only ever delete elb-* tags we built.
    """
    from api.services.upgrade import aca_template

    try:
        acr_name = aca_template._env(aca_template.PLATFORM_ACR_NAME_ENV)
    except Exception:
        return False
    if not acr_name:
        return False
    try:
        endpoint, _repo, _tag = acr_inventory.parse_image_ref(image_ref)
    except ValueError:
        return False
    host = endpoint.split("://", 1)[-1].lower()
    return host == f"{acr_name}.azurecr.io".lower()


def _gc_orphan_tags(
    rev_mod: object,
    registry: object,
    retained: set[str],
    result: GcResult,
) -> None:
    """Delete ACR tags referenced by no retained revision (best-effort)."""
    try:
        images_by_rev: dict[str, set[str]] = rev_mod.revision_image_refs()  # type: ignore[attr-defined]
    except Exception as exc:
        LOGGER.warning("upgrade.gc: revision_image_refs failed; skipping tag GC: %s", exc)
        return

    retained_refs: set[str] = set()
    for name in retained:
        retained_refs |= images_by_rev.get(name, set())

    orphan_refs: set[str] = set()
    for name, refs in images_by_rev.items():
        if name in retained:
            continue
        for ref in refs:
            if ref not in retained_refs and _is_platform_acr_ref(ref):
                orphan_refs.add(ref)

    for ref in sorted(orphan_refs):
        try:
            deleted, reason = registry.delete_tag_best_effort(ref)  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.warning("upgrade.gc: delete_tag %s raised (best-effort): %s", ref, exc)
            continue
        if deleted:
            result.deleted_tags.append(ref)
        else:
            LOGGER.info("upgrade.gc: tag %s not deleted: %s", ref, reason)


@shared_task(name="api.tasks.upgrade.collect_garbage")
def collect_garbage() -> dict:
    """Celery wrapper around :func:`collect_garbage_inline`."""
    return collect_garbage_inline().as_dict()
