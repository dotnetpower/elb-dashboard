"""Escape-hatch command generator for the upgrade flow.

Module summary: Produces a copy-pasteable `az containerapp update` command
set the operator can run from an outside `az login` shell to recover when
a new revision fails to come up and the in-app rollback path is also
unreachable. No secrets are baked into the commands — the operator
authenticates themselves at the shell.

Responsibility: String building only. No I/O.
Edit boundaries: Update when the rollback/rollout contract changes.
Key entry points: `EscapeHatchPlan`, `build_plan`.
Risky contracts: Commands only contain subscription / RG / app /
  container / image refs. Never include MI secrets, ACR tokens, SAS, or
  bearer tokens.
Validation: `uv run pytest -q api/tests/test_upgrade_escape_hatch.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from api.services.upgrade.aca_template import (
    AZURE_RESOURCE_GROUP_ENV,
    AZURE_SUBSCRIPTION_ID_ENV,
    CONTAINER_APP_NAME_ENV,
    SidecarImages,
)

_CONTAINER_NAMES = ("api", "worker", "beat", "frontend", "terminal")


@dataclass(frozen=True)
class EscapeHatchPlan:
    """Recovery instructions surfaced to the operator on rollout failure."""

    commands: list[str]
    container_app: str
    subscription_id: str
    resource_group: str
    target_images: dict[str, str]


def build_plan(images: SidecarImages) -> EscapeHatchPlan:
    """Compose the `az containerapp update` invocations for ``images``.

    Each command is explicit about subscription / RG / app so it can be
    pasted into any `az login`-ed shell without first calling
    `az account set` (which would mutate the operator's default profile).
    """
    sub = os.environ.get(AZURE_SUBSCRIPTION_ID_ENV, "").strip()
    rg = os.environ.get(AZURE_RESOURCE_GROUP_ENV, "").strip()
    app = os.environ.get(CONTAINER_APP_NAME_ENV, "").strip()
    role_to_image = {
        "api": images.api,
        "worker": images.api,
        "beat": images.api,
        "frontend": images.frontend,
        "terminal": images.terminal,
    }
    sub_arg = f" --subscription {sub}" if sub else ""
    commands: list[str] = []
    for container in _CONTAINER_NAMES:
        image = role_to_image[container]
        commands.append(
            "az containerapp update"
            f" --name {app or '<container-app>'}"
            f" --resource-group {rg or '<resource-group>'}"
            f"{sub_arg}"
            f" --container-name {container}"
            f" --image {image}"
        )
    return EscapeHatchPlan(
        commands=commands,
        container_app=app,
        subscription_id=sub,
        resource_group=rg,
        target_images=images.as_dict(),
    )
