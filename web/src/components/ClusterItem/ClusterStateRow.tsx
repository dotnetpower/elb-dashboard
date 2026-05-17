import { Loader2 } from "lucide-react";

/**
 * STATE row only renders when ARM provisioning is NOT a steady "Succeeded".
 * The top-of-card "Running" chip already conveys the healthy case;
 * surfacing a redundant green pill just adds noise.
 */
export function ClusterStateRow({
  provisioningState,
}: {
  provisioningState?: string | null;
}) {
  const ps = provisioningState ?? "?";
  if (ps === "Succeeded" || ps === "?") return null;

  return (
    <div
      style={{
        fontSize: 11,
        display: "flex",
        alignItems: "center",
        gap: 6,
      }}
    >
      <span
        className="muted"
        style={{
          fontSize: 9,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
        }}
      >
        State
      </span>
      {(ps === "Creating" || ps === "Updating") && (
        <span
          className="dv3-pill dv3-pill-accent"
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          <Loader2 size={10} className="spin" />
          {ps}
        </span>
      )}
      {ps === "Deleting" && (
        <span
          className="dv3-pill dv3-pill-warning"
          style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
        >
          <Loader2 size={10} className="spin" />
          {ps}
        </span>
      )}
      {ps === "Failed" && <span className="dv3-pill dv3-pill-danger">{ps}</span>}
      {ps !== "Creating" &&
        ps !== "Updating" &&
        ps !== "Deleting" &&
        ps !== "Failed" && <span className="dv3-pill dv3-pill-faint">{ps}</span>}
    </div>
  );
}
