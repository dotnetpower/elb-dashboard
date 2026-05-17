export interface AcrSummaryCellsProps {
  loginServer: string;
  sku: string | undefined;
  builtCount: number;
  totalCount: number;
}

export function AcrSummaryCells({
  loginServer,
  sku,
  builtCount,
  totalCount,
}: AcrSummaryCellsProps) {
  return (
    <>
      <div
        className="dv3-cell-grid dv3-cell-grid--3"
        style={{ marginBottom: "var(--space-3)" }}
      >
        <div className="cell">
          <span className="label">Login Server</span>
          <div
            className="value mono"
            style={{ wordBreak: "break-all", fontSize: 12 }}
          >
            {loginServer}
          </div>
        </div>
        <div className="cell">
          <span className="label">SKU</span>
          <div className="value">{sku ?? "?"}</div>
        </div>
        <div
          className={`cell${builtCount === totalCount ? " success" : " accent"}`}
        >
          <span className="label">Images built</span>
          <div className="value mono">
            {builtCount}/{totalCount}
          </div>
        </div>
      </div>

      {totalCount > 0 && (
        <div
          style={{
            height: 3,
            background: "var(--border-weak)",
            borderRadius: 2,
            marginBottom: "var(--space-3)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${(builtCount / totalCount) * 100}%`,
              background:
                builtCount === totalCount
                  ? "var(--success)"
                  : "var(--accent)",
              borderRadius: 2,
              transition: "width 0.3s ease",
            }}
          />
        </div>
      )}
    </>
  );
}
