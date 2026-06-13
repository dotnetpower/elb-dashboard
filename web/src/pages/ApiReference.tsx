import { useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Package, Power, RefreshCw, Server } from "lucide-react";
import { Link } from "react-router-dom";

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
  RepairPeeringButton,
  isPeerWithPlatformRecovery,
} from "@/pages/apiReference/RepairPeeringButton";
import {
  GrantLbSubnetRbacButton,
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

      {/* Always-on control-plane section. Rendered whenever a cluster context
          is known — including while the cluster is stopped — because its
          ensure-running endpoint is exactly how the cluster is woken. It lives
          on a different host (the dashboard api sidecar) than the spec-derived
          elb-openapi groups below, hence the distinct accent + host banner. */}
      {enabled && clusterName && (
        <CoreApiSection
          context={{
            subscriptionId: sub,
            resourceGroup: clusterRg,
            clusterName,
          }}
          originLabel={typeof window !== "undefined" ? window.location.origin : ""}
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

      {spec && grouped.length > 0 && !clusterStopped && (
        // A1: two-column layout — sticky sidebar (tag list + endpoint search +
        // method chips) on the left, tag sections on the right. The sidebar is
        // sticky to `top: 16px` so scrolling through long specs still keeps
        // navigation in reach without taking over the viewport.
        <div
          className="api-reference-layout"
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(240px, 280px) 1fr",
            gap: 16,
            alignItems: "start",
          }}
        >
          <ApiReferenceSidebar groups={grouped} />
          <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
            {grouped.map(({ tag, endpoints }) => (
              <TagSection
                key={tag.name}
                tag={tag}
                endpoints={endpoints}
                baseUrl={spec.baseUrl}
                proxyInfo={{ sub, rg: clusterRg, clusterName }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function OpenApiLoadingState() {
  return <ApiReferenceSkeleton label="Discovering OpenAPI service on AKS" />;
}

function MissingConfigState() {
  return (
    <PanelState border="1px solid var(--border-weak)" textAlign="center">
      <AlertTriangle size={20} style={{ color: "var(--warning)", marginBottom: 8 }} />
      <p style={{ color: "var(--text-muted)", fontSize: 13 }}>
        Configure Subscription and Workload RG in the Dashboard first.
      </p>
    </PanelState>
  );
}

function ClusterPicker({
  clusters,
  selectedName,
  onSelect,
}: {
  clusters: import("@/api/endpoints").AksClusterSummary[];
  selectedName: string;
  onSelect: (name: string) => void;
}) {
  // Issues-first ordering so the running cluster surfaces above the
  // stopped one. Mirrors the Dashboard's ClusterCard sort so the user
  // sees the same ordering on both surfaces.
  const sorted = useMemo(() => {
    const bucket = (c: import("@/api/endpoints").AksClusterSummary): number => {
      if (isAksWorkloadReady(c)) return 0;
      if (c.power_state === "Stopped") return 2;
      return 1;
    };
    return [...clusters].sort((a, b) => {
      const ba = bucket(a);
      const bb = bucket(b);
      if (ba !== bb) return ba - bb;
      return a.name.localeCompare(b.name);
    });
  }, [clusters]);

  return (
    <PanelState border="1px solid var(--border-weak)">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <Server size={14} style={{ color: "var(--text-faint)" }} />
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--text-faint)",
          }}
        >
          Cluster
        </span>
        <div
          role="radiogroup"
          aria-label="Select an AKS cluster for the OpenAPI service"
          style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
        >
          {sorted.map((c) => {
            const running = isAksWorkloadReady(c);
            const isSelected = c.name === selectedName;
            const dotColor = running
              ? "var(--success)"
              : c.power_state === "Stopped"
                ? "var(--danger)"
                : "var(--text-faint)";
            return (
              <button
                key={`${c.resource_group}/${c.name}`}
                type="button"
                role="radio"
                aria-checked={isSelected}
                onClick={() => onSelect(c.name)}
                title={`${c.name} (${c.resource_group}) · ${
                  running ? "Running" : c.power_state ?? "Unknown"
                }`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "4px 10px",
                  fontSize: 12,
                  fontWeight: isSelected ? 600 : 500,
                  color: isSelected
                    ? "var(--text-primary)"
                    : "var(--text-secondary)",
                  background: isSelected
                    ? "rgba(122,167,255,0.12)"
                    : "transparent",
                  border: `1px solid ${
                    isSelected ? "var(--accent)" : "var(--border-medium)"
                  }`,
                  borderRadius: 6,
                  cursor: "pointer",
                  lineHeight: 1.2,
                }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: dotColor,
                    flexShrink: 0,
                  }}
                />
                {c.name}
                {!running && (
                  <span
                    style={{
                      fontSize: 10,
                      color: "var(--text-faint)",
                      fontWeight: 500,
                    }}
                  >
                    ({c.power_state ?? "stopped"})
                  </span>
                )}
              </button>
            );
          })}
        </div>
        <span
          style={{
            marginLeft: "auto",
            fontSize: 11,
            color: "var(--text-faint)",
          }}
        >
          Selection persists per browser.
        </span>
      </div>
    </PanelState>
  );
}

function ClusterStoppedState({
  clusterName,
  powerState,
  region,
  refreshing,
  onRefresh,
}: {
  clusterName: string;
  powerState: string;
  region: string;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  return (
    <PanelState border="1px solid rgba(242,153,74,0.2)">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <StateIcon background="rgba(242,153,74,0.1)">
          <Power size={18} style={{ color: "var(--warning)" }} />
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>AKS cluster is stopped</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            The OpenAPI service runs inside the AKS cluster. Start the cluster to access
            the API.
          </div>
        </div>
      </div>
      <div
        style={{
          display: "flex",
          gap: 8,
          flexWrap: "wrap",
          padding: "12px 16px",
          background: "var(--bg-secondary)",
          borderRadius: 8,
          fontSize: 12,
          color: "var(--text-muted)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Server size={12} style={{ color: "var(--text-faint)" }} />
          <span>{clusterName}</span>
        </div>
        <span style={{ color: "var(--border-medium)" }}>·</span>
        <span style={{ color: "var(--warning)", fontWeight: 600 }}>{powerState}</span>
        <span style={{ color: "var(--border-medium)" }}>·</span>
        <span>{region}</span>
      </div>
      <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
        <Link
          to="/"
          className="glass-button glass-button--primary"
          style={{ fontSize: 12, textDecoration: "none" }}
        >
          <Power size={12} /> Go to Dashboard to start cluster
        </Link>
        <button
          className="glass-button"
          onClick={onRefresh}
          disabled={refreshing}
          style={{ fontSize: 12 }}
        >
          <RefreshCw size={12} className={refreshing ? "spin" : ""} /> Refresh
        </button>
      </div>
    </PanelState>
  );
}

function MissingOpenApiImageState() {
  return (
    <PanelState border="1px solid rgba(184,119,217,0.2)">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <StateIcon background="rgba(184,119,217,0.1)">
          <Package size={18} style={{ color: "var(--purple)" }} />
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>OpenAPI image not built</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            The <InlineCode>elb-openapi</InlineCode> container image needs to be built in
            your ACR before deploying the API service.
          </div>
        </div>
      </div>
      <Link
        to="/"
        className="glass-button glass-button--primary"
        style={{ fontSize: 12, textDecoration: "none" }}
      >
        <Package size={12} /> Build images from Dashboard ACR card
      </Link>
    </PanelState>
  );
}

function SpecLoadingState() {
  return <ApiReferenceSkeleton compact label="Loading API specification" />;
}

function OpenApiManifestDiagnostic({
  reason,
  retrying,
  onRetry,
}: {
  reason: "read_failed" | "signal_missing";
  retrying: boolean;
  onRetry: () => void;
}) {
  const readFailed = reason === "read_failed";
  return (
    <PanelState border="1px solid rgba(242,153,74,0.2)">
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <StateIcon background="rgba(242,153,74,0.1)">
          <AlertTriangle size={16} style={{ color: "var(--warning)" }} />
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>
            {readFailed
              ? "elb-openapi deployment status unavailable"
              : "Redeploy detection not available yet"}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {readFailed ? (
              <>
                The dashboard could not read the live{" "}
                <InlineCode>elb-openapi</InlineCode> deployment (workload-cluster
                unreachable or missing kubectl RBAC), so it cannot tell whether a
                redeploy is needed. Resolve cluster access, then refresh.
              </>
            ) : (
              <>
                This control plane (<InlineCode>api</InlineCode> image) predates
                manifest-drift detection, so it never reports whether the live{" "}
                <InlineCode>elb-openapi</InlineCode> manifest is outdated. Redeploy
                the control plane (rebuild + roll the <InlineCode>api</InlineCode>{" "}
                image) to enable the redeploy prompt.
              </>
            )}
          </div>
        </div>
      </div>
      <button
        type="button"
        className="glass-button"
        style={{ fontSize: 12 }}
        onClick={onRetry}
        disabled={retrying}
      >
        <RefreshCw size={12} /> {retrying ? "Refreshing…" : "Refresh"}
      </button>
    </PanelState>
  );
}

function ApiReferenceSkeleton({
  label,
  compact = false,
}: {
  label: string;
  compact?: boolean;
}) {
  return (
    <section
      className="api-reference-skeleton"
      aria-label={label}
      aria-busy="true"
      style={{
        display: "grid",
        gridTemplateColumns: compact
          ? "minmax(180px, 240px) 1fr"
          : "minmax(220px, 280px) 1fr",
        gap: 16,
        alignItems: "start",
      }}
    >
      <div
        className="glass-card"
        style={{
          padding: 14,
          display: "grid",
          gap: 12,
          position: "sticky",
          top: 16,
        }}
      >
        <SkeletonLine width="58%" height={12} />
        <SkeletonLine width="92%" height={30} radius={7} />
        <div style={{ display: "flex", gap: 6 }}>
          <SkeletonLine width="44px" height={20} radius={5} />
          <SkeletonLine width="54px" height={20} radius={5} />
          <SkeletonLine width="48px" height={20} radius={5} />
        </div>
        {[0, 1, 2, 3].map((index) => (
          <div
            key={index}
            style={{
              display: "grid",
              gridTemplateColumns: "44px 1fr",
              gap: 8,
              alignItems: "center",
            }}
          >
            <SkeletonLine width="40px" height={18} radius={5} />
            <SkeletonLine width={index % 2 === 0 ? "86%" : "68%"} height={11} />
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gap: 14, minWidth: 0 }}>
        <div className="glass-card" style={{ padding: 16, display: "grid", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Loader2 size={15} className="spin" style={{ color: "var(--accent)" }} />
            <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{label}</span>
          </div>
          <SkeletonLine width="46%" height={14} />
          <SkeletonLine width="72%" height={11} />
        </div>

        {[0, 1, 2].map((index) => (
          <ApiEndpointSkeleton key={index} index={index} />
        ))}
      </div>
    </section>
  );
}

function ApiEndpointSkeleton({ index }: { index: number }) {
  const methodWidths = [44, 50, 42];
  return (
    <div
      className="endpoint-card"
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: 10,
        overflow: "hidden",
        boxShadow: "var(--shadow-panel)",
      }}
    >
      <div
        className="endpoint-card__header"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "12px 16px",
        }}
      >
        <SkeletonLine width={`${methodWidths[index] ?? 44}px`} height={25} radius={5} />
        <SkeletonLine width={index === 0 ? "190px" : "260px"} height={13} />
        <SkeletonLine width="140px" height={12} />
        <div style={{ display: "flex", gap: 4, marginLeft: "auto" }}>
          <SkeletonLine width="96px" height={20} radius={5} />
          <SkeletonLine width="86px" height={20} radius={5} />
        </div>
      </div>
      {index === 0 && (
        <div
          className="endpoint-card__body"
          style={{
            borderTop: "1px solid var(--border-weak)",
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
          }}
        >
          <div style={{ padding: "16px 20px", display: "grid", gap: 12 }}>
            <SkeletonLine width="72%" height={12} />
            <SkeletonLine width="28%" height={10} />
            <SkeletonLine width="100%" height={38} radius={7} />
            <SkeletonLine width="28%" height={10} />
            <SkeletonLine width="100%" height={54} radius={7} />
          </div>
          <div
            style={{
              padding: "16px 20px",
              borderLeft: "1px solid var(--border-weak)",
              background: "var(--bg-secondary)",
              display: "grid",
              gap: 12,
            }}
          >
            <SkeletonLine width="24%" height={10} />
            <SkeletonLine width="100%" height={130} radius={8} />
          </div>
        </div>
      )}
    </div>
  );
}

function SkeletonLine({
  width,
  height,
  radius = 999,
}: {
  width: string;
  height: number;
  radius?: number;
}) {
  return (
    <span
      className="skeleton"
      style={{
        display: "block",
        width,
        height,
        borderRadius: radius,
      }}
    />
  );
}

function SpecErrorState({
  message,
  showRepair,
  showGrantRbac,
  subscriptionId,
  resourceGroup,
  clusterName,
  onResolved,
}: {
  message: string;
  showRepair?: boolean;
  showGrantRbac?: boolean;
  subscriptionId?: string;
  resourceGroup?: string;
  clusterName?: string;
  onResolved?: () => void;
}) {
  const hasTarget = Boolean(subscriptionId && resourceGroup && clusterName);
  return (
    <PanelState border="1px solid rgba(242,114,111,0.2)" padding="16px 20px">
      <AlertTriangle
        size={14}
        style={{ color: "var(--danger)", verticalAlign: "middle", marginRight: 6 }}
      />
      <span style={{ fontSize: 12 }}>Failed to load openapi.json: {message}</span>
      {showGrantRbac && hasTarget && (
        <GrantLbSubnetRbacButton
          subscriptionId={subscriptionId!}
          resourceGroup={resourceGroup!}
          clusterName={clusterName!}
          onResolved={() => onResolved?.()}
          size="block"
        />
      )}
      {showRepair && !showGrantRbac && hasTarget && (
        <RepairPeeringButton
          subscriptionId={subscriptionId!}
          resourceGroup={resourceGroup!}
          clusterName={clusterName!}
          onResolved={() => onResolved?.()}
          size="block"
        />
      )}
    </PanelState>
  );
}

function OpenApiPodStartingState({
  data,
  refreshing,
  onRefresh,
}: {
  data: OpenApiSpecDegraded;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  // `openapi_pod_starting` is benign and self-resolving (the page auto-polls
  // and flips to the live API Reference once the pod serves).
  // `openapi_pod_not_ready` means the pod is up but failing readiness (e.g.
  // CrashLoopBackOff) — still NOT a peering problem, so we surface a muted
  // warning that points at the pod logs rather than the "Repair VNet peering"
  // affordance.
  const failed = data.degraded_reason === "openapi_pod_not_ready";
  const accent = failed ? "var(--warning)" : "var(--accent)";
  const tint = failed ? "rgba(242,153,74,0.1)" : "rgba(122,167,255,0.1)";
  const border = failed
    ? "1px solid rgba(242,153,74,0.2)"
    : "1px solid rgba(122,167,255,0.2)";
  const title = failed ? "elb-openapi pod is not ready" : "elb-openapi is starting";
  const message =
    data.pod_message ??
    (failed
      ? "The elb-openapi pod is up but not passing its readiness check. Check the pod logs."
      : "The elb-openapi pod is starting. This usually finishes within ~2 minutes on a fresh node while the container image is pulled.");
  return (
    <PanelState border={border}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
        <StateIcon background={tint}>
          {failed ? (
            <AlertTriangle size={16} style={{ color: accent }} />
          ) : (
            <Loader2 size={16} className="spin" style={{ color: accent }} />
          )}
        </StateIcon>
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{title}</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{message}</div>
        </div>
      </div>
      <button
        type="button"
        className="glass-button"
        style={{ fontSize: 12 }}
        onClick={onRefresh}
        disabled={refreshing}
      >
        <RefreshCw size={12} className={refreshing ? "spin" : ""} />{" "}
        {refreshing ? "Checking…" : "Check again"}
      </button>
    </PanelState>
  );
}

function PanelState({
  children,
  border,
  padding = "24px 28px",
  textAlign,
}: {
  children: ReactNode;
  border: string;
  padding?: string;
  textAlign?: CSSProperties["textAlign"];
}) {
  return (
    <div
      style={{
        background: "var(--bg-primary)",
        border,
        borderRadius: 10,
        padding,
        textAlign,
      }}
    >
      {children}
    </div>
  );
}

function StateIcon({
  children,
  background,
}: {
  children: ReactNode;
  background: string;
}) {
  return (
    <div
      style={{
        width: 36,
        height: 36,
        borderRadius: 10,
        background,
        display: "grid",
        placeItems: "center",
      }}
    >
      {children}
    </div>
  );
}

function InlineCode({ children }: { children: ReactNode }) {
  return (
    <code
      style={{
        fontFamily: "var(--font-mono)",
        background: "var(--bg-tertiary)",
        padding: "1px 5px",
        borderRadius: 3,
      }}
    >
      {children}
    </code>
  );
}
