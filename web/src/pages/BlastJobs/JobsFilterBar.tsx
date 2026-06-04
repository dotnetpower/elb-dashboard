import { Search } from "lucide-react";

import type { BlastJobsState, FilterKind } from "./useBlastJobsState";

export interface JobsFilterBarProps {
  filter: FilterKind;
  setFilter: BlastJobsState["setFilter"];
  search: string;
  setSearch: BlastJobsState["setSearch"];
  counts: BlastJobsState["counts"];
}

const FILTERS: FilterKind[] = ["all", "queued", "running", "completed", "failed"];

export function JobsFilterBar({
  filter,
  setFilter,
  search,
  setSearch,
  counts,
}: JobsFilterBarProps) {
  return (
    <div
      className="jobs-filter-bar"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-3)",
        flexWrap: "wrap",
      }}
    >
      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        {FILTERS.map((f) => (
          <button
            key={f}
            className={`glass-button ${filter === f ? "glass-button--primary" : ""}`}
            onClick={() => setFilter(f)}
            style={{ fontSize: 11, textTransform: "capitalize" }}
          >
            {f}
            {f !== "all" && ` (${counts[f as Exclude<FilterKind, "all">]})`}
          </button>
        ))}
      </div>
      <div style={{ position: "relative", flex: "1 1 180px", maxWidth: 280 }}>
        <Search
          size={13}
          style={{
            position: "absolute",
            left: 8,
            top: "50%",
            transform: "translateY(-50%)",
            color: "var(--text-faint)",
            pointerEvents: "none",
          }}
        />
        <input
          type="text"
          placeholder="Search jobs…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            width: "100%",
            padding: "5px 8px 5px 26px",
            background: "var(--glass-bg)",
            border: "1px solid var(--border-weak)",
            borderRadius: 6,
            color: "var(--text-primary)",
            fontSize: 12,
            outline: "none",
          }}
        />
      </div>
    </div>
  );
}
