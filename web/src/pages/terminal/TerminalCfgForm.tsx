import { useState } from "react";
import { FileCog, SendToBack } from "lucide-react";

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
// the form easy to extend without growing the JSX.
const FIELDS: Array<{
  key: keyof ElbCfgFormFields;
  label: string;
  placeholder: string;
  hint?: string;
}> = [
  { key: "program", label: "Program", placeholder: "blastn" },
  { key: "db", label: "Database", placeholder: "blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA" },
  { key: "queries", label: "Queries", placeholder: "my-query.fa", hint: "Bare name expands under the queries container." },
  { key: "results", label: "Results", placeholder: "results/run-001", hint: "Bare name expands under the results container." },
  { key: "machineType", label: "Machine type", placeholder: "(platform default)" },
  { key: "numNodes", label: "Nodes", placeholder: "1" },
  { key: "region", label: "Region", placeholder: "(from environment)" },
  { key: "resourceGroup", label: "Resource group", placeholder: "(from environment)" },
  { key: "storageAccount", label: "Storage account", placeholder: "(from environment)" },
  { key: "acrName", label: "ACR name", placeholder: "(from environment)" },
  { key: "output", label: "Output path", placeholder: "~/elastic-blast.ini" },
];

export function TerminalCfgForm({ onApply }: TerminalCfgFormProps) {
  const [fields, setFields] = useState<ElbCfgFormFields>(ELB_CFG_FORM_DEFAULTS);
  const command = buildElbCfgCommand(fields);

  const update = (key: keyof ElbCfgFormFields, value: string) =>
    setFields((prev) => ({ ...prev, [key]: value }));

  return (
    <section className="terminal-cockpit__panel terminal-cockpit__panel--cfg">
      <div className="terminal-cockpit__panel-title">
        <FileCog size={14} strokeWidth={1.5} />
        Config Builder
      </div>
      <p className="terminal-cockpit__cfg-intro">
        Compose an <code>elastic-blast.ini</code> without hand-editing. Empty fields fall back to the
        platform environment defaults. The generated command runs the terminal&apos;s <code>elb-cfg</code>
        helper, which is the single source of truth for the config layout.
      </p>
      <div className="terminal-cockpit__cfg-grid">
        {FIELDS.map((field) => (
          <label key={field.key} className="terminal-cockpit__cfg-field">
            <span className="terminal-cockpit__cfg-label">{field.label}</span>
            <input
              className="terminal-cockpit__cfg-input"
              type="text"
              value={fields[field.key]}
              placeholder={field.placeholder}
              spellCheck={false}
              onChange={(event) => update(field.key, event.target.value)}
            />
            {field.hint && <span className="terminal-cockpit__cfg-hint">{field.hint}</span>}
          </label>
        ))}
      </div>
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
          onClick={() => setFields(ELB_CFG_FORM_DEFAULTS)}
        >
          Reset
        </button>
      </div>
    </section>
  );
}
