import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw, X } from "lucide-react";

import { armProxyApi, monitoringApi } from "@/api/endpoints";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { clearConfig, type ResourceConfig } from "@/components/SetupWizard";
import {
  aggregateDiagnostics,
  getDegradedInfo,
  type CardDiagnosticInput,
  type DegradedInfo,
} from "@/utils/monitorDegraded";

/**
 * Workspace-level diagnostics banner.
 *
 * Re-runs the same monitor queries that the AKS / Storage / ACR cards run.
 * TanStack Query deduplicates by query key, so this does not add network
 * load — it just gives the banner a place to observe degraded reasons.
 *
 * The banner renders only when `aggregateDiagnostics` decides the issue is
 * workspace-wide (auth_wrong_tenant on any card, or 2+ cards in auth/not-found
 * state). A single forbidden card on one leaf resource is left to that card
 * alone.
 */
export interface WorkspaceDiagnosticsBannerProps {
  config: ResourceConfig;
  onResetWorkspace?: () => void;
}

const DISMISS_STORAGE_PREFIX = "elb-diag-banner-dismissed-";

function dismissKey(reason: string): string {
  return DISMISS_STORAGE_PREFIX + reason;
}

function readDismissed(reason: string): boolean {
  try {
    return localStorage.getItem(dismissKey(reason)) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(reason: string): void {
  try {
    localStorage.setItem(dismissKey(reason), "1");
  } catch {
    /* noop */
  }
  notifyDismissChange();
}

function clearDismissed(): void {
  try {
    for (let i = localStorage.length - 1; i >= 0; i -= 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith(DISMISS_STORAGE_PREFIX)) {
        localStorage.removeItem(key);
      }
    }
  } catch {
    /* noop */
  }
  notifyDismissChange();
}

// In-process subscription for dismissed-state mutations. Combined with the
// browser-level `storage` event below, this keeps the banner consistent
// across multiple tabs and across components that share a reason key.
const dismissListeners = new Set<() => void>();
function notifyDismissChange(): void {
  for (const listener of dismissListeners) {
    listener();
  }
}

function subscribeDismissed(listener: () => void): () => void {
  dismissListeners.add(listener);
  const onStorage = (event: StorageEvent): void => {
    if (!event.key || event.key.startsWith(DISMISS_STORAGE_PREFIX)) {
      listener();
    }
  };
  try {
    window.addEventListener("storage", onStorage);
  } catch {
    /* noop (SSR / non-browser context) */
  }
  return () => {
    dismissListeners.delete(listener);
    try {
      window.removeEventListener("storage", onStorage);
    } catch {
      /* noop */
    }
  };
}

function useDismissed(reason: string | null): boolean {
  // useSyncExternalStore subscribes the React tree to the in-process and
  // cross-tab dismiss events, replacing the previous `useState(0) + force`
  // re-render hack. When `reason` is null the hook still subscribes (cheap)
  // and returns false, so banner show/hide stays in sync with React's
  // concurrent rendering rules.
  return useSyncExternalStore(
    subscribeDismissed,
    () => (reason ? readDismissed(reason) : false),
    () => false,
  );
}

export function WorkspaceDiagnosticsBanner({
  config,
  onResetWorkspace,
}: WorkspaceDiagnosticsBannerProps) {
  const subscriptionId = config.subscriptionId;
  const workloadRg = config.workloadResourceGroup;
  const acrRg = config.acrResourceGroup;
  const acrName = config.acrName;
  const storageRg = config.workloadResourceGroup;
  const storageAccount = config.storageAccountName;

  const aksEnabled = Boolean(subscriptionId && workloadRg);
  const storageEnabled = Boolean(subscriptionId && storageRg && storageAccount);
  const acrEnabled = Boolean(subscriptionId && acrRg && acrName);

  const aksQuery = useQuery({
    queryKey: ["aks", subscriptionId, workloadRg],
    queryFn: () => monitoringApi.aks(subscriptionId, workloadRg),
    enabled: aksEnabled,
  });

  const storageQuery = useQuery({
    queryKey: ["storage", subscriptionId, storageRg, storageAccount],
    queryFn: () => monitoringApi.storage(subscriptionId, storageRg, storageAccount),
    enabled: storageEnabled,
  });

  const acrQuery = useQuery({
    queryKey: ["acr", subscriptionId, acrRg, acrName],
    queryFn: () => monitoringApi.acr(subscriptionId, acrRg, acrName),
    enabled: acrEnabled,
  });

  // Visible-subscription check: detect the case where `localStorage` has a
  // `subscriptionId` saved that the current Azure credential cannot see
  // (typical when a developer switched az profiles). The SubscriptionPicker
  // chip in the header already flags this, but the banner needs to know so
  // the actionable "Reset workspace" guidance is one click away.
  const subsQuery = useQuery({
    queryKey: ["arm-subscriptions"],
    queryFn: armProxyApi.listSubscriptions,
    staleTime: 5 * 60_000,
  });

  const visibleSubscriptionInfo = useMemo<DegradedInfo>(() => {
    // Case 1: `/api/arm/subscriptions` itself failed or is unreachable
    // (typical when `az login` is missing or expired). The banner takes
    // priority because it masks every other workspace-level signal — the
    // picker has no list to compare against, so `invisible_subscription`
    // cannot be detected.
    if (subsQuery.isError) {
      return {
        degraded: true,
        reason: "subscriptions_unavailable",
        label: "Subscriptions unavailable",
        description:
          "The dashboard could not list any Azure subscriptions. In the deployed app, grant its managed identity the Reader role at the subscription scope (or wait for a fresh assignment to propagate); locally, run `az login` against the correct tenant. Then click Reset workspace.",
        isAuthIssue: true,
      };
    }
    // Treat an empty list the same way once the query has settled. An empty
    // list with no error usually means the credential succeeded but has zero
    // subscription assignments — actionable in the same way.
    if (subsQuery.data && subsQuery.data.length === 0) {
      return {
        degraded: true,
        reason: "subscriptions_unavailable",
        label: "No subscriptions",
        description:
          "The credential the dashboard uses has no subscriptions assigned. In the deployed app, ask an owner to grant its managed identity the Reader role at the subscription scope; locally, run `az login` against the correct tenant.",
        isAuthIssue: true,
      };
    }
    // Case 2: the saved subscriptionId is not in the visible list (the
    // common `az` profile mismatch).
    if (!subscriptionId) return getDegradedInfo(null);
    if (!subsQuery.data) return getDegradedInfo(null);
    const visible = subsQuery.data.some(
      (s) => s.subscriptionId === subscriptionId,
    );
    if (visible) return getDegradedInfo(null);
    return {
      degraded: true,
      reason: "invisible_subscription",
      label: "Subscription not visible",
      description:
        "The saved subscriptionId is not in the list of subscriptions your current Azure credential can see. Pick another or reset the workspace.",
      isAuthIssue: true,
    };
  }, [subscriptionId, subsQuery.data, subsQuery.isError]);

  const inputs = useMemo<CardDiagnosticInput[]>(
    () => [
      { card: "subscription", info: visibleSubscriptionInfo },
      { card: "aks", info: getDegradedInfo(aksQuery.data) },
      { card: "storage", info: getDegradedInfo(storageQuery.data) },
      { card: "acr", info: getDegradedInfo(acrQuery.data) },
    ],
    [visibleSubscriptionInfo, aksQuery.data, storageQuery.data, acrQuery.data],
  );

  const agg = useMemo(() => aggregateDiagnostics(inputs), [inputs]);

  const dismissed = useDismissed(agg.primaryReason);

  const [confirmReset, setConfirmReset] = useState(false);

  const handleDismiss = useCallback(() => {
    if (agg.primaryReason) writeDismissed(agg.primaryReason);
  }, [agg.primaryReason]);

  const handleResetRequest = useCallback(() => {
    setConfirmReset(true);
  }, []);

  const handleResetCancel = useCallback(() => {
    setConfirmReset(false);
  }, []);

  const handleResetConfirm = useCallback(() => {
    setConfirmReset(false);
    clearConfig();
    clearDismissed();
    if (onResetWorkspace) {
      onResetWorkspace();
    } else {
      window.location.reload();
    }
  }, [onResetWorkspace]);

  if (!agg.show || dismissed) {
    return null;
  }

  const tone =
    agg.primaryReason === "auth_wrong_tenant" ||
    agg.primaryReason === "invisible_subscription" ||
    agg.primaryReason === "subscriptions_unavailable"
      ? "danger"
      : "warning";

  return (
    <>
      <div
        role="status"
        aria-live="polite"
        className={`workspace-diag-banner workspace-diag-banner--${tone}`}
      >
        <div className="workspace-diag-banner__icon" aria-hidden="true">
          <AlertTriangle size={18} strokeWidth={1.5} />
        </div>
        <div className="workspace-diag-banner__body">
          <div className="workspace-diag-banner__title">{agg.title}</div>
          <div className="workspace-diag-banner__text">{agg.body}</div>
          {agg.reasons.length > 0 && (
            <div className="workspace-diag-banner__reasons">
              <span className="workspace-diag-banner__reasons-label">Detected:</span>
              {agg.reasons.map((reason) => (
                <code key={reason} className="workspace-diag-banner__reason-chip">
                  {reason}
                </code>
              ))}
            </div>
          )}
        </div>
        <div className="workspace-diag-banner__actions">
          <button
            type="button"
            className="glass-button glass-button--primary"
            onClick={handleResetRequest}
            title="Clear saved workspace settings and re-run the setup wizard."
          >
            <RefreshCw size={12} strokeWidth={1.5} /> Reset workspace
          </button>
          <button
            type="button"
            className="workspace-diag-banner__dismiss"
            onClick={handleDismiss}
            aria-label="Dismiss diagnostics banner"
            title="Dismiss until the next time this reason appears"
          >
            <X size={14} strokeWidth={1.5} />
          </button>
        </div>
      </div>
      <ConfirmDialog
        open={confirmReset}
        title="Reset workspace settings?"
        message="This clears the saved Subscription, Resource Group, ACR and Storage names from your browser and re-opens the setup wizard. Any in-flight wizard input will be lost."
        confirmLabel="Reset workspace"
        confirmAriaLabel="Confirm reset workspace"
        onConfirm={handleResetConfirm}
        onCancel={handleResetCancel}
      />
    </>
  );
}
