import { msalInstance, apiLoginRequest } from "@/auth/msal";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";

async function getAccessToken(): Promise<string | null> {
  if (DEV_BYPASS) return null;
  const account = msalInstance.getActiveAccount();
  if (!account) {
    throw new Error("not signed in");
  }
  try {
    const result = await msalInstance.acquireTokenSilent({
      ...apiLoginRequest,
      account,
    });
    return result.accessToken;
  } catch {
    await new Promise((r) => setTimeout(r, 1500));
    const result = await msalInstance.acquireTokenSilent({
      ...apiLoginRequest,
      account,
    });
    return result.accessToken;
  }
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
