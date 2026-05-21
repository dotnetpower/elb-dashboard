import type { AksClusterSummary } from "@/api/monitoring";

type AksLifecycle = Partial<Pick<AksClusterSummary, "power_state" | "provisioning_state">>;

export function isAksProvisioned(cluster: AksLifecycle | null | undefined): boolean {
  return cluster?.provisioning_state === "Succeeded";
}

export function isAksProvisioning(cluster: AksLifecycle | null | undefined): boolean {
  const state = cluster?.provisioning_state;
  return (
    state === "Creating" ||
    state === "Starting" ||
    state === "Stopping" ||
    state === "Updating" ||
    state === "Deleting"
  );
}

export function isAksProvisioningFailed(cluster: AksLifecycle | null | undefined): boolean {
  return cluster?.provisioning_state === "Failed";
}

export function getAksProvisioningLabel(
  cluster: AksLifecycle | null | undefined,
): string | null {
  const state = cluster?.provisioning_state;
  return state && state !== "Succeeded" ? state : null;
}

export function isAksWorkloadReady(cluster: AksLifecycle | null | undefined): boolean {
  return cluster?.power_state === "Running" && isAksProvisioned(cluster);
}