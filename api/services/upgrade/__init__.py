"""In-app self-upgrade service package.

Module summary: Discovers candidate release tags from the configured git remote
and persists the single upgrade-state row that drives the SPA's "upgrade
available" indicator. Read/write helpers are split across small modules so
each one has a single responsibility (remote discovery vs. persisted state).

Responsibility: Re-export the small upgrade subsystem helpers.
Edit boundaries: New upgrade-related modules live under this package; routes
  and tasks import from here, never from `azure.mgmt.*` directly.
Key entry points: `remote_tags`, `state`.
Risky contracts: All Storage Table writes go through `state.update_state` for
  ETag-aware persistence; remote network calls go through `remote_tags`.
Validation: `uv run pytest -q api/tests/test_upgrade_state.py
  api/tests/test_upgrade_remote_tags.py api/tests/test_upgrade_routes.py`.
"""

from __future__ import annotations

from api.services.upgrade import remote_tags, state

__all__ = ["remote_tags", "state"]
