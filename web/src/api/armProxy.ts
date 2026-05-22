import { api } from "@/api/client";

export interface ArmSubscription {
  subscriptionId: string;
  displayName: string;
  state: string;
  tenantId: string;
}

export interface ArmResourceGroup {
  name: string;
  location: string;
  tags?: Record<string, string>;
}

export interface ArmStorageAccount {
  name: string;
  location: string;
  isHnsEnabled?: boolean | null;
}

export interface ArmAcr {
  name: string;
  location: string;
  loginServer?: string;
}

export interface ArmVm {
  name: string;
  location: string;
}

export interface ArmLocation {
  /** ARM region id, e.g. `koreacentral`. */
  name: string;
  /** Friendly name, e.g. `Korea Central`. */
  displayName: string;
  /** Regional bucket, e.g. `(Asia Pacific) Korea Central`. */
  regionalDisplayName: string;
}

export const armProxyApi = {
  listSubscriptions: () => api.get<ArmSubscription[]>("/arm/subscriptions"),

  listResourceGroups: (subscriptionId: string) =>
    api.get<ArmResourceGroup[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups`,
    ),

  listLocations: (subscriptionId: string) =>
    api.get<ArmLocation[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/locations`,
    ),

  listStorageAccounts: (subscriptionId: string, rg: string) =>
    api.get<ArmStorageAccount[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups/${encodeURIComponent(rg)}/storage-accounts`,
    ),

  listAcrs: (subscriptionId: string, rg: string) =>
    api.get<ArmAcr[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups/${encodeURIComponent(rg)}/acrs`,
    ),

  listVms: (subscriptionId: string, rg: string) =>
    api.get<ArmVm[]>(
      `/arm/subscriptions/${encodeURIComponent(subscriptionId)}/resource-groups/${encodeURIComponent(rg)}/vms`,
    ),

  getRgTags: (subscriptionId: string, rg: string) =>
    api.get<{ resource_group: string; tags: Record<string, string> }>(
      `/arm/resource-group/tags?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}`,
    ),

  setRgTags: (subscriptionId: string, rg: string, tags: Record<string, string>) =>
    api.post<{ resource_group: string; tags: Record<string, string> }>(
      "/arm/resource-group/tags",
      { subscription_id: subscriptionId, resource_group: rg, tags },
    ),
};