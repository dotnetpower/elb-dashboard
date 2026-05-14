import { useCallback, useEffect, useRef, useState } from "react";

export function useClipboardFeedback(timeoutMs = 2000) {
  const [copied, setCopied] = useState<string | null>(null);
  const timeoutRef = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    },
    [],
  );

  const copyText = useCallback((text: string, label: string) => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => undefined);
    }
    setCopied(label);
    if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    timeoutRef.current = window.setTimeout(() => {
      setCopied(null);
      timeoutRef.current = null;
    }, timeoutMs);
  }, [timeoutMs]);

  return { copied, copyText };
}