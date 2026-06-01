// Pure derivation of the Runtime summary's warmup / sharding display strings.
//
// Responsibility: translate the warm-aware *effective* submit values into the
//   short labels shown in the BLAST submit Runtime summary rail.
// Edit boundaries: pure functions only — no React, no I/O. Keep in sync with
//   the submit payload in BlastSubmit (`effectiveShardingMode`, the
//   already-warm short-circuit) so the summary never disagrees with the run.
// Key entry points: `runtimeWarmupDisplay`, `runtimeShardingDisplay`.
// Risky contracts: the strings are user-visible in SubmitSummaryRail; tests
//   assert them verbatim.
// Validation: `npx vitest run src/pages/blastSubmit/runtimeSummaryDisplay.test.ts`.

import type { FormState } from "@/pages/blastSubmitModel";

export type ShardingMode = FormState["sharding_mode"];

/**
 * The warmup state that will actually apply at submit time. An already-warm
 * database satisfies warmup even if `form.enable_warmup` has not yet been
 * flipped on by the reconcile effect, so report it as ready rather than "off".
 */
export function runtimeWarmupDisplay(args: {
  isDbAlreadyWarm: boolean;
  enableWarmup: boolean;
}): string {
  if (args.isDbAlreadyWarm) return "warm cache ready";
  return args.enableWarmup ? "enabled" : "off";
}

/**
 * The sharding mode that will actually be submitted. Prefer the effective mode
 * (cluster + warm + capacity aware) over the raw form value, which can lag while
 * runtime data / warmup-status is still resolving.
 */
export function runtimeShardingDisplay(args: {
  effectiveShardingMode?: ShardingMode;
  formShardingMode: ShardingMode;
}): ShardingMode {
  return args.effectiveShardingMode ?? args.formShardingMode;
}
