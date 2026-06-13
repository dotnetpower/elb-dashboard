/**
 * API Reference page — loading skeletons.
 *
 * Layout-matched skeletons shown while the OpenAPI service is being
 * discovered on AKS or its spec is loading, so the page does not jump
 * when real content arrives. Pure presentation, no data.
 */

import { Loader2 } from "lucide-react";

export function ApiReferenceSkeleton({
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
