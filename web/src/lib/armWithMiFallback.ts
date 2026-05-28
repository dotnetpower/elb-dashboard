/**
 * ARM call helpers with backend Managed Identity fallback.
 *
 * Workspace auto-discovery and the SetupWizard try to enumerate Azure
 * resources via the *user's* MSAL access token first ("direct ARM").
 * This is the cheapest path because it skips the backend entirely, but
 * it relies on the signed-in user actually holding `Reader` (or higher)
 * on the subscription / resource group. A collaborator who only ever
 * uses the deployed SPA is intentionally given zero workload RBAC — all
 * Azure side effects route through the shared user-assigned Managed
 * Identity `id-elb-dashboard-*` instead.
 *
 * Without a fallback, that collaborator hits `Subscription ID: —` on
 * the dashboard because the direct ARM call returns an empty array (no
 * subscriptions visible to their token) and the discovery loop bails out
 * without ever asking the backend. These helpers fix that gap by
 * treating an empty or thrown response the same way and replaying the
 * call through the backend MI proxy, which uses the shared MI's much
 * broader visibility.
 *
 * Helpers are pure (no React, no global state) so they unit-test
 * cleanly. The backend proxy itself still requires a valid bearer token
 * (every `/api/arm/*` route uses `Depends(require_caller)`), so this is
 * not a path to bypass authentication — only to bypass per-user RBAC on
 * read-only ARM metadata.
 */

/**
 * Run a direct ARM list call, falling back to a backend MI proxy when
 * the direct call throws OR returns an empty list.
 *
 * The empty-array fallback is the key change: previously only `throw`
 * triggered the fallback, so an authenticated user with zero RBAC
 * received `[]` and the SPA assumed there were genuinely no
 * subscriptions to scan.
 */
export async function listWithMiFallback<T>(
  direct: () => Promise<T[]>,
  miProxy: () => Promise<T[]>,
): Promise<T[]> {
  try {
    const items = await direct();
    if (items.length > 0) return items;
  } catch {
    // fall through to MI proxy
  }
  try {
    return await miProxy();
  } catch {
    return [];
  }
}
