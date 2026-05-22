import { type PropsWithChildren, type ReactNode, useState, useCallback } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";
import { useRelativeTime } from "@/hooks/useRelativeTime";

interface Props {
  title: string;
  subtitle?: ReactNode;
  status?: "idle" | "loading" | "ok" | "ready" | "not-provisioned" | "unavailable" | "error";
  fetching?: boolean;
  rightSlot?: ReactNode;
  lastRefreshed?: Date | null;
  onRefresh?: () => void;
  accentColor?: "cluster" | "storage" | "acr" | "terminal" | "jobs";
  collapsible?: boolean;
  defaultCollapsed?: boolean;
  /**
   * When set, overrides the status chip rendered in the card header. Used by
   * cards that received a graceful-degraded payload from the backend so the
   * user sees an actionable label (e.g. "Wrong tenant", "No access") instead
   * of a misleading "OK" / "Ready". Tone maps to the existing chip palette.
   */
  statusOverride?: {
    label: string;
    tone: "warning" | "danger" | "muted";
    title?: string;
  } | null;
}

const STATUS_TAG: Record<
  NonNullable<Props["status"]>,
  { cls: string; label: string } | null
> = {
  idle: null,
  loading: { cls: "gt gt-o", label: "Loading" },
  ok: { cls: "gt gt-g", label: "OK" },
  ready: { cls: "gt gt-b", label: "Ready" },
  "not-provisioned": { cls: "gt gt-m", label: "Not Provisioned" },
  unavailable: { cls: "gt gt-m", label: "Unavailable" },
  error: { cls: "gt gt-r", label: "Error" },
};

const OVERRIDE_TONE_CLS: Record<
  NonNullable<NonNullable<Props["statusOverride"]>["tone"]>,
  string
> = {
  warning: "gt gt-o",
  danger: "gt gt-r",
  muted: "gt gt-m",
};

const STORAGE_PREFIX = "elb-card-collapsed-";

function getCollapsedState(title: string, defaultVal: boolean): boolean {
  try {
    const v = localStorage.getItem(STORAGE_PREFIX + title);
    return v != null ? v === "1" : defaultVal;
  } catch {
    return defaultVal;
  }
}

export function MonitorCard({
  title,
  subtitle,
  status = "idle",
  fetching = false,
  rightSlot,
  lastRefreshed,
  onRefresh,
  accentColor,
  collapsible = false,
  defaultCollapsed = false,
  statusOverride = null,
  children,
}: PropsWithChildren<Props>) {
  const tag = STATUS_TAG[status];
  const renderedTag = statusOverride
    ? {
        cls: OVERRIDE_TONE_CLS[statusOverride.tone],
        label: statusOverride.label,
        title: statusOverride.title,
      }
    : tag
      ? { cls: tag.cls, label: tag.label, title: undefined }
      : null;
  const relTime = useRelativeTime(lastRefreshed?.getTime() ?? null);
  const [collapsed, setCollapsed] = useState(() =>
    getCollapsedState(title, defaultCollapsed),
  );

  const toggleCollapse = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(STORAGE_PREFIX + title, next ? "1" : "0");
      } catch {
        /* noop */
      }
      return next;
    });
  }, [title]);

  const showShimmer = status === "loading" || fetching;
  const panelCls = ["panel", accentColor ? `panel--accent-${accentColor}` : ""]
    .filter(Boolean)
    .join(" ");
  const hdCls = ["panel-hd", collapsible ? "panel-hd--collapsible" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <section className={panelCls}>
      {showShimmer && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: 2,
            background: "rgba(122,167,255,0.15)",
            overflow: "hidden",
            zIndex: 1,
          }}
        >
          <div
            style={{
              width: "40%",
              height: "100%",
              background:
                "linear-gradient(90deg, transparent, var(--accent), transparent)",
              animation: "shimmer 1.5s ease-in-out infinite",
            }}
          />
        </div>
      )}
      <div className={hdCls} onClick={collapsible ? toggleCollapse : undefined}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {collapsible && (
            <ChevronDown
              size={14}
              className={`panel__collapse-icon${collapsed ? " panel__collapse-icon--collapsed" : ""}`}
            />
          )}
          <div>
            <div className="title">{title}</div>
            {subtitle && <div className="sub">{subtitle}</div>}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {renderedTag && (
            <span className={renderedTag.cls} title={renderedTag.title}>
              {renderedTag.label}
            </span>
          )}
          {rightSlot && <div onClick={(e) => e.stopPropagation()}>{rightSlot}</div>}
        </div>
      </div>
      {/* #8 Skeleton loading */}
      {status === "loading" && !children ? (
        <div className="panel-bd">
          <div className="skeleton skeleton-line" style={{ width: "85%" }} />
          <div className="skeleton skeleton-line" style={{ width: "70%" }} />
          <div className="skeleton skeleton-line" style={{ width: "55%" }} />
        </div>
      ) : (
        <div className={`panel-bd${collapsed ? " panel-bd--collapsed" : ""}`}>
          {children}
        </div>
      )}
      {/* Bottom-right whisper: refresh timestamp + manual refresh */}
      {(relTime || onRefresh) && !collapsed && (
        <div className="panel-whisper" title={lastRefreshed?.toLocaleString() ?? ""}>
          {relTime && <span>{relTime}</span>}
          {onRefresh && (
            <button
              className="panel-whisper__btn"
              onClick={onRefresh}
              disabled={status === "loading" || fetching}
              title="Refresh now"
            >
              <RefreshCw size={10} strokeWidth={1.5} className={fetching ? "spin" : ""} />
            </button>
          )}
        </div>
      )}
    </section>
  );
}
