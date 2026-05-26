/**
 * usePreferences — single source of truth for SPA-level personal preferences.
 *
 * Stored as one JSON value under `localStorage["elb-prefs"]` so adding new
 * preferences later does not multiply localStorage keys. Reads run through
 * `migrateLegacy` once so existing `elb-theme` / `elb-auto-refresh-ms`
 * values keep working without prompting the user.
 *
 * The shape is versioned (`__v`) so a future migration can rewrite older
 * payloads in place without losing user state.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { isFeatureEnabled, type FeatureFlag } from "@/config/runtime";

export type ThemeMode = "light" | "dark" | "system";
export type PreviewFeature = "customDb" | "labTools" | "liveWall";

export interface Preferences {
  __v: 1;
  /** light / dark / system (default: system). */
  theme: ThemeMode;
  /** Whether the browser should send App Insights telemetry. */
  telemetryEnabled: boolean;
  /**
   * Optional user-supplied Application Insights connection string. When
   * present (and `telemetryEnabled` is true) it overrides the deployment-
   * injected value reported by `/api/settings/app-insights`.
   */
  appInsightsConnectionString: string;
  /** Log Analytics workspace ARM id backing the App Insights component. */
  appInsightsWorkspaceResourceId: string;
  /**
   * Name of the Container App revision created by the most recent
   * "Apply to server sidecars" action (or empty when never applied).
   * Surfaces in the Telemetry settings so operators can correlate the
   * connection string in the SPA with what is running server-side.
   */
  appInsightsLastAppliedRevision: string;
  /** Last 8 characters of the InstrumentationKey that was applied. */
  appInsightsLastAppliedKeyTail: string;
  /** Preview opt-in: show Custom DB surfaces when the runtime flag allows it. */
  previewCustomDbEnabled: boolean;
  /** Preview opt-in: show Lab Tools surfaces when the runtime flag allows it. */
  previewLabToolsEnabled: boolean;
  /** Preview opt-in: show the Live Wall monitor route. */
  previewLiveWallEnabled: boolean;
}

const STORAGE_KEY = "elb-prefs";
const LEGACY_THEME_KEY = "elb-theme";

const DEFAULT_PREFERENCES: Preferences = {
  __v: 1,
  theme: "system",
  telemetryEnabled: false,
  appInsightsConnectionString: "",
  appInsightsWorkspaceResourceId: "",
  appInsightsLastAppliedRevision: "",
  appInsightsLastAppliedKeyTail: "",
  previewCustomDbEnabled: false,
  previewLabToolsEnabled: false,
  previewLiveWallEnabled: false,
};

export const PREVIEW_PREF_KEYS = {
  customDb: "previewCustomDbEnabled",
  labTools: "previewLabToolsEnabled",
  liveWall: "previewLiveWallEnabled",
} satisfies Record<PreviewFeature, keyof Preferences>;

const PREVIEW_RUNTIME_FLAGS: Partial<Record<PreviewFeature, FeatureFlag>> = {
  customDb: "customDb",
  labTools: "labTools",
};

function safeReadJson<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function migrateLegacy(): Partial<Preferences> {
  const out: Partial<Preferences> = {};
  try {
    const legacyTheme = window.localStorage.getItem(LEGACY_THEME_KEY);
    if (legacyTheme === "light" || legacyTheme === "dark") {
      out.theme = legacyTheme;
    }
  } catch {
    /* private mode */
  }
  return out;
}

function readPersisted(): Preferences {
  if (typeof window === "undefined") return DEFAULT_PREFERENCES;
  const stored = safeReadJson<Partial<Preferences>>(STORAGE_KEY) ?? {};
  if (Object.keys(stored).length === 0) {
    return { ...DEFAULT_PREFERENCES, ...migrateLegacy() };
  }
  return {
    ...DEFAULT_PREFERENCES,
    ...stored,
    __v: 1,
  };
}

function writePersisted(prefs: Preferences): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* quota / private mode — keep the in-memory value */
  }
}

interface PreferencesContextValue {
  prefs: Preferences;
  setPref: <K extends keyof Preferences>(key: K, value: Preferences[K]) => void;
  reset: () => void;
}

const PreferencesContext = createContext<PreferencesContextValue>({
  prefs: DEFAULT_PREFERENCES,
  setPref: () => {},
  reset: () => {},
});

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = useState<Preferences>(readPersisted);

  useEffect(() => {
    writePersisted(prefs);
  }, [prefs]);

  const setPref = useCallback(
    <K extends keyof Preferences>(key: K, value: Preferences[K]) => {
      setPrefs((prev) => (prev[key] === value ? prev : { ...prev, [key]: value }));
    },
    [],
  );

  const reset = useCallback(() => setPrefs(DEFAULT_PREFERENCES), []);

  const value = useMemo<PreferencesContextValue>(
    () => ({ prefs, setPref, reset }),
    [prefs, setPref, reset],
  );

  return (
    <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
  );
}

export function usePreferences(): PreferencesContextValue {
  return useContext(PreferencesContext);
}

export function usePreviewFeatureEnabled(feature: PreviewFeature): boolean {
  const { prefs } = usePreferences();
  const runtimeFlag = PREVIEW_RUNTIME_FLAGS[feature];
  if (runtimeFlag && !isFeatureEnabled(runtimeFlag)) return false;
  return Boolean(prefs[PREVIEW_PREF_KEYS[feature]]);
}

export const PREFERENCES_DEFAULTS = DEFAULT_PREFERENCES;
