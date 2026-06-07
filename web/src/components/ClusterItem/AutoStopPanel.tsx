import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock, Loader2, Power, PowerOff } from "lucide-react";

import { aksApi } from "@/api/aks";
import type {
  AutoStopPreferenceResponse,
  AutoStopStatusResponse,
} from "@/api/aks";
import { PermissionGate } from "@/components/PermissionGate";
import { useToast } from "@/components/Toast";
import { usePermissions } from "@/hooks/usePermissions";
import { computeRemainingSeconds, formatCoarseRemaining, formatCountdown, stabilizeDeadline } from "./autoStopCountdown";

// Glassmorphic Idle Auto-Stop control surfaced inside the expanded
// cluster card. Two visual states:
//   1. Toggle + dropdown ("Auto-stop when idle for [60 ▾] minutes")
//   2. Pre-stop countdown banner (verdict === "warn") with Extend button
// The banner only appears once the backend evaluator reports "warn"; the
// toggle is always visible so the user can opt in / out.

const STATUS_POLL_MS = 60_000;
const PREF_POLL_MS = 5 * 60_000;

const REASON_LABELS: Record<string, string> = {
  active: "Recent activity on this cluster.",
  idle_pending: "Idle window almost elapsed.",
  cooldown: "Cluster was recently stopped (cooldown).",
  extended: "Auto-stop is paused by Extend.",
  state_repo_unreachable: "Idle check could not read job state — staying running.",
  no_preference: "Auto-stop has not been enabled for this cluster.",
};

function reasonText(reason: string, activeJobs: number): string {
  if (reason.startsWith("active_jobs:")) {
    return `${activeJobs || reason.split(":")[1]} active job${
      activeJobs > 1 ? "s" : ""
    } on this cluster.`;
  }
  if (reason.startsWith("power_state:")) {
    const power = reason.slice("power_state:".length);
    return `Cluster is ${power}.`;
  }
  if (reason.startsWith("idle:")) {
    return `Idle for ${reason.slice("idle:".length)}.`;
  }
  return REASON_LABELS[reason] || reason || "—";
}

// Visually-hidden style for screen-reader-only summary text. The repo has
// no global `.sr-only` utility class, so this inline style provides the
// standard clip-rect off-screen pattern (kept in the layout/a11y tree but
// not painted).
const SR_ONLY_STYLE: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0,
};

// Real-time countdown text that ticks once per second. Anchored on the
// backend's RELATIVE `seconds_until_stop` snapshot plus the client
// timestamp at which that snapshot arrived (`anchorMs`, from the query's
// `dataUpdatedAt`); because both `anchorMs` and `Date.now()` are read from
// the same client clock, the elapsed difference is immune to client/server
// clock skew (critique #1 — anchoring on the absolute `next_stop_at` would
// drift by the skew). Each poll hands a fresh snapshot + anchor, so local
// drift never accumulates beyond ~1s.
//
// Isolated into its own component so only this leaf re-renders each second
// rather than the whole panel (critique #3), and it fires `onReachZero`
// exactly once when the countdown crosses zero so the parent can refetch
// even while polling is paused (critique #5).
function LiveCountdown({
  baselineSeconds,
  anchorMs,
  style,
  ariaHidden,
  onReachZero,
}: {
  baselineSeconds: number;
  anchorMs: number;
  style?: CSSProperties;
  ariaHidden?: boolean;
  onReachZero?: () => void;
}) {
  const [nowMs, setNowMs] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [baselineSeconds, anchorMs]);
  const remaining = computeRemainingSeconds(baselineSeconds, anchorMs, nowMs);
  // Depend on the zero-crossing boolean, not `remaining`, so this effect
  // only re-runs when the countdown actually reaches/leaves zero rather
  // than on every one-second tick (critique #5).
  const reachedZero = remaining <= 0;
  const firedRef = useRef(false);
  useEffect(() => {
    if (!reachedZero) {
      firedRef.current = false;
      return;
    }
    if (!firedRef.current) {
      firedRef.current = true;
      onReachZero?.();
    }
  }, [reachedZero, onReachZero]);
  return (
    <strong
      aria-hidden={ariaHidden}
      style={{ fontVariantNumeric: "tabular-nums", ...style }}
    >
      {formatCountdown(remaining)}
    </strong>
  );
}

export function AutoStopPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
  clusterIsRunning,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  clusterIsRunning: boolean;
}) {
  const qc = useQueryClient();
  const { toast } = useToast();
  // Critique #6: gate the toggle + Extend mutations behind the
  // caller's RBAC at the cluster scope so a user without
  // ``Contributor`` (or equivalent) sees a disabled control with a
  // "you need Contributor" tooltip instead of clicking through to a
  // silent 403.
  const { permissions } = usePermissions(
    subscriptionId,
    resourceGroup,
    clusterName,
  );
  const prefKey = useMemo(
    () => ["aks", "autostop", "pref", subscriptionId, resourceGroup, clusterName],
    [subscriptionId, resourceGroup, clusterName],
  );
  const statusKey = useMemo(
    () => ["aks", "autostop", "status", subscriptionId, resourceGroup, clusterName],
    [subscriptionId, resourceGroup, clusterName],
  );

  const prefQuery = useQuery({
    queryKey: prefKey,
    queryFn: () => aksApi.autoStop.get(subscriptionId, resourceGroup, clusterName),
    staleTime: PREF_POLL_MS,
    refetchInterval: PREF_POLL_MS,
    enabled: Boolean(subscriptionId && resourceGroup && clusterName),
  });
  const statusQuery = useQuery({
    queryKey: statusKey,
    queryFn: () => aksApi.autoStop.status(subscriptionId, resourceGroup, clusterName),
    staleTime: STATUS_POLL_MS,
    // Poll the status briskly only while the cluster is running AND
    // auto-stop is actively armed. A stopped cluster, an opt-out
    // preference (verdict==="disabled"), or a degraded read all stop
    // polling entirely (critique #9.8) \u2014 the next foreground refetch
    // is triggered by the user re-enabling auto-stop or the
    // ``clusterIsRunning`` flip in the parent (critique #17).
    refetchInterval: (query) => {
      const data = query.state.data as AutoStopStatusResponse | undefined;
      if (!clusterIsRunning) {
        return false;
      }
      if (!data?.enabled && data?.verdict === "disabled") {
        return false;
      }
      if (!data?.enabled) {
        return PREF_POLL_MS;
      }
      return STATUS_POLL_MS;
    },
    enabled: Boolean(subscriptionId && resourceGroup && clusterName),
  });

  const pref = prefQuery.data as AutoStopPreferenceResponse | undefined;
  const status = statusQuery.data as AutoStopStatusResponse | undefined;

  const allowed = pref?.allowed_idle_minutes ?? [15, 30, 60, 120, 240];
  const [draftEnabled, setDraftEnabled] = useState<boolean>(false);
  const [draftIdleMinutes, setDraftIdleMinutes] = useState<number>(60);

  // Sync local draft with server response on first load + when server
  // values change (e.g. another tab toggled the same cluster).
  useEffect(() => {
    if (!pref) return;
    setDraftEnabled(pref.enabled);
    setDraftIdleMinutes(pref.idle_minutes ?? 60);
  }, [pref?.enabled, pref?.idle_minutes, pref]);

  // Critique #17: when the parent flips ``clusterIsRunning`` from true
  // to false (Start/Stop button, external az CLI, idle auto-stop tick,
  // \u2026) the cached ``status`` payload becomes immediately stale \u2014
  // ``enabled`` is still true but ``verdict`` should now be
  // ``power_state:Stopped`` and the warn-banner / countdown must
  // disappear. Without this, the dashboard sits on the last-running
  // snapshot until the next ``STATUS_POLL_MS`` tick (up to one minute).
  // Invalidate on the rising edge of the stopped state so the panel
  // refetches once and converges immediately.
  const prevClusterRunningRef = useRef<boolean>(clusterIsRunning);
  useEffect(() => {
    if (prevClusterRunningRef.current && !clusterIsRunning) {
      qc.invalidateQueries({ queryKey: statusKey });
    }
    prevClusterRunningRef.current = clusterIsRunning;
  }, [clusterIsRunning, qc, statusKey]);

  // Snapshot the pre-mutation draft so we can roll back the optimistic
  // toggle when the server PUT/POST fails (critique #9.1). The previous
  // implementation silently swallowed the error: the checkbox stayed
  // ``checked`` even though the server still had ``enabled=false``,
  // which is a data-loss-feeling UX for the operator.
  const lastCommittedRef = useRef<{ enabled: boolean; idle_minutes: number }>({
    enabled: false,
    idle_minutes: 60,
  });
  useEffect(() => {
    if (!pref) return;
    lastCommittedRef.current = {
      enabled: pref.enabled,
      idle_minutes: pref.idle_minutes ?? 60,
    };
  }, [pref?.enabled, pref?.idle_minutes, pref]);

  const saveMutation = useMutation({
    mutationFn: (next: { enabled: boolean; idle_minutes: number }) =>
      aksApi.autoStop.save({
        subscription_id: subscriptionId,
        resource_group: resourceGroup,
        cluster_name: clusterName,
        enabled: next.enabled,
        idle_minutes: next.idle_minutes,
      }),
    onSuccess: (data) => {
      // Both the cache and an invalidate so a sibling tab / second user
      // editing the same cluster converges on next refetch instead of
      // sitting on a 5-min-stale local snapshot.
      qc.setQueryData(prefKey, data);
      qc.invalidateQueries({ queryKey: prefKey });
      qc.invalidateQueries({ queryKey: statusKey });
      lastCommittedRef.current = {
        enabled: data.enabled,
        idle_minutes: data.idle_minutes ?? 60,
      };
    },
    onError: (error: unknown) => {
      // Critique #9.1: roll the optimistic UI back to the last known
      // server state so the checkbox / dropdown match reality, then
      // surface the error so the operator sees WHY their toggle did
      // not take. Hidden failure was the original bug.
      setDraftEnabled(lastCommittedRef.current.enabled);
      setDraftIdleMinutes(lastCommittedRef.current.idle_minutes);
      const message = error instanceof Error ? error.message : String(error);
      toast(`Could not save auto-stop preference: ${message}`, "error");
    },
  });

  const extendMutation = useMutation({
    mutationFn: (minutes: number) =>
      aksApi.autoStop.extend(subscriptionId, resourceGroup, clusterName, minutes),
    onSuccess: (data) => {
      qc.setQueryData(prefKey, data);
      qc.invalidateQueries({ queryKey: prefKey });
      qc.invalidateQueries({ queryKey: statusKey });
    },
    onError: (error: unknown) => {
      // Critique #9.1 sibling: extend failure must also surface.
      const message = error instanceof Error ? error.message : String(error);
      toast(`Could not extend auto-stop deadline: ${message}`, "error");
    },
  });

  const handleToggle = (next: boolean) => {
    setDraftEnabled(next);
    saveMutation.mutate({ enabled: next, idle_minutes: draftIdleMinutes });
  };

  const handleIdleChange = (next: number) => {
    setDraftIdleMinutes(next);
    if (draftEnabled) {
      saveMutation.mutate({ enabled: true, idle_minutes: next });
    }
  };

  const showCountdownBanner =
    clusterIsRunning &&
    status?.enabled &&
    status?.editable !== false &&
    (status.verdict === "warn" || status.verdict === "stop");

  const formattedNextStop = status?.next_stop_at
    ? new Date(status.next_stop_at).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";

  // Auto-stop is armed with a known deadline (cluster running, opt-in
  // enabled, owned by this caller, and the evaluator projected a stop
  // time). True for `keep`, `warn`, and `stop` verdicts alike — the
  // difference is only how close the deadline is.
  const armedWithDeadline =
    clusterIsRunning &&
    Boolean(status?.enabled) &&
    status?.editable !== false &&
    Boolean(status?.next_stop_at) &&
    (status?.verdict === "keep" ||
      status?.verdict === "warn" ||
      status?.verdict === "stop");
  // Anchor the live countdown on the relative `seconds_until_stop`
  // snapshot plus the client time it arrived (`dataUpdatedAt`) rather than
  // the absolute `next_stop_at`, so the tick is immune to client/server
  // clock skew (critique #1). `dataUpdatedAt` is 0 until the first fetch.
  const rawBaseline = status?.seconds_until_stop;
  const rawAnchorMs = statusQuery.dataUpdatedAt;
  // Stabilise the projected deadline so the visible countdown does not jump
  // between polls. The backend re-derives `seconds_until_stop` every poll from
  // a wobbling activity anchor (jobstate updated_at, last_started_at, and a
  // live K8s probe that can flip to null), so the absolute deadline drifts a
  // few seconds each poll; without this the displayed "Stops in" value visibly
  // jumps backward/forward. We keep the prior deadline when the new one is
  // within tolerance and only adopt a meaningfully different deadline (real
  // extend / new activity). The ref persists across renders.
  const deadlineRef = useRef<number | null>(null);
  const { countdownBaseline, countdownAnchorMs } = useMemo(() => {
    if (
      typeof rawBaseline !== "number" ||
      !Number.isFinite(rawBaseline) ||
      rawBaseline <= 0 ||
      rawAnchorMs <= 0
    ) {
      deadlineRef.current = null;
      return { countdownBaseline: rawBaseline, countdownAnchorMs: rawAnchorMs };
    }
    const newDeadlineMs = rawAnchorMs + rawBaseline * 1000;
    const stableDeadlineMs = stabilizeDeadline(newDeadlineMs, deadlineRef.current);
    deadlineRef.current = stableDeadlineMs;
    // Re-express the stabilised absolute deadline as a (baseline, anchor) pair
    // the existing LiveCountdown understands: anchor at the snapshot arrival,
    // baseline = seconds from anchor to the stabilised deadline.
    return {
      countdownBaseline: Math.max(0, Math.round((stableDeadlineMs - rawAnchorMs) / 1000)),
      countdownAnchorMs: rawAnchorMs,
    };
  }, [rawBaseline, rawAnchorMs]);
  const canLiveCountdown =
    armedWithDeadline &&
    typeof countdownBaseline === "number" &&
    Number.isFinite(countdownBaseline) &&
    countdownAnchorMs > 0;
  // When the local countdown crosses zero the cached snapshot is stale —
  // the cluster is being stopped now. A single `invalidateQueries` forces
  // one refetch even when `refetchInterval` has been turned off (critique
  // #5), so the panel converges to the real verdict without waiting up to
  // a full `STATUS_POLL_MS` tick. Also refresh the preference so the
  // "Last auto-stop …" footer (sourced from `pref`, on a 5-min poll)
  // updates promptly rather than lagging behind the stop.
  const handleCountdownZero = useCallback(() => {
    qc.invalidateQueries({ queryKey: statusKey });
    qc.invalidateQueries({ queryKey: prefKey });
  }, [qc, statusKey, prefKey]);

  // Read-only path: the row exists but is owned by another user (e.g.
  // the cluster was previously enrolled by a teammate). Render a small
  // muted note instead of the full toggle / banner so the SPA does not
  // invite the user to PUT something that would 403.
  const readOnly =
    (pref?.exists === true && pref.editable === false) ||
    (status?.exists === true && status.editable === false);

  if (readOnly) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 10px",
          borderRadius: 8,
          background: "rgba(255, 255, 255, 0.02)",
          border: "1px solid var(--border-weak)",
          fontSize: 11,
          color: "var(--text-muted)",
        }}
        title="Another user enrolled this cluster in auto-stop; only the owner can change the setting."
      >
        <Power size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
        Auto-stop is managed by another user.
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "8px 10px",
        borderRadius: 8,
        background: "rgba(255, 255, 255, 0.025)",
        border: "1px solid var(--border-weak)",
      }}
    >
      {/* Pre-stop countdown banner — calm amber surface, no neon. */}
      {showCountdownBanner && (
        <div
          role="status"
          aria-live="polite"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "6px 10px",
            borderRadius: 6,
            background: "rgba(245, 158, 11, 0.08)",
            border: "1px solid rgba(245, 158, 11, 0.28)",
            fontSize: 12,
            color: "var(--text)",
          }}
        >
          <Clock size={14} strokeWidth={1.5} style={{ color: "var(--warning)" }} />
          <span style={{ flex: 1, minWidth: 0 }}>
            <span aria-hidden="true">
              Auto-stop in{" "}
              {canLiveCountdown ? (
                <LiveCountdown
                  baselineSeconds={countdownBaseline as number}
                  anchorMs={countdownAnchorMs}
                  ariaHidden
                  onReachZero={handleCountdownZero}
                />
              ) : (
                <strong style={{ fontVariantNumeric: "tabular-nums" }}>
                  {formatCountdown(status?.seconds_until_stop ?? 0)}
                </strong>
              )}
              {formattedNextStop ? ` (≈ ${formattedNextStop})` : ""}
            </span>
            {/* Screen-reader summary: coarse, minute-granularity text that
                only changes when the backend snapshot refreshes, so the
                surrounding `aria-live="polite"` region announces it at most
                once per poll instead of on every one-second visual tick. */}
            <span style={SR_ONLY_STYLE}>
              Auto-stop in {formatCoarseRemaining(status?.seconds_until_stop ?? 0)}.
            </span>{" "}
            <span aria-hidden="true">· </span>
            <span style={{ color: "var(--text-muted)" }}>
              {reasonText(status?.reason ?? "", status?.active_job_count ?? 0)}
            </span>
          </span>
          <PermissionGate need="can_write" permissions={permissions}>
            <button
              type="button"
              className="glass-button"
              disabled={extendMutation.isPending}
              onClick={() => extendMutation.mutate(30)}
              title="Push the auto-stop deadline out by 30 minutes"
              style={{
                fontSize: 11,
                padding: "3px 10px",
                color: "var(--accent)",
              }}
            >
              {extendMutation.isPending ? (
                <Loader2 size={11} className="spin" />
              ) : (
                "Extend 30 min"
              )}
            </button>
          </PermissionGate>
        </div>
      )}

      {/* Toggle row. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <label
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            cursor: "pointer",
            fontSize: 12,
            color: "var(--text)",
          }}
          title="When enabled, this cluster is stopped after the configured idle window to save cost. Re-start it from the Start button or by submitting a job (you'll need to start first)."
        >
          {draftEnabled ? (
            <PowerOff size={13} strokeWidth={1.5} style={{ color: "var(--warning)" }} />
          ) : (
            <Power size={13} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
          )}
          <PermissionGate need="can_write" permissions={permissions}>
            <input
              type="checkbox"
              checked={draftEnabled}
              disabled={saveMutation.isPending || prefQuery.isLoading || !!pref?.degraded}
              onChange={(e) => handleToggle(e.target.checked)}
              style={{ accentColor: "var(--accent)" }}
            />
          </PermissionGate>
          <span>Auto-stop when idle for</span>
        </label>

        <PermissionGate need="can_write" permissions={permissions}>
          <select
            className="glass-input"
            aria-label="Idle window in minutes"
            value={draftIdleMinutes}
            disabled={!draftEnabled || saveMutation.isPending}
            onChange={(e) => handleIdleChange(Number(e.target.value))}
            style={{ width: "auto", fontSize: 12, padding: "4px 26px 4px 8px" }}
          >
            {allowed.map((minutes) => (
              <option key={minutes} value={minutes}>
                {minutes < 60 ? `${minutes} min` : `${minutes / 60} h`}
              </option>
            ))}
          </select>
        </PermissionGate>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {draftEnabled ? "to save cost" : "(disabled)"}
        </span>

        {/* Always-on live countdown while armed but still outside the
            pre-stop warn window (verdict === "keep"). The amber banner
            above already carries the live countdown for warn/stop, so
            this only renders for the calmer keep state to avoid showing
            the remaining time twice. The Extend button sits alongside it
            so the user can push the deadline out at any time, not only
            once the amber pre-stop banner appears. */}
        {canLiveCountdown && status?.verdict === "keep" && (
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              marginLeft: "auto",
              fontSize: 11,
              color: "var(--text-muted)",
            }}
            title={
              formattedNextStop
                ? `Projected auto-stop at ≈ ${formattedNextStop}`
                : "Projected auto-stop time"
            }
          >
            <Clock size={11} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              Stops in{" "}
              <LiveCountdown
                baselineSeconds={countdownBaseline as number}
                anchorMs={countdownAnchorMs}
                style={{ color: "var(--text)" }}
                onReachZero={handleCountdownZero}
              />
            </span>
            <PermissionGate need="can_write" permissions={permissions}>
              <button
                type="button"
                className="glass-button"
                disabled={extendMutation.isPending}
                onClick={() => extendMutation.mutate(30)}
                title="Push the auto-stop deadline out by 30 minutes"
                style={{ fontSize: 10, padding: "2px 8px", color: "var(--accent)" }}
              >
                {extendMutation.isPending ? (
                  <Loader2 size={10} className="spin" />
                ) : (
                  "Extend 30 min"
                )}
              </button>
            </PermissionGate>
          </span>
        )}

        {saveMutation.isPending && (
          <Loader2 size={12} className="spin" style={{ color: "var(--text-faint)" }} />
        )}
      </div>

      {/* Last-evaluation footer — small, muted; only render when we have
          something to say. */}
      {(pref?.last_stop_at || pref?.last_skip_at) && (
        <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
          {pref.last_stop_at && (
            <span>
              Last auto-stop {new Date(pref.last_stop_at).toLocaleString()}{" "}
              {pref.last_stop_reason ? `(${pref.last_stop_reason})` : ""}
            </span>
          )}
          {!pref.last_stop_at && pref.last_skip_at && (
            <span>
              Last skip {new Date(pref.last_skip_at).toLocaleString()}{" "}
              {pref.last_skip_reason ? `(${pref.last_skip_reason})` : ""}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
