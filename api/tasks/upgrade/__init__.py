"""Public facade for the in-app self-upgrade Celery tasks.

Module summary: This package hosts the upgrade flow split per SRP into
sibling modules — `pipeline` (start/execute/_fail_*), `reconciler`
(post-PATCH state machine + per-state budgets), `rollback` (reverse
PATCH), and `maintenance` (orphan ACR purge + history compaction).
See each submodule's header for its specific responsibility. This
`__init__` re-exports the public surface so existing callers
(`from api.tasks import upgrade as upgrade_task`, `upgrade_task.X`)
keep working unchanged.

Responsibility: Re-export symbols. No business logic.
Edit boundaries: Do NOT add new logic here. Add to the appropriate
  submodule and re-export.
Key entry points: All public names from the submodules + the Celery
  `@shared_task` task names used by `beat_schedule`.
Risky contracts: The Celery task NAMES (`api.tasks.upgrade.X`) must
  match the strings in `api/celery_app.py::beat_schedule` and any
  external `.delay(...)` callers. Renaming the underlying functions
  is fine; renaming the registered task name is a breaking change.
Validation: `uv run pytest -q api/tests/test_upgrade_*.py`.
"""

from __future__ import annotations

# Re-exports from the SRP-split submodules. Order matters only for
# readability; Python resolves the imports lazily at access time.
from api.tasks.upgrade.maintenance import (
    compact_history,
    compact_history_inline,
    purge_orphan_acr_tags,
    purge_orphan_acr_tags_inline,
)
from api.tasks.upgrade.pipeline import (
    LOGGER,
    STATE_TRANSITION_TIMELINE,
    UpgradeStartRefused,
    _clear_latest,
    _default_enqueue,
    _fail_pre,
    _fail_rollout,
    _record_running_version,
    _set_latest,
    _utc_now,
    check_latest,
    check_latest_inline,
    execute_upgrade,
    execute_upgrade_inline,
    start_upgrade_inline,
)
from api.tasks.upgrade.reconciler import (
    _RUNNING_STATE_TERMINAL_FAILURES,
    PATCH_NEVER_LANDED_GRACE_SECONDS,
    PRE_PATCH_BUDGET_SECONDS,
    PRE_PATCH_STATES,
    PRE_PATCH_TIMEOUT_SECONDS,
    ROLLING_OUT_TIMEOUT_SECONDS,
    _check_pre_patch_stuck,
    _image_matches_version,
    _new_revision_is_ready,
    reconcile_rolling_out,
    reconcile_rolling_out_inline,
)
from api.tasks.upgrade.rollback import (
    RollbackStartRefused,
    _fail_rollback,
    start_rollback_inline,
)

__all__ = [  # noqa: RUF022 — grouped by responsibility, not alphabetical
    # discovery
    "check_latest",
    "check_latest_inline",
    # pipeline
    "STATE_TRANSITION_TIMELINE",
    "UpgradeStartRefused",
    "execute_upgrade",
    "execute_upgrade_inline",
    "start_upgrade_inline",
    # reconciler
    "PATCH_NEVER_LANDED_GRACE_SECONDS",
    "PRE_PATCH_BUDGET_SECONDS",
    "PRE_PATCH_STATES",
    "PRE_PATCH_TIMEOUT_SECONDS",
    "ROLLING_OUT_TIMEOUT_SECONDS",
    "reconcile_rolling_out",
    "reconcile_rolling_out_inline",
    # rollback
    "RollbackStartRefused",
    "start_rollback_inline",
    # maintenance
    "compact_history",
    "compact_history_inline",
    "purge_orphan_acr_tags",
    "purge_orphan_acr_tags_inline",
    # internal symbols re-exported for tests (intentional, not API)
    "LOGGER",
    "_RUNNING_STATE_TERMINAL_FAILURES",
    "_check_pre_patch_stuck",
    "_clear_latest",
    "_default_enqueue",
    "_fail_pre",
    "_fail_rollback",
    "_fail_rollout",
    "_image_matches_version",
    "_new_revision_is_ready",
    "_record_running_version",
    "_set_latest",
    "_utc_now",
]
