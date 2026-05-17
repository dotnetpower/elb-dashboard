import { useEffect, useState } from "react";

import { INITIAL, type FormState } from "@/pages/blastSubmitModel";

export const DRAFT_SCHEMA_VERSION = 5;
export const DRAFT_KEY = "elb-blast-draft";
export const OUTFMT_PREFERENCE_KEY = "elb-blast-outfmt";

const SUPPORTED_OUTFMT_VALUES = new Set([0, 5, 6, 7, 11]);

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem">;
type RemovableStorage = Pick<Storage, "removeItem">;

function safeSessionStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function safeLocalStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function parseOutfmtPreference(value: string | null): number | null {
  if (value == null) return null;
  const parsed = Number.parseInt(value, 10);
  return SUPPORTED_OUTFMT_VALUES.has(parsed) ? parsed : null;
}

export function loadOutfmtPreference(
  storage: ReadableStorage | null = safeLocalStorage(),
): number | null {
  try {
    return parseOutfmtPreference(storage?.getItem(OUTFMT_PREFERENCE_KEY) ?? null);
  } catch {
    return null;
  }
}

export function saveOutfmtPreference(
  outfmt: number,
  storage: WritableStorage | null = safeLocalStorage(),
): void {
  if (!SUPPORTED_OUTFMT_VALUES.has(outfmt)) return;
  try {
    storage?.setItem(OUTFMT_PREFERENCE_KEY, String(outfmt));
  } catch {
    /* ignore */
  }
}

export function createInitialForm(
  localStorage: ReadableStorage | null = safeLocalStorage(),
): FormState {
  return {
    ...INITIAL,
    outfmt: loadOutfmtPreference(localStorage) ?? INITIAL.outfmt,
  };
}

export function restoreDraftForm(
  sessionStorage: ReadableStorage | null = safeSessionStorage(),
  localStorage: ReadableStorage | null = safeLocalStorage(),
): FormState {
  const base = createInitialForm(localStorage);
  try {
    const saved = sessionStorage?.getItem(DRAFT_KEY);
    if (saved) {
      const parsed = JSON.parse(saved) as Partial<FormState> & {
        draft_version?: number;
      };
      const restored = { ...base, ...parsed };
      if (parsed.draft_version !== DRAFT_SCHEMA_VERSION) {
        restored.db_auto_partition = INITIAL.db_auto_partition;
        restored.sharding_mode = INITIAL.sharding_mode;
        restored.enable_warmup = INITIAL.enable_warmup;
        restored.disable_sharding = INITIAL.disable_sharding;
      }
      restored.outfmt = base.outfmt;
      return restored;
    }
  } catch {
    /* ignore */
  }
  return base;
}

/**
 * Form state with sessionStorage draft restore + auto-save. The schema
 * version invalidates the draft fields that were renamed/added; everything
 * else falls back to INITIAL via spread.
 */
export function useDraftForm() {
  const [form, setForm] = useState<FormState>(() => restoreDraftForm());

  useEffect(() => {
    safeSessionStorage()?.setItem(
      DRAFT_KEY,
      JSON.stringify({ ...form, draft_version: DRAFT_SCHEMA_VERSION }),
    );
  }, [form]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    if (key === "outfmt") saveOutfmtPreference(Number(value));
    setForm((f) => ({ ...f, [key]: value }));
  };

  const reset = () => setForm(createInitialForm());

  const clearDraft = () => {
    try {
      (safeSessionStorage() as RemovableStorage | null)?.removeItem(DRAFT_KEY);
    } catch {
      /* ignore */
    }
  };

  return { form, setForm, set, reset, clearDraft } as const;
}
