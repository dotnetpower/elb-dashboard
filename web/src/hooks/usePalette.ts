import { useState, useEffect, useCallback } from "react";

/**
 * Light-theme palette variants.
 *  - "aurora"  : the default lavender/cobalt glass look.
 *  - "msbrand" : Microsoft brand-color palette (Blue #0078D4 family).
 *
 * The palette attribute applies regardless of theme, but only the
 * light theme actually swaps tokens against the variant — dark mode
 * ignores `data-palette` so toggling has no effect there.
 */
export type Palette = "aurora" | "msbrand";

const STORAGE_KEY = "elb-palette";

function getStoredPalette(): Palette {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "aurora" || stored === "msbrand") return stored;
  } catch { /* noop */ }
  return "aurora";
}

export function usePalette() {
  const [palette, setPaletteState] = useState<Palette>(getStoredPalette);

  useEffect(() => {
    document.documentElement.setAttribute("data-palette", palette);
    try { localStorage.setItem(STORAGE_KEY, palette); } catch { /* noop */ }
  }, [palette]);

  const toggle = useCallback(() => {
    setPaletteState((prev) => (prev === "aurora" ? "msbrand" : "aurora"));
  }, []);

  return { palette, toggle };
}
