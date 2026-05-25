import { useEffect, useMemo } from "react";
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

  useEffect(() => {
    if (!form.selectedCluster && clusters.length > 0) {
      const running = clusters.find(isAksWorkloadReady);
      setForm((f) => ({
        ...f,
        selectedCluster: running?.name ?? clusters[0].name,
      }));
    }
  }, [clusters, form.selectedCluster, setForm]);

  return { clusterQuery, clusters, selectedCluster } as const;
}
