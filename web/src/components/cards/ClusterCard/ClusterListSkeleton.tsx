export function ClusterListSkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label="Loading clusters"
      style={{ display: "flex", flexDirection: "column", gap: 10 }}
    >
      {[1, 2].map((i) => (
        <div key={i} className="glass-card" style={{ padding: "var(--space-3)" }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div className="skeleton" style={{ width: 140, height: 14 }} />
            <div style={{ display: "flex", gap: 8 }}>
              <div className="skeleton" style={{ width: 80, height: 12 }} />
              <div className="skeleton" style={{ width: 50, height: 22 }} />
            </div>
          </div>
          <div style={{ marginTop: 8 }}>
            <div className="skeleton" style={{ width: 200, height: 11 }} />
          </div>
          <div style={{ marginTop: 8 }}>
            <div className="skeleton" style={{ width: 120, height: 10 }} />
          </div>
        </div>
      ))}
    </div>
  );
}
