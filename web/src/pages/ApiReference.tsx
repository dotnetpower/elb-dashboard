import { useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Loader2, Package, Power, RefreshCw, Server } from "lucide-react";
import { Link } from "react-router-dom";

import { aksApi, monitoringApi } from "@/api/endpoints";
import { OpenApiDeployPanel } from "@/components/OpenApiDeployPanel";
import { loadSavedConfig } from "@/components/SetupWizard";
import { ApiHero } from "@/pages/apiReference/ApiHero";
import { SVC_NAME } from "@/pages/apiReference/constants";
import { parseSpec } from "@/pages/apiReference/spec";
import { TagSection } from "@/pages/apiReference/TagSection";

export function ApiReference() {
  const [savedConfig] = useState(() => loadSavedConfig());

  const sub = savedConfig?.subscriptionId ?? "";
  const rg = savedConfig?.workloadResourceGroup ?? "";
  const enabled = Boolean(sub && rg);

  const clustersQuery = useQuery({
    queryKey: ["aks", sub, rg],
    queryFn: () => monitoringApi.aks(sub, rg),
    enabled,
    staleTime: 300_000,
  });
  const clusterName = clustersQuery.data?.clusters?.[0]?.name ?? "";
  const clusters = clustersQuery.data?.clusters ?? [];
  const firstCluster = clusters[0];
  const clusterStopped = firstCluster && firstCluster.power_state !== "Running";

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
    queryKey: ["openapi-svc", sub, rg, clusterName],
    queryFn: () => monitoringApi.serviceIp(sub, rg, clusterName, SVC_NAME),
    enabled: enabled && Boolean(clusterName),
    staleTime: 300_000,
    retry: 1,
  });
  const baseUrl = svcQuery.data ? `http://${svcQuery.data.external_ip}` : null;

  const specQuery = useQuery({
    queryKey: ["openapi-spec", sub, rg, clusterName],
    queryFn: () => aksApi.proxyOpenApiSpec(sub, rg, clusterName),
    enabled: Boolean(baseUrl),
    staleTime: 60_000,
  });

  const spec = useMemo(() => {
    if (!specQuery.data || !baseUrl) return null;
    return parseSpec(specQuery.data, baseUrl);
  }, [specQuery.data, baseUrl]);

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

      {svcQuery.isError && !clusterStopped && (
        <OpenApiDeployPanel
          subscriptionId={sub}
          resourceGroup={rg}
          clusterName={clusterName}
          acrName={acrName}
          storageAccount={savedConfig?.storageAccountName ?? ""}
          imageBuilt={hasOpenApiImage}
          onRetry={() => svcQuery.refetch()}
          retrying={svcQuery.isFetching}
        />
      )}

      {baseUrl && hasOpenApiImage && (
        <OpenApiDeployPanel
          variant="update"
          subscriptionId={sub}
          resourceGroup={rg}
          clusterName={clusterName}
          acrName={acrName}
          storageAccount={savedConfig?.storageAccountName ?? ""}
          imageBuilt={hasOpenApiImage}
          onRetry={() => {
            svcQuery.refetch();
            specQuery.refetch();
          }}
          retrying={svcQuery.isFetching || specQuery.isFetching}
          pinnedTag={acrQuery.data?.expected_image_tags?.["elb-openapi"]}
          currentTag={acrQuery.data?.actual_tags?.["elb-openapi"]?.[0]}
        />
      )}

      {baseUrl && specQuery.isLoading && <SpecLoadingState />}

      {specQuery.isError && <SpecErrorState message={(specQuery.error as Error).message} />}

      {spec &&
        grouped.map(({ tag, endpoints }) => (
          <TagSection
            key={tag.name}
            tag={tag}
            endpoints={endpoints}
            baseUrl={spec.baseUrl}
            proxyInfo={{ sub, rg, clusterName }}
          />
        ))}
    </div>
  );
}

function OpenApiLoadingState() {
  return (
    <CenteredState>
      <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
      <p style={{ color: "var(--text-faint)", fontSize: 12 }}>
        Discovering OpenAPI service on AKS...
      </p>
    </CenteredState>
  );
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
            The OpenAPI service runs inside the AKS cluster. Start the cluster to access the API.
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
        <button className="glass-button" onClick={onRefresh} disabled={refreshing} style={{ fontSize: 12 }}>
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
            The <InlineCode>elb-openapi</InlineCode> container image needs to be built in your ACR before deploying the API service.
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
  return (
    <CenteredState compact>
      <Loader2 size={20} className="spin" style={{ color: "var(--accent)" }} />
      <p style={{ color: "var(--text-faint)", fontSize: 12 }}>Loading API specification...</p>
    </CenteredState>
  );
}

function SpecErrorState({ message }: { message: string }) {
  return (
    <PanelState border="1px solid rgba(242,114,111,0.2)" padding="16px 20px">
      <AlertTriangle size={14} style={{ color: "var(--danger)", verticalAlign: "middle", marginRight: 6 }} />
      <span style={{ fontSize: 12 }}>Failed to load openapi.json: {message}</span>
    </PanelState>
  );
}

function CenteredState({ children, compact = false }: { children: ReactNode; compact?: boolean }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        padding: compact ? "32px 0" : "48px 0",
        gap: compact ? 8 : 12,
      }}
    >
      {children}
    </div>
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

function StateIcon({ children, background }: { children: ReactNode; background: string }) {
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
