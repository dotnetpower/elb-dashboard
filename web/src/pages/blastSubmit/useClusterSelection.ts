import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";

import { type AksClusterSummary, monitoringApi } from "@/api/endpoints";
import type { FormState } from "@/pages/blastSubmitModel";
import { isAksWorkloadReady } from "@/utils/aksStatus";

export interface UseClusterSelectionArgs {
  subId: string;
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
}

export function useClusterSelection({
  subId,
  form,
  setForm,
}: UseClusterSelectionArgs) {
  // Subscription-wide list (every ELB-managed cluster the caller can see)
  // — same envelope the dashboard's ClusterCard uses. The anchor RG is
  // intentionally unused here so the submit page surfaces multi-cluster
  // fleets (heavy / light / gpu / general) regardless of which RG each
  // cluster lives in.
  const clusterQuery = useQuery({
    queryKey: ["aks", subId, "sub"],
    queryFn: () => monitoringApi.aks(subId),
    enabled: Boolean(subId),
    refetchInterval: 30_000,
  });

  const clusters = useMemo(
    () => clusterQuery.data?.clusters ?? [],
    [clusterQuery.data?.clusters],
  );
  const selectedCluster: AksClusterSummary | undefined = clusters.find(
    (c) => c.name === form.selectedCluster,
  );

  // One-shot default selection per mount. Runs as soon as the cluster list
  // resolves (non-empty) and prefers a workload-ready (Running + Succeeded)
  // cluster over whatever the sessionStorage draft persisted. Without this,
  // a previously-selected cluster that has since been stopped would remain
  // selected across visits to /blast/submit even though a healthy peer is
  // available — confusing for fleets with multiple tiers. After the one-shot
  // runs, manual picks in the dropdown are respected for the rest of the
  // page lifetime.
  const defaultedRef = useRef(false);
  useEffect(() => {
    if (defaultedRef.current) return;
    if (clusters.length === 0) return;

    const running = clusters.find(isAksWorkloadReady);
    const current = clusters.find((c) => c.name === form.selectedCluster);

    let next: string | undefined;
    if (!form.selectedCluster || !current) {
      next = running?.name ?? clusters[0].name;
    } else if (!isAksWorkloadReady(current) && running) {
      next = running.name;
    }

    if (next && next !== form.selectedCluster) {
      setForm((f) => ({ ...f, selectedCluster: next }));
    }
    defaultedRef.current = true;
  }, [clusters, form.selectedCluster, setForm]);

  return { clusterQuery, clusters, selectedCluster } as const;
}
