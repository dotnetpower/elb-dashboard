import { msalInstance, apiLoginRequest } from "@/auth/msal";
import { clearAuthSessionIssue, notifyAuthSessionIssue } from "@/auth/sessionEvents";
import { fetchWithRetry, makeRequestId } from "@/api/resilience";
import { apiBaseUrl, isDevBypassEnabled } from "@/config/runtime";
import type { AccountInfo } from "@azure/msal-browser";

const API_BASE = apiBaseUrl();
const DEV_BYPASS = isDevBypassEnabled();

interface FetchApiOptions {
  redirectOnUnauthorized?: boolean;
  /**
   * Override the default 30 s request timeout for a single call. Used by
   * genuinely long synchronous operations (e.g. deleting every shard blob of
   * a multi-thousand-file database) that would otherwise abort mid-flight.
   */
  timeoutMs?: number;
}

interface ApiTokenCacheEntry {
  accountKey: string;
  accessToken: string;
  expiresAtMs: number;
}

const ACCESS_TOKEN_REFRESH_SKEW_MS = 60_000;

let cachedApiToken: ApiTokenCacheEntry | null = null;
let apiTokenInFlight:
  | { accountKey: string; promise: Promise<ApiTokenCacheEntry> }
  | null = null;

function accountCacheKey(account: AccountInfo): string {
  return account.homeAccountId || account.localAccountId || account.username;
}

function usableCachedToken(accountKey: string): string | null {
  if (!cachedApiToken || cachedApiToken.accountKey !== accountKey) return null;
  if (cachedApiToken.expiresAtMs - ACCESS_TOKEN_REFRESH_SKEW_MS <= Date.now()) {
    cachedApiToken = null;
    return null;
  }
  return cachedApiToken.accessToken;
}

function clearApiAccessTokenCache(): void {
  cachedApiToken = null;
  apiTokenInFlight = null;
}

async function acquireFreshApiToken(account: AccountInfo): Promise<string> {
  const accountKey = accountCacheKey(account);
  if (apiTokenInFlight?.accountKey === accountKey) {
    return (await apiTokenInFlight.promise).accessToken;
  }

  const promise: Promise<ApiTokenCacheEntry> = msalInstance
    .acquireTokenSilent({
      ...apiLoginRequest,
      account,
    })
    .then((result) => {
      const expiresAtMs = result.expiresOn?.getTime() ?? 0;
      const entry: ApiTokenCacheEntry = {
        accountKey,
        accessToken: result.accessToken,
        expiresAtMs,
      };
      if (expiresAtMs - ACCESS_TOKEN_REFRESH_SKEW_MS > Date.now()) {
        cachedApiToken = entry;
      }
      // The silent refresh succeeded — the session is healthy again.
      clearAuthSessionIssue();
      return entry;
    })
    .finally(() => {
      if (apiTokenInFlight?.promise === promise) {
        apiTokenInFlight = null;
      }
    });
  apiTokenInFlight = { accountKey, promise };
  return (await promise).accessToken;
}

async function getAccessToken(options: FetchApiOptions = {}): Promise<string | null> {
  const redirectOnUnauthorized = options.redirectOnUnauthorized !== false;
  if (DEV_BYPASS) return null;
  const account = msalInstance.getActiveAccount();
  if (!account) {
    notifyAuthSessionIssue("not_signed_in");
    throw new Error("Session expired. Please sign in again.");
  }
  const accountKey = accountCacheKey(account);
  const cached = usableCachedToken(accountKey);
  if (cached) return cached;
  // #20: Exponential backoff retry (up to 3 attempts)
  let lastError: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await acquireFreshApiToken(account);
    } catch (err) {
      lastError = err;
      if (err instanceof Error && err.name === "InteractionRequiredAuthError") {
        notifyAuthSessionIssue("interaction_required");
        if (!redirectOnUnauthorized) {
          throw err;
        }
        // Redirect to login — cannot be retried silently
        await msalInstance.acquireTokenRedirect({ ...apiLoginRequest, account });
        throw err;
      }
      // Exponential backoff: 1s, 2s, 4s; capped so future retry-count changes
      // cannot strand the UI behind a many-minute silent token refresh wait.
      if (attempt < 2) {
        await new Promise((r) =>
          setTimeout(r, Math.min(1000 * Math.pow(2, attempt), 30_000)),
        );
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

async function fetchApi(
  path: string,
  init: RequestInit = {},
  options: FetchApiOptions = {},
): Promise<Response> {
  const redirectOnUnauthorized = options.redirectOnUnauthorized !== false;
  const token = await getAccessToken(options);
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
  const response = await fetchWithRetry(
    `${API_BASE}/api${path}`,
    {
      ...init,
      headers,
    },
    options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {},
  );
  // #44: Auto-handle 401 — trigger re-authentication
  if (response.status === 401 && !DEV_BYPASS) {
    clearApiAccessTokenCache();
  }
  if (response.status === 401 && !DEV_BYPASS && redirectOnUnauthorized) {
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

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: FetchApiOptions = {},
): Promise<T> {
  const response = await fetchApi(path, init, options);
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
  post: <T>(path: string, body: unknown, options?: { timeoutMs?: number }) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }, options),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

/**
 * Low-level authenticated fetch that returns the raw Response.
 * Use for cases where the caller needs direct access to status/headers/body.
 */
export const fetchApiRaw = fetchApi;

/**
 * Low-level authenticated fetch that does not trigger an MSAL redirect on 401.
 * Use inside in-page consoles where the HTTP response should be rendered rather
 * than replacing the current SPA route with an auth round-trip.
 */
export function fetchApiRawNoRedirect(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  return fetchApi(path, init, { redirectOnUnauthorized: false });
}

/**
 * Returns the current MSAL bearer token (cached when fresh, silently refreshed
 * otherwise). Returns `null` in dev-bypass mode where no token is attached.
 *
 * Exposed for tooling that needs the same Authorization header the dashboard
 * itself would send — currently the API Reference "Copy curl" button. Do not
 * use this for normal API calls; route those through `api.*` / `fetchApiRaw*`
 * so the standard 401-handling / retry / refresh paths apply.
 */
export function getApiAccessToken(): Promise<string | null> {
  return getAccessToken({ redirectOnUnauthorized: false });
}

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

// One actionable English hint per /v1/ready upstream_code returned by the
// sibling elastic-blast-azure OpenAPI service. Keep in sync with
// docker-openapi/app/main.py::v1_ready and api/services/blast/submit_gates.py
// ::_openapi_action_for_code.
const OPENAPI_UPSTREAM_HINTS: Record<string, string> = {
  k8s_unreachable: "Start the AKS cluster and try again.",
  no_workload_nodes:
    "Scale up the BLAST workload pool — the cluster has zero Ready nodes.",
  openapi_pod_not_ready: "Restart the elb-openapi pod and try again.",
  workload_pool_check_failed: "Check AKS health in the Azure portal.",
  openapi_pod_check_failed: "Check AKS health in the Azure portal.",
};

interface OpenApiUpstreamCode {
  code: string;
  upstream_code?: string;
}

function openApiUpstreamCode(body: unknown): OpenApiUpstreamCode | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const code = (detail as { code?: unknown }).code;
  if (typeof code !== "string") return null;
  if (code !== "openapi_not_ready" && code !== "openapi_unreachable") return null;
  const upstream = (detail as { upstream_code?: unknown }).upstream_code;
  return {
    code,
    upstream_code: typeof upstream === "string" ? upstream : undefined,
  };
}

interface PreflightBlockingGate {
  id?: string;
  error_code?: string;
  message?: string;
  action?: string | null;
  action_type?: string | null;
}

/**
 * Render the SPA copy for a 409 ``blocked_by_preflight`` response.
 *
 * Returns ``null`` when the body is not a preflight envelope so the caller
 * can fall back to the generic 4xx rendering. Each blocking gate already
 * carries the upstream-derived ``action`` string (computed once by the
 * backend gate), so the SPA just stitches messages + actions together
 * instead of re-running ``OPENAPI_UPSTREAM_HINTS`` lookups.
 */
function blockedByPreflightMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const code = (detail as { code?: unknown }).code;
  if (code !== "blocked_by_preflight") return null;
  const blocking = (detail as { blocking_gates?: unknown }).blocking_gates;
  if (!Array.isArray(blocking) || blocking.length === 0) return null;
  const parts: string[] = [];
  for (const raw of blocking as PreflightBlockingGate[]) {
    if (!raw || typeof raw !== "object") continue;
    const msg = typeof raw.message === "string" && raw.message.trim() ? raw.message.trim() : "";
    const action = typeof raw.action === "string" && raw.action.trim() ? raw.action.trim() : "";
    if (!msg && !action) continue;
    if (msg && action) {
      parts.push(`${msg} — ${action}.`);
    } else if (msg) {
      parts.push(msg);
    } else {
      parts.push(`${action}.`);
    }
  }
  if (!parts.length) return null;
  return `Pre-flight check failed: ${parts.join(" ")}`;
}

/**
 * Format a caught error into a user-friendly message.
 * For 403 errors, adds guidance about the required RBAC role.
 */
export function formatApiError(err: unknown, context?: string): string {
  if (!(err instanceof Error)) return sanitiseUserFacingMessage(String(err));
  const apiErr = err as Partial<ApiError>;
  const base = err.message || "Unknown error";
  const structuredMessage = apiErrorMessage(apiErr.body);

  // C2 — proactively strip query strings, SAS signatures, bearer tokens, and
  // long base64 blobs from anything we are about to render. The server-side
  // `sanitise` helper covers structured payloads, but error.message strings
  // (especially from network/timeout paths) frequently bypass it.
  const sanitiseAndFormat = (s: string | null | undefined) =>
    s ? sanitiseUserFacingMessage(s) : s;

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
  if (apiErr.status === 409) {
    // Submit pre-flight failure: render the blocking gate messages + the
    // remediation hint the gate already computed (so we don't double-map
    // upstream_code in the SPA). See api/services/blast/submit_gates.py.
    const preflight = blockedByPreflightMessage(apiErr.body);
    if (preflight) return preflight;
    return sanitiseAndFormat(structuredMessage ?? base) ?? base;
  }
  if (apiErr.status === 400 || apiErr.status === 422) {
    return sanitiseAndFormat(structuredMessage ?? base) ?? base;
  }
  if (apiErr.status === 500) {
    // Hide internal details; show a clean message with the original reason if short enough
    const clean = base
      .replace(/^HTTP 500:\s*/, "")
      .replace(/Traceback.*$/s, "")
      .trim();
    return clean.length > 200
      ? "An internal error occurred. Please try again or check Azure Portal for details."
      : (sanitiseUserFacingMessage(clean) ?? clean);
  }
  if (apiErr.status === 503) {
    // Surface the structured "lab_tool_backend_pending" code cleanly so the UI
    // shows "Backend not implemented yet" instead of a generic 503 string.
    const body = apiErr.body as { detail?: { code?: string; message?: string } } | undefined;
    const detail = body?.detail;
    if (detail?.code === "lab_tool_backend_pending") {
      return (
        detail.message ||
        "This Lab Tool route has no backend implementation in this build yet."
      );
    }
    // Map structured /v1/ready upstream codes to actionable English copy.
    // These detail shapes come from api/services/external_blast.ready() and
    // are echoed by the openapi_ready submit gate.
    const openapiCode = openApiUpstreamCode(apiErr.body);
    const upstream = openapiCode?.upstream_code ?? "";
    if (openapiCode?.code === "openapi_not_ready") {
      const action = OPENAPI_UPSTREAM_HINTS[upstream] ?? "Check AKS cluster health and try again.";
      return `BLAST API is not ready (${upstream || "unknown_cause"}). ${action}`;
    }
    if (openapiCode?.code === "openapi_unreachable") {
      return "Cannot reach the BLAST API service. Make sure the AKS cluster is running and the elb-openapi pod is healthy.";
    }
    if (structuredMessage) return sanitiseUserFacingMessage(structuredMessage) ?? structuredMessage;
    return "Service temporarily unavailable. The Function App may be starting up — try again in a moment.";
  }
  if (apiErr.status === 429) {
    const body = apiErr.body as { detail?: { code?: string; limit_per_minute?: number } } | undefined;
    if (body?.detail?.code === "openapi_ready_rate_limited") {
      const limit = body.detail.limit_per_minute;
      return limit
        ? `Readiness probe rate-limit hit (max ${limit}/min). Wait about a minute and try again.`
        : "Readiness probe rate-limit hit. Wait about a minute and try again.";
    }
    return "Rate limit hit. Wait a moment and try again.";
  }
  // Network errors — distinguish abort/timeout from offline so the user sees
  // an actionable hint rather than a generic "Failed to fetch".
  if (
    err.name === "AbortError" ||
    base.toLowerCase().includes("aborted") ||
    base.toLowerCase().includes("timeout")
  ) {
    return "Request timed out. The backend is taking longer than expected — check the AKS cluster and try again.";
  }
  if (base.includes("Failed to fetch") || base.includes("NetworkError")) {
    return "Network error — check your internet connection or try again.";
  }
  return sanitiseUserFacingMessage(base) ?? base;
}

/**
 * C2 — last-line scrub for any string we are about to render as an error.
 *
 * Defence-in-depth against the server-side sanitiser missing something: strip
 * Azure SAS query strings, bearer tokens, subscription/tenant GUIDs, and
 * obvious base64 blobs. Also collapses Azure SDK-style error prefixes like
 * "(ResourceNotFound) The resource …" into a cleaner sentence.
 */
function sanitiseUserFacingMessage(message: string): string {
  let out = message;

  // Strip "?sig=..." style SAS query suffixes regardless of leading URL shape.
  out = out.replace(/\?[A-Za-z0-9%=&_\-+.]*\bsig=[^&\s"']+[^"'\s]*/gi, "?<sas-redacted>");
  out = out.replace(/\bsig=[A-Za-z0-9%+/=]{20,}/gi, "sig=<redacted>");

  // Strip bearer / shared-key style headers if they ever leak into error text.
  out = out.replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer <redacted>");
  out = out.replace(/\bSharedKey\s+[A-Za-z0-9+/=:]+/gi, "SharedKey <redacted>");

  // Subscription / tenant / object id GUIDs — never useful in the UI.
  out = out.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, "<id-redacted>");

  // Azure SDK "(ResourceNotFound) The resource '/subscriptions/…' was not found." →
  // "Resource not found." This is purely cosmetic but covers a common case
  // where the SDK pastes the full ARM path back at the user.
  out = out.replace(/^\(([A-Za-z]+)\)\s*/, (_, code: string) => `${humaniseAzureCode(code)} — `);

  // Collapse runs of whitespace introduced by the substitutions above.
  out = out.replace(/\s{2,}/g, " ").trim();
  return out;
}

function humaniseAzureCode(code: string): string {
  // Insert spaces before capitals: "ResourceNotFound" → "Resource Not Found".
  return code.replace(/([a-z])([A-Z])/g, "$1 $2");
}

function apiErrorMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  if ("message" in body && typeof body.message === "string") {
    return body.message;
  }
  if ("detail" in body) {
    const detail = body.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      if ("message" in detail && typeof detail.message === "string") {
        return detail.message;
      }
      if ("code" in detail && typeof detail.code === "string") {
        return detail.code;
      }
    }
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          if (!item || typeof item !== "object") return null;
          const msg = "msg" in item && typeof item.msg === "string" ? item.msg : null;
          const loc =
            "loc" in item && Array.isArray(item.loc) ? item.loc.join(".") : null;
          return msg ? (loc ? `${loc}: ${msg}` : msg) : null;
        })
        .filter((item): item is string => Boolean(item));
      if (messages.length > 0) return messages.join("; ");
    }
  }
  return null;
}

/** Check if an error is a 403 Forbidden. */
export function isForbidden(err: unknown): boolean {
  return err instanceof Error && (err as Partial<ApiError>).status === 403;
}
