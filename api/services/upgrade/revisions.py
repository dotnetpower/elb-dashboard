"""Revision + ingress-traffic ARM I/O for the blue/green upgrade flow.

Module summary: Drives the `Microsoft.App/containerApps` revision and
ingress-traffic surface that the blue/green (`activeRevisionsMode:
Multiple`) upgrade path needs — staging a zero-weight green revision,
cutting traffic over, the instant traffic-flip rollback, and the
keep-N revision garbage collection. Kept separate from
`aca_template` (which owns image-tag template swaps) so each module
has a single responsibility.

Responsibility: Resource Manager I/O for container-app revisions and
  ingress traffic weights. No state-machine writes, no image-tag
  computation.
Edit boundaries: All revision/traffic ARM calls live here. The pipeline,
  reconciler, rollback, and GC tasks consume the dataclasses and the
  module-level helpers; they never reach for the SDK revision client
  themselves.
Key entry points: `RevisionSummary`, `list_revisions`, `serving_revision`,
  `pin_traffic`, `cutover`, `flip_traffic`, `deactivate_revision`,
  `activate_revision`, `assign_label`, `revision_image_refs`,
  `strict_bluegreen`, `RevisionsError`.
Risky contracts: Every weight-changing call reads the live template via
  `aca_template.read_app_template` and PATCHes through `begin_update`
  WITHOUT setting `revision_suffix`, so it never creates a new revision —
  only `aca_template.swap_images` does that. Masked secrets are dropped
  from every update payload (same RP contract as `aca_template`). The MI
  needs `Microsoft.App/containerApps/write` (covered by the existing RG
  Contributor assignment) — no new role.
Validation: `uv run pytest -q api/tests/test_upgrade_revisions.py`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from api.services.upgrade import aca_template

LOGGER = logging.getLogger(__name__)

GREEN_LABEL = "green"
BLUE_LABEL = "blue"

_STRICT_BLUEGREEN_ENV = "STRICT_BLUEGREEN"


def strict_bluegreen() -> bool:
    """Return True when the blue/green upgrade path is enabled.

    Default OFF (charter §12a Rule 4): when unset the upgrade flow keeps
    the legacy Single-mode rolling_out→succeeded behaviour. Enabling it
    requires the Container App to run with
    ``activeRevisionsMode: Multiple`` (provisioned by the flag-gated
    Bicep change) so a green revision can be staged at 0% traffic.
    """
    return os.environ.get(_STRICT_BLUEGREEN_ENV, "").lower() == "true"


class RevisionsError(RuntimeError):
    """Raised when revision / traffic ARM I/O fails or returns a bad shape."""


@dataclass(frozen=True)
class RevisionSummary:
    """Per-revision summary combining revision status + current traffic.

    ``weight`` and ``label`` come from the container app's
    ``ingress.traffic`` block (a revision absent from that block has
    ``weight=0``); the rest come from the revision object itself.
    """

    name: str
    active: bool
    weight: int
    label: str
    created_on: datetime | None
    running_state: str
    provisioning_state: str


# ---------------------------------------------------------------------------
# Traffic-block helpers (read / mutate the ingress.traffic list).
# ---------------------------------------------------------------------------


def _ingress(app: Any) -> Any:
    properties = getattr(app, "properties", app)
    configuration = getattr(properties, "configuration", None)
    if configuration is None:
        raise RevisionsError("container app missing properties.configuration")
    ingress = getattr(configuration, "ingress", None)
    if ingress is None:
        raise RevisionsError("container app has no ingress (cannot manage traffic)")
    return ingress


def _traffic_list(app: Any) -> list[Any]:
    ingress = _ingress(app)
    traffic = getattr(ingress, "traffic", None)
    return list(traffic) if traffic else []


def _entry_field(entry: Any, *names: str) -> Any:
    for name in names:
        if isinstance(entry, dict):
            if name in entry:
                return entry[name]
        else:
            value = getattr(entry, name, None)
            if value is not None:
                return value
    return None


def _entry_revision(entry: Any) -> str:
    # ``latestRevision: true`` entries have no explicit name; the upgrade
    # flow always pins explicit revision names so we coerce that to "".
    value = _entry_field(entry, "revision_name", "revisionName")
    return str(value) if value else ""


def _entry_weight(entry: Any) -> int:
    raw = _entry_field(entry, "weight")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _entry_label(entry: Any) -> str:
    value = _entry_field(entry, "label")
    return str(value) if value else ""


def _make_traffic_weight(revision_name: str, weight: int, label: str | None) -> Any:
    """Build an SDK TrafficWeight (or a plain dict fallback)."""
    try:
        from azure.mgmt.appcontainers.models import TrafficWeight

        kwargs: dict[str, Any] = {"revision_name": revision_name, "weight": weight}
        if label:
            kwargs["label"] = label
        return TrafficWeight(**kwargs)
    except Exception:
        entry: dict[str, Any] = {"revisionName": revision_name, "weight": weight}
        if label:
            entry["label"] = label
        return entry


def _set_traffic(app: Any, weights: list[Any]) -> None:
    ingress = _ingress(app)
    ingress.traffic = weights


def _begin_update(cli: Any, app: Any) -> Any:
    rg = aca_template._env(aca_template.AZURE_RESOURCE_GROUP_ENV)
    name = aca_template._env(aca_template.CONTAINER_APP_NAME_ENV)
    aca_template._omit_masked_secrets_from_update(app)
    try:
        return cli.container_apps.begin_update(rg, name, app)
    except Exception as exc:
        raise RevisionsError(f"begin_update (traffic) failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Read helpers.
# ---------------------------------------------------------------------------


def _revisions_client(client: Any | None) -> Any:
    return client or aca_template._client()


def list_revisions(*, client: Any | None = None) -> list[RevisionSummary]:
    """List the app's revisions joined with their current traffic weight."""
    cli = _revisions_client(client)
    app = aca_template.read_app_template(client=cli)
    traffic = _traffic_list(app)
    weight_by_rev = {_entry_revision(e): _entry_weight(e) for e in traffic if _entry_revision(e)}
    label_by_rev = {
        _entry_revision(e): _entry_label(e)
        for e in traffic
        if _entry_revision(e) and _entry_label(e)
    }

    rg = aca_template._env(aca_template.AZURE_RESOURCE_GROUP_ENV)
    name = aca_template._env(aca_template.CONTAINER_APP_NAME_ENV)
    try:
        revisions = list(cli.container_apps_revisions.list_revisions(rg, name))
    except Exception as exc:
        raise RevisionsError(f"list_revisions failed: {exc}") from exc

    out: list[RevisionSummary] = []
    for rev in revisions:
        rev_name = str(getattr(rev, "name", "") or "")
        props = getattr(rev, "properties", rev)
        created = getattr(props, "created_time", None) or getattr(props, "created_on", None)
        out.append(
            RevisionSummary(
                name=rev_name,
                active=bool(getattr(props, "active", False)),
                weight=weight_by_rev.get(rev_name, 0),
                label=label_by_rev.get(rev_name, ""),
                created_on=created if isinstance(created, datetime) else None,
                running_state=str(getattr(props, "running_state", "") or ""),
                provisioning_state=str(getattr(props, "provisioning_state", "") or ""),
            )
        )
    return out


def revision_image_refs(*, client: Any | None = None) -> dict[str, set[str]]:
    """Map each revision name to the set of container image refs it pins.

    Consumed by the keep-N garbage collector to decide which ACR tags are
    still referenced by a retained revision (and therefore must NOT be
    deleted). ``redis:7-alpine`` and other non-ACR images are included
    verbatim; the GC caller filters to the platform ACR endpoint before
    deleting anything.
    """
    cli = _revisions_client(client)
    rg = aca_template._env(aca_template.AZURE_RESOURCE_GROUP_ENV)
    name = aca_template._env(aca_template.CONTAINER_APP_NAME_ENV)
    try:
        revisions = list(cli.container_apps_revisions.list_revisions(rg, name))
    except Exception as exc:
        raise RevisionsError(f"list_revisions failed: {exc}") from exc

    out: dict[str, set[str]] = {}
    for rev in revisions:
        rev_name = str(getattr(rev, "name", "") or "")
        if not rev_name:
            continue
        props = getattr(rev, "properties", rev)
        template = getattr(props, "template", None)
        containers = getattr(template, "containers", None) or []
        refs: set[str] = set()
        for container in containers:
            image = getattr(container, "image", None)
            if isinstance(container, dict):
                image = container.get("image")
            if image:
                refs.add(str(image))
        out[rev_name] = refs
    return out


def serving_revision(*, client: Any | None = None) -> str:
    """Return the revision name currently receiving the most traffic.

    Falls back to the app's ``latestRevisionName`` when the traffic block
    is empty (the pre-blue/green Single-mode default where 100% implicitly
    goes to the latest revision).
    """
    cli = _revisions_client(client)
    app = aca_template.read_app_template(client=cli)
    traffic = _traffic_list(app)
    best_name = ""
    best_weight = -1
    for entry in traffic:
        rev = _entry_revision(entry)
        weight = _entry_weight(entry)
        if rev and weight > best_weight:
            best_name, best_weight = rev, weight
    if best_name:
        return best_name
    # Empty traffic block → latest revision is the implicit 100% target.
    return aca_template.latest_revision_name(client=cli)


# ---------------------------------------------------------------------------
# Traffic mutations (never create a new revision — no revision_suffix set).
# ---------------------------------------------------------------------------


def pin_traffic(*, revision_name: str, label: str | None = None, client: Any | None = None) -> Any:
    """Pin 100% of ingress traffic to a single revision.

    Used before staging green so the freshly-created green revision does
    NOT auto-receive traffic (Multiple mode otherwise routes 100% to the
    latest revision when the traffic block is empty).
    """
    if not revision_name:
        raise RevisionsError("pin_traffic requires a revision_name")
    cli = _revisions_client(client)
    app = aca_template.read_app_template(client=cli)
    _set_traffic(app, [_make_traffic_weight(revision_name, 100, label)])
    return _begin_update(cli, app)


def cutover(
    *,
    green_revision: str,
    blue_revision: str,
    green_label: str | None = GREEN_LABEL,
    blue_label: str | None = BLUE_LABEL,
    client: Any | None = None,
) -> Any:
    """Shift 100% of traffic to green while keeping blue warm at weight 0.

    Blue stays ACTIVE (weight 0) so a subsequent :func:`flip_traffic` can
    return traffic to it in seconds without any image pull or rebuild.
    """
    if not green_revision or not blue_revision:
        raise RevisionsError("cutover requires both green_revision and blue_revision")
    cli = _revisions_client(client)
    app = aca_template.read_app_template(client=cli)
    _set_traffic(
        app,
        [
            _make_traffic_weight(green_revision, 100, green_label),
            _make_traffic_weight(blue_revision, 0, blue_label),
        ],
    )
    return _begin_update(cli, app)


def flip_traffic(
    *,
    to_revision: str,
    from_revision: str,
    to_label: str | None = BLUE_LABEL,
    from_label: str | None = GREEN_LABEL,
    client: Any | None = None,
) -> Any:
    """Instant rollback: move 100% traffic back to ``to_revision``.

    Symmetrical to :func:`cutover`. ``to_revision`` (the still-running
    blue) receives 100%; ``from_revision`` (green) is kept active at 0 so
    it can be inspected before GC deactivates it.
    """
    if not to_revision or not from_revision:
        raise RevisionsError("flip_traffic requires both to_revision and from_revision")
    cli = _revisions_client(client)
    app = aca_template.read_app_template(client=cli)
    _set_traffic(
        app,
        [
            _make_traffic_weight(to_revision, 100, to_label),
            _make_traffic_weight(from_revision, 0, from_label),
        ],
    )
    return _begin_update(cli, app)


def assign_label(*, revision_name: str, label: str, client: Any | None = None) -> Any:
    """Attach a traffic label to ``revision_name`` (idempotent).

    Preserves existing weights; only (re)assigns the label so the revision
    becomes addressable at its label FQDN for an optional manual smoke.
    """
    if not revision_name or not label:
        raise RevisionsError("assign_label requires revision_name and label")
    cli = _revisions_client(client)
    app = aca_template.read_app_template(client=cli)
    traffic = _traffic_list(app)
    rebuilt: list[Any] = []
    found = False
    for entry in traffic:
        rev = _entry_revision(entry)
        if rev == revision_name:
            rebuilt.append(_make_traffic_weight(rev, _entry_weight(entry), label))
            found = True
        else:
            # Drop the label from any other revision that held it so a
            # label is never assigned to two revisions at once.
            existing_label = _entry_label(entry)
            rebuilt.append(
                _make_traffic_weight(
                    rev, _entry_weight(entry), None if existing_label == label else existing_label
                )
            )
    if not found:
        rebuilt.append(_make_traffic_weight(revision_name, 0, label))
    _set_traffic(app, rebuilt)
    return _begin_update(cli, app)


# ---------------------------------------------------------------------------
# Revision lifecycle (activate / deactivate). ACA auto-prunes inactive
# revisions beyond the 100-revision retention limit; a deactivated
# revision runs 0 replicas (no compute) so "no garbage container" == "no
# extra ACTIVE revision".
# ---------------------------------------------------------------------------


def activate_revision(*, revision_name: str, client: Any | None = None) -> None:
    """Activate a revision (idempotent — already-active is a no-op)."""
    if not revision_name:
        raise RevisionsError("activate_revision requires a revision_name")
    cli = _revisions_client(client)
    rg = aca_template._env(aca_template.AZURE_RESOURCE_GROUP_ENV)
    name = aca_template._env(aca_template.CONTAINER_APP_NAME_ENV)
    try:
        cli.container_apps_revisions.activate_revision(rg, name, revision_name)
    except Exception as exc:
        raise RevisionsError(f"activate_revision {revision_name!r} failed: {exc}") from exc


def deactivate_revision(*, revision_name: str, client: Any | None = None) -> bool:
    """Deactivate a revision so it stops consuming compute.

    Returns True on success, False on a best-effort failure (logged, not
    raised) so the GC sweep can continue deactivating its siblings — one
    stuck revision must not block the rest of the cleanup.
    """
    if not revision_name:
        return False
    cli = _revisions_client(client)
    rg = aca_template._env(aca_template.AZURE_RESOURCE_GROUP_ENV)
    name = aca_template._env(aca_template.CONTAINER_APP_NAME_ENV)
    try:
        cli.container_apps_revisions.deactivate_revision(rg, name, revision_name)
        return True
    except Exception as exc:
        LOGGER.warning("revisions.deactivate %s failed (best-effort): %s", revision_name, exc)
        return False
