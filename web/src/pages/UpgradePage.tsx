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
 * 403 unless the caller holds an Owner/Contributor role on the deployment
 * (subscription or resource group), the `UpgradeAdmin` app role, or their oid
 * is in `UPGRADE_ADMIN_OIDS` (break-glass). The page degrades silently then.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowUpCircle, Copy, ExternalLink, History, RefreshCcw, RotateCcw, Terminal, TriangleAlert } from "lucide-react";

import {
  compareSemver,
  githubCompareUrl,
  isCommitUpdateAvailable,
  statePhase,
  upgradeApi,
  type UpgradeCandidate,
  type UpgradeCandidatesResponse,
  type UpgradeEscapeHatch,
  type UpgradeHistoryEvent,
  type UpgradeRollbackPreflight,
  type UpgradeStatus,
} from "@/api/upgrade";
import { BuildLogViewer } from "@/components/BuildLogViewer";
import { useToast } from "@/components/Toast";

const STATUS_POLL_MS = 5_000;
const BROADCAST_CHANNEL_NAME = "elb-upgrade-status";
// Sentinel encoding for the commit-channel option inside the single target
// `<select>`: `commit:<full_sha>`. Release options use the bare semver.
const COMMIT_TARGET_PREFIX = "commit:";

export function UpgradePage() {
  const [status, setStatus] = useState<UpgradeStatus | null>(null);
  const [candidates, setCandidates] = useState<UpgradeCandidatesResponse | null>(null);
  const [history, setHistory] = useState<UpgradeHistoryEvent[]>([]);
  const [escape, setEscape] = useState<UpgradeEscapeHatch | null>(null);
  const [preflight, setPreflight] = useState<UpgradeRollbackPreflight | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [adminBlocked, setAdminBlocked] = useState(false);

  const [pickedTarget, setPickedTarget] = useState<string>("");
  const [confirmDowntime, setConfirmDowntime] = useState(false);
  const [confirmBreaking, setConfirmBreaking] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [checking, setChecking] = useState(false);
  const { toast } = useToast();

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
      // The escape-hatch / rollback-preflight probes are only meaningful once
      // a rollback snapshot exists (i.e. at least one upgrade has run). On a
      // fresh / never-upgraded deployment `rollback_target` is empty and
      // `GET /upgrade/escape-hatch` returns a benign 404 — probing it on every
      // refresh just burns a round-trip and pollutes the failed-request metric.
      // Gate both probes on a non-empty snapshot.
      const hasRollbackSnapshot = Object.keys(s.rollback_target ?? {}).length > 0;
      let blocked = false;
      if (hasRollbackSnapshot) {
        // Probe escape-hatch first: when it 403s the caller is not an
        // upgrade admin and we skip the rollback-preflight probe entirely
        // so the SPA does not spam the browser console with 403s.
        try {
          const e = await upgradeApi.escapeHatch();
          setEscape(e);
        } catch (err) {
          const msg = err instanceof Error ? err.message : "";
          if (msg.includes("403")) {
            blocked = true;
          }
          setEscape(null);
        }
        setAdminBlocked(blocked);
        if (!blocked) {
          try {
            const p = await upgradeApi.rollbackPreflight();
            setPreflight(p);
          } catch {
            setPreflight(null);
          }
        } else {
          setPreflight(null);
        }
      } else {
        setEscape(null);
        setPreflight(null);
        setAdminBlocked(false);
      }
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refreshAll();
    const channel =
      typeof BroadcastChannel !== "undefined"
        ? new BroadcastChannel(BROADCAST_CHANNEL_NAME)
        : null;
    const id = window.setInterval(() => {
      // Light polling: status only.
      upgradeApi
        .status()
        .then((s) => {
          setStatus(s);
          channel?.postMessage(s);
        })
        .catch(() => undefined);
    }, STATUS_POLL_MS);
    return () => {
      window.clearInterval(id);
      channel?.close();
    };
  }, [refreshAll]);

  const phase = status ? statePhase(status.state) : "idle";
  // During the blue/green confirm window, rollback is an instant traffic
  // flip back to the still-warm blue revision — no ACR pull, so the ACR
  // pre-flight gate below does not apply.
  const fastFlip = Boolean(
    status && status.state === "confirming" && status.blue_revision,
  );
  const newerCandidates = useMemo(() => {
    if (!candidates || !status) return [] as UpgradeCandidate[];
    if (!status.running_version) return candidates.candidates;
    return candidates.candidates.filter(
      (c) => compareSemver(c.name, status.running_version) > 0,
    );
  }, [candidates, status]);

  /**
   * The commit-channel install option. Present only when the operator has the
   * commit channel on AND the discovered tracking-branch HEAD differs from the
   * running build (`isCommitUpdateAvailable`). Selecting it installs the latest
   * `main` commit rather than a tagged release. Encoded in `pickedTarget` as
   * `commit:<full_sha>` so the single `<select>` can offer both kinds.
   */
  const commitOption = useMemo(() => {
    if (!status?.track_commits) return null;
    const sha = status.latest_commit_sha || "";
    if (!sha || !isCommitUpdateAvailable(status, __APP_COMMIT__)) return null;
    return { sha, short: sha.slice(0, 7) };
  }, [status]);

  const hasAnyTarget = newerCandidates.length > 0 || Boolean(commitOption);

  /**
   * GitHub "compare" URL (running build → latest discovered ref) so the
   * operator can read exactly which commits an update brings in. Null when
   * the remote is not GitHub or the range endpoints are unknown.
   */
  const compareUrl = useMemo(
    () => githubCompareUrl(status, __APP_COMMIT__),
    [status],
  );

  /**
   * True when `pickedTarget`'s major segment is greater than the running
   * major. Major bumps may carry schema / infra changes that the in-app
   * upgrade can't reason about, so we require a second confirmation
   * checkbox before the Start button enables.
   */
  const isMajorBump = useMemo(() => {
    if (!pickedTarget || !status?.running_version) return false;
    const target = pickedTarget.split(".").map((n) => parseInt(n, 10) || 0);
    const running = status.running_version.split(".").map((n) => parseInt(n, 10) || 0);
    return target.length >= 1 && running.length >= 1 && target[0] > running[0];
  }, [pickedTarget, status]);

  // Reset breaking-change confirmation whenever the picked target changes.
  useEffect(() => {
    setConfirmBreaking(false);
  }, [pickedTarget]);

  const startUpgrade = async () => {
    if (!pickedTarget) {
      setActionError("Choose a target version first");
      return;
    }
    if (!confirmDowntime) {
      setActionError("Confirm the downtime checkbox");
      return;
    }
    if (isMajorBump && !confirmBreaking) {
      setActionError(
        "Major-version upgrade requires confirming the breaking-change checkbox.",
      );
      return;
    }
    setActionError(null);
    setSubmitting(true);
    try {
      const isCommit = pickedTarget.startsWith(COMMIT_TARGET_PREFIX);
      const updated = await upgradeApi.start(
        isCommit
          ? {
              target_kind: "commit",
              target_sha: pickedTarget.slice(COMMIT_TARGET_PREFIX.length),
              confirm_downtime: true,
            }
          : { target_version: pickedTarget, confirm_downtime: true },
      );
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
    setChecking(true);
    try {
      const updated = await upgradeApi.check();
      // The check refreshes the status row, but the target picker and history
      // read from `candidates` / `history` — refresh those too so a newly
      // discovered release is actually selectable, not just shown in a stat.
      await refreshAll();
      const latest = updated.latest_version;
      const newerThanRunning =
        latest &&
        (!updated.running_version ||
          compareSemver(latest, updated.running_version) > 0);
      toast(
        latest
          ? newerThanRunning
            ? `Checked remote — v${latest} is available to upgrade.`
            : `Checked remote — you are on the latest (v${latest}).`
          : "Checked remote — no releases found for the configured remote.",
        newerThanRunning ? "success" : "info",
      );
    } catch (err) {
      const raw = err instanceof Error ? err.message : "check failed";
      // Surface "throttled; retry in Xs" cleanly so the user knows they
      // are not blocked by anything other than the 15-second cooldown.
      const match = /retry in (\d+)s/.exec(raw);
      setActionError(
        match ? `Check throttled — try again in ${match[1]} seconds.` : raw,
      );
    } finally {
      setChecking(false);
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
          <button
            type="button"
            className="glass-button"
            onClick={() => void forceCheck()}
            disabled={checking}
          >
            {checking ? "Checking…" : "Check remote"}
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
        ) : !hasAnyTarget ? (
          <p className="muted">
            No newer release tags on {candidates?.remote ?? "the remote"}
            {status?.track_commits ? " and no new commits on the tracking branch." : "."}
          </p>
        ) : (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label htmlFor="upgrade-target" className="muted">
                Target
              </label>
              <select
                id="upgrade-target"
                className="glass-input"
                value={pickedTarget}
                onChange={(e) => setPickedTarget(e.target.value)}
                style={{ flex: 1, minWidth: 220 }}
              >
                <option value="">— pick a version —</option>
                {commitOption && (
                  <option value={`${COMMIT_TARGET_PREFIX}${commitOption.sha}`}>
                    main @ {commitOption.short} (latest commit)
                  </option>
                )}
                {newerCandidates.map((c) => (
                  <option key={c.commit_sha} value={c.name}>
                    v{c.name} ({c.commit_sha.slice(0, 7)})
                  </option>
                ))}
              </select>
            </div>
            {compareUrl && (
              <a
                href={compareUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="glass-button"
                style={{
                  alignSelf: "start",
                  fontSize: 12,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  textDecoration: "none",
                }}
              >
                <ExternalLink size={13} strokeWidth={1.7} /> View changes on GitHub
              </a>
            )}
            {pickedTarget.startsWith(COMMIT_TARGET_PREFIX) && (
              <p className="muted" style={{ margin: 0, fontSize: 11 }}>
                Preview build: installs the latest <code>main</code> commit
                (unreleased). It is built and deployed exactly like a release,
                but carries no version tag — use a tagged release for production
                stability.
              </p>
            )}
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
            <p className="muted" style={{ margin: 0, fontSize: 11 }}>
              Note: in-flight BLAST jobs that submit during the restart window may
              need to be retried by the user once the upgrade settles. Persisted job
              state (Storage Table) and uploaded results survive the restart.
            </p>
            {isMajorBump && (
              <div
                role="alert"
                style={{
                  borderRadius: 6,
                  border: "1px solid var(--danger, #dc2626)",
                  color: "var(--danger, #dc2626)",
                  padding: "8px 10px",
                  fontSize: 12,
                  display: "grid",
                  gap: 6,
                }}
              >
                <strong>Major-version upgrade.</strong> Crossing the major boundary
                (v{status.running_version?.split(".")[0]} → v
                {pickedTarget.split(".")[0]}) may carry breaking changes that
                in-app upgrade cannot reason about (schema migrations, env-var
                changes, Bicep diffs). Read the release notes before
                continuing — consider <code>azd up</code> from a workstation
                for major bumps.
                <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={confirmBreaking}
                    onChange={(e) => setConfirmBreaking(e.target.checked)}
                  />
                  <span>I have read the release notes and accept the risk.</span>
                </label>
              </div>
            )}
            <button
              type="button"
              className="glass-button glass-button--primary"
              disabled={
                submitting ||
                phase === "active" ||
                !pickedTarget ||
                !confirmDowntime ||
                (isMajorBump && !confirmBreaking)
              }
              onClick={() => void startUpgrade()}
            >
              <ArrowUpCircle size={14} strokeWidth={1.6} /> Start upgrade
            </button>
            {phase === "active" && (
              <p className="muted" style={{ margin: 0 }}>
                An upgrade is already in progress; wait for it to finish before
                starting another.
              </p>
            )}
            {phase === "succeeded" && (
              <p className="muted" style={{ margin: 0 }}>
                The last upgrade succeeded. You can start another whenever a newer
                target is available — no need to wait for the state to reset.
              </p>
            )}
          </>
        )}
      </section>

      {(phase === "succeeded" ||
        phase === "failed" ||
        status.state === "rolling_out" ||
        status.state === "confirming" ||
        status.state === "rolled_back") &&
        Object.keys(status.rollback_target).length > 0 && (
          <section className="glass-card" style={cardStack}>
            <h3 style={{ margin: 0 }}>Rollback</h3>
            {fastFlip ? (
              <p className="muted" style={{ margin: 0 }}>
                Confirm window is open. Rolling back now flips traffic back to the
                still-warm previous revision{" "}
                <code>{status.blue_revision}</code> in seconds — no image pull, no
                reboot. After the window closes the previous revision is removed and
                rollback reverts to the slower snapshot re-deploy below.
              </p>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                Replaces the deployed images with the snapshot taken before the upgrade. Requires
                that ACR still carries those tags.
              </p>
            )}
            <ImageDiffTable
              current={status.current_images}
              target={status.rollback_target}
            />
            {preflight && !preflight.available && (
              <div
                role="alert"
                style={{
                  borderRadius: 6,
                  border: "1px solid var(--danger, #dc2626)",
                  color: "var(--danger, #dc2626)",
                  padding: "8px 10px",
                  fontSize: 12,
                }}
              >
                <strong>Rollback unsafe.</strong> {preflight.reason}.
                <ul style={{ margin: "6px 0 0 18px", padding: 0 }}>
                  {preflight.images
                    .filter((img) => !img.exists)
                    .map((img) => (
                      <li key={img.image_ref}>
                        <code>{img.image_ref}</code>
                        {img.error ? ` — ${img.error}` : ""}
                      </li>
                    ))}
                </ul>
                {fastFlip
                  ? "The snapshot re-deploy path is unavailable, but the confirm-window traffic flip above does not need ACR."
                  : "Use the escape-hatch commands below or rebuild the older image."}
              </div>
            )}
            {preflight && preflight.available && (
              <div
                className="muted"
                style={{ fontSize: 12 }}
              >
                ACR pre-flight passed — all snapshot tags resolve.
                {preflight.images.find((img) => img.created_on) && (
                  <>
                    {" "}
                    Snapshot created{" "}
                    {new Date(
                      preflight.images.find((img) => img.created_on)?.created_on ?? "",
                    ).toLocaleDateString()}
                    .
                  </>
                )}
              </div>
            )}
            <button
              type="button"
              className="glass-button"
              disabled={submitting || (fastFlip ? false : preflight ? !preflight.available : false)}
              onClick={() => void triggerRollback()}
            >
              <RotateCcw size={14} strokeWidth={1.6} /> Roll back
            </button>
          </section>
        )}

      {status.job_id && (
        <section className="glass-card" style={cardStack}>
          <h3 style={{ margin: 0 }}>Build logs</h3>
          <p className="muted" style={{ margin: 0, fontSize: 12 }}>
            Per-sidecar `az acr build` output for job{" "}
            <code>{status.job_id}</code>. Updates every 3 s while the
            upgrade is active.
          </p>
          <div style={{ display: "grid", gap: 12 }}>
            {(["api", "frontend", "terminal"] as const).map((c) => (
              <BuildLogViewer
                key={c}
                jobId={status.job_id}
                component={c}
                active={phase === "active"}
              />
            ))}
          </div>
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
          <TriangleAlert size={14} /> You are signed in but lack permission for upgrade actions —
          start/rollback/escape-hatch are disabled. You need an <strong>Owner</strong> or{" "}
          <strong>Contributor</strong> role on the deployment (subscription or resource group).
          Ask an operator to grant it, then reload.
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
