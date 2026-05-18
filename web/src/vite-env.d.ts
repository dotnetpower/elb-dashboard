/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_AZURE_TENANT_ID?: string;
  readonly VITE_AZURE_CLIENT_ID?: string;
  readonly VITE_AZURE_REDIRECT_URI?: string;
  readonly VITE_AUTH_DEV_BYPASS?: string;
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
  };
}
