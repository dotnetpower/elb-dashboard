import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";

/**
 * Per-DB sharding mutation. Triggered by clicking a "downloaded only" chip
 * — the backend runs `ensure_shard_sets()` against the existing download
 * and updates the metadata blob so the next listDatabases poll reports
 * `sharded=true`. While the call is in flight we render the chip as a
 * transient "sharding…" state so the user gets feedback without waiting
 * for the next 60 s refetch.
 *
 * 409 Conflict from the per-(account, db) lock means another tab or a
 * previous click already triggered the daemon — refetch to pull the
 * in-progress flag and clear the local error UI.
 */
export function useClusterShardMutation(args: {
  subscriptionId: string;
  storageAccount?: string;
  storageResourceGroup?: string;
}) {
  const { subscriptionId, storageAccount, storageResourceGroup } = args;
  const queryClient = useQueryClient();
  const [shardError, setShardError] = useState<{ name: string; msg: string } | null>(
    null,
  );

  const invalidateDbLists = () => {
    void queryClient.invalidateQueries({
      predicate: (q) => {
        const k = q.queryKey;
        return (
          Array.isArray(k) &&
          (k[0] === "blast-databases" || k[0] === "blast-databases-with-plan") &&
          k[1] === subscriptionId &&
          k[2] === (storageAccount ?? "") &&
          k[3] === (storageResourceGroup ?? "")
        );
      },
    });
  };

  const shardMutation = useMutation({
    mutationFn: async (dbName: string) => {
      if (!storageAccount || !storageResourceGroup) {
        throw new Error("storage account not selected");
      }
      return blastApi.shardDatabase(
        subscriptionId,
        storageResourceGroup,
        storageAccount,
        dbName,
      );
    },
    onSuccess: () => {
      setShardError(null);
      invalidateDbLists();
    },
    onError: (err, dbName) => {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("409") || msg.toLowerCase().includes("already in progress")) {
        setShardError(null);
        invalidateDbLists();
        return;
      }
      setShardError({ name: dbName, msg: msg.slice(0, 160) });
    },
  });
  const shardingDb = shardMutation.isPending ? shardMutation.variables : null;

  return { shardMutation, shardError, shardingDb };
}
