/**
 * Shared self-upgrade availability poller with cross-tab fan-out.
 *
 * Responsibility: poll `/api/upgrade/status` on a 60s, tab-visibility-gated
 *   cadence, expose a derived `available` / `phase` / `attention` view, and
 *   provide an explicit `checkNow()` that forces `/api/upgrade/check`. A
 *   `BroadcastChannel("elb-upgrade-status")` mirrors every successful payload
 *   to other open tabs (and to other hook instances in the same tab) so the
 *   Settings gear dot and the Settings → Updates section stay in lock-step.
 * Edit boundaries: presentation lives in the consumers (Layout gear dot,
 *   UpdatesSection); this hook owns only fetching, polling, and fan-out.
 * Key entry points: `useUpgradeAvailability()`.
 * Risky contracts: depends on `upgradeApi.status/check` and the
 *   `isUpgradeAvailable` / `statePhase` helpers in `@/api/upgrade`; the
 *   channel name `elb-upgrade-status` matches the legacy header badge so any
 *   still-open old tab keeps interoperating.
 * Validation: `cd web && npm run build` + `npm test -- --run`.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { formatApiError } from "@/api/client";
import {
  isUpgradeAvailable,
  statePhase,
  upgradeApi,
  type UpgradeStatus,
} from "@/api/upgrade";

const POLL_INTERVAL_MS = 60_000;
const BROADCAST_CHANNEL_NAME = "elb-upgrade-status";

export type UpgradePhase = "idle" | "active" | "succeeded" | "failed" | "rolled_back";

export interface UpgradeAvailability {
  status: UpgradeStatus | null;
  loading: boolean;
  /** Last `/status` (or `/check`) error message, or null when healthy. */
  error: string | null;
  /** True when `latest_version` > `running_version`. */
  available: boolean;
  phase: UpgradePhase;
  /**
   * True when the user should notice something: an update is available, an
   * upgrade is mid-flight, or the last run failed / rolled back. Drives the
   * Settings gear dot.
   */
  attention: boolean;
  /** Force a `/upgrade/check`; resolves to the fresh status or throws (caller handles 429). */
  checkNow: () => Promise<UpgradeStatus>;
}

export function useUpgradeAvailability(): UpgradeAvailability {
  const [status, setStatus] = useState<UpgradeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const channelRef = useRef<BroadcastChannel | null>(null);

  useEffect(() => {
    let cancelled = false;
    let channel: BroadcastChannel | null = null;
    if (typeof BroadcastChannel !== "undefined") {
      channel = new BroadcastChannel(BROADCAST_CHANNEL_NAME);
      channel.onmessage = (e) => {
        if (!cancelled && e.data && typeof e.data === "object") {
          setStatus(e.data as UpgradeStatus);
          setError(null);
          setLoading(false);
        }
      };
      channelRef.current = channel;
    }
    const tick = async () => {
      try {
        const fresh = await upgradeApi.status();
        if (cancelled) return;
        setStatus(fresh);
        setError(null);
        channel?.postMessage(fresh);
      } catch (err) {
        if (!cancelled) setError(formatApiError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void tick();
    const gated = () => {
      if (!document.hidden) void tick();
    };
    const id = window.setInterval(gated, POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", gated);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", gated);
      channel?.close();
      channelRef.current = null;
    };
  }, []);

  const checkNow = useCallback(async () => {
    const fresh = await upgradeApi.check();
    setStatus(fresh);
    setError(null);
    setLoading(false);
    channelRef.current?.postMessage(fresh);
    return fresh;
  }, []);

  const available = isUpgradeAvailable(status);
  const phase: UpgradePhase = status ? statePhase(status.state) : "idle";
  const attention =
    Boolean(status?.git_remote) &&
    (available || phase === "active" || phase === "failed" || phase === "rolled_back");

  return { status, loading, error, available, phase, attention, checkNow };
}
