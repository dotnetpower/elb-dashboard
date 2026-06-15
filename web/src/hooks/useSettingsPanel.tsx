/**
 * useSettingsPanel — open/close control + provider for the global SettingsPanel.
 *
 * The provider mounts a single SettingsPanel near the SPA root so any
 * component (Layout topbar gear, DashboardHeader gear) can request the
 * panel via `useSettingsPanel().open()` without owning React state of its
 * own. Avoids the previous "every page that wants the panel must lift
 * state" coupling.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { SettingsPanel, type SettingsSectionId } from "@/components/SettingsPanel";

interface SettingsPanelContextValue {
  isOpen: boolean;
  /** Open the panel, optionally focusing a specific section (e.g. "updates"). */
  open: (section?: SettingsSectionId) => void;
  close: () => void;
}

const SettingsPanelContext = createContext<SettingsPanelContextValue>({
  isOpen: false,
  open: () => {},
  close: () => {},
});

const VALID_SECTIONS: ReadonlySet<string> = new Set<SettingsSectionId>([
  "appearance",
  "preview",
  "updates",
  "telemetry",
  "aks",
  "performance",
  "public-https",
  "control-plane",
  "vnet-peering",
  "service-bus",
  "sizing",
  "diagnostics",
  "resources",
]);

/**
 * Coerce the `open()` argument to a known section id, or `undefined`.
 *
 * Some callers wire `open` straight to an `onClick` (`onClick={openSettings}`),
 * which passes the click event in as the argument. Returning `undefined` for
 * anything that is not a known section id keeps those call sites opening the
 * panel on its default section instead of an invalid one.
 */
export function normalizeSettingsSection(next: unknown): SettingsSectionId | undefined {
  return typeof next === "string" && VALID_SECTIONS.has(next)
    ? (next as SettingsSectionId)
    : undefined;
}

export function SettingsPanelProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [section, setSection] = useState<SettingsSectionId | undefined>(undefined);
  const open = useCallback((next?: SettingsSectionId) => {
    setSection(normalizeSettingsSection(next));
    setIsOpen(true);
  }, []);
  const close = useCallback(() => setIsOpen(false), []);
  const value = useMemo<SettingsPanelContextValue>(
    () => ({ isOpen, open, close }),
    [isOpen, open, close],
  );
  return (
    <SettingsPanelContext.Provider value={value}>
      {children}
      <SettingsPanel open={isOpen} onClose={close} initialSection={section} />
    </SettingsPanelContext.Provider>
  );
}

export function useSettingsPanel(): SettingsPanelContextValue {
  return useContext(SettingsPanelContext);
}
