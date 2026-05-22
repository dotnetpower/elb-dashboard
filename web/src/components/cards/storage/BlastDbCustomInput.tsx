import { useState } from "react";
import { Download } from "lucide-react";

interface BlastDbCustomInputProps {
  disabled: boolean;
  onDownload: (name: string) => void;
}

// Mirrors api/services/ncbi_catalogue.py::RE_DB_NAME and the backend regex
// in api/routes/storage/common.py::_RE_DB_NAME. Validating client-side
// removes the 400 round-trip and gives an explicit error to the user.
const CUSTOM_DB_NAME_RE = /^[A-Za-z0-9_.-]{1,64}$/;

/**
 * Compact "Custom database" input — collapsed by default, expands inline to
 * accept an arbitrary BLAST DB name (e.g. `refseq_rna`).
 */
export function BlastDbCustomInput({ disabled, onDownload }: BlastDbCustomInputProps) {
  const [showCustom, setShowCustom] = useState(false);
  const [customDb, setCustomDb] = useState("");
  const trimmed = customDb.trim();
  const isValid = trimmed.length > 0 && CUSTOM_DB_NAME_RE.test(trimmed);
  const hasInput = customDb.length > 0;

  if (!showCustom) {
    return (
      <button
        className="glass-button"
        style={{ fontSize: 10, padding: "2px 8px" }}
        onClick={() => setShowCustom(true)}
      >
        + Custom database
      </button>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input
          className="glass-input"
          value={customDb}
          onChange={(e) => setCustomDb(e.target.value)}
          placeholder="e.g. refseq_rna"
          style={{
            width: 200,
            fontSize: 11,
            padding: "4px 8px",
            ...(hasInput && !isValid
              ? { borderColor: "var(--danger)" }
              : {}),
          }}
          spellCheck={false}
          aria-invalid={hasInput && !isValid}
        />
        <button
          className="glass-button glass-button--primary"
          style={{ fontSize: 10, padding: "2px 8px" }}
          onClick={() => {
            if (isValid) onDownload(trimmed);
          }}
          disabled={!isValid || disabled}
          title={
            !hasInput
              ? "Type a NCBI BLAST database name"
              : !isValid
                ? "Allowed: letters, digits, _ . - (1-64 chars)"
                : `Download ${trimmed}`
          }
        >
          <Download size={10} /> Get
        </button>
        <button
          className="glass-button"
          style={{ fontSize: 10, padding: "2px 8px" }}
          onClick={() => {
            setShowCustom(false);
            setCustomDb("");
          }}
        >
          Cancel
        </button>
      </div>
      {hasInput && !isValid && (
        <span style={{ fontSize: 10, color: "var(--danger)" }}>
          Only letters, digits and <code>_ . -</code> allowed (1–64 chars).
        </span>
      )}
    </div>
  );
}
