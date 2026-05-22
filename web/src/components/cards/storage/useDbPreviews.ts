import { useQueries } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";

/**
 * Fetch NCBI snapshot previews for a batch of catalog DB names.
 *
 * Solves Critique items 1, 3, and 15 — the user can now see snapshot id,
 * file count, estimated bytes, and last-modified BEFORE clicking Download.
 * ``available=false`` means the DB exists in the catalog but is not in the
 * current NCBI S3 snapshot (FTP-only or mid-publish), which previously
 * surfaced as a confusing 404 mid-copy.
 *
 * Each preview is independently cached server-side for 30 min; here we
 * cache for 10 min and disable refetching while the user keeps the modal
 * open, so opening + closing the modal a few times is essentially free.
 */
export interface DbPreviewMeta {
  db_name: string;
  snapshot?: string;
  available?: boolean;
  file_count?: number;
  volume_count?: number;
  total_bytes_estimate?: number;
  last_modified?: string | null;
  signature_etag?: string | null;
  files_sample?: string[];
  message?: string;
}

export function useDbPreviews(dbNames: string[], enabled: boolean) {
  const queries = useQueries({
    queries: dbNames.map((name) => ({
      queryKey: ["blast-db-preview", name],
      queryFn: async () => {
        try {
          return await blastApi.previewDatabase(name);
        } catch (error) {
          // Surface as DbPreviewMeta-shaped negative result so the UI can
          // still render the catalog row without a red error banner per
          // entry. The route's error toast is handled at click time.
          return {
            db_name: name,
            available: false,
            message:
              error instanceof Error
                ? error.message
                : "Could not contact NCBI for snapshot info.",
          } as DbPreviewMeta;
        }
      },
      enabled,
      staleTime: 10 * 60_000,
      refetchOnWindowFocus: false,
      refetchOnMount: false,
      retry: 0,
    })),
  });
  const byName = new Map<string, DbPreviewMeta>();
  queries.forEach((query, idx) => {
    if (query.data) byName.set(dbNames[idx], query.data as DbPreviewMeta);
  });
  const loading = queries.some((q) => q.isLoading);
  return { byName, loading };
}
