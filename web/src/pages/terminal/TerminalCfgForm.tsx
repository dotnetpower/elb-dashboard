import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Database, FileCog, SendToBack } from "lucide-react";

import { blastApi } from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { buildDatabasePath } from "@/pages/blastSubmit/helpers";
import { isBlastDbReady } from "@/utils/blastDbReady";
import {
  buildElbCfgCommand,
  ELB_CFG_FORM_DEFAULTS,
  type ElbCfgFormFields,
} from "@/pages/terminal/terminalCockpitModel";

interface TerminalCfgFormProps {
  // Push the composed `elb-cfg ...` command into the Command Preview so it
  // flows through the existing risk classification + Insert pipeline.
  onApply: (command: string) => void;
}

// Field descriptors drive the rendered inputs. Keeping them declarative makes
// the form easy to extend without growing the JSX. `kind` selects the control:
// a free-text input, a constrained number, or a fixed-choice dropdown — so the
// fields that have a known value set (program) no longer require memorising a
// magic string.
type FieldDescriptor = {
  key: keyof ElbCfgFormFields;
  label: string;
  placeholder: string;
  hint?: string;
  kind?: "text" | "number" | "select";
  options?: ReadonlyArray<{ value: string; label: string }>;
  min?: number;
};

// Canonical BLAST programs accepted by `--program` (mirrors the backend
// `^(blastn|blastp|blastx|tblastn|tblastx)$` contract in api/_http_utils.py).
const BLAST_PROGRAM_OPTIONS = [
  { value: "blastn", label: "blastn (nt → nt)" },
  { value: "blastp", label: "blastp (aa → aa)" },
  { value: "blastx", label: "blastx (nt → aa)" },
  { value: "tblastn", label: "tblastn (aa → nt)" },
  { value: "tblastx", label: "tblastx (nt → nt, translated)" },
] as const;

// Sentinel select value that reveals the free-text path input for databases
// that are not in the ready list (custom makeblastdb builds, ad-hoc uploads).
const DB_CUSTOM_VALUE = "__custom__";

// The handful of inputs a researcher actually fills in for a typical run.
// Database is rendered separately (it is a live picker, not a descriptor).
const ESSENTIAL_FIELDS: FieldDescriptor[] = [
  {
    key: "program",
    label: "Program",
    placeholder: "blastn",
    kind: "select",
    options: BLAST_PROGRAM_OPTIONS,
    hint: "Match the program to your query and database types.",
  },
  {
    key: "queries",
    label: "Queries",
    placeholder: "my-query.fa",
    hint: "Bare name expands under the queries container.",
  },
  {
    key: "results",
    label: "Results",
    placeholder: "results/run-001",
    hint: "Optional. Bare name expands under the results container.",
  },
];

// Environment-derived overrides. Empty values fall back to the platform
// defaults, so these stay collapsed behind a disclosure to keep the form
// uncluttered for the common case.
const ADVANCED_FIELDS: FieldDescriptor[] = [
  { key: "machineType", label: "Machine type", placeholder: "(platform default)" },
  { key: "numNodes", label: "Nodes", placeholder: "1", kind: "number", min: 1 },
  { key: "region", label: "Region", placeholder: "(from environment)" },
  { key: "resourceGroup", label: "Resource group", placeholder: "(from environment)" },
  { key: "storageAccount", label: "Storage account", placeholder: "(from environment)" },
  { key: "acrName", label: "ACR name", placeholder: "(from environment)" },
  { key: "output", label: "Output path", placeholder: "~/elastic-blast.ini" },
];

export function TerminalCfgForm({ onApply }: TerminalCfgFormProps) {
  const [fields, setFields] = useState<ElbCfgFormFields>(ELB_CFG_FORM_DEFAULTS);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [dbManual, setDbManual] = useState(false);
  const command = buildElbCfgCommand(fields);

  const update = (key: keyof ElbCfgFormFields, value: string) =>
    setFields((prev) => ({ ...prev, [key]: value }));

  // Resolve the workspace anchor so we can list the databases that are
  // actually present in the user's Storage account, instead of asking them
  // to type a magic path. Read once on mount — the saved config is stable.
  const config = useMemo(() => loadSavedConfig(), []);
  const subId = config?.subscriptionId ?? "";
  const storageAccount = config?.storageAccountName ?? "";
  const workloadRg = config?.workloadResourceGroup ?? "";
  const dbListingEnabled = Boolean(subId && storageAccount && workloadRg);

  const dbQuery = useQuery({
    queryKey: ["terminal-cfg-databases", subId, storageAccount, workloadRg],
    queryFn: () => blastApi.listDatabases(subId, storageAccount, workloadRg),
    enabled: dbListingEnabled,
  });

  // Only ready/downloaded databases are runnable — mirror the Submit page so
  // the picker never offers a half-copied or updating DB.
  const readyDbs = useMemo(
    () =>
      (dbQuery.data?.databases ?? [])
        .filter(isBlastDbReady)
        .map((db) => ({ name: db.name, path: buildDatabasePath(db) }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    [dbQuery.data?.databases],
  );

  const readyPaths = useMemo(() => new Set(readyDbs.map((d) => d.path)), [readyDbs]);
  const dbIsCustom = fields.db !== "" && !readyPaths.has(fields.db);
  const showDbSelect = dbListingEnabled && (dbQuery.isLoading || readyDbs.length > 0);
  const showDbManualInput = !showDbSelect || dbManual || dbIsCustom;
  const dbSelectValue = readyPaths.has(fields.db)
    ? fields.db
    : dbManual || dbIsCustom
      ? DB_CUSTOM_VALUE
      : "";

  const dbHint = !dbListingEnabled
    ? "Connect a workspace from Setup to list your downloaded databases here."
    : dbQuery.isError
      ? "Could not load the database list — enter a path manually."
      : dbQuery.isLoading
        ? "Loading databases…"
        : readyDbs.length === 0
          ? "No ready databases found. Prepare one from the dashboard, or enter a path."
          : `${readyDbs.length} ready database${readyDbs.length === 1 ? "" : "s"} available.`;

  const handleDbSelect = (value: string) => {
    if (value === DB_CUSTOM_VALUE) {
      setDbManual(true);
      return;
    }
    setDbManual(false);
    update("db", value);
  };

  const renderField = (field: FieldDescriptor) => (
    <label key={field.key} className="terminal-cockpit__cfg-field">
      <span className="terminal-cockpit__cfg-label">{field.label}</span>
      {field.kind === "select" ? (
        <select
          className="terminal-cockpit__cfg-input"
          value={fields[field.key]}
          onChange={(event) => update(field.key, event.target.value)}
        >
          {field.options?.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      ) : (
        <input
          className="terminal-cockpit__cfg-input"
          type={field.kind === "number" ? "number" : "text"}
          inputMode={field.kind === "number" ? "numeric" : undefined}
          min={field.min}
          value={fields[field.key]}
          placeholder={field.placeholder}
          spellCheck={false}
          onChange={(event) => update(field.key, event.target.value)}
        />
      )}
      {field.hint && <span className="terminal-cockpit__cfg-hint">{field.hint}</span>}
    </label>
  );

  return (
    <section className="terminal-cockpit__panel terminal-cockpit__panel--cfg">
      <div className="terminal-cockpit__panel-title">
        <FileCog size={14} strokeWidth={1.5} />
        Config Builder
      </div>
      <p className="terminal-cockpit__cfg-intro">
        Pick a program and database, then send the generated <code>elb-cfg</code> command to the
        preview. Everything else uses your environment defaults — open <em>Advanced</em> only if you
        need to override them.
      </p>

      <div className="terminal-cockpit__cfg-grid">{ESSENTIAL_FIELDS.slice(0, 1).map(renderField)}</div>

      <label className="terminal-cockpit__cfg-field terminal-cockpit__cfg-field--db">
        <span className="terminal-cockpit__cfg-label">
          <Database size={11} strokeWidth={1.5} /> Database
        </span>
        {showDbSelect && (
          <select
            className="terminal-cockpit__cfg-input"
            value={dbSelectValue}
            disabled={dbQuery.isLoading}
            onChange={(event) => handleDbSelect(event.target.value)}
          >
            <option value="">
              {dbQuery.isLoading ? "Loading databases…" : "Select a database…"}
            </option>
            {readyDbs.map((db) => (
              <option key={db.path} value={db.path}>
                {db.name}
              </option>
            ))}
            <option value={DB_CUSTOM_VALUE}>Enter a custom path…</option>
          </select>
        )}
        {showDbManualInput && (
          <input
            className="terminal-cockpit__cfg-input"
            type="text"
            value={fields.db}
            placeholder="blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA"
            spellCheck={false}
            onChange={(event) => update("db", event.target.value)}
          />
        )}
        <span className="terminal-cockpit__cfg-hint">{dbHint}</span>
      </label>

      <div className="terminal-cockpit__cfg-grid">{ESSENTIAL_FIELDS.slice(1).map(renderField)}</div>

      <button
        type="button"
        className="terminal-cockpit__cfg-advanced-toggle"
        aria-expanded={advancedOpen}
        onClick={() => setAdvancedOpen((open) => !open)}
      >
        {advancedOpen ? (
          <ChevronDown size={13} strokeWidth={1.5} />
        ) : (
          <ChevronRight size={13} strokeWidth={1.5} />
        )}
        Advanced (environment overrides)
      </button>
      {advancedOpen && (
        <div className="terminal-cockpit__cfg-grid">{ADVANCED_FIELDS.map(renderField)}</div>
      )}

      <pre className="terminal-cockpit__cfg-preview" aria-label="Generated elb-cfg command">
        {command}
      </pre>
      <div className="terminal-cockpit__actions">
        <button
          type="button"
          className="glass-button"
          onClick={() => onApply(command)}
          title="Send this command to the Command Preview for review"
        >
          <SendToBack size={13} strokeWidth={1.5} />
          Send to preview
        </button>
        <button
          type="button"
          className="glass-button"
          onClick={() => {
            setFields(ELB_CFG_FORM_DEFAULTS);
            setDbManual(false);
            setAdvancedOpen(false);
          }}
        >
          Reset
        </button>
      </div>
    </section>
  );
}
