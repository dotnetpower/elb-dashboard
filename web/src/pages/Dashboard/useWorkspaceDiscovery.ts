import { useCallback, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  listResourceGroups as armListRGs,
  listSubscriptions as armListSubs,
} from "@/api/arm";
import { armProxyApi } from "@/api/endpoints";
import {
  loadSavedConfig,
  saveConfig,
  type ResourceConfig,
} from "@/components/SetupWizard";
import { isDevBypassEnabled } from "@/config/runtime";
import { listWithMiFallback } from "@/lib/armWithMiFallback";

import { configFromTags } from "./configFromTags";

const DEV_BYPASS = isDevBypassEnabled();

const EMPTY_CONFIG: ResourceConfig = {
  subscriptionId: "",
  workloadResourceGroup: "",
  acrResourceGroup: "",
  acrName: "",
  storageAccountName: "",
  terminalResourceGroup: "rg-elb-terminal",
  terminalVmName: "vm-elb-terminal",
  region: "koreacentral",
};

export interface DiscoveredWorkspace {
  config: ResourceConfig;
  rgName: string;
}

/**
 * Owns the auto-discovery flow: subscriptions → resource groups → elb-*
 * tag scan → either auto-apply (1 hit), present a picker (>1), or fall
 * through to the manual SetupWizard (0). Returns the working config plus
 * the UI-state hooks the page needs to render the loading screen and the
 * workspace picker.
 */
export function useWorkspaceDiscovery() {
  const hasSaved = loadSavedConfig();
  const savedIsComplete = !!(
    hasSaved?.acrName && hasSaved?.storageAccountName
  );

  const [config, setConfig] = useState<ResourceConfig>(() =>
    savedIsComplete && hasSaved ? hasSaved : EMPTY_CONFIG,
  );
  const [discoveryDone, setDiscoveryDone] = useState(savedIsComplete);
  const [showWizard, setShowWizard] = useState(false);
  const [discoveredWorkspaces, setDiscoveredWorkspaces] = useState<
    DiscoveredWorkspace[]
  >([]);

  const needsDiscovery = !savedIsComplete && !discoveryDone;

  const subsQuery = useQuery({
    queryKey: ["auto-discover-subs"],
    queryFn: async () => {
      if (DEV_BYPASS) return armProxyApi.listSubscriptions();
      // Try direct ARM (uses the user's MSAL token), fall back to the
      // backend MI proxy when the user has zero subscription-scope RBAC
      // — an empty array there is the failure mode we care about, not
      // just a thrown error. The backend's shared MI sees the workload
      // subscription regardless of per-user assignments.
      return listWithMiFallback(
        async () => {
          const subs = await armListSubs();
          return subs.map((s) => ({
            subscriptionId: s.subscriptionId,
            displayName: s.displayName,
          }));
        },
        () => armProxyApi.listSubscriptions(),
      );
    },
    enabled: needsDiscovery,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  const rgsQueries = useQuery({
    queryKey: [
      "auto-discover-rgs",
      subsQuery.data?.map((s) => s.subscriptionId),
    ],
    queryFn: async () => {
      const subs = subsQuery.data ?? [];
      const results: {
        subscriptionId: string;
        rgs: {
          name: string;
          location: string;
          tags?: Record<string, string>;
        }[];
      }[] = [];
      for (const sub of subs) {
        // Same MI-fallback rationale as the subs query above: a user with
        // only RG-scope Reader (or no RBAC at all) gets an empty list
        // from direct ARM and needs the backend MI proxy to surface the
        // elb-tagged workspace RG so auto-discovery can find it.
        const rgList = DEV_BYPASS
          ? await armProxyApi.listResourceGroups(sub.subscriptionId)
          : await listWithMiFallback(
              () => armListRGs(sub.subscriptionId),
              () => armProxyApi.listResourceGroups(sub.subscriptionId),
            );
        const rgs = rgList.map((r) => ({
          name: r.name,
          location: r.location,
          tags: r.tags,
        }));
        results.push({ subscriptionId: sub.subscriptionId, rgs });
      }
      return results;
    },
    enabled: needsDiscovery && !!subsQuery.data?.length,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  // Process discovery results — auto-apply on a single hit, present a
  // picker on multiple hits, or fall through to the manual wizard.
  useEffect(() => {
    if (!needsDiscovery || !rgsQueries.data) return;
    const found: DiscoveredWorkspace[] = [];
    for (const { subscriptionId, rgs } of rgsQueries.data) {
      for (const rg of rgs) {
        const cfg = configFromTags(subscriptionId, rg);
        if (cfg) found.push({ config: cfg, rgName: rg.name });
      }
    }
    if (found.length === 1) {
      setConfig(found[0].config);
      saveConfig(found[0].config);
      setDiscoveryDone(true);
    } else if (found.length > 1) {
      setDiscoveredWorkspaces(found);
      setDiscoveryDone(true);
    } else {
      setDiscoveryDone(true);
      setShowWizard(true);
    }
  }, [needsDiscovery, rgsQueries.data]);

  // Open wizard on discovery error.
  useEffect(() => {
    if (!needsDiscovery) return;
    if (subsQuery.isError || rgsQueries.isError) {
      setDiscoveryDone(true);
      setShowWizard(true);
    }
  }, [needsDiscovery, subsQuery.isError, rgsQueries.isError]);

  // Empty subscription list (e.g. dev-bypass without ARM creds, or a
  // tenant the caller has zero RBAC on) — there is nothing to scan, so
  // skip straight to the manual wizard instead of spinning forever.
  // Without this, rgsQueries stays disabled (needs subsQuery.data.length
  // > 0) and the loading screen never resolves.
  useEffect(() => {
    if (!needsDiscovery) return;
    if (subsQuery.isSuccess && (subsQuery.data?.length ?? 0) === 0) {
      setDiscoveryDone(true);
      setShowWizard(true);
    }
  }, [needsDiscovery, subsQuery.isSuccess, subsQuery.data]);

  const handlePickWorkspace = useCallback((ws: ResourceConfig) => {
    setConfig(ws);
    saveConfig(ws);
    setDiscoveredWorkspaces([]);
  }, []);

  const skipDiscovery = useCallback(() => {
    setDiscoveryDone(true);
    setShowWizard(true);
  }, []);

  return {
    config,
    setConfig,
    discoveryDone,
    discoveredWorkspaces,
    showWizard,
    setShowWizard,
    handlePickWorkspace,
    skipDiscovery,
    setDiscoveredWorkspaces,
  } as const;
}
