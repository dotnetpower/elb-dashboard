import { msalInstance, apiLoginRequest } from "@/auth/msal";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";

async function getAccessToken(): Promise<string | null> {
  if (DEV_BYPASS) return null;
  const account = msalInstance.getActiveAccount();
  if (!account) {
    throw new Error("not signed in");
  }
  // #20: Exponential backoff retry (up to 3 attempts)
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const result = await msalInstance.acquireTokenSilent({
        ...apiLoginRequest,
        account,
      });
      return result.accessToken;
    } catch (err) {
      lastError = err;
      if (err instanceof Error && err.name === "InteractionRequiredAuthError") {
        // Redirect to login — cannot be retried silently
        await msalInstance.acquireTokenRedirect({ ...apiLoginRequest, account });
        throw err;
      }
      // Exponential backoff: 1s, 2s, 4s
      if (attempt < 2) {
        await new Promise((r) => setTimeout(r, 1000 * Math.pow(2, attempt)));
      }
    }
  }
  throw lastError;
}

export interface ApiError extends Error {
  status: number;
  body: unknown;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const token = await getAccessToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE}/api${path}`, { ...init, headers });
  // #44: Auto-handle 401 — trigger re-authentication
  if (response.status === 401 && !DEV_BYPASS) {
    const account = msalInstance.getActiveAccount();
    if (account) {
      try {
        await msalInstance.acquireTokenRedirect({ ...apiLoginRequest, account });
      } catch { /* redirect will handle it */ }
    }
    const err = new Error("Session expired. Signing in again…") as ApiError;
    err.status = 401;
    err.body = null;
    throw err;
  }
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const err = new Error(
      typeof body === "object" && body && "error" in body
        ? String((body as { error: unknown }).error)
        : `HTTP ${response.status}`,
    ) as ApiError;
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
