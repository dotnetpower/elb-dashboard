import { api } from "@/api/client";
import type { OrchestrationStatus } from "@/api/shared";

export interface ProvisionTerminalRequest {
  subscription_id: string;
  resource_group?: string;
  region?: string;
  vm_name?: string;
  vm_size?: string;
  admin_username?: string;
  allowed_ssh_cidr: string;
  workload_resource_group?: string;
  acr_resource_group?: string;
  acr_name?: string;
  storage_account?: string;
  storage_resource_group?: string;
}

export interface ProvisionTerminalStarted {
  id: string;
  statusQueryGetUri: string;
  sendEventPostUri: string;
  terminatePostUri: string;
}

export interface TerminalConnectionInfo {
  vm_name: string;
  resource_group: string;
  subscription_id?: string;
  region: string;
  fqdn: string;
  ssh_host: string;
  ssh_port: number;
  username: string;
  password_secret_uri: string;
  cloud_init_status: string;
}

export interface TerminalHealth {
  az_cli: string;
  kubectl: string;
  azcopy: string;
  python: string;
  az_login_active: boolean;
  az_login_user: string;
  az_login_age_seconds: number;
}

export const terminalApi = {
  provision: (req: ProvisionTerminalRequest) =>
    api.post<ProvisionTerminalStarted>("/terminal/provision", req),

  status: (instanceId: string) =>
    api.get<OrchestrationStatus<TerminalConnectionInfo>>(
      `/terminal/status/${encodeURIComponent(instanceId)}`,
    ),

  password: (vmName: string, subscriptionId?: string, resourceGroup?: string) => {
    const params = new URLSearchParams();
    if (subscriptionId) params.set("subscription_id", subscriptionId);
    if (resourceGroup) params.set("resource_group", resourceGroup);
    const qs = params.toString();
    return api.get<{ vm_name: string; password: string }>(
      `/terminal/${encodeURIComponent(vmName)}/password${qs ? `?${qs}` : ""}`,
    );
  },

  openSsh: (
    vmName: string,
    callerIp: string,
    subscriptionId: string,
    resourceGroup: string,
  ) =>
    api.post<{ ok: boolean; nsg: string; allowed_ip: string }>(
      `/terminal/${encodeURIComponent(vmName)}/open-ssh?caller_ip=${encodeURIComponent(callerIp)}&subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
      {},
    ),

  stopVm: (vmName: string, subscriptionId: string, resourceGroup: string) =>
    api.post<{ ok: boolean; vm_name: string; status: string }>(
      `/terminal/${encodeURIComponent(vmName)}/stop?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
      {},
    ),

  startVm: (vmName: string, subscriptionId: string, resourceGroup: string) =>
    api.post<{ ok: boolean; vm_name: string; status: string }>(
      `/terminal/${encodeURIComponent(vmName)}/start?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
      {},
    ),

  health: (vmName: string, subscriptionId: string, resourceGroup: string) =>
    api.get<TerminalHealth>(
      `/terminal/${encodeURIComponent(vmName)}/health?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(resourceGroup)}`,
    ),
};