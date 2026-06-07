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

/**
 * Select variant of {@link INPUT_STYLE}. Native `<select>` elements render a
 * browser-default chevron that clashes with the glass theme, so this style
 * suppresses it (`appearance: none`) and paints the same custom chevron the
 * canonical `select.glass-input` / `select.form-input` rules use — keeping
 * every dropdown in the app visually consistent. Reserves right padding so a
 * long selected option never runs under the chevron.
 */
export const SELECT_STYLE: React.CSSProperties = {
  ...INPUT_STYLE,
  appearance: "none",
  WebkitAppearance: "none",
  MozAppearance: "none",
  cursor: "pointer",
  paddingRight: 30,
  backgroundImage:
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%235a6272' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")",
  backgroundRepeat: "no-repeat",
  backgroundPosition: "right 10px center",
};
