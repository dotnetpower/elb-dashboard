import { AZURE_REGIONS } from "@/constants";

import type { ResourceConfig } from "./types";

export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const RG_RE = /^[-\w._()]+$/;
export const STORAGE_RE = /^[a-z0-9]{3,24}$/;
export const ACR_RE = /^[a-zA-Z0-9]{5,50}$/;
// VM_RE removed: there is no Terminal VM in the bundled Container Apps topology.

export interface ValidationErrors {
  [key: string]: string;
}

export function validateStep1(c: ResourceConfig): ValidationErrors {
  const e: ValidationErrors = {};
  if (!c.subscriptionId) e.subscriptionId = "Subscription ID is required";
  else if (!UUID_RE.test(c.subscriptionId))
    e.subscriptionId =
      "Must be a valid UUID (e.g. 12345678-1234-1234-1234-123456789abc)";
  return e;
}

export function validateStep2(c: ResourceConfig): ValidationErrors {
  const e: ValidationErrors = {};
  if (!c.workloadResourceGroup) e.workloadResourceGroup = "Workload RG is required";
  else if (!RG_RE.test(c.workloadResourceGroup))
    e.workloadResourceGroup =
      "Invalid name. Use letters, numbers, hyphens, underscores.";
  if (!c.acrResourceGroup) e.acrResourceGroup = "ACR RG is required";
  else if (!RG_RE.test(c.acrResourceGroup)) e.acrResourceGroup = "Invalid name";
  if (!c.region) e.region = "Primary region is required";
  // Terminal RG no longer required — the browser terminal is the in-process
  // `terminal` sidecar in the Container App, not a Linux VM. The legacy field
  // is kept on the config object only so old saved configs do not crash.
  return e;
}

export function validateStep3(c: ResourceConfig): ValidationErrors {
  const e: ValidationErrors = {};
  if (!c.storageAccountName)
    e.storageAccountName = "Storage Account name is required";
  else if (!STORAGE_RE.test(c.storageAccountName))
    e.storageAccountName = "3-24 lowercase letters and numbers only";
  if (!c.acrName) e.acrName = "Container Registry name is required";
  else if (!ACR_RE.test(c.acrName))
    e.acrName = "5-50 alphanumeric characters only";
  // Terminal VM no longer required.
  return e;
}

export const REGIONS = AZURE_REGIONS.map((r) => r.value);
