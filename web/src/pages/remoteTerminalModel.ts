import { CloudCog, Key, Monitor, Network, Server, Shield } from "lucide-react";

import type { ProvisionTerminalRequest } from "@/api/endpoints";

export const TERMINAL_INSTANCE_STORAGE_KEY = "elb-terminal-instance-id";

export const VM_SIZES = [
  { value: "Standard_D2s_v5", label: "D2s v5 — 2 vCPU, 8 GB", tier: "Light" },
  { value: "Standard_D4s_v5", label: "D4s v5 — 4 vCPU, 16 GB", tier: "Recommended" },
  { value: "Standard_D8s_v5", label: "D8s v5 — 8 vCPU, 32 GB", tier: "Heavy" },
  { value: "Standard_D16s_v5", label: "D16s v5 — 16 vCPU, 64 GB", tier: "Heavy" },
  { value: "Standard_E4s_v5", label: "E4s v5 — 4 vCPU, 32 GB (memory opt)", tier: "Memory" },
  { value: "Standard_E8s_v5", label: "E8s v5 — 8 vCPU, 64 GB (memory opt)", tier: "Memory" },
] as const;

const RG_RE = /^[-\w._()]+$/;
const VM_NAME_RE = /^[a-zA-Z0-9][-a-zA-Z0-9]{0,62}[a-zA-Z0-9]?$/;
const CIDR_RE = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\/\d{1,2}$/;

export interface ValidationErrors {
  [key: string]: string;
}

export function validateTerminalForm(form: ProvisionTerminalRequest): ValidationErrors {
  const errors: ValidationErrors = {};
  if (!form.subscription_id) errors.subscription_id = "Subscription is required";
  if (!form.resource_group) errors.resource_group = "Resource group is required";
  else if (!RG_RE.test(form.resource_group)) {
    errors.resource_group = "Invalid resource group name";
  }
  if (!form.region) errors.region = "Region is required";
  if (!form.vm_name) errors.vm_name = "VM name is required";
  else if (!VM_NAME_RE.test(form.vm_name)) {
    errors.vm_name = "1-64 chars, alphanumeric + hyphens";
  }
  if (!form.vm_size) errors.vm_size = "VM size is required";
  if (!form.admin_username) errors.admin_username = "Username is required";
  else if (form.admin_username.length < 1 || form.admin_username.length > 64) {
    errors.admin_username = "1-64 characters";
  }
  if (!form.allowed_ssh_cidr) errors.allowed_ssh_cidr = "SSH CIDR is required for NSG rule";
  else if (!CIDR_RE.test(form.allowed_ssh_cidr)) {
    errors.allowed_ssh_cidr = "Must be IP/mask (e.g. 1.2.3.4/32)";
  }
  return errors;
}

export const PROVISION_STEPS = [
  { key: "rg", icon: Server, label: "Resource Group" },
  { key: "network", icon: Network, label: "Network & IP" },
  { key: "keyvault", icon: Key, label: "Key Vault" },
  { key: "password", icon: Shield, label: "Generate Password" },
  { key: "vm", icon: Monitor, label: "Create VM" },
  { key: "cloud-init", icon: CloudCog, label: "Cloud Init" },
] as const;

export function getTerminalStepIndex(
  status: string | null,
  customStatus: unknown,
): number {
  if (!status) return -1;
  if (status === "Completed") return PROVISION_STEPS.length;
  if (status === "Failed" || status === "Terminated") return -2;
  const orchestrationStatus = customStatus as { phase?: string; step?: string } | null;
  const phase = orchestrationStatus?.phase ?? orchestrationStatus?.step;
  if (phase) {
    const index = PROVISION_STEPS.findIndex((step) => step.key === phase);
    if (index >= 0) return index;
  }
  if (status === "Running" || status === "Pending") return 1;
  return 0;
}