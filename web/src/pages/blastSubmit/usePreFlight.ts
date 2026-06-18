import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { formatApiError } from "@/api/client";
import { blastApi } from "@/api/endpoints";
import type { ApiAdmission, ApiResponseMeta } from "@/api/blast";

import type { ToastFn } from "./types";

export interface PreFlightCheck {
  id: string;
  status: string;
  title: string;
  detail?: string;
  action?: string;
  action_type?: string;
  action_params?: Record<string, string>;
  severity?: string;
  suggested_dbs?: string[];
}

export interface PreFlightResult {
  status?: string;
  ready: boolean;
  decision?: string;
  checks: PreFlightCheck[];
  critical_blockers: number;
  summary: string;
  admission?: ApiAdmission;
  meta?: ApiResponseMeta;
}

export interface UsePreFlightArgs {
  toast: ToastFn;
  payload: () => {
    subscription_id: string;
    resource_group: string;
    acr_resource_group?: string;
    acr_name?: string;
    storage_account: string;
    aks_cluster_name: string;
    db: string;
    additional_options?: string;
    taxid?: number;
    is_inclusive?: boolean;
    allow_approximate_sharding?: boolean;
    db_auto_partition?: boolean;
    db_effective_search_space?: number;
    db_total_bytes?: number;
    db_total_letters?: number;
    db_total_sequences?: number;
    disable_sharding?: boolean;
    enable_warmup?: boolean;
    evalue?: number;
    max_target_seqs?: number;
    outfmt?: number;
    query_data?: string;
    shard_sets?: number[];
    sharding_mode?: "off" | "approximate" | "precise";
    word_size?: number;
  };
}

export function usePreFlight({ toast, payload }: UsePreFlightArgs) {
  const [preFlightResult, setPreFlightResult] = useState<PreFlightResult | null>(
    null,
  );

  const preFlightMutation = useMutation({
    mutationFn: () => blastApi.preFlight(payload()),
    onSuccess: (result) => {
      setPreFlightResult(result);
      if (result.ready) {
        toast("All pre-flight checks passed", "success");
      }
    },
    onError: (err: Error) => {
      toast(`Pre-flight check failed: ${formatApiError(err, "blast")}`, "error");
    },
  });

  return { preFlightResult, preFlightMutation } as const;
}
