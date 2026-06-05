/**
 * Shared inline style tokens for the Settings panel sections.
 *
 * Extracted from `SettingsPanel.tsx` (issue #24 SRP split) so per-section
 * modules can render the same text inputs / selects without re-importing the
 * monolith. Pure constant, no React state.
 */

export const INPUT_STYLE: React.CSSProperties = {
  background: "var(--bg-tertiary)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-weak)",
  borderRadius: 6,
  padding: "8px 10px",
  fontSize: 12,
  fontFamily: "var(--font-mono)",
  width: "100%",
  boxSizing: "border-box",
};
