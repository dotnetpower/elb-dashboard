const LOADING_ROWS = [0, 1, 2, 3];
const JOB_WIDTHS = ["78%", "64%", "72%", "58%"];
const META_WIDTHS = ["56%", "68%", "48%", "62%"];
const USER_WIDTHS = ["54px", "68px", "46px", "60px"];
const TIME_WIDTHS = ["44px", "58px", "50px", "62px"];

export function JobsLoadingSkeleton() {
  return (
    <section
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-label="Loading recent BLAST searches"
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          padding: "6px 0",
          color: "var(--text-primary)",
        }}
      >
        <SkeletonBlock width="13px" height="13px" radius="4px" />
        <SkeletonBlock width="42px" height="12px" />
        <SkeletonBlock width="58px" height="11px" />
      </div>
      <div className="table-scroll" style={{ marginBottom: "var(--space-3)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border-weak)" }}>
              <HeaderCell align="left" padding="4px 0" label="Job" />
              <HeaderCell align="left" padding="4px 6px" label="User" />
              <HeaderCell align="center" padding="4px 6px" label="Status" />
              <HeaderCell align="right" padding="4px 6px" label="Time" />
              <th style={{ width: 36 }} />
            </tr>
          </thead>
          <tbody>
            {LOADING_ROWS.map((rowIndex) => (
              <tr key={rowIndex} style={{ borderBottom: "1px solid var(--border-weak)" }}>
                <td style={{ padding: "8px 0" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <SkeletonBlock width="7px" height="7px" radius="999px" />
                    <div style={{ display: "grid", gap: 5, minWidth: 0, width: "100%" }}>
                      <SkeletonBlock width={JOB_WIDTHS[rowIndex]} height="13px" />
                      <SkeletonBlock width={META_WIDTHS[rowIndex]} height="10px" />
                    </div>
                  </div>
                </td>
                <td style={{ padding: "8px 6px" }}>
                  <SkeletonBlock width={USER_WIDTHS[rowIndex]} height="11px" />
                </td>
                <td style={{ padding: "8px 6px", textAlign: "center" }}>
                  <SkeletonBlock width="64px" height="18px" radius="4px" />
                </td>
                <td style={{ padding: "8px 6px", textAlign: "right" }}>
                  <div style={{ display: "grid", gap: 5, justifyItems: "end" }}>
                    <SkeletonBlock width={TIME_WIDTHS[rowIndex]} height="11px" />
                    <SkeletonBlock width="52px" height="10px" />
                  </div>
                </td>
                <td style={{ padding: "8px 0", textAlign: "right", width: 36 }}>
                  <SkeletonBlock width="24px" height="22px" radius="4px" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HeaderCell({
  align,
  padding,
  label,
}: {
  align: "left" | "center" | "right";
  padding: string;
  label: string;
}) {
  return (
    <th
      style={{
        textAlign: align,
        padding,
        color: "var(--text-faint)",
        fontSize: 10,
        textTransform: "uppercase",
        fontWeight: 500,
      }}
    >
      {label}
    </th>
  );
}

function SkeletonBlock({
  width,
  height,
  radius = "999px",
}: {
  width: string;
  height: string;
  radius?: string;
}) {
  return (
    <span
      className="skeleton"
      aria-hidden
      style={{ display: "inline-block", width, height, borderRadius: radius }}
    />
  );
}
