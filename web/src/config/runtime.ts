type RuntimeConfig = {
  VITE_API_BASE_URL?: string;
  VITE_AZURE_TENANT_ID?: string;
  VITE_AZURE_CLIENT_ID?: string;
  VITE_AZURE_REDIRECT_URI?: string;
  VITE_AUTH_DEV_BYPASS?: string;
  VITE_FEATURE_CUSTOM_DB?: string;
  VITE_FEATURE_LAB_TOOLS?: string;
  VITE_FEATURE_TERMINAL?: string;
};

export type FeatureFlag = "customDb" | "labTools" | "terminal";

const FEATURE_FLAG_KEYS: Record<FeatureFlag, keyof RuntimeConfig> = {
  customDb: "VITE_FEATURE_CUSTOM_DB",
  labTools: "VITE_FEATURE_LAB_TOOLS",
  terminal: "VITE_FEATURE_TERMINAL",
};

const buildConfig: RuntimeConfig = {
  VITE_API_BASE_URL: import.meta.env.VITE_API_BASE_URL,
  VITE_AZURE_TENANT_ID: import.meta.env.VITE_AZURE_TENANT_ID,
  VITE_AZURE_CLIENT_ID: import.meta.env.VITE_AZURE_CLIENT_ID,
  VITE_AZURE_REDIRECT_URI: import.meta.env.VITE_AZURE_REDIRECT_URI,
  VITE_AUTH_DEV_BYPASS: import.meta.env.VITE_AUTH_DEV_BYPASS,
  VITE_FEATURE_CUSTOM_DB: import.meta.env.VITE_FEATURE_CUSTOM_DB,
  VITE_FEATURE_LAB_TOOLS: import.meta.env.VITE_FEATURE_LAB_TOOLS,
  VITE_FEATURE_TERMINAL: import.meta.env.VITE_FEATURE_TERMINAL,
};

function runtimeConfig(): RuntimeConfig {
  if (typeof window === "undefined") return {};
  return window.__ELB_RUNTIME_CONFIG__ ?? {};
}

export function configValue(key: keyof RuntimeConfig, fallback = ""): string {
  const runtimeValue = runtimeConfig()[key]?.trim();
  if (runtimeValue) return runtimeValue;
  const buildValue = buildConfig[key]?.trim();
  if (buildValue) return buildValue;
  return fallback;
}

export function isDevBypassEnabled(): boolean {
  return configValue("VITE_AUTH_DEV_BYPASS") === "true";
}

export function apiBaseUrl(): string {
  return configValue("VITE_API_BASE_URL");
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ALL_ZERO_UUID = "00000000-0000-0000-0000-000000000000";

/** A clientId is usable only if it is a valid UUID and not the all-zero
 *  placeholder shipped in `.env.example`. AAD returns AADSTS700038 if the
 *  placeholder slips through, so reject it client-side before MSAL is built. */
export function isUsableClientId(value: string | undefined | null): boolean {
  const v = (value ?? "").trim().toLowerCase();
  if (!v) return false;
  if (v === ALL_ZERO_UUID) return false;
  return UUID_RE.test(v);
}

export function azureClientId(): string {
  const v = configValue("VITE_AZURE_CLIENT_ID");
  return isUsableClientId(v) ? v : "";
}

export function parseFeatureFlag(value: string | undefined, fallback = true): boolean {
  const normalized = value?.trim().toLowerCase();
  if (!normalized) return fallback;
  if (["0", "false", "no", "off", "disabled"].includes(normalized)) return false;
  if (["1", "true", "yes", "on", "enabled"].includes(normalized)) return true;
  return fallback;
}

export function isFeatureEnabled(flag: FeatureFlag): boolean {
  return parseFeatureFlag(configValue(FEATURE_FLAG_KEYS[flag]), true);
}
