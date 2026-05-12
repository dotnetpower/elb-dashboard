export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
export const DEFAULT_MAX_ATTEMPTS = 3;

const RETRYABLE_STATUS_CODES = new Set([408, 429, 500, 502, 503, 504]);

export interface FetchWithRetryOptions {
  timeoutMs?: number;
  maxAttempts?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  random?: () => number;
}

export function makeRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function shouldRetryStatus(status: number): boolean {
  return RETRYABLE_STATUS_CODES.has(status);
}

export function shouldRetryError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === "TypeError" || error.name === "AbortError";
}

export function retryAfterDelayMs(value: string | null, now = Date.now()): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const dateMs = Date.parse(value);
  if (!Number.isNaN(dateMs)) return Math.max(0, dateMs - now);
  return null;
}

export function retryDelayMs(
  attemptIndex: number,
  retryAfter: string | null,
  options: Pick<FetchWithRetryOptions, "baseDelayMs" | "maxDelayMs" | "random"> = {},
): number {
  const retryAfterMs = retryAfterDelayMs(retryAfter);
  if (retryAfterMs !== null) return Math.min(retryAfterMs, options.maxDelayMs ?? 8_000);
  const baseDelayMs = options.baseDelayMs ?? 300;
  const maxDelayMs = options.maxDelayMs ?? 8_000;
  const jitter = 0.75 + (options.random?.() ?? Math.random()) * 0.5;
  return Math.min(Math.round(baseDelayMs * 2 ** attemptIndex * jitter), maxDelayMs);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

function createTimeoutSignal(signal: AbortSignal | undefined, timeoutMs: number) {
  const controller = new AbortController();
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  const abortFromCaller = () => controller.abort(signal?.reason);
  if (signal?.aborted) {
    abortFromCaller();
  } else {
    signal?.addEventListener("abort", abortFromCaller, { once: true });
    timeoutId = globalThis.setTimeout(
      () => controller.abort(new Error("Request timed out")),
      timeoutMs,
    );
  }

  return {
    signal: controller.signal,
    cleanup: () => {
      if (timeoutId !== undefined) globalThis.clearTimeout(timeoutId);
      signal?.removeEventListener("abort", abortFromCaller);
    },
  };
}

export async function fetchWithRetry(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: FetchWithRetryOptions = {},
): Promise<Response> {
  if (init.signal?.aborted) {
    throw init.signal.reason instanceof Error
      ? init.signal.reason
      : new DOMException("The operation was aborted.", "AbortError");
  }

  const maxAttempts = Math.max(1, options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS);
  const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  let lastError: unknown;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const timeout = createTimeoutSignal(init.signal ?? undefined, timeoutMs);
    try {
      const response = await fetch(input, { ...init, signal: timeout.signal });
      if (!shouldRetryStatus(response.status) || attempt === maxAttempts - 1) {
        return response;
      }
      timeout.cleanup();
      await sleep(retryDelayMs(attempt, response.headers.get("Retry-After"), options));
    } catch (error) {
      lastError = error;
      if (
        init.signal?.aborted ||
        !shouldRetryError(error) ||
        attempt === maxAttempts - 1
      ) {
        throw error;
      }
      timeout.cleanup();
      await sleep(retryDelayMs(attempt, null, options));
    } finally {
      timeout.cleanup();
    }
  }

  throw lastError instanceof Error ? lastError : new Error("Request failed");
}
