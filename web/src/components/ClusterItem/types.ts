import type { WarmupDbInfo } from "@/api/endpoints";
import type { BlastDatabase } from "@/api/blast";

export type DbChip = {
  name: string;
  warm?: WarmupDbInfo;
  sharded: boolean;
  shardLayouts: number;
  shardingInProgress: boolean;
  shardingError: string | null;
  /** Server-computed warmup feasibility — only set when cluster topology was supplied. */
  warmupPlan?: BlastDatabase["warmup_plan"];
};
