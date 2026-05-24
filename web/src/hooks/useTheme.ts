/**
 * useTheme — apply the chosen theme to `data-theme` on <html>.
 *
 * Reads from `usePreferences` so the choice persists alongside other prefs
 * in `localStorage["elb-prefs"]`. Theme value can be `light`, `dark`, or
 * `system`; the third follows `prefers-color-scheme` and reacts to changes
 * live.
 */
import { useEffect, useMemo, useState } from "react";

import { usePreferences, type ThemeMode } from "@/hooks/usePreferences";

export type EffectiveTheme = "dark" | "light";

function readSystemPreference(): EffectiveTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme() {
  const { prefs, setPref } = usePreferences();
  const [systemTheme, setSystemTheme] = useState<EffectiveTheme>(readSystemPreference);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => {
      setSystemTheme(event.matches ? "dark" : "light");
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const effective: EffectiveTheme = useMemo(
    () => (prefs.theme === "system" ? systemTheme : prefs.theme),
    [prefs.theme, systemTheme],
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", effective);
  }, [effective]);

  return {
    theme: prefs.theme,
    effective,
    setTheme: (next: ThemeMode) => setPref("theme", next),
    /** Legacy 2-state toggle kept for any caller that wires the old icon
     *  button — switches between explicit dark/light, dropping "system". */
    toggle: () => setPref("theme", effective === "dark" ? "light" : "dark"),
  };
}
