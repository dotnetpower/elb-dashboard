import { AlertTriangle, ArrowRight, CheckCircle2, Dna, Upload, X } from "lucide-react";

import { MAX_UPLOAD_BYTES } from "@/constants";
import { EXAMPLE_FASTA } from "@/pages/blastSubmitModel";
import type { QuerySectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader, Tip } from "@/pages/blastSubmit/ui";

export function QuerySection({
  form,
  set,
  fileInputRef,
  toast,
  isFasta,
  seqCount,
  charCount,
}: QuerySectionProps) {
  return (
    <section className="glass-card glass-card--strong blast-section">
      <SectionHeader
        step={2}
        icon={<Dna size={16} strokeWidth={1.5} />}
        title="Enter Query Sequence"
        subtitle="Paste FASTA sequence(s) or upload a file"
      />

      <div className="blast-textarea-wrap">
        <textarea
          className="glass-input blast-textarea"
          rows={10}
          value={form.query_data}
          onChange={(event) => set("query_data", event.target.value)}
          placeholder={
            ">sequence_id description\nATCGATCG...\n\nPaste your FASTA sequence here, or click 'Load example' below."
          }
          spellCheck={false}
        />
        {form.query_data && (
          <div className="blast-textarea-stats">
            {isFasta ? (
              <span style={{ color: "var(--success)" }}>
                <CheckCircle2 size={10} /> Valid FASTA
              </span>
            ) : (
              <span style={{ color: "var(--warning)" }}>
                <AlertTriangle size={10} /> Not FASTA format
              </span>
            )}
            <span className="blast-textarea-stats__sep" />
            <span>
              {seqCount} sequence{seqCount !== 1 ? "s" : ""}
            </span>
            <span className="blast-textarea-stats__sep" />
            <span>{charCount.toLocaleString()} characters</span>
          </div>
        )}
      </div>

      <div className="blast-query-actions">
        <label className="glass-button blast-action-btn" style={{ cursor: "pointer" }}>
          <Upload size={13} strokeWidth={1.5} /> Upload file
          <input
            ref={fileInputRef}
            type="file"
            accept=".fa,.fasta,.fna,.faa"
            style={{ display: "none" }}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (!file) return;
              if (file.size > MAX_UPLOAD_BYTES) {
                toast(`File too large. Max ${MAX_UPLOAD_BYTES / 1024 / 1024} MB.`, "error");
                return;
              }
              const reader = new FileReader();
              reader.onload = () => {
                if (typeof reader.result === "string") set("query_data", reader.result);
              };
              reader.readAsText(file);
            }}
          />
        </label>
        <button
          className="glass-button blast-action-btn"
          onClick={() => {
            set("query_data", EXAMPLE_FASTA);
            set("program", "blastn");
            toast("Example loaded — E. coli 16S rRNA (matches 16S_ribosomal_RNA DB)", "info");
          }}
          type="button"
        >
          <Dna size={13} /> Load example
        </button>
        {form.query_data && (
          <button
            className="glass-button blast-action-btn"
            onClick={() => set("query_data", "")}
            type="button"
          >
            <X size={13} strokeWidth={1.5} /> Clear
          </button>
        )}
      </div>

      <div className="blast-subrange-row">
        <span className="glass-label" style={{ fontSize: 11, minWidth: "fit-content", marginBottom: 0 }}>
          Query subrange <Tip text="Restrict search to a range of the query (1-based)." />
        </span>
        <input
          className="glass-input blast-small-input"
          value={form.query_from}
          onChange={(event) => set("query_from", event.target.value)}
          placeholder="From"
          type="number"
          min={1}
        />
        <ArrowRight size={12} style={{ color: "var(--text-faint)" }} />
        <input
          className="glass-input blast-small-input"
          value={form.query_to}
          onChange={(event) => set("query_to", event.target.value)}
          placeholder="To"
          type="number"
          min={1}
        />
      </div>

      <label style={{ marginTop: 12, display: "block" }}>
        <span className="glass-label">Job Title</span>
        <input
          className="glass-input"
          value={form.job_title}
          onChange={(event) => set("job_title", event.target.value)}
          placeholder="Enter a descriptive title for your BLAST search"
          maxLength={200}
        />
      </label>
    </section>
  );
}
