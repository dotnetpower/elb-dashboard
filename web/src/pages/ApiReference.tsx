import { useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Package, Power, RefreshCw, Server } from "lucide-react";
import { Link } from "react-router-dom";

import { aksApi, monitoringApi } from "@/api/endpoints";
import { OpenApiDeployPanel } from "@/components/OpenApiDeployPanel";
import { loadSavedConfig } from "@/components/SetupWizard";
import { ApiHero } from "@/pages/apiReference/ApiHero";
import { ApiReferenceSidebar } from "@/pages/apiReference/ApiReferenceSidebar";
import { ApiResponseContractPanel } from "@/pages/apiReference/ApiResponseContractPanel";
import { ApiTokenPanel } from "@/pages/apiReference/ApiTokenPanel";
import { resolveApiReferenceClusterContext } from "@/pages/apiReference/clusterContext";
import { SVC_NAME } from "@/pages/apiReference/constants";
import { parseSpec } from "@/pages/apiReference/spec";
import { TagSection } from "@/pages/apiReference/TagSection";
import { isAksWorkloadReady } from "@/utils/aksStatus";

function normaliseImageTag(value: string): string {
  return value.trim().replace(/^v/i, "");
}

export function ApiReference() {
  const [savedConfig] = useState(() => loadSavedConfig());

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
  } = resolveApiReferenceClusterContext({
    clusters,
    anchorResourceGroup: anchorRg,
  });
  const clusterStopped = firstCluster && !isAksWorkloadReady(firstCluster);

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
  const baseUrl = svcQuery.data?.external_ip
    ? `http://${svcQuery.data.external_ip}`
    : null;
  const serviceMissingOrPending = svcQuery.isSuccess && !svcQuery.data?.external_ip;

  const specQuery = useQuery({
    queryKey: ["openapi-spec", sub, clusterRg, clusterName],
    queryFn: () => aksApi.proxyOpenApiSpec(sub, clusterRg, clusterName),
    enabled: Boolean(baseUrl),
    staleTime: 60_000,
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
    return parseSpec(specQuery.data, baseUrl);
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
    const byTag = spec.tags
      .map((tag) => ({
        tag,
        endpoints: spec.endpoints.filter((endpoint) => endpoint.tags.includes(tag.name)),
      }))
      .filter((group) => group.endpoints.length > 0);

    const tagged = new Set(byTag.flatMap((group) => group.endpoints));
    const untagged = spec.endpoints.filter((endpoint) => !tagged.has(endpoint));
    if (untagged.length > 0) {
      byTag.push({
        tag: { name: "Other", description: "Ungrouped endpoints" },
        endpoints: untagged,
      });
    }
    return byTag;
  }, [spec]);

  return (
    <div className="page-stack mono-page api-reference-page">
      <ApiHero
        spec={spec}
        baseUrl={baseUrl}
        onRefresh={() => specQuery.refetch()}
        refreshing={specQuery.isFetching}
      />

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
          storageAccount={savedConfig?.storageAccountName ?? ""}
          storageResourceGroup={anchorRg}
          imageBuilt={hasOpenApiImage}
          onRetry={() => svcQuery.refetch()}
          retrying={svcQuery.isFetching}
        />
      )}

      {baseUrl &&
        hasOpenApiImage &&
        (() => {
          // Only render the update panel when the actual deployed container
          // image tag differs from the dashboard-pinned tag. The OpenAPI
          // document's `info.version` is the API app version, not the image
          // tag, so it is not a reliable update signal.
          const pinnedTag = acrQuery.data?.expected_image_tags?.["elb-openapi"];
          const deployedTag = deploymentQuery.data?.image_tag;
          if (
            !pinnedTag ||
            !deployedTag ||
            normaliseImageTag(pinnedTag) === normaliseImageTag(deployedTag)
          ) {
            return null;
          }
          return (
            <OpenApiDeployPanel
              variant="update"
              subscriptionId={sub}
              resourceGroup={clusterRg}
              clusterName={clusterName}
              acrName={acrName}
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
        })()}

      {baseUrl && specQuery.isLoading && <SpecLoadingState />}

      {specQuery.isError && (
        <SpecErrorState message={(specQuery.error as Error).message} />
      )}

      {baseUrl && hasOpenApiImage && clusterName && (
        <ApiTokenPanel
          subscriptionId={sub}
          resourceGroup={clusterRg}
          clusterName={clusterName}
        />
      )}

      <ApiResponseContractPanel loading={contractLoading} />

      {spec && grouped.length > 0 && (
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

function SpecErrorState({ message }: { message: string }) {
  return (
    <PanelState border="1px solid rgba(242,114,111,0.2)" padding="16px 20px">
      <AlertTriangle
        size={14}
        style={{ color: "var(--danger)", verticalAlign: "middle", marginRight: 6 }}
      />
      <span style={{ fontSize: 12 }}>Failed to load openapi.json: {message}</span>
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
