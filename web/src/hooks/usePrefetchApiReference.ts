/**
 * usePrefetchApiReference — warm the React Query cache for the API
 * Reference page while the user is still on the Dashboard.
 *
 * The /docs (ApiReference) page chains four queries before it can render
 * the spec:
 *   1) monitoringApi.aks(sub, rg)              — find the AKS cluster
 *   2) monitoringApi.acr(sub, acrRg, acrName)  — confirm elb-openapi tag
 *   3) monitoringApi.serviceIp(...)            — find the LoadBalancer IP
 *   4) aksApi.proxyOpenApiSpec(...)            — fetch openapi.json
 *
 * Steps 3 + 4 hit the AKS k8s API and the in-cluster openapi pod, which
 * routinely takes 2–5 s end-to-end and is what the user perceives as the
 * "Discovering OpenAPI service on AKS..." spinner.
 *
 * This hook fires the same queries with the same query keys + staleTime
 * as the page itself, so the page picks them straight from cache when it
 * mounts. Any failure is swallowed silently — the page will retry with
 * its own UX surface.
 */
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { aksApi, monitoringApi } from "@/api/endpoints";
import { SVC_NAME } from "@/pages/apiReference/constants";
import { isAksWorkloadReady } from "@/utils/aksStatus";

interface PrefetchInput {
  /** Active subscription. Empty string skips the prefetch. */
  subscriptionId: string;
  /** Workload RG (where the AKS cluster lives). */
  workloadResourceGroup: string;
  /** ACR RG (may differ from workload RG). Optional — skips ACR prefetch when empty. */
  acrResourceGroup: string;
  /** ACR registry name. Optional — skips ACR prefetch when empty. */
  acrName: string;
}

export function usePrefetchApiReference(cfg: PrefetchInput): void {
  const qc = useQueryClient();
  const { subscriptionId: sub, workloadResourceGroup: rg, acrResourceGroup: acrRg, acrName } = cfg;

  useEffect(() => {
    if (!sub || !rg) return;
    let cancelled = false;

    const run = async () => {
      // 1) AKS cluster list — needed to learn the cluster name.
      const clustersPromise = qc.prefetchQuery({
        queryKey: ["aks", sub, rg],
        queryFn: () => monitoringApi.aks(sub, rg),
        staleTime: 300_000,
      });

      // 2) ACR tags — independent of the cluster name, fire in parallel.
      const acrPromise =
        acrRg && acrName
          ? qc.prefetchQuery({
              queryKey: ["acr", sub, acrRg, acrName],
              queryFn: () => monitoringApi.acr(sub, acrRg, acrName),
              staleTime: 300_000,
            })
          : Promise.resolve();

      try {
        await Promise.allSettled([clustersPromise, acrPromise]);
      } catch {
        return;
      }
      if (cancelled) return;

      // After (1) lands the cluster name shows up in the cache; chain
      // (3) and (4) onto it so they can run before the user navigates.
      const clustersData = qc.getQueryData<{
        clusters?: { name: string; power_state?: string; provisioning_state?: string }[];
      }>(["aks", sub, rg]);
      const clusterName = clustersData?.clusters?.[0]?.name;
      const clusterRunning = isAksWorkloadReady(clustersData?.clusters?.[0]);
      if (!clusterName || !clusterRunning) return;

      try {
        await qc.prefetchQuery({
          queryKey: ["openapi-svc", sub, rg, clusterName],
          queryFn: () => monitoringApi.serviceIp(sub, rg, clusterName, SVC_NAME),
          staleTime: 300_000,
          retry: 1,
        });
      } catch {
        return;
      }
      if (cancelled) return;

      // Only fire the spec fetch if the service IP actually resolved —
      // otherwise the page will surface its own "service not found" UX
      // and a prefetch error here would just pollute the cache.
      const svcData = qc.getQueryData<{ external_ip?: string }>(["openapi-svc", sub, rg, clusterName]);
      if (!svcData?.external_ip) return;

      try {
        await qc.prefetchQuery({
          queryKey: ["openapi-spec", sub, rg, clusterName],
          queryFn: () => aksApi.proxyOpenApiSpec(sub, rg, clusterName),
          staleTime: 60_000,
        });
      } catch (error) {
        if (import.meta.env.DEV) {
          console.debug("OpenAPI spec prefetch skipped", error);
        }
      }
    };

    // Defer so we don't compete with the dashboard's own initial paint.
    const handle = window.setTimeout(run, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [qc, sub, rg, acrRg, acrName]);
}
