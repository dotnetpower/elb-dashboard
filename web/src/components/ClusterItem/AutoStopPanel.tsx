import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clock, Loader2, Power, PowerOff } from "lucide-react";

import { aksApi } from "@/api/aks";
import type {
  AutoStopPreferenceResponse,
  AutoStopStatusResponse,
} from "@/api/aks";

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

function formatSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "0s";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  const totalMin = Math.floor(seconds / 60);
  if (totalMin < 60) {
    const remSec = Math.round(seconds % 60);
    return remSec ? `${totalMin}m ${remSec}s` : `${totalMin}m`;
  }
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

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
    // auto-stop is enabled — a stopped/disabled cluster cannot transition
    // to "warn" without user input.
    refetchInterval: (query) => {
      const data = query.state.data as AutoStopStatusResponse | undefined;
      if (!clusterIsRunning) {
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
            Auto-stop in{" "}
            <strong>{formatSeconds(status?.seconds_until_stop ?? 0)}</strong>
            {formattedNextStop ? ` (≈ ${formattedNextStop})` : ""} ·{" "}
            <span style={{ color: "var(--text-muted)" }}>
              {reasonText(status?.reason ?? "", status?.active_job_count ?? 0)}
            </span>
          </span>
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
          <input
            type="checkbox"
            checked={draftEnabled}
            disabled={saveMutation.isPending || prefQuery.isLoading || !!pref?.degraded}
            onChange={(e) => handleToggle(e.target.checked)}
            style={{ accentColor: "var(--accent)" }}
          />
          <span>Auto-stop when idle for</span>
        </label>

        <select
          aria-label="Idle window in minutes"
          value={draftIdleMinutes}
          disabled={!draftEnabled || saveMutation.isPending}
          onChange={(e) => handleIdleChange(Number(e.target.value))}
          style={{
            fontSize: 12,
            padding: "2px 6px",
            background: "transparent",
            border: "1px solid var(--border-weak)",
            borderRadius: 4,
            color: "var(--text)",
          }}
        >
          {allowed.map((minutes) => (
            <option key={minutes} value={minutes}>
              {minutes < 60 ? `${minutes} min` : `${minutes / 60} h`}
            </option>
          ))}
        </select>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {draftEnabled ? "to save cost" : "(disabled)"}
        </span>

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
