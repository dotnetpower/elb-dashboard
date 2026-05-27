import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Eye, EyeOff, KeyRound, Loader2, RefreshCw, RotateCcw } from "lucide-react";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";

export function ApiTokenPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}) {
  const [revealed, setRevealed] = useState(false);
  const queryClient = useQueryClient();
  const { copied, copyText } = useClipboardFeedback();
  const queryKey = ["openapi-token", subscriptionId, resourceGroup, clusterName];

  const tokenQuery = useQuery({
    queryKey,
    queryFn: () => aksApi.openApiToken(subscriptionId, resourceGroup, clusterName),
    enabled: Boolean(subscriptionId && resourceGroup && clusterName),
    staleTime: 30_000,
    retry: 1,
  });

  const tokenMutation = useMutation({
    mutationFn: (regenerate: boolean) =>
      aksApi.generateOpenApiToken(subscriptionId, resourceGroup, clusterName, regenerate),
    onSuccess: (data) => {
      queryClient.setQueryData(queryKey, data);
      setRevealed(true);
    },
  });

  const token = tokenMutation.data ?? tokenQuery.data;
  const configured = Boolean(token?.configured && token.token);
  const busy = tokenQuery.isLoading || tokenMutation.isPending;
  const visibleToken = configured
    ? revealed
      ? token?.token
      : token?.masked_token
    : "No API token generated";
  const message = tokenMutation.error
    ? formatApiError(tokenMutation.error, "aks")
    : tokenQuery.error
      ? formatApiError(tokenQuery.error, "aks")
      : null;
  // Backend tried to auto-heal a legacy `elb-openapi` deployment (no
  // ELB_OPENAPI_API_TOKEN env entry) and the patch failed. Surface the
  // exact code+message so the operator does not have to wonder why
  // "Generate" appears to do nothing — it is usually an AKS admin RBAC
  // gap on the cluster RG.
  const selfHealError = token?.self_heal_error ?? null;

  return (
    <section
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: "var(--radius)",
        padding: "12px 14px",
        display: "grid",
        gap: 12,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              background: "var(--bg-tertiary)",
              color: configured ? "var(--success)" : "var(--accent)",
            }}
          >
            <KeyRound size={15} strokeWidth={1.5} />
          </span>
          <div>
            <div style={{ fontSize: 14, fontWeight: 650, color: "var(--text-primary)" }}>
              API Token
            </div>
            <div style={{ fontSize: 11, color: "var(--text-faint)", fontFamily: "var(--font-mono)" }}>
              X-ELB-API-Token · {configured ? "configured" : "not configured"}
            </div>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="glass-button"
            onClick={() => tokenQuery.refetch()}
            disabled={busy}
            title="Refresh token status"
            aria-label="Refresh token status"
            style={{ fontSize: 11 }}
          >
            <RefreshCw size={12} className={tokenQuery.isFetching ? "spin" : ""} /> Refresh
          </button>
          <button
            type="button"
            className="glass-button"
            onClick={() => tokenMutation.mutate(configured)}
            disabled={busy || !subscriptionId || !resourceGroup || !clusterName}
            title={configured ? "Regenerate API token" : "Generate API token"}
            aria-label={configured ? "Regenerate API token" : "Generate API token"}
            style={{ fontSize: 11 }}
          >
            {tokenMutation.isPending ? (
              <Loader2 size={12} className="spin" />
            ) : (
              <RotateCcw size={12} />
            )}
            {configured ? "Regenerate" : "Generate"}
          </button>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1fr) auto auto",
          gap: 8,
          alignItems: "center",
          padding: "10px 12px",
          background: "var(--bg-secondary)",
          border: "1px solid var(--border-weak)",
          borderRadius: 8,
          minWidth: 0,
        }}
      >
        {busy && !token ? (
          <span
            className="skeleton"
            aria-label="Loading token status"
            aria-busy="true"
            style={{ height: 14, width: "62%", borderRadius: 4, display: "block" }}
          />
        ) : (
          <code
            style={{
              color: configured ? "var(--text-primary)" : "var(--text-faint)",
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              minWidth: 0,
            }}
          >
            {visibleToken}
          </code>
        )}
        <button
          type="button"
          className="glass-button"
          onClick={() => setRevealed((value) => !value)}
          disabled={!configured}
          title={revealed ? "Hide token" : "Reveal token"}
          aria-label={revealed ? "Hide token" : "Reveal token"}
          style={{ fontSize: 11 }}
        >
          {revealed ? <EyeOff size={12} /> : <Eye size={12} />}
          {revealed ? "Hide" : "Reveal"}
        </button>
        <button
          type="button"
          className="glass-button"
          onClick={() => configured && copyText(token!.token, "openapi-token")}
          disabled={!configured}
          title="Copy API token"
          aria-label="Copy API token"
          style={{ fontSize: 11 }}
        >
          <Copy size={12} /> {copied === "openapi-token" ? "Copied" : "Copy"}
        </button>
      </div>

      {message && (
        <div style={{ fontSize: 12, color: "var(--danger)", lineHeight: 1.45 }}>{message}</div>
      )}
      {selfHealError && (
        <div
          role="alert"
          style={{
            fontSize: 12,
            color: "var(--danger)",
            lineHeight: 1.45,
            background: "color-mix(in srgb, var(--danger) 8%, transparent)",
            border: "1px solid color-mix(in srgb, var(--danger) 35%, transparent)",
            borderRadius: 6,
            padding: "8px 10px",
            display: "grid",
            gap: 4,
          }}
        >
          <div style={{ fontWeight: 600 }}>
            Auto-recovery failed: {selfHealError.code}
          </div>
          <div style={{ color: "var(--text-secondary)", fontWeight: 400 }}>
            {selfHealError.message}
          </div>
          <div style={{ color: "var(--text-faint)", fontWeight: 400 }}>
            The dashboard detected that the elb-openapi deployment is missing the
            ELB_OPENAPI_API_TOKEN env entry and tried to patch it in place, but
            the Kubernetes API rejected the patch. Verify the api sidecar
            managed identity holds Azure Kubernetes Service RBAC Cluster Admin
            on the cluster resource group, then click Generate to retry.
          </div>
        </div>
      )}
      {token?.updated_at && (
        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Updated {token.updated_at}
        </div>
      )}
    </section>
  );
}
