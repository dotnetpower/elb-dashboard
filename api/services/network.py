"""Resource-group helpers for the platform UAMI workflow.

Responsibility: Idempotent `resource_groups.create_or_update` shim for the
control-plane code paths that need to materialise a resource group on
behalf of the operator.
Edit boundaries: Resource-group lifecycle only. Networking primitives
(VNets, subnets, NSGs, public IPs, NICs) belong in dedicated modules — the
historical SSH-to-Remote-Terminal-VM helpers were removed when the
browser terminal moved to a Container App sidecar (see
docs/container-apps-migration.md).
Key entry points: `ensure_resource_group`.
Risky contracts: Keep Azure credentials centralised; never log credentials
or secrets.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging

from azure.core.credentials import TokenCredential

from api.services.azure_clients import resource_client

LOGGER = logging.getLogger(__name__)


def ensure_resource_group(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    region: str,
) -> None:
    """Create or update the resource group. Idempotent."""
    rc = resource_client(credential, subscription_id)
    LOGGER.info("ensure_resource_group rg=%s region=%s", resource_group, region)
    rc.resource_groups.create_or_update(
        resource_group,
        {"location": region, "tags": {"managed-by": "elb-dashboard"}},
    )
