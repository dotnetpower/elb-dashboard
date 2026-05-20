import { Server } from "lucide-react";

export interface BlastJobIdentityProps {
  title: string;
  fallbackTitle: string;
  program: string;
  db: string;
  query?: string | null;
  clusterName?: string | null;
  note?: string | null;
  noteTone?: string;
  compact?: boolean;
  className?: string;
}

export function BlastJobIdentity({
  title,
  fallbackTitle,
  program,
  db,
  query,
  clusterName,
  note,
  noteTone,
  compact = false,
  className,
}: BlastJobIdentityProps) {
  const displayTitle = title || fallbackTitle;
  const classNames = [
    "blast-job-identity",
    compact ? "blast-job-identity--compact" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={classNames} title={displayTitle}>
      <span className="blast-job-identity__title">{displayTitle}</span>
      <span className="blast-job-identity__meta">
        <span>
          {program} · {db}
        </span>
        {query && query !== title && <span>{query}</span>}
        {note && (
          <span className="blast-job-identity__note" style={{ color: noteTone }}>
            {note}
          </span>
        )}
        {clusterName && (
          <span className="blast-job-identity__cluster">
            <Server size={8} strokeWidth={1.5} /> {clusterName}
          </span>
        )}
      </span>
    </span>
  );
}