import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { aksApi, monitoringApi } from "@/api/endpoints";
import type { ApiError } from "@/api/client";
import { OpenApiDeployPanel } from "@/components/OpenApiDeployPanel";
import { loadSavedConfig } from "@/components/SetupWizard";
import { ApiHero } from "@/pages/apiReference/ApiHero";
import { ApiReferenceSidebar } from "@/pages/apiReference/ApiReferenceSidebar";
import { ApiResponseContractPanel } from "@/pages/apiReference/ApiResponseContractPanel";
import { ApiTokenPanel } from "@/pages/apiReference/ApiTokenPanel";
import { CoreApiSection } from "@/pages/apiReference/CoreApiSection";
import { PlsTransitionBanner } from "@/pages/apiReference/PlsTransitionBanner";
import {
  isPeerWithPlatformRecovery,
} from "@/pages/apiReference/RepairPeeringButton";
import {
  isGrantLbSubnetRbacRecovery,
} from "@/pages/apiReference/GrantLbSubnetRbacButton";
import { resolveApiReferenceClusterContext } from "@/pages/apiReference/clusterContext";
import { SVC_NAME } from "@/pages/apiReference/constants";
import {
  readOpenApiPodStartup,
  type OpenApiSpecDegraded,
} from "@/pages/apiReference/openApiPodStartup";
import { parseSpec } from "@/pages/apiReference/spec";
import { TagSection } from "@/pages/apiReference/TagSection";
import {
  ClusterPicker,
  ClusterStoppedState,
  MissingConfigState,
  MissingOpenApiImageState,
  OpenApiLoadingState,
  OpenApiManifestDiagnostic,
  OpenApiPodStartingState,
  SpecErrorState,
  SpecLoadingState,
} from "@/pages/apiReference/panelStates";
import { isAksWorkloadReady } from "@/utils/aksStatus";

const PREFERRED_CLUSTER_STORAGE_KEY = "elb-api-ref-cluster";

function loadPreferredClusterName(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(PREFERRED_CLUSTER_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function savePreferredClusterName(name: string): void {
  if (typeof window === "undefined") return;
  try {
    if (name) {
      window.localStorage.setItem(PREFERRED_CLUSTER_STORAGE_KEY, name);
    } else {
      window.localStorage.removeItem(PREFERRED_CLUSTER_STORAGE_KEY);
    }
  } catch {
    /* ignore */
  }
}

function normaliseImageTag(value: string): string {
  return value.trim().replace(/^v/i, "");
}

export function ApiReference() {
  const [savedConfig] = useState(() => loadSavedConfig());
  const [preferredClusterName, setPreferredClusterName] = useState<string>(
    () => loadPreferredClusterName(),
  );

  const sub = savedConfig?.subscriptionId ?? "";
  const anchorRg = savedConfig?.workloadResourceGroup ?? "";
  const enabled = Boolean(sub && anchorRg);

  const clustersQuery = useQuery({
    queryKey: ["aks", sub, "sub"],
    queryFn: () => monitoringApi.aks(sub),
    enabled: Boolean(sub),
    staleTime: 300_000,
  });
  const clusters = clustersQuery.data?.clusters ?? [];
  const {
    cluster: firstCluster,
    clusterName,
    resourceGroup: clusterRg,
    candidates,
  } = resolveApiReferenceClusterContext({
    clusters,
    anchorResourceGroup: anchorRg,
    preferredClusterName,
  });
  const clusterStopped = firstCluster && !isAksWorkloadReady(firstCluster);
  const hasMultipleClusters = candidates.length > 1;

  // If the user's stored preference no longer exists in the fleet
  // (cluster deleted), drop it so the auto-selector takes over again
  // on the next render instead of silently sticking to the fallback.
  useEffect(() => {
    if (!preferredClusterName) return;
    if (candidates.length === 0) return;
    const stillPresent = candidates.some((c) => c.name === preferredClusterName);
    if (!stillPresent) {
      setPreferredClusterName("");
      savePreferredClusterName("");
    }
  }, [preferredClusterName, candidates]);

  const onSelectCluster = (name: string) => {
    setPreferredClusterName(name);
    savePreferredClusterName(name);
  };

  const acrRg = savedConfig?.acrResourceGroup ?? "";
  const acrName = savedConfig?.acrName ?? "";
  const acrQuery = useQuery({
    queryKey: ["acr", sub, acrRg, acrName],
    queryFn: () => monitoringApi.acr(sub, acrRg, acrName),
    enabled: Boolean(sub && acrRg && acrName),
    staleTime: 300_000,
  });
  const hasOpenApiImage = acrQuery.data?.actual_tags
    ? "elb-openapi" in acrQuery.data.actual_tags
    : false;

  const svcQuery = useQuery({
    queryKey: ["openapi-svc", sub, clusterRg, clusterName],
    queryFn: () => monitoringApi.serviceIp(sub, clusterRg, clusterName, SVC_NAME),
    enabled: enabled && Boolean(clusterName),
    staleTime: 300_000,
    retry: 1,
  });

  // Public HTTPS state lives in the api sidecar's runtime cache (no kubectl
  // round trip). When `enabled=true` the `setup_openapi_public_https` task
  // has installed ingress-nginx + cert-manager + a Let's Encrypt-signed
  // Ingress, so we flip `baseUrl` from the internal LB IP path to the
  // HTTPS public FQDN. That makes the API Reference "Try it" surface, the
  // Swagger UI link, and the "Copy curl" button all point at the
  // externally-reachable URL.
  const publicHttpsQuery = useQuery({
    queryKey: ["openapi-public-https", sub, clusterRg, clusterName],
    queryFn: () => aksApi.openApiPublicHttpsStatus(sub, clusterRg, clusterName),
    enabled: enabled && Boolean(clusterName),
    staleTime: 60_000,
    retry: 1,
  });
  const publicHttpsBaseUrl =
    publicHttpsQuery.data?.enabled && publicHttpsQuery.data.public_base_url
      ? publicHttpsQuery.data.public_base_url
      : null;

  // Internal LB URL — always shown in the hero so operators can see the
  // in-VNet endpoint even when the public HTTPS endpoint is exposed.
  const internalBaseUrl = svcQuery.data?.external_ip
    ? `http://${svcQuery.data.external_ip}`
    : null;

  // `baseUrl` drives spec parsing + curl examples — prefer the public
  // HTTPS endpoint when present so the "Try it" surface points at the
  // externally-reachable URL.
  const baseUrl = publicHttpsBaseUrl ?? internalBaseUrl;
  const serviceMissingOrPending = svcQuery.isSuccess && !svcQuery.data?.external_ip;

  const specQuery = useQuery({
    queryKey: ["openapi-spec", sub, clusterRg, clusterName],
    queryFn: () => aksApi.proxyOpenApiSpec(sub, clusterRg, clusterName),
    enabled: Boolean(baseUrl),
    staleTime: 60_000,
    // While the pod is still cold-starting (image pull on a fresh node), the
    // spec route degrades to `openapi_pod_starting`. Poll until it serves so
    // the "Starting…" panel flips to the live API Reference on its own — no
    // manual refresh. A crash-looping pod (`openapi_pod_not_ready`) is NOT
    // auto-polled to avoid hammering a known-bad rollout.
    refetchInterval: (query) => {
      const data = query.state.data as OpenApiSpecDegraded | undefined;
      return data?.degraded_reason === "openapi_pod_starting" ? 8_000 : false;
    },
  });

  const deploymentQuery = useQuery({
    queryKey: ["openapi-deployment", sub, clusterRg, clusterName],
    queryFn: () => aksApi.openApiDeployment(sub, clusterRg, clusterName),
    enabled: Boolean(baseUrl && hasOpenApiImage && clusterName),
    staleTime: 60_000,
    retry: 1,
  });

  const spec = useMemo(() => {
    if (!specQuery.data || !baseUrl) return null;
    const parsed = parseSpec(specQuery.data, baseUrl);
    // Drop endpoints whose tags are not declared in the spec's top-level
    // `tags` list. These surface as the "Other / Ungrouped" group and are
    // the upstream's external direct-caller contract (prefix
    // `/api/v1/elastic-blast`, tag "External ElasticBLAST"). They are not
    // reachable through the dashboard's internal proxy — the proxy allowlist
    // intentionally only permits `/v1/`, `/healthz`, `/openapi.json`,
    // `/docs/` — so exercising them here always fails with
    // `openapi_path_not_allowlisted`. Hide them rather than widen the
    // security allowlist; external callers use the published spec directly.
    const declaredTags = new Set(parsed.tags.map((tag) => tag.name));
    const endpoints = parsed.endpoints.filter((endpoint) =>
      endpoint.tags.some((tag) => declaredTags.has(tag)),
    );
    return { ...parsed, endpoints };
  }, [specQuery.data, baseUrl]);

  const contractLoading =
    enabled &&
    !clusterStopped &&
    (clustersQuery.isLoading ||
      acrQuery.isLoading ||
      svcQuery.isLoading ||
      (Boolean(baseUrl) && specQuery.isLoading));

  const grouped = useMemo(() => {
    if (!spec) return [];
    // `spec.endpoints` is already filtered to declared-tag endpoints (the
    // untagged "Other" group is dropped in the `spec` memo above), so every
    // endpoint here belongs to exactly one rendered tag section.
    return spec.tags
      .map((tag) => ({
        tag,
        endpoints: spec.endpoints.filter((endpoint) => endpoint.tags.includes(tag.name)),
      }))
      .filter((group) => group.endpoints.length > 0);
  }, [spec]);

  // The spec-derived tag sections (System, Cluster, Databases, Jobs …) only
  // render when the cluster is up and the live OpenAPI document parsed. The
  // always-on Core control-plane section, by contrast, must render even while
  // the cluster is stopped (its ensure-running endpoint is how the cluster is
  // woken), so it is placed inside the same two-column layout but gated
  // separately from the spec groups below.
  const showApiGroups = Boolean(spec && grouped.length > 0 && !clusterStopped);

  return (
    <div className="page-stack mono-page api-reference-page">
      <ApiHero
        spec={spec}
        baseUrl={internalBaseUrl ?? baseUrl}
        publicHttpsUrl={publicHttpsBaseUrl}
        imageTag={deploymentQuery.data?.image_tag}
        onRefresh={() => specQuery.refetch()}
        refreshing={specQuery.isFetching}
      />

      {enabled && hasMultipleClusters && (
        <ClusterPicker
          clusters={candidates}
          selectedName={clusterName}
          onSelect={onSelectCluster}
        />
      )}

      {(!enabled || svcQuery.isLoading || clustersQuery.isLoading) &&
        enabled &&
        !clusterStopped && <OpenApiLoadingState />}

      {!enabled && <MissingConfigState />}

      {enabled && clusterStopped && (
        <ClusterStoppedState
          clusterName={firstCluster?.name ?? ""}
          powerState={firstCluster?.power_state ?? "Unknown"}
          region={firstCluster?.region ?? ""}
          refreshing={clustersQuery.isFetching}
          onRefresh={() => clustersQuery.refetch()}
        />
      )}

      {enabled && !clusterStopped && acrQuery.isSuccess && !hasOpenApiImage && (
        <MissingOpenApiImageState />
      )}

      {(svcQuery.isError || serviceMissingOrPending) && !clusterStopped && (
        <OpenApiDeployPanel
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
          acrName={acrName}
          acrResourceGroup={acrRg}
          storageAccount={savedConfig?.storageAccountName ?? ""}
          storageResourceGroup={anchorRg}
          imageBuilt={hasOpenApiImage}
          onRetry={() => svcQuery.refetch()}
          retrying={svcQuery.isFetching}
        />
      )}

      {baseUrl &&
        hasOpenApiImage &&
        !clusterStopped &&
        (() => {
          // Prompt a redeploy when EITHER the pinned image tag changed
          // upstream OR the live Deployment's manifest predates this
          // dashboard's generation (a redeploy-only change such as the
          // single-replica queue owner). The image-tag redeploy also
          // re-applies the manifest, so when both are true we show only the
          // image panel to avoid stacking two identical actions. The
          // OpenAPI document's `info.version` is the API app version, not the
          // image tag, so it is not a reliable update signal.
          const pinnedTag = acrQuery.data?.expected_image_tags?.["elb-openapi"];
          const deployedTag = deploymentQuery.data?.image_tag;
          const imageOutdated = Boolean(
            pinnedTag &&
              deployedTag &&
              normaliseImageTag(pinnedTag) !== normaliseImageTag(deployedTag),
          );
          const dep = deploymentQuery.data;
          const manifestOutdated = Boolean(dep?.manifest_outdated);
          // A live deployment whose api image predates manifest-drift detection
          // returns NO `manifest_outdated` field at all (vs. an explicit
          // `false` from a current api). Distinguish the two so the operator
          // isn't left wondering why no redeploy prompt appears: an absent
          // signal means the CONTROL PLANE (api image) needs a redeploy before
          // elb-openapi drift can even be evaluated.
          const manifestSignalMissing =
            dep != null && dep.manifest_outdated === undefined;
          // The deployment read itself failed (workload-cluster unreachable /
          // kubectl RBAC). Surface it instead of silently dropping the banner —
          // a "redeploy needed" prompt that fails closed is worse than a noisy
          // one, because the operator assumes everything is current. A 404
          // (`openapi_deployment_not_found`) is NOT a read failure though — it
          // means elb-openapi simply isn't deployed yet, which the
          // service-IP-driven Deploy panel above already handles. Treating it
          // as "unreachable / missing RBAC" mislabels a fresh cluster, so
          // exclude it here.
          const deploymentReadFailed =
            deploymentQuery.isError &&
            (deploymentQuery.error as Partial<ApiError> | null)?.status !== 404;

          if (imageOutdated || manifestOutdated) {
            return (
              <OpenApiDeployPanel
                variant="update"
                reason={imageOutdated ? "image" : "manifest"}
                subscriptionId={sub}
                resourceGroup={clusterRg}
                clusterName={clusterName}
                acrName={acrName}
                acrResourceGroup={acrRg}
                storageAccount={savedConfig?.storageAccountName ?? ""}
                storageResourceGroup={anchorRg}
                imageBuilt={hasOpenApiImage}
                onRetry={() => {
                  svcQuery.refetch();
                  specQuery.refetch();
                  deploymentQuery.refetch();
                }}
                retrying={
                  svcQuery.isFetching || specQuery.isFetching || deploymentQuery.isFetching
                }
                pinnedTag={pinnedTag}
                currentTag={deployedTag}
              />
            );
          }
          if (deploymentReadFailed || manifestSignalMissing) {
            return (
              <OpenApiManifestDiagnostic
                reason={deploymentReadFailed ? "read_failed" : "signal_missing"}
                retrying={deploymentQuery.isFetching}
                onRetry={() => deploymentQuery.refetch()}
              />
            );
          }
          return null;
        })()}

      {baseUrl && specQuery.isLoading && !clusterStopped && <SpecLoadingState />}

      {specQuery.isError && !clusterStopped && (
        <SpecErrorState
          message={(specQuery.error as Error).message}
          showGrantRbac={isGrantLbSubnetRbacRecovery(specQuery.error)}
          showRepair={isPeerWithPlatformRecovery(specQuery.error)}
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
          onResolved={() => specQuery.refetch()}
        />
      )}

      {specQuery.isSuccess && !clusterStopped && isGrantLbSubnetRbacRecovery(specQuery.data) && (
        <SpecErrorState
          message="The elb-openapi internal LoadBalancer has no IP yet — the AKS cluster identity is missing Network Contributor on its node subnet."
          showGrantRbac
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
          onResolved={() => specQuery.refetch()}
        />
      )}

      {/* Spec returned 200 with `degraded: true` — the api sidecar reached
          the proxy route but the upstream elb-openapi did not answer. Render
          the same recovery affordance as the hard error case. */}
      {specQuery.isSuccess &&
        !clusterStopped &&
        !isGrantLbSubnetRbacRecovery(specQuery.data) &&
        isPeerWithPlatformRecovery(specQuery.data) && (
        <SpecErrorState
          message="The elb-openapi service did not respond. The dashboard could not load the live OpenAPI spec."
          showRepair
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
          onResolved={() => specQuery.refetch()}
        />
      )}

      {/* Spec returned 200 with `degraded_reason: openapi_pod_starting` (or
          `openapi_pod_not_ready`) — the elb-openapi pod is still booting (image
          cold-pull on a fresh node) or restarting. This is NOT a peering break,
          so render a calm "Starting…" state instead of the red repair error. */}
      {specQuery.isSuccess &&
        !clusterStopped &&
        readOpenApiPodStartup(specQuery.data) && (
          <OpenApiPodStartingState
            data={readOpenApiPodStartup(specQuery.data)!}
            refreshing={specQuery.isFetching}
            onRefresh={() => specQuery.refetch()}
          />
        )}

      {baseUrl && hasOpenApiImage && clusterName && !clusterStopped && (
        <PlsTransitionBanner
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
          acrName={acrName}
          acrResourceGroup={acrRg}
          storageAccount={savedConfig?.storageAccountName ?? ""}
          storageResourceGroup={anchorRg}
        />
      )}

      {baseUrl && hasOpenApiImage && clusterName && !clusterStopped && (
        <ApiTokenPanel
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
        />
      )}

      {!clusterStopped && <ApiResponseContractPanel loading={contractLoading} />}

      {/* Two-column layout — sticky sidebar (tag list + endpoint search +
          method chips) on the left, tag sections on the right. The Core
          control-plane section is the FIRST section in the right column (above
          the spec-derived "System" group) so it reads consistently with the
          other groups, sharing the same card flow but keeping its teal accent +
          host banner. It renders even while the cluster is stopped (the spec
          groups and sidebar do not), because its ensure-running endpoint is
          exactly how the cluster is woken. */}
      {enabled && clusterName && (
        <div
          className="api-reference-layout"
          style={
            showApiGroups
              ? {
                  display: "grid",
                  gridTemplateColumns: "minmax(240px, 280px) 1fr",
                  gap: 16,
                  alignItems: "start",
                }
              : { display: "flex", flexDirection: "column", gap: 16 }
          }
        >
          {showApiGroups && <ApiReferenceSidebar groups={grouped} />}
          <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
            <CoreApiSection
              context={{
                subscriptionId: sub,
                resourceGroup: clusterRg,
                clusterName,
              }}
              originLabel={typeof window !== "undefined" ? window.location.origin : ""}
            />
            {showApiGroups &&
              grouped.map(({ tag, endpoints }) => (
                <TagSection
                  key={tag.name}
                  tag={tag}
                  endpoints={endpoints}
                  baseUrl={spec!.baseUrl}
                  proxyInfo={{ sub, rg: clusterRg, clusterName }}
                />
              ))}
          </div>
        </div>
      )}
    </div>
  );
}

