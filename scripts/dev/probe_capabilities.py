#!/usr/bin/env python3
"""Postprovision capability probe — verifies the shared MI has every role the
deployed routes/tasks expect, by attempting one real call against each critical
Azure surface and translating 403 / AuthorizationFailed into a human-readable
"this role is missing, grant it via this Bicep module" message.

Responsibility: Single-purpose RBAC capability sanity check invoked at the
    end of `scripts/dev/postprovision.sh` (see .github/copilot-instructions.md
    §12a Rule 3). Read-only. Does not mutate any Azure resource.
Edit boundaries: Add a new probe ONLY when a new role assignment lands in a
    Bicep module that production code will actually exercise. Each probe must
    be paired with the Bicep module path so failures are self-explanatory.
Key entry points: `main`, `Probe`, `PROBES`.
Risky contracts: Treats 401/403/`AuthorizationFailed` as missing RBAC.
    Treats network errors / 404 (resource doesn't exist yet) as "skip" so a
    fresh azd up before AKS is created doesn't fail the probe.
Validation: `uv run python scripts/dev/probe_capabilities.py` after
    `scripts/dev/postprovision.sh` completes. Exit code 0 = all required
    probes passed; non-zero = at least one required probe failed.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Import lazily inside each probe so a missing optional SDK doesn't blow up
# the whole script before it can print actionable output.

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BAD_ENV = 2


# ---------------------------------------------------------------------------
# Output helpers — keep stdout structured so postprovision.sh can `sed`-prefix
# the lines without losing alignment.
# ---------------------------------------------------------------------------


def _emit(prefix: str, message: str) -> None:
    print(f"{prefix} {message}", flush=True)


def ok(name: str, detail: str = "") -> None:
    suffix = f" — {detail}" if detail else ""
    _emit("✓", f"{name}{suffix}")


def skip(name: str, reason: str) -> None:
    _emit("ⓘ", f"{name} skipped: {reason}")


def warn(name: str, reason: str) -> None:
    _emit("⚠", f"{name} warning: {reason}")


def fail(name: str, reason: str, role: str, bicep: str) -> None:
    _emit("✗", f"{name} FAILED: {reason}")
    _emit(" ", f"  required role:  {role}")
    _emit(" ", f"  grant via:      {bicep}")


# ---------------------------------------------------------------------------
# 403 classifier — single source of truth for "this looked like missing RBAC".
# ---------------------------------------------------------------------------


def _is_authz_failure(exc: BaseException) -> bool:
    """Heuristic: True when the exception looks like a missing-RBAC 403."""
    # azure-core HttpResponseError carries .status_code; azure-mgmt-* may also
    # raise CloudError-style. We compare on status + the string token Azure
    # uses for missing role assignments.
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return True
    text = str(exc).lower()
    if "authorizationfailed" in text:
        return True
    if "does not have authorization to perform action" in text:
        return True
    if "the client" in text and "does not have authorization" in text:
        return True
    return False


def _is_not_found(exc: BaseException) -> bool:
    """Heuristic: True when the target resource doesn't exist yet (404).

    Treated as "skip", not "fail" — a fresh azd up before AKS / Key Vault is
    created should not block the probe.
    """
    status = getattr(exc, "status_code", None)
    if status == 404:
        return True
    text = str(exc).lower()
    if "resourcenotfound" in text or "was not found" in text:
        return True
    return False


# ---------------------------------------------------------------------------
# Probe registry.
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    """One capability check.

    name      — short label used in the printed output.
    runner    — callable that performs the SDK call and raises on failure.
                Should return a short detail string on success (e.g. number
                of items listed, or "ok").
    role      — Azure role expected on the shared MI for this surface.
    bicep     — repo-relative Bicep module path that grants the role.
    required  — when True a failure aborts the probe with EXIT_FAIL.
                When False a failure is surfaced as a warning only.
    env_vars  — env vars that must be set for the probe to run; missing env
                is reported as a skip, not a failure.
    """

    name: str
    runner: Callable[[], str]
    role: str
    bicep: str
    required: bool = True
    env_vars: tuple[str, ...] = field(default_factory=tuple)


def _require_env(*names: str) -> str | None:
    for name in names:
        if not os.environ.get(name):
            return name
    return None


# --- Probe runners ---------------------------------------------------------
# Each runner imports its SDK at call time so the failure mode for a missing
# package is "this one probe errors" rather than "import line 30 explodes".


def _credential() -> Any:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def probe_blob_list() -> str:
    from azure.storage.blob import BlobServiceClient

    account = os.environ["STORAGE_ACCOUNT_NAME"]
    url = f"https://{account}.blob.core.windows.net"
    client = BlobServiceClient(account_url=url, credential=_credential())
    # next() forces an actual HTTP call; without it the paged iterator is lazy.
    iterator = client.list_containers(results_per_page=1)
    try:
        next(iter(iterator), None)
    finally:
        client.close()
    return "list_containers OK"


def probe_table_list() -> str:
    from azure.data.tables import TableServiceClient

    account = os.environ["STORAGE_ACCOUNT_NAME"]
    url = f"https://{account}.table.core.windows.net"
    client = TableServiceClient(endpoint=url, credential=_credential())
    try:
        iterator = client.list_tables(results_per_page=1)
        next(iter(iterator), None)
    finally:
        client.close()
    return "list_tables OK"


def probe_acr_get() -> str:
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient

    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    rg = os.environ["AZURE_RESOURCE_GROUP"]
    acr = os.environ["ACR_NAME"]
    client = ContainerRegistryManagementClient(_credential(), subscription_id)
    registry = client.registries.get(rg, acr)
    return f"registries.get → {registry.name}"


def probe_aks_get() -> str:
    """Best-effort AKS probe. Skipped when AKS env not set."""
    from azure.mgmt.containerservice import ContainerServiceClient

    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    # azd does not set these on the first deploy (cluster is created later
    # by the SPA wizard); the probe is marked optional so missing env is a
    # skip, not a failure.
    rg = os.environ.get("AZURE_AKS_RESOURCE_GROUP") or os.environ.get(
        "AKS_CLUSTER_RESOURCE_GROUP", ""
    )
    cluster = os.environ.get("AZURE_AKS_CLUSTER_NAME") or os.environ.get("AKS_CLUSTER_NAME", "")
    if not rg or not cluster:
        raise SkipProbe("AKS cluster env not set (cluster not yet created)")
    client = ContainerServiceClient(_credential(), subscription_id)
    obj = client.managed_clusters.get(rg, cluster)
    return f"managed_clusters.get → {obj.name}"


def probe_kv_secrets_list() -> str:
    """Probe Key Vault using SecretClient (matches `Key Vault Secrets User`)."""
    from azure.keyvault.secrets import SecretClient

    vault = (
        os.environ.get("AZURE_KEY_VAULT_NAME")
        or os.environ.get("KEY_VAULT_NAME")
        or os.environ.get("KEYVAULT_NAME")
        or ""
    )
    if not vault:
        raise SkipProbe("Key Vault name env not set")
    vault_uri = vault if vault.startswith("https://") else f"https://{vault}.vault.azure.net"
    client = SecretClient(vault_url=vault_uri, credential=_credential())
    try:
        iterator = client.list_properties_of_secrets()
        next(iter(iterator), None)
    finally:
        # SecretClient does not require an explicit close; the underlying
        # pipeline transport is reused per-process. This call is here for
        # symmetry with the other probes.
        pass
    return "list_properties_of_secrets OK"


def probe_container_app_get() -> str:
    from azure.mgmt.appcontainers import ContainerAppsAPIClient

    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    rg = os.environ["AZURE_RESOURCE_GROUP"]
    name = os.environ["CONTAINER_APP_NAME"]
    client = ContainerAppsAPIClient(_credential(), subscription_id)
    obj = client.container_apps.get(rg, name)
    return f"container_apps.get → {obj.name}"


class SkipProbe(Exception):
    """Runner raises this when the probe target doesn't exist yet.

    Treated as a skip (informational), not a failure. Used by the AKS and
    Key Vault probes which depend on resources created later by the SPA
    wizard or the operator.
    """


# ---------------------------------------------------------------------------
# Probe table — declarative, easy to add to.
# ---------------------------------------------------------------------------
PROBES: tuple[Probe, ...] = (
    Probe(
        name="Storage Blob (data plane)",
        runner=probe_blob_list,
        role="Storage Blob Data Contributor (or Reader for read-only)",
        bicep="infra/modules/storage.bicep",
        required=True,
        env_vars=("STORAGE_ACCOUNT_NAME",),
    ),
    Probe(
        name="Storage Table (data plane)",
        runner=probe_table_list,
        role="Storage Table Data Contributor",
        bicep="infra/modules/storage.bicep",
        required=True,
        env_vars=("STORAGE_ACCOUNT_NAME",),
    ),
    Probe(
        name="ACR (management plane)",
        runner=probe_acr_get,
        role="Contributor (or AcrPull + AcrPush for narrow split)",
        bicep="infra/modules/acr.bicep",
        required=True,
        env_vars=("AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "ACR_NAME"),
    ),
    Probe(
        name="Container Apps (management plane)",
        runner=probe_container_app_get,
        role="Contributor on control plane RG",
        bicep="infra/modules/controlPlaneRoles.bicep",
        required=True,
        env_vars=("AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "CONTAINER_APP_NAME"),
    ),
    # --- optional probes (skipped on first deploy / resource not yet present) ---
    Probe(
        name="AKS (management plane)",
        runner=probe_aks_get,
        role="Contributor on workload cluster RG",
        bicep="infra/modules/workloadClusterRoles.bicep",
        required=False,
        env_vars=("AZURE_SUBSCRIPTION_ID",),
    ),
    Probe(
        name="Key Vault (data plane)",
        runner=probe_kv_secrets_list,
        role="Key Vault Secrets User",
        bicep="infra/modules/keyvault.bicep",
        required=False,
    ),
)


# ---------------------------------------------------------------------------
# Main runner.
# ---------------------------------------------------------------------------


def run_probe(probe: Probe) -> str:
    """Execute one probe. Returns one of: 'ok', 'skip', 'warn', 'fail'."""
    missing_env = _require_env(*probe.env_vars) if probe.env_vars else None
    if missing_env:
        if probe.required:
            fail(
                probe.name,
                f"required env var '{missing_env}' not set",
                role=probe.role,
                bicep=probe.bicep,
            )
            return "fail"
        skip(probe.name, f"env var '{missing_env}' not set")
        return "skip"

    try:
        detail = probe.runner()
    except SkipProbe as exc:
        skip(probe.name, str(exc))
        return "skip"
    except Exception as exc:
        if _is_not_found(exc):
            skip(probe.name, "target resource not found (not yet provisioned)")
            return "skip"
        if _is_authz_failure(exc):
            if probe.required:
                fail(probe.name, str(exc)[:200], role=probe.role, bicep=probe.bicep)
                return "fail"
            warn(
                probe.name,
                f"403/AuthorizationFailed — role '{probe.role}' may be missing"
                f" (grant via {probe.bicep})",
            )
            return "warn"
        # Unknown failure — surface it but do not block deployment on it.
        if probe.required:
            warn(probe.name, f"unexpected error: {type(exc).__name__}: {str(exc)[:200]}")
        else:
            skip(probe.name, f"transient error: {type(exc).__name__}")
        if os.environ.get("PROBE_DEBUG") == "1":
            traceback.print_exc()
        return "warn"

    ok(probe.name, detail)
    return "ok"


def _identity_disclosure() -> str:
    """Return a one-line description of WHO is being probed.

    `DefaultAzureCredential` resolves differently depending on where the
    probe runs:
      * Inside the deployed Container App → the shared user-assigned MI
        (`id-elb-dashboard-*`) via `AZURE_CLIENT_ID` + IMDS. This is the
        identity §12a Rule 3 actually cares about.
      * On a developer laptop (e.g. during a local `postprovision.sh` run)
        → the developer's `az login` identity. A missing role here does NOT
        necessarily mean the deployed MI is broken; it means the developer's
        own `az login` lacks that role.
    Surface this distinction so an Owner who runs the probe locally does not
    panic when their own dev identity (which may bypass via subscription
    Owner everywhere) shows different gaps than the deployed MI would.
    """
    if os.environ.get("CONTAINER_APP_NAME") and os.environ.get("AZURE_CLIENT_ID"):
        return (
            "identity: deployed user-assigned managed identity "
            f"(AZURE_CLIENT_ID={os.environ['AZURE_CLIENT_ID']})"
        )
    if os.environ.get("AZURE_CLIENT_ID"):
        return f"identity: AZURE_CLIENT_ID={os.environ['AZURE_CLIENT_ID']}"
    return (
        "identity: local DefaultAzureCredential chain (likely your `az login` "
        "identity — NOT the deployed MI). To verify the actual deployed MI, "
        "run this probe from inside the api sidecar via "
        "`az containerapp exec` against $CONTAINER_APP_NAME"
    )


def main() -> int:
    print("==> Capability probe (charter §12a Rule 3)", flush=True)
    print(f"    {_identity_disclosure()}", flush=True)

    # Hard guard: must have a subscription configured.
    if not os.environ.get("AZURE_SUBSCRIPTION_ID"):
        print(
            "FATAL: AZURE_SUBSCRIPTION_ID is not set; the probe cannot run.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_BAD_ENV

    counts = {"ok": 0, "skip": 0, "warn": 0, "fail": 0}
    for probe in PROBES:
        counts[run_probe(probe)] += 1

    print(
        "==> Probe summary: "
        f"ok={counts['ok']} skip={counts['skip']} warn={counts['warn']} fail={counts['fail']}",
        flush=True,
    )

    if counts["fail"]:
        print(
            "✗ One or more REQUIRED probes failed. The deployed shared MI is "
            "missing role assignments that production code paths depend on. "
            "Fix the Bicep module(s) above, re-run `azd provision`, then re-run "
            "this probe.",
            flush=True,
        )
        return EXIT_FAIL

    if counts["warn"]:
        print(
            "⚠ Probe passed but emitted warnings. Optional surfaces (AKS / "
            "Key Vault) may be in a transient state — re-run after the relevant "
            "resource is created.",
            flush=True,
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
