/**
 * useAppInsights — initialise the App Insights JS SDK for the SPA.
 *
 * Connection string is resolved in this order:
 *   1. user-supplied value in `usePreferences().prefs.appInsightsConnectionString`
 *   2. deployment-injected value from `/api/settings/app-insights`
 *
 * The SDK is created lazily once `prefs.telemetryEnabled` is true AND a
 * non-empty connection string is available. When the user disables
 * telemetry the previously-loaded instance is unloaded so no further
 * events are buffered or sent.
 *
 * Initialisation is idempotent across React StrictMode double-renders —
 * we keep a single module-level instance keyed by the active connection
 * string.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import {
  ApplicationInsights,
  type IExceptionTelemetry,
  type IPageViewTelemetry,
} from "@microsoft/applicationinsights-web";
import { useMsal } from "@azure/msal-react";

import { usePreferences } from "@/hooks/usePreferences";
import { settingsApi } from "@/api/settings";

interface AppInsightsContextValue {
  /** True when an instance is live and accepting events. */
  active: boolean;
  /** Active connection string (masked at UI render time). */
  connectionString: string;
  /** "user" / "deployment" / "none". */
  source: "user" | "deployment" | "none";
  /** Effective bag of events the SDK is currently configured to send. */
  trackPageView: (telemetry?: IPageViewTelemetry) => void;
  trackException: (telemetry: IExceptionTelemetry) => void;
}

const noop = () => {};

const Context = createContext<AppInsightsContextValue>({
  active: false,
  connectionString: "",
  source: "none",
  trackPageView: noop,
  trackException: noop,
});

let _activeInstance: ApplicationInsights | null = null;
let _activeConnectionString = "";

function teardown(): void {
  if (_activeInstance) {
    try {
      _activeInstance.unload(false);
    } catch {
      /* SDK may have never fully booted; ignore */
    }
  }
  _activeInstance = null;
  _activeConnectionString = "";
}

function ensureInstance(connectionString: string): ApplicationInsights {
  if (_activeInstance && _activeConnectionString === connectionString) {
    return _activeInstance;
  }
  teardown();
  const ai = new ApplicationInsights({
    config: {
      connectionString,
      enableAutoRouteTracking: true,
      autoTrackPageVisitTime: true,
      enableCorsCorrelation: true,
      disableFetchTracking: false,
      disableAjaxTracking: false,
      // Trim noisy default sampling so the dev tier of App Insights is not
      // overwhelmed during dashboards-open-all-day sessions; operators can
      // override by re-creating the resource with a different rate.
      samplingPercentage: 100,
    },
  });
  ai.loadAppInsights();
  _activeInstance = ai;
  _activeConnectionString = connectionString;
  return ai;
}

export function AppInsightsProvider({ children }: { children: ReactNode }) {
  const { prefs } = usePreferences();
  const { accounts } = useMsal();
  const [deploymentConnectionString, setDeploymentConnectionString] = useState("");
  // True once a NON-EMPTY deployment string has resolved. We keep retrying
  // until then so a transient empty/error response does not leave the SPA
  // permanently blank (see the focus/visibility effect below).
  const resolvedRef = useRef(false);

  const signedIn = accounts.length > 0;

  // Look up the deployment-injected connection string lazily after sign-in.
  // The endpoint requires a bearer token, so calling it before MSAL has an
  // active account would return 401 and spam the SPA error console.
  //
  // Idempotent + self-healing: once a non-empty value resolves we stop
  // (resolvedRef), so this is at most one extra request per wake event until
  // the string is known. A Container App revision swap restarts the api
  // sidecar; a request landing in that window returns empty/errors, which
  // previously stuck the panel blank until a manual page refresh. Leaving
  // resolvedRef false on empty/error lets the focus/visibility effect retry.
  const refreshDeploymentString = useCallback(() => {
    if (!signedIn || resolvedRef.current) return;
    settingsApi
      .getAppInsightsStatus()
      .then((status) => {
        const cs = (status.deployment_connection_string ?? "").trim();
        if (cs) {
          resolvedRef.current = true;
          setDeploymentConnectionString(cs);
        }
      })
      .catch(() => {
        // Non-fatal — keep going with whatever the user supplied. resolvedRef
        // stays false so the next focus/visibility regain retries.
      });
  }, [signedIn]);

  useEffect(() => {
    refreshDeploymentString();
  }, [refreshDeploymentString]);

  // Retry on tab focus / visibility regain until the string is known. This is
  // what makes a revision swap self-heal: the operator switching back to the
  // dashboard tab re-fetches and the connection string reappears without a
  // manual refresh. No-op once resolvedRef is true (guarded inside the
  // callback), so it does not add steady-state traffic.
  useEffect(() => {
    if (!signedIn) return;
    const onVisible = () => {
      if (document.visibilityState === "visible") refreshDeploymentString();
    };
    window.addEventListener("focus", refreshDeploymentString);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", refreshDeploymentString);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [signedIn, refreshDeploymentString]);

  const userConnectionString = prefs.appInsightsConnectionString.trim();
  const effective = userConnectionString || deploymentConnectionString.trim();
  const source: AppInsightsContextValue["source"] =
    userConnectionString.length > 0
      ? "user"
      : deploymentConnectionString.trim().length > 0
        ? "deployment"
        : "none";

  const ai = useMemo<ApplicationInsights | null>(() => {
    if (!prefs.telemetryEnabled || !effective) {
      teardown();
      return null;
    }
    try {
      return ensureInstance(effective);
    } catch {
      teardown();
      return null;
    }
  }, [prefs.telemetryEnabled, effective]);

  useEffect(() => {
    return () => {
      // We intentionally do NOT teardown on unmount of the provider in
      // production — the SPA only mounts it once. The unload only runs
      // when telemetry is disabled or the string changes (handled above).
    };
  }, []);

  const value = useMemo<AppInsightsContextValue>(() => {
    if (!ai) {
      return {
        active: false,
        connectionString: "",
        source,
        trackPageView: noop,
        trackException: noop,
      };
    }
    return {
      active: true,
      connectionString: effective,
      source,
      trackPageView: (telemetry) => ai.trackPageView(telemetry),
      trackException: (telemetry) => ai.trackException(telemetry),
    };
  }, [ai, effective, source]);

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useAppInsights(): AppInsightsContextValue {
  return useContext(Context);
}

export const _testHelpers = {
  teardown,
  ensureInstance,
};
