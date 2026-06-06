"""ACR repository + image listing.

Responsibility: ACR repository + image listing.
Edit boundaries: Keep reusable domain logic here; routes and tasks call this layer.
Key entry points: _collect_succeeded_acr_images, _collect_building_acr_images, list_acr_repositories
Risky contracts: Keep Azure credentials centralized and sanitise data at HTTP/log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import logging
import os
from itertools import islice
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.azure_clients import acr_client
from api.services.image_tags import IMAGE_TAGS

LOGGER = logging.getLogger(__name__)
_ACR_RUNS_LIST_LIMIT = max(1, int(os.environ.get("ACR_RUNS_LIST_LIMIT", "100")))


def _collect_succeeded_acr_images(actual_tags: dict[str, list[str]], images: list[Any]) -> None:
    for image in images:
        repo = image.repository or ""
        tag = image.tag or ""
        if not repo or not tag:
            continue
        actual_tags.setdefault(repo, [])
        if tag not in actual_tags[repo]:
            actual_tags[repo].append(tag)


def _collect_building_acr_images(
    building_images: list[str],
    build_details: list[dict[str, str]],
    status: str,
    run_id: str,
    images: list[Any],
) -> None:
    for image in images:
        full = f"{image.repository or ''}:{image.tag or ''}"
        if full in building_images:
            continue
        building_images.append(full)
        build_details.append({"image": full, "status": status, "run_id": run_id})


def _augment_actual_tags_from_manifests(
    actual_tags: dict[str, list[str]],
    login_server: str,
) -> None:
    """Fill ``actual_tags`` from the registry's live manifests for expected images.

    The ACR Task run history is a bounded window (``_ACR_RUNS_LIST_LIMIT``)
    dominated by frequent platform image rebuilds (``elb-api`` /
    ``elb-frontend`` / ``elb-terminal``). BLAST images that were built once
    at provisioning time get pushed out of that window, so relying solely on
    ``Run.output_images`` reports them as "not built" even though their tags
    still resolve in the registry. This probes each expected ``image:tag``
    manifest directly so the card's "built" count reflects ground truth.

    Best-effort: any failure (missing data-plane role, transient outage,
    SDK import error) leaves ``actual_tags`` untouched so the run-history
    path remains the fallback. Never raises.
    """
    if not login_server:
        return
    refs = [f"{login_server}/{repo}:{tag}" for repo, tag in IMAGE_TAGS.items()]
    try:
        from api.services.upgrade import acr_inventory

        results = acr_inventory.lookup_images(refs)
    except Exception as exc:
        LOGGER.debug("acr manifest probe skipped (%s)", type(exc).__name__)
        return
    for info in results:
        if not info.exists:
            continue
        try:
            _, rest = info.image_ref.split("/", 1)
            repo, tag = rest.rsplit(":", 1)
        except ValueError:
            continue
        actual_tags.setdefault(repo, [])
        if tag not in actual_tags[repo]:
            actual_tags[repo].append(tag)


# ---------------------------------------------------------------------------
# Remote Terminal VM (legacy status surface)
# ---------------------------------------------------------------------------


def list_acr_repositories(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    """Return registry metadata with actual vs expected image tag status."""

    management = acr_client(credential, subscription_id)
    registry = management.registries.get(resource_group, registry_name)
    login_server = registry.login_server or f"{registry_name}.azurecr.io"

    actual_tags: dict[str, list[str]] = {}
    building_images: list[str] = []
    build_details: list[dict[str, str]] = []
    # Persisted run_id -> {image, tag} mapping recorded at build submission
    # time. ACR's Run.output_images only populates after the push step
    # succeeds, so Queued/Started/Running runs typically have an empty
    # output_images list — without this mapping, the per-image rows in the
    # ACR card show a "Build" button after a browser refresh instead of
    # the correct "Building" state.
    pending_by_run_id: dict[str, dict[str, str]] = {}
    pruner = None
    try:
        from api.services import acr_build_state

        pending_by_run_id = acr_build_state.load_pending_builds(registry_name)
        pruner = acr_build_state.prune_terminal_builds
    except Exception as exc:
        LOGGER.debug("acr_build_state load skipped (%s)", type(exc).__name__)

    terminal_run_ids: set[str] = set()
    try:
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient

        preview = ContainerRegistryManagementClient(
            credential, subscription_id, api_version="2019-06-01-preview"
        )
        for run in islice(preview.runs.list(resource_group, registry_name), _ACR_RUNS_LIST_LIMIT):
            status = run.status or ""
            run_id = run.run_id or ""
            if status == "Succeeded":
                if run.output_images:
                    _collect_succeeded_acr_images(actual_tags, run.output_images)
                if run_id:
                    terminal_run_ids.add(run_id)
            elif status in ("Queued", "Started", "Running"):
                if run.output_images:
                    _collect_building_acr_images(
                        building_images,
                        build_details,
                        status or "Unknown",
                        run_id,
                        run.output_images,
                    )
                elif run_id and run_id in pending_by_run_id:
                    # ACR hasn't filled output_images yet — fall back to
                    # the persisted submission record so the row shows the
                    # correct "Building" state.
                    mapping = pending_by_run_id[run_id]
                    full = f"{mapping['image']}:{mapping['tag']}"
                    if full not in building_images:
                        building_images.append(full)
                        build_details.append({"image": full, "status": status, "run_id": run_id})
            elif status in ("Failed", "Canceled", "Error", "Timeout"):
                if run_id:
                    terminal_run_ids.add(run_id)
    except Exception as exc:
        LOGGER.warning("ACR runs query failed (non-fatal): %s", type(exc).__name__)

    # Best-effort cleanup so the pending table doesn't grow without bound.
    # Only prune rows whose run we just observed reach a terminal status.
    if pruner is not None and pending_by_run_id:
        stale_run_ids = terminal_run_ids & set(pending_by_run_id.keys())
        if stale_run_ids:
            try:
                pruner(registry_name, stale_run_ids)
            except Exception as exc:
                LOGGER.debug("acr_build_state prune skipped (%s)", type(exc).__name__)

    # Ground-truth pass: the run-history loop above only sees a bounded
    # window of recent ACR Task runs, so images built once at provisioning
    # time can fall out of view. Probe the expected image manifests
    # directly so the card's built/total count stays accurate.
    _augment_actual_tags_from_manifests(actual_tags, login_server)

    return {
        "name": registry.name,
        "login_server": login_server,
        "sku": registry.sku.name if registry.sku else None,
        "expected_image_tags": IMAGE_TAGS,
        "actual_tags": actual_tags,
        "building_images": building_images,
        "build_details": build_details,
    }


def get_acr_registry_detail(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    registry_name: str,
) -> dict[str, Any]:
    """Rich Well-Architected / CAF configuration surface for one ACR registry.

    Single management call (`registries.get`). Policy fields require the Premium
    SKU to be meaningful; the rules treat a `None` policy as "not available on
    this SKU" and skip rather than fabricate.
    """
    management = acr_client(credential, subscription_id)
    registry = management.registries.get(resource_group, registry_name)

    policies = getattr(registry, "policies", None)
    quarantine = getattr(policies, "quarantine_policy", None) if policies else None
    trust = getattr(policies, "trust_policy", None) if policies else None
    retention = getattr(policies, "retention_policy", None) if policies else None
    export = getattr(policies, "export_policy", None) if policies else None
    encryption = getattr(registry, "encryption", None)

    def _status_enabled(obj: Any) -> bool | None:
        if obj is None:
            return None
        status = getattr(obj, "status", None)
        if status is None:
            return None
        return str(status).lower() == "enabled"

    return {
        "name": registry.name,
        "sku": registry.sku.name if registry.sku else None,
        "admin_user_enabled": getattr(registry, "admin_user_enabled", None),
        "public_network_access": getattr(registry, "public_network_access", None),
        "zone_redundancy": getattr(registry, "zone_redundancy", None),
        "anonymous_pull_enabled": getattr(registry, "anonymous_pull_enabled", None),
        "data_endpoint_enabled": getattr(registry, "data_endpoint_enabled", None),
        "quarantine_policy": _status_enabled(quarantine),
        "trust_policy": _status_enabled(trust),
        "retention_policy": _status_enabled(retention),
        "export_policy": _status_enabled(export),
        "cmk_enabled": _status_enabled(encryption),
    }
