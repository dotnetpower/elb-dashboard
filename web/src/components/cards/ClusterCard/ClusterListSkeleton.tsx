export function ClusterListSkeleton() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {[1, 2].map((i) => (
        <div key={i} className="glass-card" style={{ padding: "var(--space-3)" }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div
              style={{
                width: 140,
                height: 14,
                background: "var(--glass-bg-strong)",
                borderRadius: 4,
              }}
            />
            <div style={{ display: "flex", gap: 8 }}>
              <div
                style={{
                  width: 80,
                  height: 12,
                  background: "var(--glass-bg-strong)",
                  borderRadius: 4,
                }}
              />
              <div
                style={{
                  width: 50,
                  height: 22,
                  background: "var(--glass-bg-strong)",
                  borderRadius: 4,
                }}
              />
            </div>
          </div>
          <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
            <div
              style={{
                width: 200,
                height: 11,
                background: "var(--glass-bg)",
                borderRadius: 3,
              }}
            />
          </div>
          <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
            <div
              style={{
                width: 120,
                height: 10,
                background: "var(--glass-bg)",
                borderRadius: 3,
              }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
