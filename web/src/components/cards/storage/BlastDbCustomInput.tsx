import { useState } from "react";
import { Download } from "lucide-react";

interface BlastDbCustomInputProps {
  disabled: boolean;
  onDownload: (name: string) => void;
}

/**
 * Compact "Custom database" input — collapsed by default, expands inline to
 * accept an arbitrary BLAST DB name (e.g. `refseq_rna`).
 */
export function BlastDbCustomInput({ disabled, onDownload }: BlastDbCustomInputProps) {
  const [showCustom, setShowCustom] = useState(false);
  const [customDb, setCustomDb] = useState("");

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
    <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
      <input
        className="glass-input"
        value={customDb}
        onChange={(e) => setCustomDb(e.target.value)}
        placeholder="e.g. refseq_rna"
        style={{ width: 160, fontSize: 11, padding: "4px 8px" }}
        spellCheck={false}
      />
      <button
        className="glass-button glass-button--primary"
        style={{ fontSize: 10, padding: "2px 8px" }}
        onClick={() => {
          if (customDb) onDownload(customDb);
        }}
        disabled={!customDb || disabled}
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
  );
}
