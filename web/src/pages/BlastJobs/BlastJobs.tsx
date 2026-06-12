import { AlertTriangle } from "lucide-react";

import { formatApiError } from "@/api/client";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DegradedNotice } from "@/components/DegradedNotice";

import { DateGroupSection } from "./DateGroupSection";
import { JobsFilterBar } from "./JobsFilterBar";
import { JobsHeader } from "./JobsHeader";
import { JobsLoadingSkeleton } from "./JobsLoadingSkeleton";
import { NoFilteredEmpty, NoJobsEmpty } from "./JobsEmptyState";
import { ServiceBusInboundStrip } from "./ServiceBusInboundStrip";
import { useBlastJobsState } from "./useBlastJobsState";

export function BlastJobs() {
  const state = useBlastJobsState();
  const {
    deleteTarget,
    setDeleteTarget,
    filter,
    setFilter,
    search,
    setSearch,
    source,
    setSource,
    cluster,
    jobsQuery,
    deleteMutation,
    allJobs,
    degradedNotice,
    externalDegradedNotice,
    filtered,
    grouped,
    counts,
    sourceCounts,
    handleDelete,
  } = state;

  return (
    <div className="page-stack jobs-page">
      <JobsHeader
        allJobsLength={allJobs.length}
        counts={counts}
        cluster={cluster}
        jobsQuery={jobsQuery}
      />

      <ServiceBusInboundStrip />

      {jobsQuery.isLoading && <JobsLoadingSkeleton />}

      {allJobs.length > 0 && (
        <JobsFilterBar
          filter={filter}
          setFilter={setFilter}
          search={search}
          setSearch={setSearch}
          counts={counts}
          source={source}
          setSource={setSource}
          sourceCounts={sourceCounts}
        />
      )}

      {deleteMutation.isError && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(224,123,138,0.08)",
            border: "1px solid rgba(224,123,138,0.2)",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--danger)",
          }}
        >
          <AlertTriangle size={12} style={{ verticalAlign: "middle", marginRight: 4 }} />
          Delete failed: {formatApiError(deleteMutation.error, "blast")}
        </div>
      )}

      {externalDegradedNotice && (
        <DegradedNotice
          reason={externalDegradedNotice.reason}
          message={externalDegradedNotice.message}
          scope="OpenAPI jobs"
        />
      )}

      {allJobs.length === 0 && !jobsQuery.isLoading && (
        <NoJobsEmpty cluster={cluster} degradedNotice={degradedNotice} />
      )}
      {filtered.length === 0 && allJobs.length > 0 && !jobsQuery.isLoading && (
        <NoFilteredEmpty search={search} filter={filter} />
      )}

      {grouped.map(({ label, jobs: groupJobs }) => (
        <DateGroupSection
          key={label}
          label={label}
          jobs={groupJobs}
          defaultOpen={label !== "Earlier"}
          onDelete={handleDelete}
          deleting={deleteMutation.isPending}
        />
      ))}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete BLAST search"
        message="This will stop the search and clean up resources. This cannot be undone."
        confirmLabel="Delete"
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget);
          setDeleteTarget(null);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
