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

import { SettingsPanel } from "@/components/SettingsPanel";

interface SettingsPanelContextValue {
  isOpen: boolean;
  open: () => void;
  close: () => void;
}

const SettingsPanelContext = createContext<SettingsPanelContextValue>({
  isOpen: false,
  open: () => {},
  close: () => {},
});

export function SettingsPanelProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const open = useCallback(() => setIsOpen(true), []);
  const close = useCallback(() => setIsOpen(false), []);
  const value = useMemo<SettingsPanelContextValue>(
    () => ({ isOpen, open, close }),
    [isOpen, open, close],
  );
  return (
    <SettingsPanelContext.Provider value={value}>
      {children}
      <SettingsPanel open={isOpen} onClose={close} />
    </SettingsPanelContext.Provider>
  );
}

export function useSettingsPanel(): SettingsPanelContextValue {
  return useContext(SettingsPanelContext);
}
