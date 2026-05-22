/**
 * `/upgrade` page — the operator-facing control surface for the
 * in-app self-upgrade flow. Polls `/api/upgrade/status` on a short
 * interval; surfaces candidates, the start CTA (with downtime
 * confirmation), live progress, build-log drill-downs, rollback (with
 * snapshot diff), the escape-hatch recovery commands, and the audit
 * history tail.
 *
 * Auth: any signed-in user can see the status / candidates / history.
 * Mutating actions (start / rollback) and the escape-hatch view return
 * 403 unless the caller's oid is in `UPGRADE_ADMIN_OIDS` (or has the
 * UpgradeAdmin app role). The page degrades silently in that case.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowUpCircle, Copy, History, RefreshCcw, RotateCcw, Terminal, TriangleAlert } from "lucide-react";

import {
  compareSemver,
  statePhase,
  upgradeApi,
  type UpgradeCandidate,
  type UpgradeCandidatesResponse,
  type UpgradeEscapeHatch,
  type UpgradeHistoryEvent,
  type UpgradeStatus,
} from "@/api/upgrade";

const STATUS_POLL_MS = 5_000;

export function UpgradePage() {
  const [status, setStatus] = useState<UpgradeStatus | null>(null);
  const [candidates, setCandidates] = useState<UpgradeCandidatesResponse | null>(null);
  const [history, setHistory] = useState<UpgradeHistoryEvent[]>([]);
  const [escape, setEscape] = useState<UpgradeEscapeHatch | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [adminBlocked, setAdminBlocked] = useState(false);

  const [pickedTarget, setPickedTarget] = useState<string>("");
  const [confirmDowntime, setConfirmDowntime] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    try {
      const [s, c, h] = await Promise.all([
        upgradeApi.status(),
        upgradeApi.candidates(),
        upgradeApi.history(50),
      ]);
      setStatus(s);
      setCandidates(c);
      setHistory(h.events);
      // Best-effort escape hatch (admin only — surfaces 403 silently)
      try {
        const e = await upgradeApi.escapeHatch();
        setEscape(e);
        setAdminBlocked(false);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "";
        if (msg.includes("403")) setAdminBlocked(true);
        else setEscape(null);
      }
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refreshAll();
    const id = window.setInterval(() => {
      // Light polling: status only.
      upgradeApi
        .status()
        .then((s) => setStatus(s))
        .catch(() => undefined);
    }, STATUS_POLL_MS);
    return () => window.clearInterval(id);
  }, [refreshAll]);

  const phase = status ? statePhase(status.state) : "idle";
  const newerCandidates = useMemo(() => {
    if (!candidates || !status) return [] as UpgradeCandidate[];
    if (!status.running_version) return candidates.candidates;
    return candidates.candidates.filter(
      (c) => compareSemver(c.name, status.running_version) > 0,
    );
  }, [candidates, status]);

  const startUpgrade = async () => {
    if (!pickedTarget) {
      setActionError("Choose a target version first");
      return;
    }
    if (!confirmDowntime) {
      setActionError("Confirm the downtime checkbox");
      return;
    }
    setActionError(null);
    setSubmitting(true);
    try {
      const updated = await upgradeApi.start({
        target_version: pickedTarget,
        confirm_downtime: true,
      });
      setStatus(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "start failed");
    } finally {
      setSubmitting(false);
    }
  };

  const triggerRollback = async () => {
    setActionError(null);
    setSubmitting(true);
    try {
      const updated = await upgradeApi.rollback();
      setStatus(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "rollback failed");
    } finally {
      setSubmitting(false);
    }
  };

  const forceCheck = async () => {
    setActionError(null);
    try {
      const updated = await upgradeApi.check();
      setStatus(updated);
    } catch (err) {
      const raw = err instanceof Error ? err.message : "check failed";
      // Surface "throttled; retry in Xs" cleanly so the user knows they
      // are not blocked by anything other than the 15-second cooldown.
      const match = /retry in (\d+)s/.exec(raw);
      setActionError(
        match ? `Check throttled — try again in ${match[1]} seconds.` : raw,
      );
    }
  };

  if (!status) {
    return (
      <div className="glass-card" style={{ display: "grid", gap: 12 }}>
        <h2 style={{ margin: 0 }}>Upgrade</h2>
        <p className="muted">Loading upgrade status…</p>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <h2 style={{ margin: 0 }}>Self-upgrade</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="button"
            className="glass-button"
            onClick={() => void refreshAll()}
            disabled={refreshing}
          >
            <RefreshCcw size={14} strokeWidth={1.5} /> Refresh
          </button>
          <button type="button" className="glass-button" onClick={() => void forceCheck()}>
            Check remote
          </button>
        </div>
      </header>

      {actionError && (
        <div
          className="glass-card"
          role="alert"
          style={{ borderColor: "var(--danger)", color: "var(--danger)" }}
        >
          {actionError}
        </div>
      )}

      <section className="glass-card" style={cardStack}>
        <h3 style={{ margin: 0 }}>Status</h3>
        <dl style={statsGrid}>
          <Stat label="Running" value={`v${status.running_version || "?"}`} />
          <Stat
            label="Latest available"
            value={status.latest_version ? `v${status.latest_version}` : "—"}
          />
          <Stat
            label="State"
            value={status.state}
            tone={phase === "failed" ? "danger" : phase === "active" ? "warn" : "ok"}
          />
          <Stat label="Progress" value={`${status.phase_progress || 0}%`} />
          <Stat label="Job" value={status.job_id || "—"} mono />
          <Stat
            label="Last check"
            value={status.latest_checked_at ? new Date(status.latest_checked_at).toLocaleString() : "—"}
          />
        </dl>
        <p className="muted" style={{ margin: 0 }}>
          {status.phase_detail || "idle"}
        </p>
      </section>

      <section className="glass-card" style={cardStack}>
        <h3 style={{ margin: 0 }}>Start an upgrade</h3>
        {candidates?.configured === false ? (
          <p className="muted">
            Set <code>UPGRADE_GIT_REMOTE</code> on the Container App to enable upgrades.
          </p>
        ) : newerCandidates.length === 0 ? (
          <p className="muted">No newer release tags on {candidates?.remote ?? "the remote"}.</p>
        ) : (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label htmlFor="upgrade-target" className="muted">
                Target
              </label>
              <select
                id="upgrade-target"
                value={pickedTarget}
                onChange={(e) => setPickedTarget(e.target.value)}
              >
                <option value="">— pick a version —</option>
                {newerCandidates.map((c) => (
                  <option key={c.commit_sha} value={c.name}>
                    v{c.name} ({c.commit_sha.slice(0, 7)})
                  </option>
                ))}
              </select>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                checked={confirmDowntime}
                onChange={(e) => setConfirmDowntime(e.target.checked)}
              />
              <span>
                I accept a short downtime (≈ 1 minute) while the new revision boots.
              </span>
            </label>
            <button
              type="button"
              className="glass-button glass-button--primary"
              disabled={submitting || phase === "active" || phase === "succeeded"}
              onClick={() => void startUpgrade()}
            >
              <ArrowUpCircle size={14} strokeWidth={1.6} /> Start upgrade
            </button>
            {(phase === "active" || phase === "succeeded") && (
              <p className="muted" style={{ margin: 0 }}>
                The state must return to <code>idle</code> before another upgrade can start.
              </p>
            )}
          </>
        )}
      </section>

      {(phase === "succeeded" ||
        phase === "failed" ||
        status.state === "rolling_out" ||
        status.state === "rolled_back") &&
        Object.keys(status.rollback_target).length > 0 && (
          <section className="glass-card" style={cardStack}>
            <h3 style={{ margin: 0 }}>Rollback</h3>
            <p className="muted" style={{ margin: 0 }}>
              Replaces the deployed images with the snapshot taken before the upgrade. Requires
              that ACR still carries those tags.
            </p>
            <ImageDiffTable
              current={status.current_images}
              target={status.rollback_target}
            />
            <button
              type="button"
              className="glass-button"
              disabled={submitting}
              onClick={() => void triggerRollback()}
            >
              <RotateCcw size={14} strokeWidth={1.6} /> Roll back
            </button>
          </section>
        )}

      {escape && (
        <section className="glass-card" style={cardStack}>
          <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 6 }}>
            <Terminal size={14} /> Escape-hatch commands
          </h3>
          <p className="muted" style={{ margin: 0 }}>
            Use these from any <code>az login</code>-ed shell if the new revision is unreachable
            and the rollback button above does not respond.
          </p>
          <pre style={preStyle}>{escape.commands.join("\n")}</pre>
          <button
            type="button"
            className="glass-button"
            onClick={() => void navigator.clipboard.writeText(escape.commands.join("\n"))}
          >
            <Copy size={14} strokeWidth={1.6} /> Copy commands
          </button>
        </section>
      )}

      {adminBlocked && (
        <div className="glass-card" role="status">
          <TriangleAlert size={14} /> You are signed in but not on the upgrade-admin allowlist —
          start/rollback/escape-hatch actions are disabled. Ask an operator to add your oid to
          <code>UPGRADE_ADMIN_OIDS</code>.
        </div>
      )}

      <section className="glass-card" style={cardStack}>
        <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 6 }}>
          <History size={14} /> Recent events
        </h3>
        {history.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>
            No events recorded yet.
          </p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
            {history.slice(0, 20).map((e, i) => (
              <li key={`${e.ts}-${i}`} style={historyRow}>
                <span className="muted" style={{ width: 160, flexShrink: 0 }}>
                  {e.ts}
                </span>
                <span style={{ width: 120, flexShrink: 0 }}>{e.event}</span>
                <span style={{ flex: 1, opacity: 0.85 }}>
                  {summariseEventDetail(e)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function summariseEventDetail(event: UpgradeHistoryEvent): string {
  const skip = new Set(["ts", "job_id", "event"]);
  const parts: string[] = [];
  for (const [k, v] of Object.entries(event)) {
    if (skip.has(k)) continue;
    if (typeof v === "string") parts.push(`${k}=${v}`);
    else if (typeof v === "number" || typeof v === "boolean") parts.push(`${k}=${v}`);
  }
  return parts.join(" · ");
}

function Stat({
  label,
  value,
  tone,
  mono,
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "danger";
  mono?: boolean;
}) {
  const color =
    tone === "danger"
      ? "var(--danger, #dc2626)"
      : tone === "warn"
        ? "var(--warning, #d97706)"
        : "inherit";
  return (
    <div>
      <div className="muted" style={{ fontSize: 11, textTransform: "uppercase" }}>
        {label}
      </div>
      <div
        style={{
          fontSize: 14,
          fontWeight: 600,
          color,
          fontFamily: mono ? "var(--font-mono, monospace)" : "inherit",
        }}
      >
        {value}
      </div>
    </div>
  );
}

function ImageDiffTable({
  current,
  target,
}: {
  current: Record<string, string>;
  target: Record<string, string>;
}) {
  const roles = Array.from(new Set([...Object.keys(current), ...Object.keys(target)]));
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ textAlign: "left" }}>
          <th>Sidecar</th>
          <th>Current</th>
          <th>Rollback target</th>
        </tr>
      </thead>
      <tbody>
        {roles.map((role) => (
          <tr key={role}>
            <td style={{ padding: "4px 0", fontWeight: 600 }}>{role}</td>
            <td className="muted" style={{ padding: "4px 0", fontFamily: "var(--font-mono, monospace)" }}>
              {current[role] ?? "—"}
            </td>
            <td style={{ padding: "4px 0", fontFamily: "var(--font-mono, monospace)" }}>
              {target[role] ?? "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const cardStack: React.CSSProperties = { display: "grid", gap: 12 };
const statsGrid: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
  gap: 12,
  margin: 0,
};
const preStyle: React.CSSProperties = {
  background: "rgba(0,0,0,0.25)",
  border: "1px solid var(--border-weak)",
  borderRadius: 6,
  padding: 12,
  fontSize: 12,
  overflow: "auto",
  margin: 0,
};
const historyRow: React.CSSProperties = {
  display: "flex",
  gap: 12,
  fontSize: 12,
  borderBottom: "1px solid var(--border-weak)",
  padding: "4px 0",
};
