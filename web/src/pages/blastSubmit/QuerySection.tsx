import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Copy,
  Dna,
  FileText,
  RefreshCw,
  Search,
  Upload,
  X,
} from "lucide-react";

import { MAX_UPLOAD_BYTES } from "@/constants";
import {
  baseComposition,
  deduplicateFasta,
  hasAmbiguousBases,
  looksLikeNucleotide,
  parseFasta,
  primerDiagnostics,
  reverseComplementFasta,
} from "@/pages/blastSubmit/fastaUtils";
import {
  QUERY_EXAMPLE_TEMPLATES,
  queryExamplesForDatabase,
  type QueryExampleTemplate,
} from "@/pages/blastSubmit/queryExamples";
import { SequenceBuilderDialog } from "@/pages/blastSubmit/SequenceBuilderDialog";
import { getDbBaseName } from "@/pages/blastSubmit/helpers";
import { buildGeneratedJobTitle } from "@/pages/blastSubmitModel";
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
  const [exampleModalOpen, setExampleModalOpen] = useState(false);
  const [sequenceBuilderOpen, setSequenceBuilderOpen] = useState(false);
  const [dragActive, setDragActive] = useState(false);

  // Remembers the last title we auto-generated from an example so switching
  // examples can refresh it, while a title the researcher typed by hand (which
  // will not match this ref) is preserved untouched.
  const lastAutoTitleRef = useRef<string | null>(null);

  // Sequence-level diagnostics. Computed off the concatenation of every
  // parsed record so the GC% / IUPAC warning describe the whole query
  // rather than the first record only. blastn-only — protein sequences
  // are detected and skipped automatically by `looksLikeNucleotide`.
  const diagnostics = useMemo(() => {
    if (!form.query_data.trim()) return null;
    const records = parseFasta(form.query_data);
    const concat = records.map((r) => r.sequence).join("");
    if (!concat) return null;
    const nucleotide = looksLikeNucleotide(concat);
    if (!nucleotide) return { nucleotide: false } as const;
    const stats = baseComposition(concat);
    return {
      nucleotide: true,
      gc: stats.gc,
      ambiguous: stats.ambiguous,
      hasIupac: hasAmbiguousBases(concat),
    } as const;
  }, [form.query_data]);

  // tblastn uses a *protein* query against a translated nucleotide DB,
  // so GC% / reverse-complement make no sense for it. blastx uses a
  // nucleotide query translated into protein — the query IS nucleotide.
  const isNucleotideProgram =
    form.program === "blastn" || form.program === "blastx" || form.program === "tblastx";

  // Per-record primer-design diagnostics. Only activated for short
  // nucleotide oligos (≤ 50 nt) because Tm/hairpin/self-dimer are
  // primer/probe concepts — they would be meaningless noise for a
  // multi-kb genomic query. Skips records that fail nucleotide sanity.
  const primerFindings = useMemo(() => {
    if (!isNucleotideProgram) return [];
    if (!form.query_data.trim()) return [];
    const records = parseFasta(form.query_data);
    const out: Array<{
      id: string;
      length: number;
      diagnostics: NonNullable<ReturnType<typeof primerDiagnostics>>;
    }> = [];
    for (const record of records) {
      if (record.sequence.length === 0 || record.sequence.length > 50) continue;
      const d = primerDiagnostics(record.sequence);
      if (!d) continue;
      // FASTA header is the raw ">name description" line; take the first
      // whitespace-delimited token as the display id.
      const id = record.header.trim().split(/\s+/)[0] || "(unnamed)";
      out.push({ id, length: record.sequence.length, diagnostics: d });
    }
    return out;
  }, [form.query_data, isNucleotideProgram]);

  const loadFromText = (text: string) => {
    if (text.length === 0) return;
    set("query_data", text);
  };

  const handleFile = (file: File) => {
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(`File too large. Max ${MAX_UPLOAD_BYTES / 1024 / 1024} MB.`, "error");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        loadFromText(reader.result);
        toast(`Loaded ${file.name}`, "success");
      }
    };
    reader.readAsText(file);
  };

  const handleReverseComplement = () => {
    if (!form.query_data.trim()) return;
    if (!isNucleotideProgram) {
      toast(
        "Reverse complement only applies to nucleotide programs (blastn / tblastn / tblastx).",
        "info",
      );
      return;
    }
    const flipped = reverseComplementFasta(form.query_data);
    set("query_data", flipped);
    toast("Reverse-complemented all sequences.", "success");
  };

  const handleDeduplicate = () => {
    if (!form.query_data.trim()) return;
    const { text, removed, kept } = deduplicateFasta(form.query_data);
    if (removed === 0) {
      toast(`No duplicates — ${kept} unique sequences.`, "info");
      return;
    }
    set("query_data", text);
    toast(
      `Removed ${removed} duplicate sequence${removed === 1 ? "" : "s"}; ${kept} unique kept.`,
      "success",
    );
  };

  const loadExample = (example: QueryExampleTemplate) => {
    set("query_data", example.fasta);
    set("program", example.blastProgram);
    set("query_from", "");
    set("query_to", "");
    // Refresh the auto title when it is empty or still holds the title we
    // generated for a previously picked example. A manually edited title
    // (which differs from `lastAutoTitleRef`) is left as the researcher set it.
    const current = form.job_title.trim();
    if (!current || current === lastAutoTitleRef.current) {
      const nextTitle = buildGeneratedJobTitle(example.label);
      set("job_title", nextTitle);
      lastAutoTitleRef.current = nextTitle;
    }
    setExampleModalOpen(false);
  };

  // Insert a FASTA fetched from NCBI via the "Generate query" modal. Mirrors
  // `loadExample`'s title refresh: a label is derived from the FASTA header so
  // an untouched auto title stays in sync, while a hand-edited title is kept.
  const handleGeneratedInsert = (fasta: string) => {
    set("query_data", fasta);
    set("program", "blastn");
    set("query_from", "");
    set("query_to", "");
    const current = form.job_title.trim();
    if (!current || current === lastAutoTitleRef.current) {
      const header = (fasta.split("\n")[0] || "").replace(/^>/, "").trim();
      const label = (header.split(",")[0] || "NCBI query").slice(0, 60);
      const nextTitle = buildGeneratedJobTitle(label);
      set("job_title", nextTitle);
      lastAutoTitleRef.current = nextTitle;
    }
  };

  // Step 2 (Search set) must precede step 3 (Query). When no database is
  // selected we disable every input on this section and surface a one-line
  // notice instead. This guarantees
  // the example picker can always render hits scoped to a real DB.
  const selectedDbName = getDbBaseName(form.db);
  const dbSelected = Boolean(selectedDbName);

  return (
    <section className="glass-card glass-card--strong blast-section bsl-input">
      <SectionHeader
        step={3}
        icon={<Dna size={16} strokeWidth={1.5} />}
        title="Enter Query Sequence"
        subtitle="Paste FASTA sequence(s) or upload a file"
      />

      {!dbSelected && (
        <div
          className="glass-card"
          role="status"
          style={{
            padding: "10px 12px",
            marginBottom: 12,
            display: "flex",
            alignItems: "center",
            gap: 8,
            background: "rgba(255,255,255,0.04)",
            color: "var(--text-muted)",
            fontSize: 12,
          }}
        >
          <AlertTriangle
            size={13}
            strokeWidth={1.5}
            style={{ color: "var(--warning)" }}
          />
          <span>
            Pick a database in <strong>Step 2 · Search set</strong> first. Examples are
            filtered to the DB you choose.
          </span>
        </div>
      )}

      <div
        className="blast-accession-row"
        aria-disabled={!dbSelected}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
          opacity: dbSelected ? 1 : 0.55,
        }}
      >
        <span
          className="glass-label"
          style={{ fontSize: 11, minWidth: "fit-content", marginBottom: 0 }}
        >
          Or fetch by NCBI accession
          <Tip text="Submit the search using an NCBI nuccore accession (e.g. NM_000546.6). The backend fetches the FASTA via E-utilities at submit time. Inline FASTA above takes precedence when both are filled." />
        </span>
        <input
          className="glass-input blast-small-input"
          value={form.query_accession}
          onChange={(event) => set("query_accession", event.target.value)}
          placeholder="NM_000546.6"
          maxLength={64}
          disabled={!dbSelected}
          spellCheck={false}
          style={{ fontFamily: "var(--font-mono, monospace)", minWidth: 180 }}
        />
        {form.query_accession.trim() && (
          <span
            style={{
              fontSize: 11,
              color: form.query_data.trim() ? "var(--text-faint)" : "var(--success)",
            }}
          >
            {form.query_data.trim()
              ? "FASTA above will be used"
              : "Will fetch at submit"}
          </span>
        )}
      </div>

      <div
        className={`blast-textarea-wrap${dragActive ? " blast-textarea-wrap--drag" : ""}`}
        aria-disabled={!dbSelected}
        style={!dbSelected ? { opacity: 0.55, pointerEvents: "none" } : undefined}
        onDragEnter={(event) => {
          if (!dbSelected) return;
          event.preventDefault();
          if (event.dataTransfer.types.includes("Files")) setDragActive(true);
        }}
        onDragOver={(event) => {
          if (!dbSelected) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
          setDragActive(false);
        }}
        onDrop={(event) => {
          if (!dbSelected) return;
          event.preventDefault();
          setDragActive(false);
          const file = event.dataTransfer.files?.[0];
          if (file) handleFile(file);
        }}
      >
        <textarea
          className="glass-input blast-textarea"
          rows={10}
          value={form.query_data}
          onChange={(event) => set("query_data", event.target.value)}
          disabled={!dbSelected}
          placeholder={
            dbSelected
              ? ">sequence_id description\nATCGATCG...\n\nPaste FASTA, drop a .fasta file, or click 'Load example' below."
              : "Select a database in Step 2 first, then paste FASTA here."
          }
          spellCheck={false}
        />
        {dragActive && (
          <div className="blast-textarea-drop">
            <Upload size={18} strokeWidth={1.5} />
            <span>Drop FASTA file to load</span>
          </div>
        )}
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
            {diagnostics?.nucleotide && (
              <>
                <span className="blast-textarea-stats__sep" />
                <span title="Total GC% across all parsed nucleotide sequences.">
                  GC {diagnostics.gc.toFixed(1)}%
                </span>
                {diagnostics.hasIupac && (
                  <>
                    <span className="blast-textarea-stats__sep" />
                    <span
                      style={{ color: "var(--warning)" }}
                      title="Contains IUPAC ambiguity codes (R, Y, S, W, K, M, B, D, H, V). Consider disabling DUST or relaxing filtering."
                    >
                      <AlertTriangle size={10} /> IUPAC codes
                    </span>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {primerFindings.length > 0 && <PrimerDiagnosticsPanel findings={primerFindings} />}

      <div className="blast-query-actions">
        <label
          className="glass-button blast-action-btn blast-action-btn--upload"
          style={{
            cursor: dbSelected ? "pointer" : "not-allowed",
            opacity: dbSelected ? 1 : 0.5,
          }}
          aria-disabled={!dbSelected}
          title={dbSelected ? undefined : "Select a database in Step 2 first."}
        >
          <Upload size={13} strokeWidth={1.5} /> Upload file
          <input
            ref={fileInputRef}
            type="file"
            accept=".fa,.fasta,.fna,.faa"
            disabled={!dbSelected}
            style={{ display: "none" }}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (!file) return;
              handleFile(file);
            }}
          />
        </label>
        <button
          className="glass-button blast-action-btn blast-action-btn--example"
          onClick={() => setExampleModalOpen(true)}
          type="button"
          disabled={!dbSelected}
          title={dbSelected ? undefined : "Select a database in Step 2 first."}
        >
          <Dna size={13} /> Load example
        </button>
        <button
          className="glass-button blast-action-btn blast-action-btn--example"
          onClick={() => setSequenceBuilderOpen(true)}
          type="button"
          disabled={!dbSelected}
          title={
            dbSelected
              ? "Search NCBI by organism/gene/accession and fetch a query sequence."
              : "Select a database in Step 2 first."
          }
        >
          <Search size={13} strokeWidth={1.5} /> Generate query
        </button>
        {form.query_data && isNucleotideProgram && (
          <button
            className="glass-button blast-action-btn blast-action-btn--transform"
            onClick={handleReverseComplement}
            type="button"
            disabled={!dbSelected}
            title="Replace each sequence with its reverse complement (5'→3' becomes 3'→5'). Useful for primer-pair sanity checks."
          >
            <RefreshCw size={13} strokeWidth={1.5} /> Reverse complement
          </button>
        )}
        {form.query_data && seqCount > 1 && (
          <button
            className="glass-button blast-action-btn blast-action-btn--dedupe"
            onClick={handleDeduplicate}
            type="button"
            disabled={!dbSelected}
            title="Remove sequences that are exact duplicates of an earlier record."
          >
            <Copy size={13} strokeWidth={1.5} /> Deduplicate
          </button>
        )}
        {form.query_data && (
          <button
            className="glass-button blast-action-btn blast-action-btn--clear"
            onClick={() => set("query_data", "")}
            type="button"
            disabled={!dbSelected}
          >
            <X size={13} strokeWidth={1.5} /> Clear
          </button>
        )}
      </div>

      <div
        className="blast-subrange-row"
        aria-disabled={!dbSelected}
        style={!dbSelected ? { opacity: 0.55 } : undefined}
      >
        <span
          className="glass-label"
          style={{ fontSize: 11, minWidth: "fit-content", marginBottom: 0 }}
        >
          Query subrange <Tip text="Restrict search to a range of the query (1-based)." />
        </span>
        <input
          className="glass-input blast-small-input"
          value={form.query_from}
          onChange={(event) => set("query_from", event.target.value)}
          placeholder="From"
          type="number"
          min={1}
          disabled={!dbSelected}
        />
        <ArrowRight size={12} style={{ color: "var(--text-faint)" }} />
        <input
          className="glass-input blast-small-input"
          value={form.query_to}
          onChange={(event) => set("query_to", event.target.value)}
          placeholder="To"
          type="number"
          min={1}
          disabled={!dbSelected}
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

      {exampleModalOpen && (
        <QueryExampleDialog
          examples={QUERY_EXAMPLE_TEMPLATES}
          selectedDbName={selectedDbName}
          onClose={() => setExampleModalOpen(false)}
          onSelect={loadExample}
        />
      )}
      {sequenceBuilderOpen && (
        <SequenceBuilderDialog
          onClose={() => setSequenceBuilderOpen(false)}
          onInsert={handleGeneratedInsert}
          toast={toast}
        />
      )}
    </section>
  );
}

function QueryExampleDialog({
  examples,
  selectedDbName,
  onClose,
  onSelect,
}: {
  examples: QueryExampleTemplate[];
  selectedDbName: string;
  onClose: () => void;
  onSelect: (example: QueryExampleTemplate) => void;
}) {
  const visible = queryExamplesForDatabase(examples, selectedDbName);

  // Escape-to-close, matching the app's other dialogs (backdrop click already
  // closes; this adds keyboard parity).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="glass-dialog-backdrop" onClick={onClose}>
      <div
        className="glass-card glass-card--strong glass-dialog query-example-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="query-example-dialog-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="query-example-dialog__header">
          <div>
            <div className="glass-badge glass-badge--accent">FASTA templates</div>
            <h3 id="query-example-dialog-title">Load Query Example</h3>
            <div
              style={{
                marginTop: 4,
                fontSize: 11,
                color: "var(--text-muted)",
              }}
            >
              {visible.length > 0
                ? `Showing examples that hit ${selectedDbName} (${visible.length}/${examples.length}).`
                : `No curated examples match ${selectedDbName}.`}
            </div>
          </div>
          <button
            className="glass-button"
            type="button"
            onClick={onClose}
            aria-label="Close examples"
          >
            <X size={14} strokeWidth={1.5} />
          </button>
        </div>
        {visible.length === 0 ? (
          <div
            className="glass-card"
            role="status"
            style={{
              padding: 14,
              background: "rgba(255,255,255,0.04)",
              color: "var(--text-muted)",
              fontSize: 12,
            }}
          >
            This database does not have a curated query example yet.
          </div>
        ) : (
          <div className="query-example-grid">
            {visible.map((example) => (
              <button
                key={example.id}
                type="button"
                className="query-example-card"
                onClick={() => onSelect(example)}
              >
                <div className="query-example-card__topline">
                  <span>{example.group}</span>
                  <span>
                    {example.length.toLocaleString()}
                    {example.blastProgram === "blastp" ? " aa" : " bp"}
                  </span>
                </div>
                <div className="query-example-card__title">
                  <FileText size={13} strokeWidth={1.5} />
                  {example.label}
                </div>
                <p>{example.description}</p>
                <div className="query-example-card__meta">
                  <span>{example.sequenceCount} sequence</span>
                  <span>{example.blastProgram}</span>
                  {example.recommendedDb && <span>{example.recommendedDb}</span>}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Tm-color rule of thumb used by the molecular-diagnostics community:
// PCR primers/probes are happiest in 55–65 °C; outside that band the
// assay needs special handling (long extension, touchdown PCR, etc.).
function tmColour(tm: number | null): string {
  if (tm === null) return "var(--text-muted)";
  if (tm < 50 || tm > 70) return "var(--danger)";
  if (tm < 55 || tm > 65) return "var(--warning)";
  return "var(--success)";
}

function PrimerDiagnosticsPanel({
  findings,
}: {
  findings: Array<{
    id: string;
    length: number;
    diagnostics: import("@/pages/blastSubmit/fastaUtils").PrimerDiagnostics;
  }>;
}) {
  return (
    <div
      className="glass-card"
      style={{
        padding: 12,
        marginTop: 8,
        marginBottom: 12,
        background: "rgba(255,255,255,0.04)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
          fontSize: 12,
          color: "var(--text-muted)",
        }}
      >
        <Dna size={13} strokeWidth={1.5} />
        <span style={{ fontWeight: 600 }}>Primer / probe diagnostics</span>
        <span title="Only sequences ≤ 50 nt are scanned. Tm uses the Wallace rule for ≤ 13 nt and the salt-adjusted GC formula otherwise.">
          (≤ 50 nt only)
        </span>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {findings.map((f) => {
          const { tm, gc, gcRun, hairpinLength, selfDimerLength } = f.diagnostics;
          const hairpinWarn = hairpinLength >= 4;
          const dimerWarn = selfDimerLength >= 4;
          const gcRunWarn = gcRun >= 4; // > 3 G/C run = high mispriming risk
          return (
            <div
              key={`${f.id}-${f.length}`}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                fontSize: 12,
                color: "var(--text-primary)",
                flexWrap: "wrap",
              }}
            >
              <span
                style={{
                  minWidth: 120,
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-muted)",
                }}
              >
                {f.id} ({f.length} nt)
              </span>
              <span
                style={{ color: tmColour(tm), fontVariantNumeric: "tabular-nums" }}
                title="Estimated melting temperature."
              >
                Tm {tm === null ? "—" : `${tm.toFixed(1)} °C`}
              </span>
              <span style={{ color: "var(--text-muted)" }} title="GC content.">
                GC {gc.toFixed(0)}%
              </span>
              <span
                style={{ color: gcRunWarn ? "var(--warning)" : "var(--text-muted)" }}
                title="Longest consecutive G/C run. Runs ≥ 4 raise mispriming risk."
              >
                GC-run {gcRun}
              </span>
              {hairpinWarn && (
                <span
                  style={{ color: "var(--warning)" }}
                  title="Potential hairpin / self-complementary stem detected."
                >
                  <AlertTriangle size={11} /> hairpin stem {hairpinLength}
                </span>
              )}
              {dimerWarn && (
                <span
                  style={{ color: "var(--warning)" }}
                  title="Potential self-dimer (3′-complementary overlap) detected."
                >
                  <AlertTriangle size={11} /> self-dimer {selfDimerLength}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
