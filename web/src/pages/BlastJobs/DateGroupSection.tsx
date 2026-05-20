import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";

import type { BlastJobSummary } from "@/api/endpoints";
import { isDashboardJobActive } from "@/components/cards/ClusterBento/jobMapping";

import { JobRow } from "./JobRow";
import { type DateGroup } from "./dateGroup";

export interface DateGroupSectionProps {
  label: DateGroup;
  jobs: BlastJobSummary[];
  defaultOpen: boolean;
  onDelete: (id: string) => void;
  deleting: boolean;
}

export function DateGroupSection({
  label,
  jobs,
  defaultOpen,
  onDelete,
  deleting,
}: DateGroupSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  const runningCount = jobs.filter(isDashboardJobActive).length;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (runningCount === 0) return undefined;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [runningCount]);

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: "6px 0",
          color: "var(--text-primary)",
        }}
      >
        <ChevronDown
          size={13}
          style={{
            transform: open ? "rotate(0deg)" : "rotate(-90deg)",
            transition: "transform 0.15s ease",
            color: "var(--text-faint)",
          }}
        />
        <span style={{ fontSize: 12, fontWeight: 600 }}>{label}</span>
        <span className="muted" style={{ fontSize: 11 }}>
          {jobs.length} job{jobs.length !== 1 ? "s" : ""}
          {runningCount > 0 && (
            <span style={{ color: "var(--warning)", marginLeft: 6 }}>
              {runningCount} active
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="table-scroll" style={{ marginBottom: "var(--space-3)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <th
                  style={{
                    textAlign: "left",
                    padding: "4px 0",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Job
                </th>
                <th
                  style={{
                    textAlign: "left",
                    padding: "4px 6px",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  User
                </th>
                <th
                  style={{
                    textAlign: "center",
                    padding: "4px 6px",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Status
                </th>
                <th
                  style={{
                    textAlign: "right",
                    padding: "4px 6px",
                    color: "var(--text-faint)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    fontWeight: 500,
                  }}
                >
                  Time
                </th>
                <th style={{ width: 36 }} />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow
                  key={job.job_id}
                  job={job}
                  onDelete={onDelete}
                  deleting={deleting}
                  now={now}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
