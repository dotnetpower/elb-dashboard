import { msalInstance, armLoginRequest } from "@/auth/msal";
import {
  InteractionRequiredAuthError,
  BrowserAuthError,
} from "@azure/msal-browser";

export interface SubscriptionSummary {
  subscriptionId: string;
  displayName: string;
  state: string;
  tenantId: string;
}

interface ArmSubscriptionListResponse {
  value: Array<{
    subscriptionId: string;
    displayName: string;
    state: string;
    tenantId: string;
  }>;
  nextLink?: string;
}

async function getArmAccessToken(): Promise<string> {
  const account = msalInstance.getActiveAccount();
  if (!account) {
    throw new Error("not signed in");
  }
  try {
    const result = await msalInstance.acquireTokenSilent({
      ...armLoginRequest,
      account,
    });
    return result.accessToken;
  } catch (err) {
    // If another interaction is already in progress, wait and retry once.
    if (
      err instanceof BrowserAuthError &&
      (err as { errorCode?: string }).errorCode === "interaction_in_progress"
    ) {
      await new Promise((r) => setTimeout(r, 2000));
      const retry = await msalInstance.acquireTokenSilent({
        ...armLoginRequest,
        account,
      });
      return retry.accessToken;
    }
    if (err instanceof InteractionRequiredAuthError) {
      await msalInstance.acquireTokenRedirect({
        ...armLoginRequest,
        account,
      });
      throw new Error("redirecting for ARM consent");
    }
    throw err;
  }
}

/** Lists every subscription the signed-in user can read. Uses ARM directly
 *  (no backend hop) so it works before the Function App is deployed. */
export async function listSubscriptions(): Promise<SubscriptionSummary[]> {
  const token = await getArmAccessToken();
  const subs: SubscriptionSummary[] = [];
  let url: string | undefined =
    "https://management.azure.com/subscriptions?api-version=2022-12-01";
  while (url) {
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) {
      throw new Error(
        `ARM subscriptions list failed: HTTP ${resp.status} ${await resp.text()}`,
      );
    }
    const json = (await resp.json()) as ArmSubscriptionListResponse;
    for (const s of json.value) {
      subs.push({
        subscriptionId: s.subscriptionId,
        displayName: s.displayName,
        state: s.state,
        tenantId: s.tenantId,
      });
    }
    url = json.nextLink;
  }
  return subs.sort((a, b) => a.displayName.localeCompare(b.displayName));
}

interface ArmListResponse<T> {
  value: T[];
  nextLink?: string;
}

async function armPagedList<T>(initialUrl: string): Promise<T[]> {
  const token = await getArmAccessToken();
  const out: T[] = [];
  let url: string | undefined = initialUrl;
  while (url) {
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) {
      throw new Error(
        `ARM list failed: HTTP ${resp.status} ${await resp.text()}`,
      );
    }
    const json = (await resp.json()) as ArmListResponse<T>;
    out.push(...json.value);
    url = json.nextLink;
  }
  return out;
}

export interface ResourceGroupSummary {
  name: string;
  location: string;
  tags?: Record<string, string>;
}

export async function listResourceGroups(
  subscriptionId: string,
): Promise<ResourceGroupSummary[]> {
  const items = await armPagedList<{ name: string; location: string; tags?: Record<string, string> }>(
    `https://management.azure.com/subscriptions/${encodeURIComponent(subscriptionId)}/resourcegroups?api-version=2022-09-01`,
  );
  return items
    .map((g) => ({ name: g.name, location: g.location, tags: g.tags }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export interface StorageAccountSummary {
  name: string;
  location: string;
  resourceGroup: string;
}

export async function listStorageAccounts(
  subscriptionId: string,
  resourceGroup: string,
): Promise<StorageAccountSummary[]> {
  const items = await armPagedList<{ name: string; location: string; id: string }>(
    `https://management.azure.com/subscriptions/${encodeURIComponent(subscriptionId)}/resourceGroups/${encodeURIComponent(resourceGroup)}/providers/Microsoft.Storage/storageAccounts?api-version=2023-05-01`,
  );
  return items
    .map((s) => ({ name: s.name, location: s.location, resourceGroup }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export interface AcrSummary {
  name: string;
  location: string;
  loginServer: string | null;
  resourceGroup: string;
}

export async function listAcrs(
  subscriptionId: string,
  resourceGroup: string,
): Promise<AcrSummary[]> {
  const items = await armPagedList<{
    name: string;
    location: string;
    properties?: { loginServer?: string };
  }>(
    `https://management.azure.com/subscriptions/${encodeURIComponent(subscriptionId)}/resourceGroups/${encodeURIComponent(resourceGroup)}/providers/Microsoft.ContainerRegistry/registries?api-version=2023-07-01`,
  );
  return items
    .map((r) => ({
      name: r.name,
      location: r.location,
      loginServer: r.properties?.loginServer ?? null,
      resourceGroup,
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export interface VmSummary {
  name: string;
  location: string;
  resourceGroup: string;
}

export async function listVms(
  subscriptionId: string,
  resourceGroup: string,
): Promise<VmSummary[]> {
  const items = await armPagedList<{ name: string; location: string }>(
    `https://management.azure.com/subscriptions/${encodeURIComponent(subscriptionId)}/resourceGroups/${encodeURIComponent(resourceGroup)}/providers/Microsoft.Compute/virtualMachines?api-version=2024-03-01`,
  );
  return items
    .map((v) => ({ name: v.name, location: v.location, resourceGroup }))
    .sort((a, b) => a.name.localeCompare(b.name));
}
