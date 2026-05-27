import type { RefObject } from "react";

import type { BlastDatabase, WarmupDbInfo } from "@/api/endpoints";
import type { FormState, PROGRAMS } from "@/pages/blastSubmitModel";

export type SetBlastField = <K extends keyof FormState>(key: K, value: FormState[K]) => void;

export type ProgramMeta = (typeof PROGRAMS)[0];

export type ToastFn = (msg: string, type: "info" | "success" | "error") => void;

export interface QuerySectionProps {
  form: FormState;
  set: SetBlastField;
  fileInputRef: RefObject<HTMLInputElement>;
  toast: ToastFn;
  isFasta: boolean;
  seqCount: number;
  charCount: number;
}

export interface ProgramSectionProps {
  form: FormState;
  set: SetBlastField;
  programMeta: ProgramMeta;
}

export interface DatabaseSectionProps {
  form: FormState;
  set: SetBlastField;
  programMeta: ProgramMeta;
  databases?: BlastDatabase[];
  dbLoading?: boolean;
  warmDbs?: Map<string, WarmupDbInfo>;
  warmupKnown: boolean;
  dbWarning: string | null;
  dbMissingFromStorage: boolean;
  /** True when the selected DB exists in Storage but is mid-copy / mid-update. */
  dbNotReady?: boolean;
  dbNotReadyReason?: string | null;
  dbBaseName: string;
}

export interface TaxonomyFilterSectionProps {
  form: FormState;
  set: SetBlastField;
}
