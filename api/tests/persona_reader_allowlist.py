"""Curated read-only allowlist for the `reader_caller` persona.

Module summary: Source-of-truth registry of FastAPI route handler functions
that a subscription-Reader caller (`Reader` + `Storage Blob Data Reader`)
must keep being able to invoke through the api sidecar. Adding or removing
an entry here requires a separate maintainer-reviewed PR per
.github/copilot-instructions.md §12a Rule 2 — a hardening PR that needs the
Reader to lose an action must split out the allowlist change.

Responsibility: Pure data — list of `(import_path, function_name, why)`
    triples consumed by `test_persona_matrix.py`. The test asserts that
    each listed handler does NOT depend on `require_upgrade_admin` (or any
    stricter gate added later).
Edit boundaries: Do NOT import FastAPI, services, or test fixtures from
    this module. Keep it side-effect-free so the test suite can import it
    cheaply at collection time.
Key entry points: `READER_ALLOWLIST`.
Risky contracts: Entries must reference handler functions that exist in
    the api/routes/ tree. The test fails loudly if an entry's symbol can
    no longer be imported — that means either the route was renamed (split
    out a PR to fix the allowlist) or the route was removed (split out a PR
    to drop the entry).
Validation: `uv run pytest -q api/tests/test_persona_matrix.py`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReaderAllowedRoute:
    """One entry in the Reader allowlist.

    module     — dotted import path of the route module (e.g. ``api.routes.me``).
    function   — handler function name within that module.
    why        — short human justification — surfaced in test failure messages.
    """

    module: str
    function: str
    why: str


# ---------------------------------------------------------------------------
# Reader allowlist. Format: (module, function, justification).
#
# These are the dashboard's read paths plus the polling endpoints the SPA
# needs to render its base UI for a Reader-only operator. A Reader is NOT
# expected to be able to submit BLAST, scale AKS, build ACR images, prepare
# databases, or mutate upgrade state — those endpoints intentionally do not
# appear in this list.
#
# When a new read-only endpoint lands, add it here in the SAME PR as the
# route. When a route is renamed, update the entry in the SAME PR as the
# rename. When a Reader genuinely needs to LOSE access to one of these,
# remove it via a separate maintainer-reviewed PR per §12a Rule 2.
# ---------------------------------------------------------------------------
READER_ALLOWLIST: tuple[ReaderAllowedRoute, ...] = (
    # ---- identity / liveness ----
    ReaderAllowedRoute(
        module="api.routes.me",
        function="me",
        why="Reader needs identity + visible subscriptions to render the SPA shell.",
    ),
    # ---- dashboard monitoring tiles (read-only Azure SDK fan-out) ----
    ReaderAllowedRoute(
        module="api.routes.monitor.aks",
        function="list_aks",
        why="Dashboard AKS card — list clusters visible to the MI.",
    ),
    ReaderAllowedRoute(
        module="api.routes.monitor.aks",
        function="aks_nodes",
        why="Dashboard AKS node tile (read-only).",
    ),
    ReaderAllowedRoute(
        module="api.routes.monitor.aks",
        function="aks_pods",
        why="Dashboard AKS pod tile (read-only).",
    ),
    ReaderAllowedRoute(
        module="api.routes.monitor.aks",
        function="aks_events",
        why="Dashboard AKS events tile (read-only).",
    ),
    ReaderAllowedRoute(
        module="api.routes.monitor.acr",
        function="list_acr",
        why="Dashboard ACR card — list registries / images visible to the MI.",
    ),
    ReaderAllowedRoute(
        module="api.routes.monitor.storage",
        function="storage_summary",
        why="Dashboard storage usage card (read-only).",
    ),
    # ---- BLAST read paths ----
    ReaderAllowedRoute(
        module="api.routes.blast.jobs",
        function="blast_jobs_list",
        why="Reader must see the job list (read-only).",
    ),
    ReaderAllowedRoute(
        module="api.routes.blast.jobs",
        function="blast_job_get",
        why="Reader must see a single job's status (read-only).",
    ),
    ReaderAllowedRoute(
        module="api.routes.blast.jobs",
        function="blast_job_events",
        why="Reader must see a job's progress events (read-only).",
    ),
    # ---- task / operation polling (SPA spinner backing endpoint) ----
    ReaderAllowedRoute(
        module="api.routes.tasks",
        function="get_task_status",
        why="Reader must poll Celery task status for read-only flows.",
    ),
    # ---- Service Bus Playground send (INTENTIONAL policy relaxation) ----
    # A subscription Reader may enqueue a BLAST request via the Playground.
    # This is a deliberate exception to "Reader is read-only": the enqueue runs
    # under the shared MI (no SAS token to the browser) and triggers BLAST
    # execution. Granting it to Reader is a conscious product decision recorded
    # in the feature change note; the route itself is require_caller-only.
    ReaderAllowedRoute(
        module="api.routes.settings.service_bus",
        function="send",
        why="Service Bus Playground send is intentionally Reader-accessible.",
    ),
    ReaderAllowedRoute(
        module="api.routes.settings.service_bus",
        function="drain_now",
        why="Playground 'drain now' accelerates the beat the Reader already triggers via send.",
    ),
    ReaderAllowedRoute(
        module="api.routes.settings.service_bus",
        function="observed_completions",
        why="Read-only view of completion-topic events observed by the demo consumer.",
    ),
)
