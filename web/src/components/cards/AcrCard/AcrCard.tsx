import { Hammer } from "lucide-react";

import { formatApiError } from "@/api/client";
import { MonitorCard } from "@/components/MonitorCard";

import { AcrImageTable, ExpandedErrorBlock } from "./AcrImageTable";
import { AcrSummaryCells } from "./AcrSummaryCells";
import { BuildConfirmDialog } from "./BuildConfirmDialog";
import {
  BuildDoneBanner,
  BuildErrorBanner,
  BuildingBanner,
  ServerBuildingBanner,
} from "./BuildStatusBanners";
import { useAcrBuilds } from "./useAcrBuilds";

export interface AcrCardProps {
  subscriptionId: string;
  resourceGroup: string;
  registryName: string;
}

export function AcrCard({
  subscriptionId,
  resourceGroup,
  registryName,
}: AcrCardProps) {
  const state = useAcrBuilds({ subscriptionId, resourceGroup, registryName });
  const {
    enabled,
    query,
    hasServerBuilding,
    expectedImages,
    builtCount,
    totalCount,
    buildStatus,
    showConfirm,
    setShowConfirm,
    expandedError,
    setExpandedError,
    buildResults,
    buildError,
    elapsed,
    singleBuilding,
    handleBuild,
    handleBuildSingle,
  } = state;

  const status = !enabled
    ? "idle"
    : query.isLoading
      ? "loading"
      : query.isError
        ? "error"
        : "ready";

  return (
    <MonitorCard
      title="Azure Container Registry"
      subtitle={
        enabled ? `${registryName} · ${resourceGroup}` : "Configure ACR name"
      }
      status={
        buildStatus === "building" || hasServerBuilding ? "loading" : status
      }
      fetching={query.isFetching}
      lastRefreshed={
        query.dataUpdatedAt ? new Date(query.dataUpdatedAt) : null
      }
      onRefresh={() => query.refetch()}
      accentColor="acr"
      collapsible
      rightSlot={
        enabled && (
          <div className="dashboard-hide-mobile" style={{ display: "flex", gap: 4, alignItems: "center" }}>
            {buildStatus !== "building" && !hasServerBuilding && (
              <button
                className="glass-button glass-button--primary"
                onClick={() => setShowConfirm(true)}
                style={{ fontSize: 10 }}
              >
                <Hammer size={11} strokeWidth={1.5} /> Build
              </button>
            )}
          </div>
        )
      }
    >
      {!enabled && (
        <div className="muted">
          Set Subscription ID, ACR RG, and ACR Name above.
        </div>
      )}
      {query.isError && (
        <div className="muted" style={{ color: "var(--danger)" }}>
          Failed to load ACR: {formatApiError(query.error, "acr")}
        </div>
      )}

      {query.data && (
        <>
          <AcrSummaryCells
            loginServer={query.data.login_server}
            sku={query.data.sku ?? undefined}
            builtCount={builtCount}
            totalCount={totalCount}
          />
          <AcrImageTable
            expectedImages={expectedImages}
            actualTags={query.data.actual_tags}
            buildDetails={query.data.build_details}
            buildResults={buildResults}
            buildStatus={buildStatus}
            singleBuilding={singleBuilding}
            onToggleError={(img) =>
              setExpandedError(expandedError === img ? null : img)
            }
            onBuildSingle={handleBuildSingle}
          />
          <ExpandedErrorBlock
            expandedError={expandedError}
            buildResults={buildResults}
          />
        </>
      )}

      {showConfirm && (
        <BuildConfirmDialog
          totalCount={totalCount}
          builtCount={builtCount}
          onStart={handleBuild}
          onCancel={() => setShowConfirm(false)}
        />
      )}

      {buildStatus === "building" && (
        <BuildingBanner elapsed={elapsed} singleBuilding={singleBuilding} />
      )}

      {buildStatus === "done" && <BuildDoneBanner elapsed={elapsed} />}

      {buildError && <BuildErrorBanner message={buildError} />}

      {hasServerBuilding && buildStatus === "building" && !singleBuilding && (
        <ServerBuildingBanner
          elapsed={elapsed}
          buildingImages={query.data?.building_images}
        />
      )}
    </MonitorCard>
  );
}
