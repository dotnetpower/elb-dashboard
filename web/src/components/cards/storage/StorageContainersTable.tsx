import { Lock } from "lucide-react";

interface StorageContainer {
  name: string;
  public_access?: string | null;
  last_modified_time?: string | null;
}

interface StorageContainersTableProps {
  containers: StorageContainer[];
}

/**
 * Compact list of every blob container on the storage account, rendered in
 * the v3 dashboard token system (`dv3-container-list`). Each row shows the
 * container name (mono), an optional "updated …" hint, and the container's
 * public-access setting (we coerce empty / null / "None" to "Private" with a
 * lock icon since that is the production posture and "None" reads like a
 * permissions error to non-experts).
 */
function humanAccess(raw?: string | null): string {
  if (!raw) return "Private";
  const lower = raw.toLowerCase();
  if (lower === "none" || lower === "off" || lower === "private") return "Private";
  if (lower === "blob") return "Public (blob)";
  if (lower === "container") return "Public (container)";
  return raw;
}

function formatRelative(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const diffSec = (Date.now() - d.getTime()) / 1000;
  if (diffSec < 60) return "just now";
  if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
  if (diffSec < 86_400) return `${Math.round(diffSec / 3600)}h ago`;
  if (diffSec < 30 * 86_400) return `${Math.round(diffSec / 86_400)}d ago`;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function StorageContainersTable({ containers }: StorageContainersTableProps) {
  if (containers.length === 0) {
    return null;
  }
  return (
    <div className="dv3-container-list" style={{ marginBottom: "var(--space-3)" }}>
      {containers.map((c) => {
        const access = humanAccess(c.public_access);
        const isPrivate = access === "Private";
        const rel = formatRelative(c.last_modified_time);
        return (
          <div className="dv3-container-row" key={c.name}>
            <span className="name">{c.name}</span>
            {rel && (
              <span
                className="meta"
                title={
                  c.last_modified_time
                    ? new Date(c.last_modified_time).toLocaleString()
                    : undefined
                }
              >
                updated {rel}
              </span>
            )}
            <span className="right">
              <span
                className={`dv3-pill ${isPrivate ? "dv3-pill-faint" : "dv3-pill-warning"}`}
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                title={
                  isPrivate
                    ? "No anonymous access (managed-identity / SAS only)"
                    : "Anonymous read access enabled at this scope"
                }
              >
                <Lock size={10} strokeWidth={1.5} />
                {access}
              </span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
