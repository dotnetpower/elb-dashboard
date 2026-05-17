import { useCallback, useEffect, useRef, useState } from "react";

import type { SidecarsSnapshot } from "@/hooks/useSidecarMetrics";

import {
  PARTICLES_PER_TICK_CAP,
  PARTICLE_LIFETIME_MS,
  PARTICLE_QUEUE_HARD_CAP,
  PARTICLE_STAGGER_SEC,
} from "./constants";
import { usePageVisible, useReducedMotion } from "./visibilityHooks";

export interface ParticleEvent {
  id: number;
  row: 1 | 2 | 3 | 4;
  delaySec: number;
}

export type RowKey = "row1" | "row2" | "row3" | "row4";

export function useEventParticles(data: SidecarsSnapshot | undefined): {
  particles: ParticleEvent[];
  lastCounts: Record<RowKey, number>;
  remove: (id: number) => void;
} {
  const reducedMotion = useReducedMotion();
  const pageVisible = usePageVisible();
  const [particles, setParticles] = useState<ParticleEvent[]>([]);
  const [lastCounts, setLastCounts] = useState<Record<RowKey, number>>({
    row1: 0,
    row2: 0,
    row3: 0,
    row4: 0,
  });
  const seenTsRef = useRef<number | null>(null);
  const idRef = useRef(1);
  const timersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  const remove = useCallback((id: number) => {
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setParticles((prev) =>
      prev.some((p) => p.id === id) ? prev.filter((p) => p.id !== id) : prev,
    );
  }, []);

  useEffect(() => {
    if (!data || data.ts == null) return;
    if (seenTsRef.current === data.ts) return;
    seenTsRef.current = data.ts;

    const events = data.events ?? {};
    const rawCounts: Record<RowKey, number> = {
      row1: Math.max(0, Math.trunc(Number(events.row1 ?? 0))),
      row2: Math.max(0, Math.trunc(Number(events.row2 ?? 0))),
      row3: Math.max(0, Math.trunc(Number(events.row3 ?? 0))),
      row4: Math.max(0, Math.trunc(Number(events.row4 ?? 0))),
    };
    setLastCounts((prev) =>
      prev.row1 === rawCounts.row1 &&
      prev.row2 === rawCounts.row2 &&
      prev.row3 === rawCounts.row3 &&
      prev.row4 === rawCounts.row4
        ? prev
        : rawCounts,
    );

    if (reducedMotion || !pageVisible) return;

    const additions: ParticleEvent[] = [];
    (["row1", "row2", "row3", "row4"] as const).forEach((rowKey, i) => {
      const cap = Math.min(PARTICLES_PER_TICK_CAP, rawCounts[rowKey]);
      for (let j = 0; j < cap; j++) {
        additions.push({
          id: idRef.current++,
          row: (i + 1) as 1 | 2 | 3 | 4,
          delaySec: j * PARTICLE_STAGGER_SEC,
        });
      }
    });
    if (additions.length === 0) return;

    setParticles((prev) => {
      const merged = [...prev, ...additions];
      if (merged.length <= PARTICLE_QUEUE_HARD_CAP) return merged;
      const overflow = merged.length - PARTICLE_QUEUE_HARD_CAP;
      const dropped = merged.slice(0, overflow);
      for (const d of dropped) {
        const t = timersRef.current.get(d.id);
        if (t) {
          clearTimeout(t);
          timersRef.current.delete(d.id);
        }
      }
      return merged.slice(overflow);
    });

    for (const p of additions) {
      const totalMs = PARTICLE_LIFETIME_MS + Math.ceil(p.delaySec * 1000);
      const t = setTimeout(() => remove(p.id), totalMs);
      timersRef.current.set(p.id, t);
    }
  }, [data, reducedMotion, pageVisible, remove]);

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const t of timers.values()) clearTimeout(t);
      timers.clear();
    };
  }, []);

  return { particles, lastCounts, remove };
}
