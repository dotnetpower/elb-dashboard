import { msalInstance, apiLoginRequest } from "@/auth/msal";
import { notifyAuthSessionIssue } from "@/auth/sessionEvents";
import { fetchWithRetry, makeRequestId } from "@/api/resilience";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";

async function getAccessToken(): Promise<string | null> {
  if (DEV_BYPASS) return null;
  const account = msalInstance.getActiveAccount();
  if (!account) {
    notifyAuthSessionIssue("not_signed_in");
    throw new Error("Session expired. Please sign in again.");
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
        notifyAuthSessionIssue("interaction_required");
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
  notifyAuthSessionIssue("token_refresh_failed");
  throw lastError;
}

export interface ApiError extends Error {
  status: number;
  body: unknown;
}

export interface ApiTextResponse {
  text: string;
  contentType: string;
  filename: string | null;
}

async function fetchApi(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await getAccessToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!headers.has("x-client-request-id")) {
    headers.set("x-client-request-id", makeRequestId());
  }
  const response = await fetchWithRetry(`${API_BASE}/api${path}`, {
    ...init,
    headers,
  });
  // #44: Auto-handle 401 — trigger re-authentication
  if (response.status === 401 && !DEV_BYPASS) {
    notifyAuthSessionIssue("api_unauthorized");
    const account = msalInstance.getActiveAccount();
    if (account) {
      try {
        await msalInstance.acquireTokenRedirect({ ...apiLoginRequest, account });
      } catch {
        /* redirect will handle it */
      }
    }
    const err = new Error("Session expired. Signing in again…") as ApiError;
    err.status = 401;
    err.body = null;
    throw err;
  }
  return response;
}

function parseBody(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function filenameFromDisposition(value: string | null): string | null {
  if (!value) return null;
  const match = value.match(/filename\*?=(?:UTF-8''|\")?([^";]+)/i);
  if (!match) return null;
  return decodeURIComponent(match[1].replace(/"$/, "").trim());
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchApi(path, init);
  const text = await response.text();
  const body = parseBody(text);
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

async function requestText(
  path: string,
  init: RequestInit = {},
): Promise<ApiTextResponse> {
  const response = await fetchApi(path, init);
  const text = await response.text();
  if (!response.ok) {
    const body = parseBody(text);
    const err = new Error(
      typeof body === "object" && body && "error" in body
        ? String((body as { error: unknown }).error)
        : `HTTP ${response.status}`,
    ) as ApiError;
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return {
    text,
    contentType: response.headers.get("Content-Type") ?? "text/plain;charset=utf-8",
    filename: filenameFromDisposition(response.headers.get("Content-Disposition")),
  };
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  getText: (path: string) => requestText(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

/**
 * Low-level authenticated fetch that returns the raw Response.
 * Use for cases where the caller needs direct access to status/headers/body.
 */
export const fetchApiRaw = fetchApi;

// ---------------------------------------------------------------------------
// RBAC-friendly error formatting
// ---------------------------------------------------------------------------
const RBAC_HINTS: Record<string, string> = {
  aks: "Contributor on the workload resource group",
  acr: "Contributor on the ACR resource group",
  storage: "Storage Blob Data Contributor on the storage account",
  terminal: "Contributor on the terminal resource group",
  blast: "Contributor on the workload resource group",
  default: "appropriate RBAC role on the target resource",
};

/**
 * Format a caught error into a user-friendly message.
 * For 403 errors, adds guidance about the required RBAC role.
 */
export function formatApiError(err: unknown, context?: string): string {
  if (!(err instanceof Error)) return String(err);
  const apiErr = err as Partial<ApiError>;
  const base = err.message || "Unknown error";

  if (apiErr.status === 403) {
    const hint = (context && RBAC_HINTS[context]) || RBAC_HINTS["default"];
    return `Permission denied — you need ${hint}. Ask your Azure admin to assign the role.`;
  }
  if (apiErr.status === 401) {
    return "Session expired. Please sign in again.";
  }
  if (apiErr.status === 404) {
    return "Resource not found. It may have been deleted or not yet created.";
  }
  if (apiErr.status === 500) {
    // Hide internal details; show a clean message with the original reason if short enough
    const clean = base
      .replace(/^HTTP 500:\s*/, "")
      .replace(/Traceback.*$/s, "")
      .trim();
    return clean.length > 200
      ? "An internal error occurred. Please try again or check Azure Portal for details."
      : clean;
  }
  if (apiErr.status === 503) {
    return "Service temporarily unavailable. The Function App may be starting up — try again in a moment.";
  }
  // Network errors
  if (base.includes("Failed to fetch") || base.includes("NetworkError")) {
    return "Network error — check your internet connection or try again.";
  }
  return base;
}

/** Check if an error is a 403 Forbidden. */
export function isForbidden(err: unknown): boolean {
  return err instanceof Error && (err as Partial<ApiError>).status === 403;
}
