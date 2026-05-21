/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_AZURE_TENANT_ID?: string;
  readonly VITE_AZURE_CLIENT_ID?: string;
  readonly VITE_AZURE_REDIRECT_URI?: string;
  readonly VITE_AUTH_DEV_BYPASS?: string;
  readonly VITE_FEATURE_CUSTOM_DB?: string;
  readonly VITE_FEATURE_LAB_TOOLS?: string;
  readonly VITE_FEATURE_TERMINAL?: string;
  readonly VITE_DOCS_MOCK_PREVIEW?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  __ELB_RUNTIME_CONFIG__?: {
    VITE_API_BASE_URL?: string;
    VITE_AZURE_TENANT_ID?: string;
    VITE_AZURE_CLIENT_ID?: string;
    VITE_AZURE_REDIRECT_URI?: string;
    VITE_AUTH_DEV_BYPASS?: string;
    VITE_FEATURE_CUSTOM_DB?: string;
    VITE_FEATURE_LAB_TOOLS?: string;
    VITE_FEATURE_TERMINAL?: string;
  };
}

// Injected by vite.config.ts `define` at build time. See readme/version
// stamp section — APP_VERSION is the SemVer from web/package.json,
// APP_COMMIT is the short git SHA (or "dev"), and APP_BUILD_TIME is ISO-8601.
declare const __APP_VERSION__: string;
declare const __APP_COMMIT__: string;
declare const __APP_BUILD_TIME__: string;
