import { Configuration, PublicClientApplication } from "@azure/msal-browser";

const tenantId = import.meta.env.VITE_AZURE_TENANT_ID ?? "common";
const clientId = import.meta.env.VITE_AZURE_CLIENT_ID ?? "";
// Resolve redirect URI at runtime — never bake in localhost for production.
// .env.production sets VITE_AZURE_REDIRECT_URI=__RUNTIME__ to override .env.local.
// Any non-URL value (empty, undefined, __RUNTIME__) falls back to window.location.origin.
function resolveRedirectUri(): string {
  const env = import.meta.env.VITE_AZURE_REDIRECT_URI;
  if (typeof env === "string" && env.startsWith("http")) return env;
  return window.location.origin;
}
const redirectUri = resolveRedirectUri();

if (!clientId) {
  // eslint-disable-next-line no-console
  console.warn(
    "VITE_AZURE_CLIENT_ID is not set — MSAL will fail to initialise. Configure web/.env.local.",
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
  scopes: [
    `api://${clientId}/user_impersonation`,
  ],
};

/** Scope for downstream ARM calls — the same access token is forwarded by
 *  the backend via OBO. */
export const armLoginRequest = {
  scopes: ["https://management.azure.com/user_impersonation"],
};

export const msalInstance = new PublicClientApplication(msalConfig);
