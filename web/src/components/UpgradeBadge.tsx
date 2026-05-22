/**
 * Header indicator that becomes visible when a newer release tag is
 * available on `UPGRADE_GIT_REMOTE`. Polls `/api/upgrade/status` every
 * 60 s; clicking the badge routes to `/upgrade`.
 *
 * Stays inert (renders nothing) when the operator hasn't configured a
 * remote, so a fresh deployment doesn't dangle a dead control.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpCircle, RotateCcw, ShieldAlert } from "lucide-react";

import {
  upgradeApi,
  isUpgradeAvailable,
  statePhase,
  type UpgradeStatus,
} from "@/api/upgrade";

const POLL_INTERVAL_MS = 60_000;

export function UpgradeBadge() {
  const [status, setStatus] = useState<UpgradeStatus | null>(null);
  const [error, setError] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const fresh = await upgradeApi.status();
        if (!cancelled) {
          setStatus(fresh);
          setError(false);
        }
      } catch {
        if (!cancelled) setError(true);
      }
    };
    void tick();
    const id = window.setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (error || !status) return null;
  const available = isUpgradeAvailable(status);
  const phase = statePhase(status.state);
  if (!available && phase === "idle") return null;

  let icon = <ArrowUpCircle size={14} strokeWidth={1.6} />;
  let label = `Upgrade to v${status.latest_version}`;
  let tone: "info" | "warn" | "danger" | "ok" = "info";
  if (phase === "active") {
    icon = <RotateCcw size={14} strokeWidth={1.6} />;
    label = `Upgrading… ${status.phase_progress || 0}%`;
    tone = "warn";
  } else if (phase === "failed") {
    icon = <ShieldAlert size={14} strokeWidth={1.6} />;
    label = "Upgrade failed";
    tone = "danger";
  } else if (phase === "succeeded") {
    icon = <ArrowUpCircle size={14} strokeWidth={1.6} />;
    label = `Now on v${status.running_version}`;
    tone = "ok";
  } else if (phase === "rolled_back") {
    icon = <RotateCcw size={14} strokeWidth={1.6} />;
    label = "Rolled back";
    tone = "warn";
  }

  return (
    <Link
      to="/upgrade"
      className={`upgrade-badge upgrade-badge--${tone}`}
      title={status.phase_detail || label}
      style={badgeStyle(tone)}
    >
      {icon}
      <span>{label}</span>
    </Link>
  );
}

function badgeStyle(tone: "info" | "warn" | "danger" | "ok"): React.CSSProperties {
  const palette: Record<typeof tone, string> = {
    info: "var(--accent)",
    warn: "var(--warning, #d97706)",
    danger: "var(--danger, #dc2626)",
    ok: "var(--success, #16a34a)",
  } as Record<typeof tone, string>;
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "4px 10px",
    borderRadius: 999,
    fontSize: 12,
    fontWeight: 500,
    color: palette[tone],
    border: `1px solid ${palette[tone]}`,
    background: "rgba(255,255,255,0.04)",
    textDecoration: "none",
    lineHeight: 1,
    whiteSpace: "nowrap",
  };
}
