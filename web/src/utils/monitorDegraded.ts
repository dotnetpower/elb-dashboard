/**
 * Translate the backend `degraded_reason` taxonomy into UI-facing labels and
 * actionable diagnostics. The backend taxonomy is owned by
 * `api/routes/monitor/common.py::_classify_exception`; any new code added there
 * must be mirrored here so the dashboard banner and per-card labels stay in
 * sync.
 *
 * Why a shared helper:
 *   - Multiple cards (ACR, Storage, Cluster, Sidecars) need the same mapping.
 *   - The top-of-dashboard `WorkspaceDiagnosticsBanner` aggregates degraded
 *     reasons across cards and applies a single rule for what counts as a
 *     workspace-wide problem.
 *
 * This module has no React dependency so it can be unit-tested in isolation.
 */

/** Stable degraded codes returned by the backend monitor router. */
export type DegradedReason =
  | "auth_wrong_tenant"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "azure_error"
  | "network_blocked"
  | "firewall_blocked"
  | "access_denied"
  | "redis_unavailable"
  | "no_samples"
  | "no_result_files"
  | "storage_unreachable"
  | "all_reads_failed"
  | "openapi_upstream_error"
  | "openapi_http_401"
  /**
   * The selected AKS cluster exists in ARM but its power_state is not
   * `Running` (typically Stopped). The K8s API server is unreachable so
   * the monitor route returns this instead of a connection-error degrade.
   * Origin: `api/services/cluster_health.py::get_cluster_health`.
   */
  | "cluster_stopped"
  /**
   * The selected AKS cluster could not be found in ARM (deleted or wrong
   * RG). Same origin as `cluster_stopped`; surfaced as a distinct card
   * label so the user can decide between "start the cluster" and "pick
   * another cluster / re-run setup".
   */
  | "cluster_not_found"
  /**
   * Synthetic, SPA-only: the `subscriptionId` saved in `localStorage` is not
   * in the list returned by `/api/arm/subscriptions`. There is no backend
   * payload for this — the diagnostics banner synthesises it from the
   * subscription list query.
   */
  | "invisible_subscription"
  /**
   * Synthetic, SPA-only: `/api/arm/subscriptions` itself failed or returned
   * an empty list. Usually means az login is missing/expired or ARM consent
   * was never granted. The banner shows this with priority because it
   * masks every other workspace-level signal (the picker has no list to
   * compare against, so an `invisible_subscription` cannot even be detected).
   */
  | "subscriptions_unavailable"
  | string; // tolerate forward-compat codes (e.g. `http_500`) without crashing

export interface DegradedInfo {
  /** True when the payload is a partial/empty response from a graceful degrade. */
  degraded: boolean;
  /** Raw reason code from the backend (`degraded_reason`). Null if not degraded. */
  reason: DegradedReason | null;
  /** Short human label suitable for a card status chip. */
  label: string;
  /** Sentence-form description for tooltips / banners. */
  description: string;
  /** True when the reason is an authentication/authorisation problem the user can act on. */
  isAuthIssue: boolean;
}

/**
 * Extract degraded info from a monitor API payload.
 *
 * Accepts unknown so callers (typed via different summary shapes) can pass
 * `query.data` directly without ceremony. Returns a non-degraded info object
 * when the payload is null, missing the flag, or explicitly `degraded: false`.
 */
export function getDegradedInfo(data: unknown): DegradedInfo {
  if (!data || typeof data !== "object") {
    return notDegraded();
  }
  const record = data as { degraded?: unknown; degraded_reason?: unknown };
  if (record.degraded !== true) {
    return notDegraded();
  }
  const reason =
    typeof record.degraded_reason === "string" && record.degraded_reason
      ? (record.degraded_reason as DegradedReason)
      : "azure_error";
  return describeReason(reason);
}

function notDegraded(): DegradedInfo {
  return {
    degraded: false,
    reason: null,
    label: "",
    description: "",
    isAuthIssue: false,
  };
}

interface ReasonDescriptor {
  label: string;
  description: string;
  isAuthIssue: boolean;
}

const REASON_TABLE: Record<string, ReasonDescriptor> = {
  subscriptions_unavailable: {
    label: "Sign in to Azure",
    description:
      "The dashboard could not list any Azure subscriptions for your current credential. Your `az login` session may be missing or expired, or ARM consent was never granted. Run `az login` in the terminal and reload, or pick a different Azure profile.",
    isAuthIssue: true,
  },
  invisible_subscription: {
    label: "Subscription not visible",
    description:
      "The saved subscriptionId is not in the list of subscriptions your current Azure credential can see. Pick another subscription from the dropdown, or reset the workspace to re-run the setup wizard.",
    isAuthIssue: true,
  },
  auth_wrong_tenant: {
    label: "Wrong tenant",
    description:
      "Your Azure CLI session and the selected subscription belong to different tenants. Sign in to the correct tenant or pick a subscription from the current tenant.",
    isAuthIssue: true,
  },
  unauthorized: {
    label: "Auth required",
    description:
      "Azure rejected the credential (HTTP 401). Run `az login` against the tenant that owns this subscription, or pick a different subscription.",
    isAuthIssue: true,
  },
  forbidden: {
    label: "No access",
    description:
      "Your identity is signed in but does not have permission to read this resource (HTTP 403). Ask an owner to grant Reader or the appropriate data-plane role.",
    isAuthIssue: true,
  },
  not_found: {
    label: "Not found",
    description:
      "The resource was not found in the selected subscription. It may have been renamed or deleted, or you may have stale workspace settings from a previous wizard run.",
    isAuthIssue: false,
  },
  azure_error: {
    label: "Azure error",
    description: "Azure returned an error while reading this resource.",
    isAuthIssue: false,
  },
  network_blocked: {
    label: "Network blocked",
    description:
      "Storage public access is disabled and this client is not in the firewall. Use the local-debug helper script to allowlist your IP, or run from inside the VNet.",
    isAuthIssue: false,
  },
  firewall_blocked: {
    label: "Firewall blocked",
    description: "Storage firewall rejected this client IP.",
    isAuthIssue: false,
  },
  access_denied: {
    label: "Access denied",
    description: "Data-plane RBAC rejected the request.",
    isAuthIssue: true,
  },
  redis_unavailable: {
    label: "Redis offline",
    description: "The sidecar Redis broker is unreachable.",
    isAuthIssue: false,
  },
  cluster_stopped: {
    label: "Cluster stopped",
    description:
      "The AKS cluster is in Stopped power state. Monitoring will resume automatically after you start it from the AKS card or the Azure portal.",
    isAuthIssue: false,
  },
  cluster_not_found: {
    label: "Cluster missing",
    description:
      "The selected AKS cluster could not be found in ARM. It may have been deleted or moved to a different resource group; pick another cluster from the dropdown or re-run the setup wizard.",
    isAuthIssue: false,
  },
};

function describeReason(reason: DegradedReason): DegradedInfo {
  const descriptor = REASON_TABLE[reason];
  if (descriptor) {
    return {
      degraded: true,
      reason,
      label: descriptor.label,
      description: descriptor.description,
      isAuthIssue: descriptor.isAuthIssue,
    };
  }
  // Forward-compatible: render unknown codes verbatim but still mark as degraded.
  return {
    degraded: true,
    reason,
    label: "Degraded",
    description: `Backend reported degraded_reason="${reason}".`,
    isAuthIssue: false,
  };
}

/**
 * Aggregate degraded info from several monitor cards. The banner uses this to
 * decide whether to render a single workspace-wide guidance block instead of
 * forcing the user to read each card individually.
 *
 * Threshold: two or more auth-issue cards (or any `auth_wrong_tenant` /
 * `not_found` on the subscription itself) is treated as a workspace problem;
 * a single forbidden card on a leaf resource is left to the card alone.
 */
export interface AggregatedDiagnostics {
  /** True when the banner should be visible. */
  show: boolean;
  /** The dominant reason driving the banner (most actionable wins). */
  primaryReason: DegradedReason | null;
  /** All distinct reasons present, in order of severity. */
  reasons: DegradedReason[];
  /** Cards that are degraded by an auth issue. */
  authIssueCount: number;
  /** Cards that report `not_found` (typical for stale wizard settings). */
  notFoundCount: number;
  /** Banner-ready short title. */
  title: string;
  /** Banner-ready longer body. */
  body: string;
}

export interface CardDiagnosticInput {
  /** Stable card identifier (acr/storage/aks/...) for logging only. */
  card: string;
  info: DegradedInfo;
}

const SEVERITY_ORDER: DegradedReason[] = [
  "subscriptions_unavailable",
  "invisible_subscription",
  "auth_wrong_tenant",
  "unauthorized",
  "forbidden",
  "not_found",
  "network_blocked",
  "firewall_blocked",
  "access_denied",
  "azure_error",
  "redis_unavailable",
];

function severityIndex(reason: DegradedReason): number {
  const idx = SEVERITY_ORDER.indexOf(reason);
  return idx === -1 ? SEVERITY_ORDER.length : idx;
}

export function aggregateDiagnostics(
  inputs: CardDiagnosticInput[],
): AggregatedDiagnostics {
  const degraded = inputs.filter((entry) => entry.info.degraded);
  const reasons = Array.from(
    new Set(
      degraded
        .map((entry) => entry.info.reason)
        .filter((reason): reason is DegradedReason => Boolean(reason)),
    ),
  ).sort((a, b) => severityIndex(a) - severityIndex(b));

  const authIssueCount = degraded.filter((entry) => entry.info.isAuthIssue).length;
  const notFoundCount = degraded.filter(
    (entry) => entry.info.reason === "not_found",
  ).length;

  const primaryReason = reasons[0] ?? null;

  // Trigger the workspace banner when the problem is systemic, not when one
  // leaf resource is missing or one card is forbidden.
  const show =
    primaryReason === "subscriptions_unavailable" ||
    primaryReason === "invisible_subscription" ||
    primaryReason === "auth_wrong_tenant" ||
    primaryReason === "unauthorized" ||
    authIssueCount >= 2 ||
    notFoundCount >= 2;

  return {
    show,
    primaryReason,
    reasons,
    authIssueCount,
    notFoundCount,
    title: bannerTitle(primaryReason, notFoundCount),
    body: bannerBody(primaryReason, notFoundCount, authIssueCount),
  };
}

function bannerTitle(
  primary: DegradedReason | null,
  notFoundCount: number,
): string {
  if (primary === "subscriptions_unavailable") {
    return "Sign in to Azure to load workspace data";
  }
  if (primary === "invisible_subscription") {
    return "Saved subscription is not visible";
  }
  if (primary === "auth_wrong_tenant") {
    return "Wrong Azure tenant for the selected subscription";
  }
  if (primary === "unauthorized") {
    return "Azure credential rejected";
  }
  if (notFoundCount >= 2) {
    return "Workspace resources not found";
  }
  if (primary === "forbidden") {
    return "Insufficient Azure permissions";
  }
  return "Workspace diagnostics";
}

function bannerBody(
  primary: DegradedReason | null,
  notFoundCount: number,
  authIssueCount: number,
): string {
  if (primary === "subscriptions_unavailable") {
    return (
      "The dashboard could not list any Azure subscriptions for your current credential. Run `az login --tenant <your-tenant>` in a terminal (or pick a different az profile such as `az-jungha`), then click Reset workspace to retry. Until this is fixed every monitor card will be empty."
    );
  }
  if (primary === "invisible_subscription") {
    return (
      "The subscriptionId saved by the setup wizard is not in the list returned by your current Azure credential. This usually means `az login` was run on a different profile or tenant since the wizard was last completed. Either pick a visible subscription from the header dropdown, or reset the workspace to re-run the wizard."
    );
  }
  if (primary === "auth_wrong_tenant") {
    return (
      "Your `az login` session belongs to a different tenant than the subscription this dashboard is calling. " +
      "Either run `az login --tenant <correct-tenant>` and `az account set --subscription <id>`, or pick a subscription from the tenant you are already signed in to."
    );
  }
  if (primary === "unauthorized") {
    return (
      "Azure refused the credential when reading workspace resources. Verify `az account show` matches the subscription selected above, then refresh."
    );
  }
  if (notFoundCount >= 2) {
    return (
      "Several workspace resources were not found in the selected subscription. This usually means the saved workspace settings (Resource Group, ACR, Storage account names) point at resources that no longer exist or live in a different subscription. Reset the workspace to re-run the setup wizard."
    );
  }
  if (primary === "forbidden") {
    return (
      "Multiple resources rejected the read with HTTP 403. Ask a subscription owner to grant Reader (and the data-plane roles documented in get-started)."
    );
  }
  if (authIssueCount > 0) {
    return "Several cards are degraded because of authentication or authorization problems. Check Azure CLI session and role assignments.";
  }
  return "Several cards reported a degraded state.";
}
