import { api } from "@/api/client";

export interface ProvisionTerminalRequest {
  subscription_id: string;
  resource_group?: string;
  region?: string;
  vm_name?: string;
  vm_size?: string;
  admin_username?: string;
  allowed_ssh_cidr: string;
}

export interface ProvisionTerminalStarted {
  id: string;
  statusQueryGetUri: string;
  sendEventPostUri: string;
  terminatePostUri: string;
}

export interface OrchestrationStatus<TOutput = unknown> {
  instance_id: string;
  runtime_status: string;
  custom_status: unknown;
  created_time: string;
  last_updated_time: string;
  output: TOutput | null;
}

export interface TerminalConnectionInfo {
  vm_name: string;
  resource_group: string;
  region: string;
  fqdn: string;
  ssh_host: string;
  ssh_port: number;
  username: string;
  password_secret_uri: string;
  cloud_init_status: string;
}

export const terminalApi = {
  provision: (req: ProvisionTerminalRequest) =>
    api.post<ProvisionTerminalStarted>("/terminal/provision", req),
  status: (instanceId: string) =>
    api.get<OrchestrationStatus<TerminalConnectionInfo>>(
      `/terminal/status/${instanceId}`,
    ),
  password: (vmName: string) =>
    api.get<{ vm_name: string; password: string }>(
      `/terminal/${encodeURIComponent(vmName)}/password`,
    ),
};

export interface AksClusterSummary {
  name: string;
  resource_group: string;
  region: string;
  k8s_version: string | null;
  provisioning_state: string | null;
  power_state: string | null;
  node_count: number | null;
  node_sku: string | null;
  kubelet_object_id: string | null;
}

export interface StorageSummary {
  name: string;
  region: string;
  sku: string | null;
  kind: string | null;
  public_network_access: string | null;
  is_hns_enabled: boolean | null;
  containers: { name: string; public_access: string | null; last_modified_time: string | null }[];
}

export interface AcrSummary {
  name: string;
  login_server: string;
  sku: string | null;
  expected_image_tags: Record<string, string>;
}

export interface VmStatus {
  name: string;
  region: string;
  vm_size: string | null;
  provisioning_state: string | null;
  power_state: string | null;
}

export const monitoringApi = {
  aks: (subscriptionId: string, rg: string) =>
    api.get<{ clusters: AksClusterSummary[] }>(
      `/monitor/aks?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}`,
    ),
  storage: (subscriptionId: string, rg: string, accountName: string) =>
    api.get<StorageSummary>(
      `/monitor/storage?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&account_name=${encodeURIComponent(accountName)}`,
    ),
  acr: (subscriptionId: string, rg: string, registryName: string) =>
    api.get<AcrSummary>(
      `/monitor/acr?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&registry_name=${encodeURIComponent(registryName)}`,
    ),
  terminal: (subscriptionId: string, rg: string, vmName: string) =>
    api.get<VmStatus>(
      `/monitor/terminal?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&vm_name=${encodeURIComponent(vmName)}`,
    ),
};
