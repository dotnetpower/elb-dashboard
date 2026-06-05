/**
 * App Insights provisioning form: shape, validation rules, and Azure constants.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Pure
 * validation/constants with no React dependency, consumed by `ProvisionForm`
 * and `defaultProvisionForm` in the panel.
 */

export type ProvisionFormState = {
  subscription_id: string;
  resource_group: string;
  component_name: string;
  region: string;
  workspace_name: string;
  workspace_resource_group: string;
  retention_days: number;
};

export const KNOWN_AZURE_REGIONS = [
  "australiaeast",
  "brazilsouth",
  "canadacentral",
  "centralindia",
  "centralus",
  "eastasia",
  "eastus",
  "eastus2",
  "francecentral",
  "germanywestcentral",
  "japaneast",
  "japanwest",
  "koreacentral",
  "northcentralus",
  "northeurope",
  "norwayeast",
  "southafricanorth",
  "southcentralus",
  "southeastasia",
  "swedencentral",
  "switzerlandnorth",
  "uaenorth",
  "uksouth",
  "ukwest",
  "westeurope",
  "westus",
  "westus2",
  "westus3",
] as const;

export const RETENTION_DAYS_OPTIONS = [
  7, 14, 30, 60, 90, 120, 180, 270, 365, 550, 730,
] as const;
export const DEFAULT_RETENTION_DAYS = 30;

const SUBSCRIPTION_GUID_RE = /^[0-9a-fA-F-]{36}$/;
const RG_NAME_RE = /^[-\w._()]{1,90}$/;
const RESOURCE_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$/;
const REGION_RE = /^[a-z][a-z0-9]{2,29}$/;

export function validateProvisionFields(
  value: ProvisionFormState,
): Partial<Record<keyof ProvisionFormState, string>> {
  const errors: Partial<Record<keyof ProvisionFormState, string>> = {};
  if (!SUBSCRIPTION_GUID_RE.test(value.subscription_id.trim())) {
    errors.subscription_id = "Must be a 36-character GUID.";
  }
  if (!RG_NAME_RE.test(value.resource_group.trim())) {
    errors.resource_group = "1–90 characters: letters, digits, '-', '.', '_', '(', ')'.";
  }
  if (!RESOURCE_NAME_RE.test(value.component_name.trim())) {
    errors.component_name = "Start with a letter or digit; up to 255 characters.";
  }
  if (!REGION_RE.test(value.region.trim())) {
    errors.region = "Use the lowercase Azure region slug, e.g. 'koreacentral'.";
  }
  if (!RESOURCE_NAME_RE.test(value.workspace_name.trim())) {
    errors.workspace_name = "Start with a letter or digit; up to 255 characters.";
  }
  const wsRg = value.workspace_resource_group.trim();
  if (wsRg && !RG_NAME_RE.test(wsRg)) {
    errors.workspace_resource_group = "1–90 characters: letters, digits, '-', '.', '_', '(', ')'.";
  }
  if (!(RETENTION_DAYS_OPTIONS as readonly number[]).includes(value.retention_days)) {
    errors.retention_days = "Pick a value from the list.";
  }
  return errors;
}

export function validateProvisionForm(
  value: ProvisionFormState,
): { ok: true } | { ok: false; message: string } {
  const errors = validateProvisionFields(value);
  if (Object.keys(errors).length === 0) return { ok: true };
  const first = Object.values(errors)[0];
  return { ok: false, message: `Fix the highlighted fields first — ${first}` };
}

/**
 * Narrow gate for the debounced existence lookup: true only when the
 * subscription / resource group / component name are individually well-formed.
 * Deliberately ignores the region / workspace fields so the lookup can fire
 * before the whole form is valid. Accepts only the three relevant fields so
 * callers keep precise React effect dependencies.
 */
export function canLookupComponent(
  value: Pick<ProvisionFormState, "subscription_id" | "resource_group" | "component_name">,
): boolean {
  return (
    SUBSCRIPTION_GUID_RE.test(value.subscription_id) &&
    RG_NAME_RE.test(value.resource_group) &&
    RESOURCE_NAME_RE.test(value.component_name)
  );
}

