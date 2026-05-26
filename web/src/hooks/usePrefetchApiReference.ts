/**
 * usePrefetchApiReference — warm the React Query cache for the API
 * Reference page while the user is still on the Dashboard.
 *
 * The /docs (ApiReference) page chains four queries before it can render
 * the spec:
 *   1) monitoringApi.aks(sub)                  — find the AKS cluster
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

import { aksApi, monitoringApi, type AksClusterSummary } from "@/api/endpoints";
import { resolveApiReferenceClusterContext } from "@/pages/apiReference/clusterContext";
import { SVC_NAME } from "@/pages/apiReference/constants";
import { isAksWorkloadReady } from "@/utils/aksStatus";

interface PrefetchInput {
  /** Active subscription. Empty string skips the prefetch. */
  subscriptionId: string;
  /** Dashboard anchor RG. The AKS cluster may live in a different RG. */
  workloadResourceGroup: string;
  /** ACR RG (may differ from workload RG). Optional — skips ACR prefetch when empty. */
  acrResourceGroup: string;
  /** ACR registry name. Optional — skips ACR prefetch when empty. */
  acrName: string;
}

interface ApiReferencePrefetchClient {
  prefetchQuery: (options: {
    queryKey: unknown[];
    queryFn: () => unknown;
    staleTime?: number;
    retry?: number;
  }) => Promise<unknown>;
  getQueryData: <T>(queryKey: unknown[]) => T | undefined;
}

export async function prefetchApiReferenceQueries(
  qc: ApiReferencePrefetchClient,
  cfg: PrefetchInput,
  isCancelled: () => boolean = () => false,
): Promise<void> {
  const {
    subscriptionId: sub,
    workloadResourceGroup: rg,
    acrResourceGroup: acrRg,
    acrName,
  } = cfg;
  if (!sub || !rg) return;

  // 1) AKS cluster list — subscription-wide so clusters outside the
  // dashboard anchor RG are still discovered.
  const clustersPromise = qc.prefetchQuery({
    queryKey: ["aks", sub, "sub"],
    queryFn: () => monitoringApi.aks(sub),
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
  if (isCancelled()) return;

  // After (1) lands the cluster name shows up in the cache; chain
  // (3) and (4) onto it so they can run before the user navigates.
  const clustersData = qc.getQueryData<{ clusters?: AksClusterSummary[] }>([
    "aks",
    sub,
    "sub",
  ]);
  const {
    cluster,
    clusterName,
    resourceGroup: clusterRg,
  } = resolveApiReferenceClusterContext({
    clusters: clustersData?.clusters ?? [],
    anchorResourceGroup: rg,
  });
  const clusterRunning = isAksWorkloadReady(cluster);
  if (!clusterName || !clusterRunning) return;

  try {
    await qc.prefetchQuery({
      queryKey: ["openapi-svc", sub, clusterRg, clusterName],
      queryFn: () => monitoringApi.serviceIp(sub, clusterRg, clusterName, SVC_NAME),
      staleTime: 300_000,
      retry: 1,
    });
  } catch {
    return;
  }
  if (isCancelled()) return;

  // Only fire the spec fetch if the service IP actually resolved; otherwise
  // the page will surface its own deploy-panel UX.
  const svcData = qc.getQueryData<{ external_ip?: string | null }>([
    "openapi-svc",
    sub,
    clusterRg,
    clusterName,
  ]);
  if (!svcData?.external_ip) return;

  try {
    await qc.prefetchQuery({
      queryKey: ["openapi-spec", sub, clusterRg, clusterName],
      queryFn: () => aksApi.proxyOpenApiSpec(sub, clusterRg, clusterName),
      staleTime: 60_000,
    });
  } catch (error) {
    if (import.meta.env.DEV) {
      console.debug("OpenAPI spec prefetch skipped", error);
    }
  }
}

export function usePrefetchApiReference(cfg: PrefetchInput): void {
  const qc = useQueryClient();
  const {
    subscriptionId: sub,
    workloadResourceGroup: rg,
    acrResourceGroup: acrRg,
    acrName,
  } = cfg;

  useEffect(() => {
    if (!sub || !rg) return;
    let cancelled = false;

    // Defer so we don't compete with the dashboard's own initial paint.
    const handle = window.setTimeout(() => {
      void prefetchApiReferenceQueries(
        qc,
        {
          subscriptionId: sub,
          workloadResourceGroup: rg,
          acrResourceGroup: acrRg,
          acrName,
        },
        () => cancelled,
      );
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [qc, sub, rg, acrRg, acrName]);
}
