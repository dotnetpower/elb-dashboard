import { Award, Dna, Layers, Loader2, Target, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";

import { formatEvalue } from "./helpers";
import type { BlastAnalyticsState } from "./useBlastAnalyticsState";

export interface OverviewPanelProps {
  analytics: BlastAnalyticsState;
}

/**
 * Summary cards + horizontal distribution bars (E-value / Identity %).
 * Rendered inside the Graphic Summary tab as a compact "at-a-glance"
 * strip above the per-hit ruler — keeps the same screen-real-estate
 * we used to spend on a whole Overview tab.
 */
export function OverviewPanel({ analytics }: OverviewPanelProps) {
  const { statsQuery } = analytics;
  const stats = statsQuery.data?.stats;

  if (statsQuery.isLoading) {
    return (
      <div className="glass-card" style={{ padding: 40, textAlign: "center" }}>
        <Loader2 size={24} className="spin" style={{ color: "var(--accent)" }} />
        <p className="muted" style={{ marginTop: 12 }}>
          Aggregating BLAST results...
        </p>
      </div>
    );
  }

  if (statsQuery.isError) {
    return (
      <div className="glass-card" style={{ padding: 20, borderColor: "var(--danger)" }}>
        <p style={{ color: "var(--danger)" }}>
          Failed to load summary: {(statsQuery.error as Error).message}
        </p>
      </div>
    );
  }

  if (!stats) return null;

  const evalueMax = Math.max(...Object.values(stats.evalue_distribution), 0);
  const identMax = Math.max(...Object.values(stats.identity_distribution), 0);

  return (
    <>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: 12,
          marginBottom: 16,
        }}
      >
        <StatCard
          icon={<Target size={18} />}
          label="Total Hits"
          value={stats.total_hits.toLocaleString()}
        />
        <StatCard
          icon={<Dna size={18} />}
          label="Unique Queries"
          value={stats.unique_queries.toLocaleString()}
        />
        <StatCard
          icon={<Layers size={18} />}
          label="Unique Subjects"
          value={stats.unique_subjects.toLocaleString()}
        />
        <StatCard
          icon={<TrendingUp size={18} />}
          label="Avg Identity"
          value={stats.avg_identity ? `${stats.avg_identity}%` : "—"}
          accent
        />
        <StatCard
          icon={<Award size={18} />}
          label="Avg Bit Score"
          value={stats.avg_bitscore?.toFixed(1) ?? "—"}
        />
        <StatCard
          icon={<Award size={18} />}
          label="Best E-value"
          value={stats.min_evalue !== null ? formatEvalue(stats.min_evalue) : "—"}
        />
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
          gap: 12,
          marginBottom: 12,
        }}
      >
        <div className="glass-card" style={{ padding: 16 }}>
          <h4 style={{ margin: "0 0 12px", fontSize: 13 }}>E-value distribution</h4>
          {Object.entries(stats.evalue_distribution).map(([bin, count]) => (
            <HorizontalBar
              key={bin}
              label={bin}
              value={count}
              max={evalueMax}
              color="var(--accent)"
            />
          ))}
        </div>
        <div className="glass-card" style={{ padding: 16 }}>
          <h4 style={{ margin: "0 0 12px", fontSize: 13 }}>Identity % distribution</h4>
          {Object.entries(stats.identity_distribution).map(([bin, count]) => {
            const pct = parseInt(bin);
            const color =
              pct >= 90
                ? "var(--success)"
                : pct >= 70
                  ? "var(--warning)"
                  : pct >= 50
                    ? "var(--accent)"
                    : "var(--danger)";
            return (
              <HorizontalBar
                key={bin}
                label={bin}
                value={count}
                max={identMax}
                color={color}
              />
            );
          })}
        </div>
      </div>

      {stats.top_subjects.length > 0 && (
        <div className="glass-card" style={{ padding: 16, marginBottom: 12 }}>
          <h4 style={{ margin: "0 0 12px", fontSize: 13 }}>Top hit subjects</h4>
          <div style={{ overflowX: "auto" }}>
            <table className="table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Subject ID</th>
                  <th>Hit count</th>
                  <th style={{ width: 200 }}>Distribution</th>
                </tr>
              </thead>
              <tbody>
                {stats.top_subjects.map((s, index) => (
                  <tr key={s.id}>
                    <td className="muted">{index + 1}</td>
                    <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{s.id}</td>
                    <td>{s.count}</td>
                    <td>
                      <div
                        style={{
                          width: "100%",
                          height: 12,
                          background: "var(--glass-bg)",
                          borderRadius: 3,
                        }}
                      >
                        <div
                          style={{
                            width: `${(s.count / stats.top_subjects[0].count) * 100}%`,
                            height: "100%",
                            background: "var(--accent)",
                            borderRadius: 3,
                          }}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {stats.files_parsed !== undefined && (
        <p className="muted" style={{ fontSize: 12, textAlign: "center" }}>
          Parsed {stats.files_parsed} of {stats.total_files} result file
          {stats.total_files !== 1 ? "s" : ""}
        </p>
      )}
    </>
  );
}

function HorizontalBar({
  label,
  value,
  max,
  color,
}: {
  label: string;
  value: number;
  max: number;
  color: string;
}) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        marginBottom: 4,
        fontSize: 12,
      }}
    >
      <span
        style={{
          minWidth: 110,
          textAlign: "right",
          fontFamily: "var(--font-mono, monospace)",
          color: "var(--text-muted)",
        }}
      >
        {label}
      </span>
      <div
        style={{
          flex: 1,
          height: 16,
          background: "var(--glass-bg)",
          borderRadius: 4,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${Math.max(pct, 1)}%`,
            height: "100%",
            background: color,
            borderRadius: 4,
            transition: "width 0.3s ease-out",
          }}
        />
      </div>
      <span
        style={{
          minWidth: 50,
          fontFamily: "var(--font-mono, monospace)",
          fontSize: 12,
        }}
      >
        {value.toLocaleString()}
      </span>
    </div>
  );
}

function StatCard({
  icon,
  label,
  value,
  accent,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="glass-card" style={{ padding: 12, textAlign: "center" }}>
      <div
        style={{
          color: accent ? "var(--accent)" : "var(--text-muted)",
          marginBottom: 4,
        }}
      >
        {icon}
      </div>
      <div
        style={{
          fontSize: 20,
          fontWeight: 700,
          color: accent ? "var(--accent)" : "var(--text-primary)",
        }}
      >
        {value}
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
        {label}
      </div>
    </div>
  );
}
