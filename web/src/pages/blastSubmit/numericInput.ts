/**
 * Numeric `<input type="number">` change parsing for the BLAST submit form.
 *
 * The naive pattern `parseInt(e.target.value, 10) || fallback` has two real
 * bugs: a deliberately-entered `"0"` is falsy and silently becomes the
 * fallback, and a transient empty / non-numeric value produces `NaN` that the
 * `|| fallback` masks (so the user never learns their input was rejected).
 *
 * `parseNumericInput` keeps a valid number (including `0`) and only falls back
 * when the value is genuinely not a finite number (empty field, garbage paste).
 */

export function parseNumericInput(raw: string, fallback: number): number {
  const trimmed = raw.trim();
  if (trimmed === "") return fallback;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : fallback;
}
