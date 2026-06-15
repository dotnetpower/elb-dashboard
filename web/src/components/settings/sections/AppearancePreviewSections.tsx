import { Monitor, Moon, Sun } from "lucide-react";

import { usePreferences, type ThemeMode } from "@/hooks/usePreferences";
import { useTheme } from "@/hooks/useTheme";
import { Group, Row, Section, Segmented, StatusLine, Toggle } from "@/components/settings/primitives";

/**
 * Appearance + Preview settings sections.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Both are
 * small, self-contained sections backed only by the browser-local
 * `useTheme` / `usePreferences` hooks — no Azure data, no panel state.
 */

export function AppearanceSection() {
  const { theme, setTheme } = useTheme();
  return (
    <Section heading="Appearance">
      <Group>
        <Row
          label="Theme"
          hint="Choose a fixed palette or follow your OS preference."
          control={
            <Segmented<ThemeMode>
              ariaLabel="Theme"
              value={theme}
              onChange={setTheme}
              options={[
                { value: "light", label: <><Sun size={12} /> Light</> },
                { value: "dark", label: <><Moon size={12} /> Dark</> },
                { value: "system", label: <><Monitor size={12} /> System</> },
              ]}
            />
          }
        />
      </Group>
    </Section>
  );
}

export function PreviewSection() {
  const { prefs, setPref } = usePreferences();
  return (
    <Section heading="Preview">
      <Group>
        <Row
          label="Custom DB"
          hint="Show the custom database builder route and navigation entry. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewCustomDbEnabled}
              onChange={(value) => setPref("previewCustomDbEnabled", value)}
              label="Custom DB preview"
            />
          }
        />
        <Row
          label="Lab Tools"
          hint="Show Lab Tools in the top navigation. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewLabToolsEnabled}
              onChange={(value) => setPref("previewLabToolsEnabled", value)}
              label="Lab Tools preview"
            />
          }
        />
        <Row
          label="Live Wall"
          hint="Show the Live Wall monitor route and navigation entry. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewLiveWallEnabled}
              onChange={(value) => setPref("previewLiveWallEnabled", value)}
              label="Live Wall preview"
            />
          }
        />
        <Row
          label="Terminal"
          hint="Show the browser terminal route, navigation entry, dashboard card, and keyboard shortcut. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewTerminalEnabled}
              onChange={(value) => setPref("previewTerminalEnabled", value)}
              label="Terminal preview"
            />
          }
        />
        <Row
          label="Service Bus Playground"
          hint="Show the Service Bus Playground page: send BLAST request messages onto the queue and watch the consumer execute them. Requires the deployment Service Bus integration to be active. Disabled by default."
          control={
            <Toggle
              checked={prefs.previewServiceBusPlaygroundEnabled}
              onChange={(value) => setPref("previewServiceBusPlaygroundEnabled", value)}
              label="Service Bus Playground preview"
            />
          }
        />
      </Group>
      <StatusLine kind="info">
        Preview selections are stored in this browser only and take effect immediately.
      </StatusLine>
    </Section>
  );
}
