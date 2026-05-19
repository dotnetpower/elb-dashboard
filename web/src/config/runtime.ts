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
