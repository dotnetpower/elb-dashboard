import type { CSSProperties } from "react";

export interface RowParticleProps {
  delaySec?: number;
  durationSec?: number;
  endRight?: string;
  onEnd?: () => void;
}

export function RowParticle({
  delaySec = 0,
  durationSec,
  endRight,
  onEnd,
}: RowParticleProps) {
  const style: CSSProperties & Record<string, string> = {
    animationDelay: `${delaySec}s`,
    // One-shot: each particle represents a single real event drained from
    // the snapshot. Looping would re-introduce the decorative behaviour we
    // just removed. The class default (set in the <style> block below) is
    // overridden by this inline value.
    animationIterationCount: "1",
    animationFillMode: "forwards",
  };
  if (endRight) style["--row-end"] = endRight;
  if (durationSec) style.animationDuration = `${durationSec}s`;
  return (
    <span
      className="topo-row-particle"
      aria-hidden
      style={style}
      onAnimationEnd={onEnd}
    />
  );
}
