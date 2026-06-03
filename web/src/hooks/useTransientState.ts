import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Transient state that auto-resets to a base value after a delay, cancelling
 * any pending timer on re-trigger and on unmount.
 *
 * This replaces the recurring "copy feedback" anti-pattern:
 *
 *   const [copied, setCopied] = useState(false);
 *   // in a handler:
 *   setCopied(true);
 *   setTimeout(() => setCopied(false), 2000); // <- never cleared
 *
 * The bare `setTimeout` keeps a closure alive and fires `setState` even after
 * the component unmounts (a React "state update on an unmounted component"
 * warning and a small per-click timer leak). `useTransientState` owns the
 * timer in a ref and clears it on unmount and on every re-trigger, so only one
 * timer is ever live and it never touches an unmounted component.
 *
 * @param resetValue value the state returns to after `defaultDurationMs`
 * @param defaultDurationMs default auto-reset delay; can be overridden per call
 * @returns `[value, flash]` where `flash(next, durationMs?)` sets the value and
 *   schedules the reset
 */
export function useTransientState<T>(
  resetValue: T,
  defaultDurationMs = 2000,
): [T, (next: T, durationMs?: number) => void] {
  const [value, setValue] = useState<T>(resetValue);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const flash = useCallback(
    (next: T, durationMs?: number) => {
      clear();
      setValue(next);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        setValue(resetValue);
      }, durationMs ?? defaultDurationMs);
    },
    [clear, resetValue, defaultDurationMs],
  );

  useEffect(() => clear, [clear]);

  return [value, flash];
}
