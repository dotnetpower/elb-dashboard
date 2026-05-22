import { Configuration, PublicClientApplication } from "@azure/msal-browser";

import { azureClientId, configValue } from "@/config/runtime";

const tenantId = configValue("VITE_AZURE_TENANT_ID", "common");
const clientId = azureClientId();
// Resolve redirect URI at runtime — never bake in localhost for production.
// .env.production sets VITE_AZURE_REDIRECT_URI=__RUNTIME__ to override .env.local.
// Any non-URL value (empty, undefined, __RUNTIME__) falls back to the browser origin.
function resolveRedirectUri(): string {
  const env = configValue("VITE_AZURE_REDIRECT_URI");
  if (typeof env === "string" && env.startsWith("http")) return env;
  if (typeof window !== "undefined") return window.location.origin;
  return "http://localhost:8090";
}
const redirectUri = resolveRedirectUri();

if (!clientId) {
  console.warn(
    "VITE_AZURE_CLIENT_ID is empty or a placeholder — MSAL will not initialise. " +
      "Configure web/.env.local with the Entra App Registration clientId, or run `azd up` " +
      "and re-run `scripts/dev/local-run.sh web` (it pulls API_CLIENT_ID from azd env).",
  );
}

export const msalConfig: Configuration = {
  auth: {
    clientId,
    authority: `https://login.microsoftonline.com/${tenantId}`,
    redirectUri,
    postLogoutRedirectUri: redirectUri,
    navigateToLoginRequestUrl: true,
  },
  cache: {
    cacheLocation: "sessionStorage",
  },
};

/** Scope for talking to our own Function App API.
 *  The App Registration must expose a scope named `user_impersonation`. */
export const apiLoginRequest = {
  scopes: [`api://${clientId}/user_impersonation`],
};

/** Scope for downstream ARM calls — the same access token is forwarded by
 *  the backend via OBO. */
export const armLoginRequest = {
  scopes: ["https://management.azure.com/user_impersonation"],
};

export const msalInstance = new PublicClientApplication(msalConfig);
