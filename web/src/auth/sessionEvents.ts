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

export function notifyAuthSessionIssue(
  reason: AuthSessionReason,
  message = "Your browser sign-in session needs a refresh. Sign in again to continue.",
) {
  window.dispatchEvent(
    new CustomEvent<AuthSessionIssue>(AUTH_SESSION_ISSUE_EVENT, {
      detail: { reason, message },
    }),
  );
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