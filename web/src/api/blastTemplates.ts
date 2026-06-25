/**
 * blastTemplates — typed client for `/api/blast/templates` (saved submit presets).
 *
 * A template stores a named snapshot of the submit form's *option* fields
 * (never the query data — that is re-entered each run). Backed by per-user
 * Azure Table storage; the `fields` blob is the same `ExportableFormFields`
 * shape the config export/duplicate flow already uses.
 */
import { api } from "@/api/client";
import type { ExportableFormFields } from "@/pages/blastSubmit/configSerializer";

export interface BlastTemplate {
  id: string;
  name: string;
  fields: ExportableFormFields;
  created_at: string;
  updated_at: string;
}

export interface BlastTemplateList {
  templates: BlastTemplate[];
}

export const blastTemplatesApi = {
  list: () => api.get<BlastTemplateList>("/api/blast/templates"),
  create: (name: string, fields: ExportableFormFields) =>
    api.post<BlastTemplate>("/api/blast/templates", { name, fields }),
  update: (id: string, body: { name?: string; fields?: ExportableFormFields }) =>
    api.put<BlastTemplate>(`/api/blast/templates/${encodeURIComponent(id)}`, body),
  remove: (id: string) =>
    api.del<{ deleted: boolean }>(`/api/blast/templates/${encodeURIComponent(id)}`),
};
