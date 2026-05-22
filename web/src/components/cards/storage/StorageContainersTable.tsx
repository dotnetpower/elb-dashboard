import { Lock } from "lucide-react";

interface StorageContainer {
  name: string;
  public_access?: string | null;
  last_modified_time?: string | null;
  blob_count?: number | null;
  size_bytes?: number | null;
  usage_pending?: boolean;
  usage_truncated?: boolean;
  usage_error?: string | null;
  usage_cache_state?: string | null;
  usage_refreshed_at?: string | null;
}

interface StorageContainersTableProps {
  containers: StorageContainer[];
}

/**
 * Compact list of Storage containers on the account, grouped so researcher data
 * stays visible while control-plane state remains available behind disclosure.
 */
const PLATFORM_CONTAINER_NAMES = new Set([
  "audit",
  "dead-letter",
  "job-artifacts",
  "job-payloads",
  "schedules",
]);

const WORKSPACE_CONTAINER_ORDER = ["blast-db", "queries", "results"];

export function isPlatformContainer(name: string): boolean {
  return PLATFORM_CONTAINER_NAMES.has(name);
}

function containerSortKey(container: StorageContainer): [number, string] {
  const order = WORKSPACE_CONTAINER_ORDER.indexOf(container.name);
  return [order === -1 ? WORKSPACE_CONTAINER_ORDER.length : order, container.name];
}

export function splitStorageContainers(containers: StorageContainer[]): {
  workspaceContainers: StorageContainer[];
  platformContainers: StorageContainer[];
} {
  const workspaceContainers = containers
    .filter((container) => !isPlatformContainer(container.name))
    .sort((left, right) => {
      const [leftOrder, leftName] = containerSortKey(left);
      const [rightOrder, rightName] = containerSortKey(right);
      return leftOrder - rightOrder || leftName.localeCompare(rightName);
    });
  const platformContainers = containers
    .filter((container) => isPlatformContainer(container.name))
    .sort((left, right) => left.name.localeCompare(right.name));
  return { workspaceContainers, platformContainers };
}

function humanAccess(raw?: string | null): string {
  if (!raw) return "Private";
  const lower = raw.toLowerCase();
  if (lower === "none" || lower === "off" || lower === "private") return "Private";
  if (lower === "blob") return "Public (blob)";
  if (lower === "container") return "Public (container)";
  return raw;
}

export function formatBytes(value?: number | null): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  let amount = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  const digits = amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${unit}`;
}

export function formatContainerUsage(container: StorageContainer): string | null {
  if (container.usage_pending) return "calculating usage";
  if (container.usage_error) return "usage unavailable";
  const bytes = formatBytes(container.size_bytes);
  const count = container.blob_count;
  const blobText =
    typeof count === "number"
      ? `${count.toLocaleString()} ${count === 1 ? "blob" : "blobs"}`
      : null;
  const usage = bytes && blobText ? `${bytes} · ${blobText}` : (bytes ?? blobText);
  if (!usage) return null;
  return container.usage_truncated ? `>= ${usage}` : usage;
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

function StorageContainerRows({ containers }: { containers: StorageContainer[] }) {
  if (containers.length === 0) return null;
  return (
    <div className="dv3-container-list">
      {containers.map((c) => {
        const access = humanAccess(c.public_access);
        const isPrivate = access === "Private";
        const rel = formatRelative(c.last_modified_time);
        const usage = formatContainerUsage(c);
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
            {usage && (
              <span
                className="meta"
                title={
                  c.usage_pending
                    ? "Usage is being calculated in the background; cached totals will appear on the next refresh."
                    : c.usage_truncated
                      ? "Usage is a capped best-effort sample; the actual total may be larger."
                      : c.usage_error
                        ? "Storage usage could not be calculated for this container."
                        : c.usage_refreshed_at
                          ? `Usage refreshed at ${new Date(c.usage_refreshed_at).toLocaleString()}`
                          : undefined
                }
              >
                {usage}
              </span>
            )}
            <span className="right">
              <span
                className={`dv3-pill ${isPrivate ? "dv3-pill-accent" : "dv3-pill-warning"}`}
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

function totalSize(containers: StorageContainer[]): number | null {
  let sawSize = false;
  const total = containers.reduce((sum, container) => {
    if (typeof container.size_bytes !== "number") return sum;
    sawSize = true;
    return sum + container.size_bytes;
  }, 0);
  return sawSize ? total : null;
}

export function StorageContainersTable({ containers }: StorageContainersTableProps) {
  if (containers.length === 0) {
    return null;
  }
  const { workspaceContainers, platformContainers } = splitStorageContainers(containers);
  const platformSize = formatBytes(totalSize(platformContainers));
  return (
    <div style={{ marginBottom: "var(--space-3)" }}>
      <StorageContainerRows containers={workspaceContainers} />
      {platformContainers.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary
            className="muted"
            style={{ cursor: "pointer", fontSize: 12, padding: "6px 10px" }}
          >
            Platform state ({platformContainers.length}
            {platformSize ? ` · ${platformSize}` : ""})
          </summary>
          <StorageContainerRows containers={platformContainers} />
        </details>
      )}
    </div>
  );
}
