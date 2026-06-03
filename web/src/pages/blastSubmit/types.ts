import type { RefObject } from "react";

import type { BlastDatabase, BlastProgram, WarmupDbInfo } from "@/api/endpoints";
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
  programMeta: ProgramMeta;
  /**
   * Select a program. The parent decides whether to switch, overwrite the
   * database, or block the change (and toast) based on which molecule types
   * have a ready database downloaded — keep that logic in the parent so this
   * section stays presentational.
   */
  onSelectProgram: (value: BlastProgram) => void;
  /** Which molecule types have at least one ready DB; blocks tabs otherwise. */
  dbAvailableByType: { nucl: boolean; prot: boolean };
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

export interface OptimizeSectionProps {
  form: FormState;
  set: SetBlastField;
  programMeta: ProgramMeta;
}
