import { ChevronLeft, ChevronRight, Filter, RefreshCw, X } from "lucide-react";

import { parseNonNegativeInput, parsePercentInput } from "./helpers";
import type {
  BlastAnalyticsState,
  HitSortBy,
  HitSortDir,
} from "./useBlastAnalyticsState";

export interface ResultFilterBarProps {
  analytics: BlastAnalyticsState;
  onRefresh: () => void;
}

/**
 * NCBI-style filter bar: range inputs (from-to) for Identity / Query
 * cover / E-value, organism / accession / query selectors, and an
 * explicit [Apply] [Reset] pair so changes don't hit the server on every
 * keystroke. Active filters that differ from defaults show up as
 * removable chips below the inputs (mirrors NCBI's "Filter Results"
 * panel which also keeps a record of the active narrowing).
 */
export function ResultFilterBar({ analytics, onRefresh }: ResultFilterBarProps) {
  const {
    pending,
    updatePending,
    applyFilters,
    resetFilters,
    filtersDirty,
    queryIds,
    page,
    pageCount,
    setPage,
    alignQuery,
    pageSizeOptions,
  } = analytics;

  const filteredHits = alignQuery.data?.filtered_hits ?? alignQuery.data?.total_hits ?? 0;
  const totalHits = alignQuery.data?.total_hits ?? 0;
  const returned = alignQuery.data?.returned ?? 0;
  const isFetching = alignQuery.isFetching;

  // Pre-flight validation for the Apply button: if a user typed a
  // min > max in one of the range pairs the backend would just return
  // zero hits with no explanation. Disable Apply in that case and
  // surface a small inline error instead.
  const rangeErrors: string[] = [];
  if (pending.minIdentity > pending.maxIdentity) {
    rangeErrors.push("Identity min must be ≤ max");
  }
  if (pending.minQueryCover > pending.maxQueryCover) {
    rangeErrors.push("HSP cover min must be ≤ max");
  }
  if (pending.maxEvalue < 0) {
    rangeErrors.push("Max E-value must be ≥ 0");
  }
  const applyDisabled = !filtersDirty || isFetching || rangeErrors.length > 0;

  return (
    <div
      className="glass-card"
      style={{
        padding: 12,
        marginBottom: 16,
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 12,
        }}
      >
        <Filter size={14} className="muted" />
        <select
          className="form-input"
          style={{ width: 220, fontSize: 13 }}
          value={pending.queryFilter}
          onChange={(event) => updatePending("queryFilter", event.target.value)}
        >
          <option value="">All queries</option>
          {queryIds.map((qid) => (
            <option key={qid} value={qid}>
              {qid}
            </option>
          ))}
        </select>
        <input
          className="form-input"
          style={{ width: 150, fontSize: 13 }}
          value={pending.subjectFilter}
          placeholder="Accession"
          onChange={(event) => updatePending("subjectFilter", event.target.value)}
        />
        <input
          className="form-input"
          style={{ width: 170, fontSize: 13 }}
          value={pending.organismFilter}
          placeholder="Organism or taxid"
          onChange={(event) => updatePending("organismFilter", event.target.value)}
        />
        <RangeInputPair
          label="Identity %"
          min={pending.minIdentity}
          max={pending.maxIdentity}
          onMinChange={(value) =>
            updatePending("minIdentity", parsePercentInput(String(value)))
          }
          onMaxChange={(value) =>
            updatePending("maxIdentity", parsePercentInput(String(value)))
          }
          unit="%"
        />
        <RangeInputPair
          label="HSP cover"
          min={pending.minQueryCover}
          max={pending.maxQueryCover}
          onMinChange={(value) =>
            updatePending("minQueryCover", parsePercentInput(String(value)))
          }
          onMaxChange={(value) =>
            updatePending("maxQueryCover", parsePercentInput(String(value)))
          }
          unit="%"
        />
        <label
          className="muted"
          style={{ display: "flex", alignItems: "center", gap: 6 }}
        >
          Max E
          <input
            className="form-input"
            type="number"
            min={0}
            step="any"
            style={{ width: 86, fontSize: 13 }}
            value={pending.maxEvalue}
            onChange={(event) =>
              updatePending("maxEvalue", parseNonNegativeInput(event.target.value, 10))
            }
          />
        </label>
        <select
          className="form-input"
          style={{ width: 142, fontSize: 13 }}
          value={pending.sortBy}
          onChange={(event) => updatePending("sortBy", event.target.value as HitSortBy)}
        >
          <option value="relevance">Best match</option>
          <option value="evalue">E-value</option>
          <option value="bitscore">Bit score</option>
          <option value="pident">Identity</option>
          <option value="qcovs">HSP cover</option>
          <option value="length">Length</option>
        </select>
        <button
          className="btn btn--ghost btn--sm"
          onClick={() =>
            updatePending(
              "sortDir",
              (pending.sortDir === "asc" ? "desc" : "asc") as HitSortDir,
            )
          }
        >
          {pending.sortDir === "asc" ? "Asc" : "Desc"}
        </button>
        <label
          className="muted"
          style={{ display: "flex", alignItems: "center", gap: 6 }}
        >
          Show
          <select
            className="form-input"
            style={{ width: 70, fontSize: 13 }}
            value={pending.pageSize}
            onChange={(event) => updatePending("pageSize", Number(event.target.value))}
          >
            {pageSizeOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <span style={{ flex: 1 }} />
        <button
          className="btn btn--primary btn--sm"
          onClick={applyFilters}
          disabled={applyDisabled}
          title={
            rangeErrors.length > 0
              ? rangeErrors.join(" · ")
              : filtersDirty
                ? "Apply pending filter changes"
                : "No pending changes"
          }
        >
          {filtersDirty ? "Apply" : "Applied"}
        </button>
        <button
          className="btn btn--ghost btn--sm"
          onClick={resetFilters}
          disabled={isFetching}
        >
          Reset
        </button>
        <button
          className="btn btn--ghost btn--sm"
          onClick={onRefresh}
          disabled={isFetching}
          title="Refetch with the currently applied filters"
        >
          <RefreshCw size={14} className={isFetching ? "spin" : ""} />
        </button>
      </div>

      <FilterChipRow analytics={analytics} />

      {rangeErrors.length > 0 && (
        <div
          role="alert"
          style={{
            fontSize: 11,
            color: "var(--danger)",
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
          }}
        >
          {rangeErrors.map((message) => (
            <span key={message}>⚠ {message}</span>
          ))}
        </div>
      )}

      <div
        style={{
          display: "flex",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 12,
          color: "var(--text-muted)",
          fontSize: 12,
        }}
      >
        <span>
          {alignQuery.data
            ? `${returned} shown, ${filteredHits.toLocaleString()} filtered of ${totalHits.toLocaleString()} hits`
            : "—"}
        </span>
        {alignQuery.data?.files_parsed !== undefined && (
          <span>
            {alignQuery.data.files_parsed} / {alignQuery.data.total_files ?? 0} files
          </span>
        )}
        <span style={{ flex: 1 }} />
        <button
          className="btn btn--ghost btn--sm"
          onClick={() => setPage(Math.max(1, page - 1))}
          disabled={page <= 1 || isFetching}
        >
          <ChevronLeft size={14} />
        </button>
        <span style={{ fontSize: 12 }}>
          {pageCount ? `Page ${page} / ${pageCount}` : "No pages"}
        </span>
        <button
          className="btn btn--ghost btn--sm"
          onClick={() => setPage(Math.min(pageCount, page + 1))}
          disabled={!pageCount || page >= pageCount || isFetching}
        >
          <ChevronRight size={14} />
        </button>
      </div>
    </div>
  );
}

interface RangeInputPairProps {
  label: string;
  min: number;
  max: number;
  onMinChange: (value: number) => void;
  onMaxChange: (value: number) => void;
  unit?: string;
}

function RangeInputPair({
  label,
  min,
  max,
  onMinChange,
  onMaxChange,
  unit,
}: RangeInputPairProps) {
  return (
    <label className="muted" style={{ display: "flex", alignItems: "center", gap: 4 }}>
      {label}
      <input
        className="form-input"
        type="number"
        min={0}
        max={100}
        step={1}
        style={{ width: 64, fontSize: 13 }}
        value={min}
        onChange={(event) => onMinChange(parsePercentInput(event.target.value))}
        title={`Minimum ${label.toLowerCase()}`}
      />
      <span style={{ fontSize: 11 }}>to</span>
      <input
        className="form-input"
        type="number"
        min={0}
        max={100}
        step={1}
        style={{ width: 64, fontSize: 13 }}
        value={max}
        onChange={(event) => onMaxChange(parsePercentInput(event.target.value))}
        title={`Maximum ${label.toLowerCase()}`}
      />
      {unit && <span style={{ fontSize: 11 }}>{unit}</span>}
    </label>
  );
}

interface FilterChipRowProps {
  analytics: BlastAnalyticsState;
}

function FilterChipRow({ analytics }: FilterChipRowProps) {
  const { applied, updatePending, applyFilters } = analytics;

  const chips: Array<{ key: string; label: string; reset: () => void }> = [];
  if (applied.queryFilter)
    chips.push({
      key: "query",
      label: `Query: ${applied.queryFilter}`,
      reset: () => updatePending("queryFilter", ""),
    });
  if (applied.subjectFilter)
    chips.push({
      key: "subject",
      label: `Accession: ${applied.subjectFilter}`,
      reset: () => updatePending("subjectFilter", ""),
    });
  if (applied.organismFilter)
    chips.push({
      key: "organism",
      label: `Organism: ${applied.organismFilter}`,
      reset: () => updatePending("organismFilter", ""),
    });
  if (applied.minIdentity > 0 || applied.maxIdentity < 100)
    chips.push({
      key: "identity",
      label: `Identity ${applied.minIdentity}–${applied.maxIdentity}%`,
      reset: () => {
        updatePending("minIdentity", 0);
        updatePending("maxIdentity", 100);
      },
    });
  if (applied.minQueryCover > 0 || applied.maxQueryCover < 100)
    chips.push({
      key: "cover",
      label: `HSP cover ${applied.minQueryCover}–${applied.maxQueryCover}%`,
      reset: () => {
        updatePending("minQueryCover", 0);
        updatePending("maxQueryCover", 100);
      },
    });
  if (applied.maxEvalue !== 10)
    chips.push({
      key: "evalue",
      label: `Max E: ${applied.maxEvalue}`,
      reset: () => updatePending("maxEvalue", 10),
    });

  if (chips.length === 0) return null;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 6,
      }}
    >
      <span className="muted" style={{ fontSize: 11 }}>
        Active:
      </span>
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          onClick={() => {
            chip.reset();
            applyFilters();
          }}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 999,
            background: "color-mix(in srgb, var(--accent) 12%, transparent)",
            color: "var(--accent)",
            border: "1px solid color-mix(in srgb, var(--accent) 40%, transparent)",
            cursor: "pointer",
          }}
          title="Remove this filter"
        >
          {chip.label} <X size={10} strokeWidth={2} />
        </button>
      ))}
    </div>
  );
}
