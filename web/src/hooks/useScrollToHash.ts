import { useEffect } from "react";
import { useLocation } from "react-router-dom";

export function useScrollToHash(delayMs = 200): void {
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash) {
      return;
    }
    const id = hash.replace(/^#/, "");
    if (!id) {
      return;
    }
    const handle = window.setTimeout(() => {
      const target = document.getElementById(id);
      target?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, delayMs);
    return () => window.clearTimeout(handle);
  }, [hash, delayMs]);
}
