import { AlertTriangle, RotateCw } from "lucide-react";

export interface DeployHeaderProps {
  isUpdate: boolean;
  clusterName: string;
  pinnedTag?: string;
  currentTag?: string;
}

export function DeployHeader({
  isUpdate,
  clusterName,
  pinnedTag,
  currentTag,
}: DeployHeaderProps) {
  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 8,
        }}
      >
        {isUpdate ? (
          <RotateCw size={14} style={{ color: "var(--accent)" }} />
        ) : (
          <AlertTriangle size={16} style={{ color: "var(--warning)" }} />
        )}
        <span style={{ fontWeight: 600, fontSize: isUpdate ? 13 : 14 }}>
          {isUpdate ? "Update OpenAPI service" : "OpenAPI service not found"}
        </span>
        {isUpdate && pinnedTag && (
          <span
            style={{
              fontSize: 10,
              padding: "2px 8px",
              borderRadius: 10,
              background: "var(--bg-tertiary)",
              color: "var(--text-faint)",
              fontFamily: "var(--font-mono)",
              fontWeight: 600,
            }}
            title={
              currentTag && currentTag !== pinnedTag
                ? `Running tag may differ — latest in ACR: ${currentTag}`
                : "Tag pinned in this dashboard"
            }
          >
            v{pinnedTag}
          </span>
        )}
      </div>
      {!isUpdate && (
        <p
          style={{
            color: "var(--text-muted)",
            fontSize: 12,
            margin: "0 0 12px",
          }}
        >
          The{" "}
          <code
            style={{
              fontFamily: "var(--font-mono)",
              background: "var(--bg-tertiary)",
              padding: "1px 5px",
              borderRadius: 3,
            }}
          >
            elb-openapi
          </code>{" "}
          service is not running on{" "}
          <strong>{clusterName || "the cluster"}</strong>. Deploy it now to
          load the live API specification.
        </p>
      )}
      {isUpdate && (
        <p
          style={{
            color: "var(--text-muted)",
            fontSize: 11,
            margin: "0 0 10px",
          }}
        >
          Re-roll the{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>elb-openapi</code>{" "}
          deployment with the tag pinned in this dashboard. Use this after the
          sibling
          <code
            style={{
              fontFamily: "var(--font-mono)",
              marginLeft: 4,
            }}
          >
            elastic-blast-azure
          </code>{" "}
          repo bumps the image. The pod is recreated with{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>
            imagePullPolicy: Always
          </code>
          .
        </p>
      )}
    </>
  );
}
