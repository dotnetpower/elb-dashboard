/**
 * useBlastTemplates — list + create/delete saved submit templates.
 *
 * Used by the submit form's template control. Polling is unnecessary (templates
 * change only on explicit user action), so this is a plain cached query with
 * mutation-driven invalidation.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { blastTemplatesApi, type BlastTemplate } from "@/api/blastTemplates";
import type { ExportableFormFields } from "@/pages/blastSubmit/configSerializer";

const QUERY_KEY = ["blast", "templates"] as const;

export function useBlastTemplates() {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => blastTemplatesApi.list(),
    staleTime: 30_000,
    retry: false,
  });

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY });

  const createMutation = useMutation({
    mutationFn: ({ name, fields }: { name: string; fields: ExportableFormFields }) =>
      blastTemplatesApi.create(name, fields),
    onSuccess: invalidate,
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => blastTemplatesApi.remove(id),
    onSuccess: invalidate,
  });

  const templates: BlastTemplate[] = query.data?.templates ?? [];

  return {
    templates,
    isLoading: query.isLoading,
    isError: query.isError,
    create: createMutation,
    remove: removeMutation,
  };
}
