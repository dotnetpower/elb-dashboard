import { Copy } from "lucide-react";

/** Kubelet Object ID strip — copy-to-clipboard + AcrPull recovery hint. */
export function IdentitySection({ kubeletObjectId }: { kubeletObjectId: string }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          marginBottom: 8,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <span
          style={{
            width: 3,
            height: 14,
            borderRadius: 2,
            background: "var(--purple)",
          }}
        />
        Identity
      </div>
      <div
        style={{
          borderRadius: 8,
          border: "1px solid var(--border-weak)",
          padding: "10px 12px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
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
          Kubelet OID
        </span>
        <code
          style={{
            fontSize: 11,
            fontFamily: "var(--font-mono)",
            color: "var(--text-primary)",
            wordBreak: "break-all",
          }}
        >
          {kubeletObjectId}
        </code>
        <button
          className="glass-button"
          style={{ padding: "2px 8px", fontSize: 10 }}
          onClick={() => navigator.clipboard.writeText(kubeletObjectId)}
          title="Copy OID"
        >
          <Copy size={11} strokeWidth={1.5} /> Copy
        </button>
        <span
          className="muted"
          style={{
            fontSize: 10,
            marginLeft: "auto",
          }}
        >
          AcrPull on the registry must be granted to this object id.
        </span>
      </div>
    </div>
  );
}
