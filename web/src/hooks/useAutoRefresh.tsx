import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

/**
 * Global auto-refresh interval for dashboard cards.
 *
 * The Dashboard header exposes a dropdown that lets the user pick how often
 * polled queries (cluster status, storage account, ACR repositories, terminal
 * sidecar health, jobs) should refetch. The chosen value is persisted to
 * localStorage so the choice survives reloads. Cards consume the value via
 * `useAutoRefresh()` and pass it as the `refetchInterval` of their TanStack
 * `useQuery` calls.
 */

const STORAGE_KEY = "elb-auto-refresh-ms";
const DEFAULT_MS = 30_000;

export const AUTO_REFRESH_OPTIONS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 5_000, label: "5s" },
  { value: 15_000, label: "15s" },
  { value: 30_000, label: "30s" },
  { value: 60_000, label: "60s" },
];

const VALID_VALUES = new Set(AUTO_REFRESH_OPTIONS.map((o) => o.value));

interface AutoRefreshContextValue {
  intervalMs: number;
  setIntervalMs: (ms: number) => void;
  /** Approximate seconds until the next refresh cycle (drives the RefreshRing). */
  secondsToRefresh: number;
}

const AutoRefreshContext = createContext<AutoRefreshContextValue>({
  intervalMs: DEFAULT_MS,
  setIntervalMs: () => {},
  secondsToRefresh: DEFAULT_MS / 1000,
});

function readPersisted(): number {
  if (typeof window === "undefined") return DEFAULT_MS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_MS;
    const n = Number(raw);
    return VALID_VALUES.has(n) ? n : DEFAULT_MS;
  } catch {
    return DEFAULT_MS;
  }
}

export function AutoRefreshProvider({ children }: { children: ReactNode }) {
  const [intervalMs, setIntervalMsState] = useState<number>(readPersisted);

  const setIntervalMs = useCallback((ms: number) => {
    if (!VALID_VALUES.has(ms)) return;
    setIntervalMsState(ms);
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(intervalMs));
    } catch {
      // Quota / private mode — fail silently; the in-memory value still works.
    }
  }, [intervalMs]);

  // Approximate cadence countdown. Cards refetch on their own react-query
  // timers, so this is a rhythm indicator for the *configured* interval, not a
  // promise tied to a specific query. It resets whenever the interval changes
  // and pauses while the tab is hidden.
  const [secondsToRefresh, setSecondsToRefresh] = useState<number>(
    Math.round(intervalMs / 1000),
  );
  useEffect(() => {
    const total = Math.round(intervalMs / 1000);
    setSecondsToRefresh(total);
    const start = Date.now();
    const id = window.setInterval(() => {
      if (document.hidden) return;
      const remMs = intervalMs - ((Date.now() - start) % intervalMs);
      setSecondsToRefresh(Math.max(1, Math.ceil(remMs / 1000)));
    }, 1000);
    return () => window.clearInterval(id);
  }, [intervalMs]);

  const value = useMemo<AutoRefreshContextValue>(
    () => ({ intervalMs, setIntervalMs, secondsToRefresh }),
    [intervalMs, setIntervalMs, secondsToRefresh],
  );

  return <AutoRefreshContext.Provider value={value}>{children}</AutoRefreshContext.Provider>;
}

export function useAutoRefresh(): AutoRefreshContextValue {
  return useContext(AutoRefreshContext);
}

/** Convenience hook for cards that just need the interval, not the setter. */
export function useAutoRefreshInterval(): number {
  return useContext(AutoRefreshContext).intervalMs;
}
