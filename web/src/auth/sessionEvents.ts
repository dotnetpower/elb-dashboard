import { useSyncExternalStore } from "react";

export type AuthSessionReason =
  | "not_signed_in"
  | "interaction_required"
  | "token_refresh_failed"
  | "api_unauthorized"
  | "arm_unauthorized";

export interface AuthSessionIssue {
  reason: AuthSessionReason;
  message: string;
}

const AUTH_SESSION_ISSUE_EVENT = "elb:auth-session-issue";

// Module-level "current session issue" store. While this is non-null the
// browser sign-in session is broken/expired, and the App-level gate routes
// the user to the in-app sign-in page instead of leaving the dashboard
// mounted behind a stale banner. It is cleared again the moment a fresh
// access token is acquired (see clearAuthSessionIssue callers).
let currentIssue: AuthSessionIssue | null = null;
const storeListeners = new Set<() => void>();

function emitStoreChange(): void {
  for (const listener of storeListeners) listener();
}

export function notifyAuthSessionIssue(
  reason: AuthSessionReason,
  message = "Your browser sign-in session needs a refresh. Sign in again to continue.",
) {
  currentIssue = { reason, message };
  window.dispatchEvent(
    new CustomEvent<AuthSessionIssue>(AUTH_SESSION_ISSUE_EVENT, {
      detail: currentIssue,
    }),
  );
  emitStoreChange();
}

/** Clear the active session issue once a fresh token is acquired (silent or
 *  interactive). This re-opens the dashboard after the user signs back in. */
export function clearAuthSessionIssue(): void {
  if (currentIssue === null) return;
  currentIssue = null;
  emitStoreChange();
}

/** Current snapshot of the session issue (null = session is healthy). */
export function getAuthSessionIssue(): AuthSessionIssue | null {
  return currentIssue;
}

export function subscribeAuthSessionIssues(
  handler: (issue: AuthSessionIssue) => void,
) {
  const listener = (event: Event) => {
    handler((event as CustomEvent<AuthSessionIssue>).detail);
  };
  window.addEventListener(AUTH_SESSION_ISSUE_EVENT, listener);
  return () => window.removeEventListener(AUTH_SESSION_ISSUE_EVENT, listener);
}

function subscribeAuthSessionStore(listener: () => void): () => void {
  storeListeners.add(listener);
  return () => {
    storeListeners.delete(listener);
  };
}

/** React hook exposing the current auth-session issue. Drives the App-level
 *  gate: when it returns a non-null issue the sign-in page is shown in place
 *  of the dashboard. */
export function useAuthSessionIssue(): AuthSessionIssue | null {
  return useSyncExternalStore(
    subscribeAuthSessionStore,
    getAuthSessionIssue,
    () => null,
  );
}