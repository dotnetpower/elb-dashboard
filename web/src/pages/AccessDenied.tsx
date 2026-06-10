import { useMsal } from "@azure/msal-react";
import { ShieldAlert, LogOut, RotateCcw } from "lucide-react";

interface AccessDeniedProps {
  /** Backend-supplied explanation (e.g. which resource group needs a role). */
  message: string;
}

/**
 * Full-screen "no access" screen shown when the optional dashboard entry gate
 * (`ENFORCE_DASHBOARD_RBAC=true`) denies a signed-in tenant member who holds no
 * Azure read role on the dashboard scope. Rendered in place of the app routes,
 * so it carries no nav chrome — mirrors the standalone `SignIn` page.
 */
export function AccessDenied({ message }: AccessDeniedProps) {
  const { instance } = useMsal();
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: "var(--space-5)",
      }}
    >
      <div
        className="glass-card glass-card--strong"
        style={{ width: "min(520px, 100%)", textAlign: "center" }}
        role="alert"
      >
        <div style={{ marginBottom: "var(--space-4)" }}>
          <ShieldAlert
            size={40}
            strokeWidth={1.3}
            style={{ color: "var(--warning)" }}
          />
        </div>
        <h1 style={{ marginTop: 0, fontSize: 22 }}>Access denied</h1>
        <p className="muted" style={{ lineHeight: 1.6, fontSize: 14 }}>
          {message ||
            "You are signed in but do not have an Azure role on this dashboard's resource group. Ask a subscription owner or administrator to grant you at least the Reader role, then retry."}
        </p>
        <p
          className="muted"
          style={{ fontSize: 12, lineHeight: 1.6, marginTop: "var(--space-2)" }}
        >
          Access is granted through Azure RBAC. Once the role is assigned it can
          take a minute to propagate — use <strong>Retry</strong> below afterwards.
        </p>

        <div
          style={{
            display: "flex",
            gap: 8,
            justifyContent: "center",
            flexWrap: "wrap",
            marginTop: "var(--space-4)",
          }}
        >
          <button
            type="button"
            className="glass-button glass-button--primary"
            style={{ padding: "8px 20px", fontSize: 14 }}
            onClick={() => location.reload()}
          >
            <RotateCcw size={16} strokeWidth={1.5} /> Retry
          </button>
          <button
            type="button"
            className="glass-button"
            style={{ padding: "8px 20px", fontSize: 14 }}
            onClick={() => {
              instance.logoutRedirect().catch(() => {});
            }}
          >
            <LogOut size={16} strokeWidth={1.5} /> Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
