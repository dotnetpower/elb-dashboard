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
