import {
  AlertTriangle,
  CheckCircle2,
  FileText,
  Upload,
} from "lucide-react";

import { ExamplePicker } from "@/components/ExamplePicker";
import { CUSTOM_DB_EXAMPLES, type CustomDbExampleValues } from "@/data/labToolExamples";

import { SectionHeader } from "./SectionHeader";
import { MAX_INLINE_BYTES, formatBytes } from "./formatBytes";
import type { DatabaseBuilderState } from "./useDatabaseBuilderState";

export interface FastaInputSectionProps {
  fastaData: DatabaseBuilderState["fastaData"];
  setFastaData: DatabaseBuilderState["setFastaData"];
  inputMode: DatabaseBuilderState["inputMode"];
  setInputMode: DatabaseBuilderState["setInputMode"];
  fileName: DatabaseBuilderState["fileName"];
  setFileName: DatabaseBuilderState["setFileName"];
  fastaStats: DatabaseBuilderState["fastaStats"];
  handleFileUpload: DatabaseBuilderState["handleFileUpload"];
  setDbName: DatabaseBuilderState["setDbName"];
  setDbType: DatabaseBuilderState["setDbType"];
  setTitle: DatabaseBuilderState["setTitle"];
}

export function FastaInputSection({
  fastaData,
  setFastaData,
  inputMode,
  setInputMode,
  fileName,
  setFileName,
  fastaStats,
  handleFileUpload,
  setDbName,
  setDbType,
  setTitle,
}: FastaInputSectionProps) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={2}
        icon={<FileText size={16} strokeWidth={1.5} />}
        title="FASTA Input"
        subtitle="Paste sequences or upload a FASTA file (≤ 50 MB inline)"
      />

      <ExamplePicker<CustomDbExampleValues>
        examples={CUSTOM_DB_EXAMPLES}
        label="Load an example database"
        onSelect={(v) => {
          setDbName(v.dbName);
          setDbType(v.dbType);
          setTitle(v.title);
          setFastaData(v.fastaData);
        }}
      />

      <div className="blast-program-tabs db-input-mode">
        <button
          type="button"
          onClick={() => setInputMode("paste")}
          className={`blast-program-tab${inputMode === "paste" ? " blast-program-tab--active" : ""}`}
        >
          <span className="blast-program-tab__name">Paste sequence</span>
          <span className="blast-program-tab__desc">Quick prototyping</span>
        </button>
        <button
          type="button"
          onClick={() => setInputMode("file")}
          className={`blast-program-tab${inputMode === "file" ? " blast-program-tab--active" : ""}`}
        >
          <span className="blast-program-tab__name">
            <Upload size={12} style={{ verticalAlign: "-1px", marginRight: 4 }} />
            Upload file
          </span>
          <span className="blast-program-tab__desc">.fa .fasta .fna .faa</span>
        </button>
      </div>

      {inputMode === "paste" ? (
        <div className="blast-textarea-wrap" style={{ marginTop: 12 }}>
          <textarea
            className="form-input blast-textarea"
            rows={12}
            placeholder=">sequence_id Description&#10;ATGCGATCGA..."
            value={fastaData}
            onChange={(e) => setFastaData(e.target.value)}
            style={{ width: "100%" }}
          />
          <div className="blast-textarea-stats">
            {fastaStats.seqCount > 0 ? (
              <>
                {fastaStats.isValid ? (
                  <CheckCircle2 size={12} style={{ color: "var(--success)" }} />
                ) : (
                  <AlertTriangle size={12} style={{ color: "var(--danger)" }} />
                )}
                <span>
                  {fastaStats.seqCount} sequence
                  {fastaStats.seqCount !== 1 ? "s" : ""}
                </span>
                <span>·</span>
                <span>{fastaStats.totalBases.toLocaleString()} residues</span>
                <span>·</span>
                <span>{(fastaData.length / 1024).toFixed(1)} KB</span>
              </>
            ) : (
              <span>Paste a FASTA-formatted sequence to begin.</span>
            )}
            <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
              {fastaData && (
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() => {
                    setFastaData("");
                    setFileName("");
                  }}
                  style={{ color: "var(--danger)" }}
                >
                  Clear
                </button>
              )}
            </span>
          </div>
        </div>
      ) : (
        <label
          htmlFor="fasta-file"
          className="empty-state"
          style={{
            marginTop: 12,
            borderRadius: 12,
            border: "2px dashed var(--border-medium)",
            cursor: "pointer",
            minHeight: 160,
          }}
        >
          <div className="empty-state__icon">
            <Upload size={24} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">Drop a FASTA file here</div>
          <div className="empty-state__desc">
            Accepted: .fa, .fasta, .fna, .faa, .txt — up to{" "}
            {formatBytes(MAX_INLINE_BYTES)}
          </div>
          <input
            id="fasta-file"
            type="file"
            accept=".fa,.fasta,.fna,.faa,.txt"
            onChange={handleFileUpload}
            style={{ display: "none" }}
          />
          {fileName && (
            <div
              style={{
                marginTop: 12,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
              }}
            >
              <FileText size={13} style={{ color: "var(--accent)" }} />
              <code className="code-val">{fileName}</code>
              <span className="muted">
                · {(fastaData.length / 1024).toFixed(1)} KB · {fastaStats.seqCount}{" "}
                sequence
                {fastaStats.seqCount !== 1 ? "s" : ""}
              </span>
            </div>
          )}
        </label>
      )}
    </section>
  );
}
