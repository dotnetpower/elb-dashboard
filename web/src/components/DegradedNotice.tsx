/**
 * D2 — Shared degraded-payload notice.
 *
 * Many `/api/monitor/*` and `/api/blast/*` responses ship a `degraded: true`
 * flag with a `degraded_reason` code instead of failing the HTTP call (the
 * `_graceful` wrapper in `api/routes/monitor.py`). Before this component,
 * each call-site rendered its own ad-hoc warning box, which led to copy
 * drift and inconsistent CTAs.
 *
 * `<DegradedNotice />` standardises the look + recovery hint and lets the
 * caller drop in a custom action button without re-implementing the layout.
 */
import type { ReactNode } from "react";
import { AlertTriangle, ShieldOff, WifiOff, Lock, Database } from "lucide-react";

export type DegradedReason =
  | "network_blocked"
  | "unauthorized"
  | "not_found"
  | "rate_limited"
  | "state_repo_unavailable"
  | "sidecar_stale"
  | "polling_disabled"
  | "unknown"
  | (string & {});

interface ReasonMeta {
  label: string;
  hint: string;
  icon: ReactNode;
}

const REASON_META: Record<string, ReasonMeta> = {
  network_blocked: {
    label: "Private only",
    hint:
      "Storage is Private only and this local browser session cannot reach the data plane. Use scripts/dev/storage-public-access.sh on for local debugging, or rely on the api sidecar streaming proxy.",
    icon: <WifiOff size={12} />,
  },
  unauthorized: {
    label: "Not authorized",
    hint:
      "The shared managed identity (id-elb-dashboard-*) is missing an RBAC role for this resource. See docs/auth.md §1 for the required role matrix.",
    icon: <Lock size={12} />,
  },
  not_found: {
    label: "Resource not found",
    hint:
      "The Azure resource referenced in your workspace config was not found. Open Settings and pick the correct subscription / RG.",
    icon: <ShieldOff size={12} />,
  },
  rate_limited: {
    label: "ARM rate limited",
    hint:
      "Azure ARM throttled the read. The card will retry on the next refresh interval.",
    icon: <AlertTriangle size={12} />,
  },
  state_repo_unavailable: {
    label: "State repo unavailable",
    hint:
      "Azure Table Storage backing the job/audit state is unreachable. Check the storage account private endpoint.",
    icon: <Database size={12} />,
  },
  sidecar_stale: {
    label: "Sidecar metrics stale",
    hint:
      "The sidecar collector hasn't reported in the expected window. Verify the worker / beat / redis sidecars are running.",
    icon: <AlertTriangle size={12} />,
  },
  polling_disabled: {
    label: "Live updates paused",
    hint:
      "Auto-refresh is paused (tab inactive or user toggle). Data is from the last successful fetch.",
    icon: <AlertTriangle size={12} />,
  },
};

const FALLBACK_META: ReasonMeta = {
  label: "Degraded",
  hint: "The backend returned a partial response. Refresh to retry.",
  icon: <AlertTriangle size={12} />,
};

export interface DegradedNoticeProps {
  /** Machine-readable reason from the API (e.g. `network_blocked`). */
  reason: DegradedReason;
  /** Optional override of the human-readable explanation. Falls back to the canned hint. */
  message?: string;
  /** Short noun describing what is degraded ("Job listing", "Storage", "AKS events"). */
  scope?: string;
  /** Optional action slot (e.g. "Open Storage", "Retry"). */
  action?: ReactNode;
  /** Make the card more compact (used inside cramped layouts like sidecar grids). */
  dense?: boolean;
}

export function DegradedNotice({
  reason,
  message,
  scope,
  action,
  dense = false,
}: DegradedNoticeProps) {
  const meta = REASON_META[reason] ?? FALLBACK_META;
  const heading = scope
    ? `${scope} degraded · ${meta.label}`
    : `Degraded · ${meta.label}`;
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        padding: dense ? "8px 10px" : "10px 14px",
        background: "rgba(240,198,116,0.08)",
        border: "1px solid rgba(240,198,116,0.25)",
        borderRadius: 8,
        fontSize: dense ? 11 : 12,
        color: "var(--text-primary)",
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
      }}
    >
      <span
        aria-hidden
        style={{
          color: "var(--warning)",
          display: "inline-flex",
          alignItems: "center",
          marginTop: 2,
        }}
      >
        {meta.icon}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: dense ? 10 : 11,
            letterSpacing: "0.04em",
            textTransform: "uppercase",
            fontWeight: 600,
            marginBottom: 4,
          }}
        >
          {heading}
        </div>
        <div className="muted" style={{ fontSize: dense ? 11 : 12, lineHeight: 1.45 }}>
          {message ?? meta.hint}
        </div>
        {action && <div style={{ marginTop: 8 }}>{action}</div>}
      </div>
    </div>
  );
}
