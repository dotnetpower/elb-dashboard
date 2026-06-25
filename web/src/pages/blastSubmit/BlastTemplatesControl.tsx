/**
 * BlastTemplatesControl — save/apply per-user submit-option templates.
 *
 * Renders a "load template" dropdown + a "save as template" inline form above
 * the submit fields. A template stores only the option fields (program, db,
 * algorithm params, sharding) — never the query data — so applying one fills
 * the parameters and leaves the researcher's query untouched.
 */
import { useState } from "react";
import { BookMarked, Save, Trash2 } from "lucide-react";

import { useToast } from "@/components/Toast";
import { useBlastTemplates } from "@/hooks/useBlastTemplates";
import {
  pickExportableForm,
  type ExportableFormFields,
} from "@/pages/blastSubmit/configSerializer";
import type { FormState } from "@/pages/blastSubmitModel";

/** Option-only snapshot: drop the query + per-run fields a preset must not pin. */
export function pickTemplateFields(form: FormState): ExportableFormFields {
  const fields = pickExportableForm(form);
  delete fields.query_data;
  delete fields.query_from;
  delete fields.query_to;
  delete fields.job_title;
  return fields;
}

export function BlastTemplatesControl({
  form,
  onApply,
}: {
  form: FormState;
  onApply: (fields: ExportableFormFields) => void;
}) {
  const { templates, isLoading, create, remove } = useBlastTemplates();
  const { toast } = useToast();
  const [selectedId, setSelectedId] = useState("");
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");

  const handleApply = (id: string) => {
    setSelectedId(id);
    const template = templates.find((t) => t.id === id);
    if (!template) return;
    onApply(template.fields);
    toast(`Applied template "${template.name}" — your query is unchanged.`, "success");
  };

  const handleSave = () => {
    const trimmed = name.trim();
    if (!trimmed) {
      toast("Enter a template name first.", "error");
      return;
    }
    create.mutate(
      { name: trimmed, fields: pickTemplateFields(form) },
      {
        onSuccess: () => {
          toast(`Saved template "${trimmed}".`, "success");
          setName("");
          setSaving(false);
        },
        onError: (err) =>
          toast(
            `Could not save template: ${err instanceof Error ? err.message : "unknown error"}`,
            "error",
          ),
      },
    );
  };

  const handleDelete = (id: string, label: string) => {
    remove.mutate(id, {
      onSuccess: () => {
        if (selectedId === id) setSelectedId("");
        toast(`Deleted template "${label}".`, "info");
      },
    });
  };

  const selected = templates.find((t) => t.id === selectedId);

  return (
    <div
      className="glass-card"
      style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: 10 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <BookMarked size={15} strokeWidth={1.5} style={{ color: "var(--text-muted)" }} />
        <span style={{ fontSize: 13, fontWeight: 600 }}>Submit templates</span>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          (parameters only — query is never stored)
        </span>
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select
          value={selectedId}
          onChange={(e) => handleApply(e.target.value)}
          disabled={isLoading || templates.length === 0}
          aria-label="Apply a saved template"
          style={{ flex: "1 1 200px", minWidth: 180, padding: "6px 8px" }}
        >
          <option value="">
            {templates.length === 0 ? "No saved templates" : "Apply a template…"}
          </option>
          {templates.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        {selected && (
          <button
            type="button"
            onClick={() => handleDelete(selected.id, selected.name)}
            disabled={remove.isPending}
            title={`Delete "${selected.name}"`}
            aria-label={`Delete template ${selected.name}`}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "6px 10px",
              fontSize: 12,
              color: "var(--danger, #f87171)",
              background: "none",
              border: "1px solid var(--border-weak)",
              borderRadius: 8,
              cursor: "pointer",
            }}
          >
            <Trash2 size={13} /> Delete
          </button>
        )}
      </div>

      {saving ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Template name"
            maxLength={120}
            aria-label="New template name"
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSave();
            }}
            style={{ flex: "1 1 200px", minWidth: 180, padding: "6px 8px" }}
          />
          <button
            type="button"
            onClick={handleSave}
            disabled={create.isPending}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "6px 12px",
              fontSize: 12,
              color: "var(--accent)",
              background: "none",
              border: "1px solid var(--border-weak)",
              borderRadius: 8,
              cursor: "pointer",
            }}
          >
            <Save size={13} /> {create.isPending ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => {
              setSaving(false);
              setName("");
            }}
            style={{
              padding: "6px 10px",
              fontSize: 12,
              color: "var(--text-muted)",
              background: "none",
              border: "none",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setSaving(true)}
          style={{
            alignSelf: "flex-start",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            padding: "6px 12px",
            fontSize: 12,
            color: "var(--accent)",
            background: "none",
            border: "1px solid var(--border-weak)",
            borderRadius: 8,
            cursor: "pointer",
          }}
        >
          <Save size={13} /> Save current parameters as template
        </button>
      )}
    </div>
  );
}
