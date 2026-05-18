import { isDevBypassEnabled } from "@/config/runtime";

export const STORAGE_KEY = "elb-resource-config";
export const DEV_BYPASS = isDevBypassEnabled();

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
