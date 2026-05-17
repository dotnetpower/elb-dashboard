export const STORAGE_KEY = "elb-resource-config";
export const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";

export interface ResourceConfig {
  subscriptionId: string;
  workloadResourceGroup: string;
  acrResourceGroup: string;
  acrName: string;
  storageAccountName: string;
  terminalResourceGroup: string;
  terminalVmName: string;
  region: string;
}

export const DEFAULTS: ResourceConfig = {
  subscriptionId: "",
  workloadResourceGroup: "",
  acrResourceGroup: "",
  acrName: "",
  storageAccountName: "",
  terminalResourceGroup: "rg-elb-terminal",
  terminalVmName: "vm-elb-terminal",
  region: "koreacentral",
};

export type Step = 1 | 2 | 3 | 4;
