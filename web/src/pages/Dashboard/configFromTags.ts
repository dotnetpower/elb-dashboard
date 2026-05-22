import type { ResourceConfig } from "@/components/SetupWizard";

import { isAksManagedResourceGroup } from "@/lib/aksManagedRg";

/** Try to build a ResourceConfig from elb-* tags on a resource group. */
export function configFromTags(
  subscriptionId: string,
  rg: { name: string; location: string; tags?: Record<string, string> },
): ResourceConfig | null {
  // Azure creates managed infrastructure resource groups (`MC_…`, `ME_…`)
  // that can inherit deployment tags and look like ElasticBLAST workspaces.
  // They are not user-selectable workload RGs, so suppress them entirely.
  if (isAksManagedResourceGroup(rg)) return null;
  const t = rg.tags ?? {};
  // Must have at least one elb- tag to qualify
  const hasElb = Object.keys(t).some((k) => k.startsWith("elb-"));
  if (!hasElb) return null;
  return {
    subscriptionId,
    workloadResourceGroup: rg.name,
    acrResourceGroup: t["elb-acr-rg"] || rg.name,
    acrName: t["elb-acr"] || "",
    storageAccountName: t["elb-storage"] || "",
    terminalResourceGroup: t["elb-terminal-rg"] || "rg-elb-terminal",
    terminalVmName: t["elb-terminal-vm"] || "vm-elb-terminal",
    region: t["elb-region"] || rg.location || "koreacentral",
  };
}
