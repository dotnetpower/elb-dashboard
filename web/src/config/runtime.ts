type RuntimeConfig = {
  VITE_API_BASE_URL?: string;
  VITE_AZURE_TENANT_ID?: string;
  VITE_AZURE_CLIENT_ID?: string;
  VITE_AZURE_REDIRECT_URI?: string;
  VITE_AUTH_DEV_BYPASS?: string;
};

const buildConfig: RuntimeConfig = {
  VITE_API_BASE_URL: import.meta.env.VITE_API_BASE_URL,
  VITE_AZURE_TENANT_ID: import.meta.env.VITE_AZURE_TENANT_ID,
  VITE_AZURE_CLIENT_ID: import.meta.env.VITE_AZURE_CLIENT_ID,
  VITE_AZURE_REDIRECT_URI: import.meta.env.VITE_AZURE_REDIRECT_URI,
  VITE_AUTH_DEV_BYPASS: import.meta.env.VITE_AUTH_DEV_BYPASS,
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
