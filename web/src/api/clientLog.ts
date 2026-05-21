import { fetchApiRawNoRedirect } from "@/api/client";

export interface ClientLogPayload {
  level?: "error" | "warning" | "info";
  source: string;
  message: string;
  stack?: string | null;
  component_stack?: string | null;
  url?: string | null;
  user_agent?: string | null;
  request_id?: string | null;
}

const LIMITS = {
  source: 64,
  message: 1000,
  stack: 6000,
  component_stack: 6000,
  url: 2048,
  user_agent: 256,
  request_id: 64,
};

let globalHandlersInstalled = false;

function truncate(value: string | null | undefined, limit: number): string | null | undefined {
  if (value == null) return value;
  return value.length > limit ? value.slice(0, limit - 3) + "..." : value;
}

function normalisePayload(payload: ClientLogPayload): Required<ClientLogPayload> {
  return {
    level: payload.level ?? "error",
    source: truncate(payload.source || "browser", LIMITS.source) || "browser",
    message:
      truncate(payload.message || "Unknown browser error", LIMITS.message) ||
      "Unknown browser error",
    stack: truncate(payload.stack ?? null, LIMITS.stack) ?? null,
    component_stack: truncate(payload.component_stack ?? null, LIMITS.component_stack) ?? null,
    url: truncate(payload.url ?? window.location.href, LIMITS.url) ?? null,
    user_agent: truncate(payload.user_agent ?? navigator.userAgent, LIMITS.user_agent) ?? null,
    request_id: truncate(payload.request_id ?? null, LIMITS.request_id) ?? null,
  };
}

function errorLikeToMessage(value: unknown): string {
  if (value instanceof Error) return value.message || value.name;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function errorLikeToStack(value: unknown): string | null {
  return value instanceof Error ? value.stack ?? null : null;
}

export function reportClientError(payload: ClientLogPayload): void {
  const body = normalisePayload(payload);
  void fetchApiRawNoRedirect("/client-log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {
    // Logging must never create a second visible app failure.
  });
}

export function reportUnknownClientError(source: string, value: unknown): void {
  reportClientError({
    level: "error",
    source,
    message: errorLikeToMessage(value),
    stack: errorLikeToStack(value),
  });
}

export function installClientErrorHandlers(): void {
  if (globalHandlersInstalled || typeof window === "undefined") return;
  globalHandlersInstalled = true;

  window.addEventListener("error", (event) => {
    reportClientError({
      level: "error",
      source: "window.error",
      message: event.message || errorLikeToMessage(event.error),
      stack: errorLikeToStack(event.error),
      url: event.filename || window.location.href,
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    reportClientError({
      level: "error",
      source: "window.unhandledrejection",
      message: errorLikeToMessage(event.reason),
      stack: errorLikeToStack(event.reason),
    });
  });
}