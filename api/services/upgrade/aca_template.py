"""Container App template snapshot / image-tag helpers for the upgrade flow.

Module summary: Reads the currently deployed Container App's template and
returns the per-sidecar image references the upgrade flow rolls back to
when a new revision fails to come up. The "current images" snapshot is
the ground truth for rollback — no external manifest is required.

Responsibility: Resource Manager I/O for `Microsoft.App/containerApps`.
Edit boundaries: All ARM client construction lives here. Applier /
  rollback / rollout watcher consume the data classes only.
Key entry points: `SidecarImages`, `read_current_images`, `swap_images`,
  `latest_revision_name`, `read_app_template`, `TemplateError`.
Risky contracts: PATCH-like operations mutate the template object the
  ARM SDK returned to us; callers must always pass the result of
  `read_app_template` (so the ARM ETag / template integrity is fresh).
  The MI must have `Microsoft.App/containerApps/write` — covered by the
  existing RG Contributor assignment.
Validation: `uv run pytest -q api/tests/test_upgrade_aca_template.py`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

CONTAINER_APP_NAME_ENV = "CONTAINER_APP_NAME"
AZURE_RESOURCE_GROUP_ENV = "AZURE_RESOURCE_GROUP"
AZURE_SUBSCRIPTION_ID_ENV = "AZURE_SUBSCRIPTION_ID"
PLATFORM_ACR_NAME_ENV = "PLATFORM_ACR_NAME"

# Which container names the upgrade flow rewrites. Order matches the
# bundled `ca-elb-dashboard` template (`api`, `worker`, `beat` all run
# the `elb-api` image; `frontend` and `terminal` each have their own).
_SIDECAR_TO_IMAGE: dict[str, str] = {
    "api": "elb-api",
    "worker": "elb-api",
    "beat": "elb-api",
    "frontend": "elb-frontend",
    "terminal": "elb-terminal",
}


class TemplateError(RuntimeError):
    """Raised when ACA template I/O fails or returns an unexpected shape."""


@dataclass(frozen=True)
class SidecarImages:
    """Per-component image refs read from (or written to) the ACA template.

    Component names are the logical roles (`api`, `frontend`, `terminal`)
    used by the upgrade flow, NOT the per-container names in the template
    — multiple containers (e.g. api/worker/beat) share the `api` image
    role and are rewritten together.
    """

    api: str
    frontend: str
    terminal: str

    def as_dict(self) -> dict[str, str]:
        return {"api": self.api, "frontend": self.frontend, "terminal": self.terminal}


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TemplateError(f"{name} is not set")
    return value


def _client() -> Any:
    """Construct a ContainerAppsAPIClient bound to the current sub.

    Lazy import so test environments without `azure-mgmt-appcontainers`
    still load the module fine.
    """
    from azure.mgmt.appcontainers import ContainerAppsAPIClient

    return ContainerAppsAPIClient(get_credential(), _env(AZURE_SUBSCRIPTION_ID_ENV))


def read_app_template(*, client: Any | None = None) -> Any:
    """Fetch the live Container App resource (template + revision info)."""
    rg = _env(AZURE_RESOURCE_GROUP_ENV)
    name = _env(CONTAINER_APP_NAME_ENV)
    cli = client or _client()
    try:
        return cli.container_apps.get(rg, name)
    except Exception as exc:  # azure-core raises a wide variety
        raise TemplateError(f"failed to read container app {name!r}: {exc}") from exc


def latest_revision_name(*, client: Any | None = None) -> str:
    """Return the current `latestRevisionName` from the live resource."""
    app = read_app_template(client=client)
    name = getattr(getattr(app, "properties", None), "latest_revision_name", None) or getattr(
        app, "latest_revision_name", None
    )
    if not name:
        raise TemplateError("container app has no latestRevisionName")
    return str(name)


def read_current_images(*, client: Any | None = None) -> SidecarImages:
    """Snapshot the image refs currently set on each upgraded sidecar."""
    app = read_app_template(client=client)
    return _extract_images(app)


def swap_images(
    *,
    target_version: str,
    revision_suffix: str | None = None,
    client: Any | None = None,
) -> tuple[Any, SidecarImages, SidecarImages]:
    """Patch the Container App template to point at the new image tags.

    Returns ``(poller, previous_images, target_images)``. The caller is
    responsible for awaiting the poller and committing the rollback
    snapshot before draining the response.
    """
    rg = _env(AZURE_RESOURCE_GROUP_ENV)
    name = _env(CONTAINER_APP_NAME_ENV)
    cli = client or _client()
    app = read_app_template(client=cli)
    previous = _extract_images(app)
    target = _compute_target_images(target_version)
    _apply_images_to_template(app, target)
    if revision_suffix:
        _set_revision_suffix(app, revision_suffix)
    try:
        poller = cli.container_apps.begin_update(rg, name, app)
    except Exception as exc:
        raise TemplateError(f"begin_update failed: {exc}") from exc
    return poller, previous, target


def apply_images(
    *,
    images: SidecarImages,
    revision_suffix: str | None = None,
    client: Any | None = None,
) -> Any:
    """Patch the template to the supplied image refs (used by rollback)."""
    rg = _env(AZURE_RESOURCE_GROUP_ENV)
    name = _env(CONTAINER_APP_NAME_ENV)
    cli = client or _client()
    app = read_app_template(client=cli)
    _apply_explicit_images_to_template(app, images)
    if revision_suffix:
        _set_revision_suffix(app, revision_suffix)
    try:
        return cli.container_apps.begin_update(rg, name, app)
    except Exception as exc:
        raise TemplateError(f"begin_update failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Template inspection / mutation helpers (kept private so tests can drive
# them through the public API).
# ---------------------------------------------------------------------------


def _template_containers(app: Any) -> list[Any]:
    properties = getattr(app, "properties", app)
    template = getattr(properties, "template", None)
    if template is None:
        raise TemplateError("container app missing properties.template")
    containers = getattr(template, "containers", None) or []
    if not containers:
        raise TemplateError("container app template has no containers")
    return list(containers)


def _extract_images(app: Any) -> SidecarImages:
    refs: dict[str, str] = {}
    for container in _template_containers(app):
        cname = getattr(container, "name", "") or ""
        role = _SIDECAR_TO_IMAGE.get(cname)
        if role is None:
            continue
        image = getattr(container, "image", "") or ""
        # api/worker/beat all share role=elb-api; first one wins (they
        # should all match in practice).
        refs.setdefault(_role_for(cname), image)
    missing = [r for r in ("api", "frontend", "terminal") if r not in refs]
    if missing:
        raise TemplateError(f"missing sidecar containers in template: {missing}")
    return SidecarImages(api=refs["api"], frontend=refs["frontend"], terminal=refs["terminal"])


def _role_for(container_name: str) -> str:
    if container_name in {"api", "worker", "beat"}:
        return "api"
    return container_name


def compute_target_images(target_version: str) -> SidecarImages:
    """Public helper: compute target image refs for ``target_version``."""
    return _compute_target_images(target_version)


def _compute_target_images(target_version: str) -> SidecarImages:
    acr = _env(PLATFORM_ACR_NAME_ENV).lower()
    base = f"{acr}.azurecr.io"
    tag = f"v{target_version}"
    return SidecarImages(
        api=f"{base}/elb-api:{tag}",
        frontend=f"{base}/elb-frontend:{tag}",
        terminal=f"{base}/elb-terminal:{tag}",
    )


def _apply_images_to_template(app: Any, target: SidecarImages) -> None:
    role_to_image = {
        "api": target.api,
        "frontend": target.frontend,
        "terminal": target.terminal,
    }
    for container in _template_containers(app):
        cname = getattr(container, "name", "") or ""
        role = _role_for(cname)
        if role in role_to_image and cname in _SIDECAR_TO_IMAGE:
            container.image = role_to_image[role]


def _apply_explicit_images_to_template(app: Any, images: SidecarImages) -> None:
    role_to_image = {
        "api": images.api,
        "frontend": images.frontend,
        "terminal": images.terminal,
    }
    for container in _template_containers(app):
        cname = getattr(container, "name", "") or ""
        role = _role_for(cname)
        if role in role_to_image and cname in _SIDECAR_TO_IMAGE:
            container.image = role_to_image[role]


def _set_revision_suffix(app: Any, suffix: str) -> None:
    properties = getattr(app, "properties", app)
    template = getattr(properties, "template", None)
    if template is None:
        return
    template.revision_suffix = suffix
