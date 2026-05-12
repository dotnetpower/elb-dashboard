import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchWithRetry,
  retryAfterDelayMs,
  retryDelayMs,
  shouldRetryError,
  shouldRetryStatus,
} from "./resilience";

describe("API resilience helpers", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("retries transient HTTP statuses only", () => {
    expect(shouldRetryStatus(408)).toBe(true);
    expect(shouldRetryStatus(429)).toBe(true);
    expect(shouldRetryStatus(500)).toBe(true);
    expect(shouldRetryStatus(502)).toBe(true);
    expect(shouldRetryStatus(503)).toBe(true);
    expect(shouldRetryStatus(504)).toBe(true);
    expect(shouldRetryStatus(400)).toBe(false);
    expect(shouldRetryStatus(401)).toBe(false);
    expect(shouldRetryStatus(403)).toBe(false);
    expect(shouldRetryStatus(404)).toBe(false);
  });

  it("parses Retry-After seconds and HTTP dates", () => {
    expect(retryAfterDelayMs("3", 1_000)).toBe(3_000);
    expect(
      retryAfterDelayMs(
        "Wed, 21 Oct 2030 07:28:00 GMT",
        Date.UTC(2030, 9, 21, 7, 27, 58),
      ),
    ).toBe(2_000);
    expect(retryAfterDelayMs("not-a-date", 1_000)).toBeNull();
    expect(retryAfterDelayMs(null, 1_000)).toBeNull();
  });

  it("caps Retry-After and adds deterministic jitter to exponential delays", () => {
    expect(retryDelayMs(0, "99", { maxDelayMs: 8_000 })).toBe(8_000);
    expect(retryDelayMs(2, null, { baseDelayMs: 100, random: () => 0.5 })).toBe(400);
  });

  it("retries browser network-style failures", () => {
    expect(shouldRetryError(new TypeError("Failed to fetch"))).toBe(true);
    expect(shouldRetryError(new DOMException("aborted", "AbortError"))).toBe(true);
    expect(shouldRetryError(new Error("validation failed"))).toBe(false);
  });

  it("retries transient responses and returns the recovered response", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("busy", { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    const response = await fetchWithRetry(
      "https://example.test/api",
      {},
      { baseDelayMs: 0 },
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ ok: true });
  });

  it("does not retry caller-cancelled requests", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(
      fetchWithRetry("https://example.test/api", { signal: controller.signal }),
    ).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
