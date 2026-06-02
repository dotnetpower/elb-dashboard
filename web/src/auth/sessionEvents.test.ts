import { afterEach, beforeEach, describe, expect, it } from "vitest";

// The session-issue store dispatches `CustomEvent`s on `window`. The default
// vitest environment is `node`, which has neither `window` nor (on older
// runtimes) `CustomEvent`, so we install tiny stand-ins here instead of
// pulling in `jsdom` purely for this file. The store reads `window` lazily
// inside each function, so installing the shim before the first call is
// enough.
class MemoryWindow extends EventTarget {}

interface CustomEventLike<T> {
  type: string;
  detail: T;
}

beforeEach(() => {
  (globalThis as { window?: unknown }).window = new MemoryWindow();
  if (typeof (globalThis as { CustomEvent?: unknown }).CustomEvent === "undefined") {
    (globalThis as { CustomEvent?: unknown }).CustomEvent = class<T> extends Event {
      detail: T;
      constructor(type: string, init?: { detail?: T }) {
        super(type);
        this.detail = init?.detail as T;
      }
    };
  }
});

afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
});

// ESM hoists these imports above the shim setup, but that is fine: the module
// only touches `window` lazily inside each function, which we call from the
// tests after the shim is installed.
import {
  clearAuthSessionIssue,
  getAuthSessionIssue,
  notifyAuthSessionIssue,
  subscribeAuthSessionIssues,
  type AuthSessionIssue,
} from "./sessionEvents";

describe("auth session issue store", () => {
  beforeEach(() => {
    // Reset the module-level store between tests.
    clearAuthSessionIssue();
  });

  it("starts with no active issue", () => {
    expect(getAuthSessionIssue()).toBeNull();
  });

  it("records the latest issue with a default message", () => {
    notifyAuthSessionIssue("token_refresh_failed");
    const issue = getAuthSessionIssue();
    expect(issue?.reason).toBe("token_refresh_failed");
    expect(issue?.message).toMatch(/sign in again/i);
  });

  it("keeps a custom message when provided", () => {
    notifyAuthSessionIssue("arm_unauthorized", "Custom expiry text");
    expect(getAuthSessionIssue()).toEqual<AuthSessionIssue>({
      reason: "arm_unauthorized",
      message: "Custom expiry text",
    });
  });

  it("clears the issue once a fresh token is acquired", () => {
    notifyAuthSessionIssue("api_unauthorized");
    expect(getAuthSessionIssue()).not.toBeNull();
    clearAuthSessionIssue();
    expect(getAuthSessionIssue()).toBeNull();
  });

  it("notifies CustomEvent subscribers on every issue", () => {
    const seen: AuthSessionIssue[] = [];
    const unsubscribe = subscribeAuthSessionIssues((issue) => seen.push(issue));
    notifyAuthSessionIssue("interaction_required");
    notifyAuthSessionIssue("not_signed_in", "Please sign in");
    unsubscribe();
    notifyAuthSessionIssue("token_refresh_failed");

    expect(seen).toHaveLength(2);
    expect(seen[0].reason).toBe("interaction_required");
    expect((seen[1] as CustomEventLike<AuthSessionIssue>["detail"]).message).toBe(
      "Please sign in",
    );
  });
});
