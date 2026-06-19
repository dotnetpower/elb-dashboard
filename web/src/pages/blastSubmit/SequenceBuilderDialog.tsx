/**
 * SequenceBuilderDialog — the New Search "Generate query" modal.
 *
 * Mirrors the NCBI BLAST web form's accession-first workflow: a researcher
 * either pastes an accession/gi directly or searches db=nuccore by
 * organism/keyword to find one, optionally picks a gene feature (or types a
 * sub-range + strand), and inserts the fetched FASTA into the query textarea.
 * All NCBI traffic is proxied through the api sidecar (see api/routes/ncbi.py);
 * the browser never talks to NCBI directly.
 */
import { useState } from "react";
import { ArrowRight, Dna, Loader2, Search, X } from "lucide-react";

import {
  getNuccoreFasta,
  getNuccoreFeatures,
  searchNuccore,
  type NuccoreGeneFeature,
  type NuccoreSearchResult,
} from "@/api/ncbi";

type Strand = "plus" | "minus";

export function errorMessage(err: unknown): string {
  // The api error carries the backend JSON body on `.body` (see api/client.ts).
  // Prefer its `message` (e.g. "This record has too many features…") over the
  // generic "HTTP 422" the client falls back to when no `error` field exists.
  if (err && typeof err === "object" && "body" in err) {
    const body = (err as { body?: unknown }).body;
    if (body && typeof body === "object") {
      const msg =
        (body as { message?: unknown }).message ??
        (body as { detail?: { message?: unknown } }).detail?.message;
      if (typeof msg === "string" && msg.trim()) return msg;
    }
  }
  if (err instanceof Error && err.message) return err.message;
  return "NCBI request failed. Please try again.";
}

/**
 * Resolve the `from`/`to`/`strand` inputs into the `seq_start`/`seq_stop` the
 * NCBI fetch expects. NCBI reverse-complements when `seq_start > seq_stop`, so
 * the minus strand sends the high coordinate first. Returns an `error` string
 * for the caller to toast, or the (possibly empty) coordinate pair.
 */
export function buildSubrange(
  from: string,
  to: string,
  strand: Strand,
): { seqStart?: number; seqStop?: number; error?: string } {
  const f = from.trim();
  const t = to.trim();
  if (!f && !t) return {};
  if (!f || !t) return { error: "Provide both From and To, or leave both empty." };
  const fromNum = Number(f);
  const toNum = Number(t);
  if (
    !Number.isInteger(fromNum) ||
    !Number.isInteger(toNum) ||
    fromNum < 1 ||
    toNum < 1
  ) {
    return { error: "From / To must be positive integers." };
  }
  // Normalise to low..high so the strand toggle is the single source of
  // direction — a researcher typing the bounds in either order still gets the
  // strand they picked (NCBI reverse-complements when seq_start > seq_stop).
  const lo = Math.min(fromNum, toNum);
  const hi = Math.max(fromNum, toNum);
  return strand === "minus"
    ? { seqStart: hi, seqStop: lo }
    : { seqStart: lo, seqStop: hi };
}

/** The FASTA header the inserted sequence will carry (mirrors NCBI's
 * `:cSTOP-START` for the minus strand). Pure so it can be unit-tested. */
export function previewFastaHeader(
  accession: string,
  from: string,
  to: string,
  strand: Strand,
): string {
  const acc = accession.trim();
  if (!acc) return "";
  const f = from.trim();
  const t = to.trim();
  if (!f && !t) return `>${acc} (whole sequence)`;
  if (!f || !t) return `>${acc} (enter both From and To)`;
  // Mirror buildSubrange's low..high normalisation so the preview matches the
  // sequence that will actually be fetched, whatever order the bounds were typed.
  const lo = Math.min(Number(f), Number(t));
  const hi = Math.max(Number(f), Number(t));
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return `>${acc}`;
  return strand === "minus" ? `>${acc}:c${hi}-${lo}` : `>${acc}:${lo}-${hi}`;
}

export function SequenceBuilderDialog({
  onClose,
  onInsert,
  toast,
}: {
  onClose: () => void;
  onInsert: (fasta: string) => void;
  toast: (message: string, kind: "success" | "error" | "info") => void;
}) {
  const [term, setTerm] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<NuccoreSearchResult[] | null>(null);

  const [accession, setAccession] = useState("");
  const [features, setFeatures] = useState<NuccoreGeneFeature[] | null>(null);
  const [featuresLoading, setFeaturesLoading] = useState(false);
  const [featureFilter, setFeatureFilter] = useState("");

  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [strand, setStrand] = useState<Strand>("plus");
  const [inserting, setInserting] = useState(false);

  const runSearch = async () => {
    const q = term.trim();
    if (!q) return;
    setSearching(true);
    setResults(null);
    try {
      const res = await searchNuccore(q, 12);
      setResults(res.results);
      if (res.results.length === 0) {
        toast("No NCBI records matched that search.", "info");
      }
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setSearching(false);
    }
  };

  const selectAccession = (acc: string) => {
    setAccession(acc);
    setFeatures(null);
    setFeatureFilter("");
    setFrom("");
    setTo("");
    setStrand("plus");
  };

  const loadFeatures = async () => {
    const acc = accession.trim();
    if (!acc) return;
    setFeaturesLoading(true);
    setFeatures(null);
    try {
      const res = await getNuccoreFeatures(acc);
      setFeatures(res.features);
      if (res.features.length === 0) {
        toast("No gene features found for this record.", "info");
      }
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setFeaturesLoading(false);
    }
  };

  const pickFeature = (f: NuccoreGeneFeature) => {
    setFrom(String(f.start));
    setTo(String(f.stop));
    setStrand(f.strand);
  };

  // Header the inserted FASTA will carry (mirrors NCBI's `:cSTOP-START` for the
  // minus strand). Sequence itself is fetched on insert.
  const previewHeader = (): string =>
    previewFastaHeader(accession, from, to, strand);

  const doInsert = async () => {
    const acc = accession.trim();
    if (!acc) {
      toast("Enter or select an accession first.", "error");
      return;
    }
    const range = buildSubrange(from, to, strand);
    if (range.error) {
      toast(range.error, "error");
      return;
    }
    setInserting(true);
    try {
      const fasta = await getNuccoreFasta(acc, {
        seqStart: range.seqStart,
        seqStop: range.seqStop,
      });
      onInsert(fasta);
      toast(`Inserted ${acc} query sequence.`, "success");
      onClose();
    } catch (err) {
      toast(errorMessage(err), "error");
    } finally {
      setInserting(false);
    }
  };

  const visibleFeatures = (features ?? []).filter((f) => {
    const needle = featureFilter.trim().toLowerCase();
    if (!needle) return true;
    return (
      (f.name ?? "").toLowerCase().includes(needle) ||
      (f.product ?? "").toLowerCase().includes(needle) ||
      (f.locus_tag ?? "").toLowerCase().includes(needle)
    );
  });

  return (
    <div className="glass-dialog-backdrop" onClick={onClose}>
      <div
        className="glass-card glass-card--strong glass-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sequence-builder-title"
        style={{ maxWidth: 640, width: "92%" }}
        onClick={(event) => event.stopPropagation()}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            marginBottom: 12,
          }}
        >
          <div>
            <div className="glass-badge">NCBI</div>
            <h3 id="sequence-builder-title" style={{ margin: "4px 0 0" }}>
              Generate query from NCBI
            </h3>
            <div style={{ marginTop: 4, fontSize: 11, color: "var(--text-muted)" }}>
              Search by organism/keyword or enter an accession, then pick a gene
              or sub-range — the same accession-first flow as NCBI BLAST.
            </div>
          </div>
          <button
            className="glass-button"
            type="button"
            onClick={onClose}
            aria-label="Close sequence builder"
          >
            <X size={14} strokeWidth={1.5} />
          </button>
        </div>

        {/* Step 1 — search */}
        <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
          <input
            className="glass-input"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void runSearch();
              }
            }}
            placeholder='Search NCBI nucleotide (e.g. "monkeypox virus complete genome")'
            style={{ flex: 1 }}
          />
          <button
            className="glass-button"
            type="button"
            onClick={() => void runSearch()}
            disabled={searching || !term.trim()}
          >
            {searching ? (
              <Loader2 size={13} strokeWidth={1.5} className="spin" />
            ) : (
              <Search size={13} strokeWidth={1.5} />
            )}{" "}
            Search
          </button>
        </div>

        {results && results.length > 0 && (
          <div
            style={{
              maxHeight: 180,
              overflowY: "auto",
              display: "flex",
              flexDirection: "column",
              gap: 6,
              marginBottom: 12,
            }}
          >
            {results.map((r) => (
              <button
                key={r.accession_version}
                type="button"
                className="glass-card"
                aria-pressed={accession.trim() === r.accession_version}
                onClick={() => selectAccession(r.accession_version)}
                style={{
                  textAlign: "left",
                  padding: "8px 10px",
                  background:
                    accession.trim() === r.accession_version
                      ? "color-mix(in srgb, var(--accent) 16%, var(--bg-tertiary))"
                      : "var(--bg-tertiary)",
                  borderColor:
                    accession.trim() === r.accession_version
                      ? "var(--border-focus)"
                      : undefined,
                  cursor: "pointer",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 8,
                    fontSize: 12,
                  }}
                >
                  <span style={{ fontFamily: "monospace" }}>{r.accession_version}</span>
                  <span style={{ color: "var(--text-muted)" }}>
                    {r.length != null ? `${r.length.toLocaleString()} bp` : ""}
                    {r.is_refseq ? " · RefSeq" : ""}
                  </span>
                </div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  {r.title || r.organism}
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Step 2 — accession + features */}
        <div style={{ display: "flex", gap: 8, marginBottom: 10, alignItems: "center" }}>
          <input
            className="glass-input"
            value={accession}
            onChange={(e) => setAccession(e.target.value)}
            placeholder="Accession (e.g. NC_063383.1)"
            style={{ flex: 1, fontFamily: "monospace" }}
          />
          <button
            className="glass-button"
            type="button"
            onClick={() => void loadFeatures()}
            disabled={featuresLoading || !accession.trim()}
            title="List gene/CDS features so you can pick a sub-range"
          >
            {featuresLoading ? (
              <Loader2 size={13} strokeWidth={1.5} className="spin" />
            ) : (
              <Dna size={13} strokeWidth={1.5} />
            )}{" "}
            Load genes
          </button>
        </div>

        {features && features.length > 0 && (
          <>
            <input
              className="glass-input"
              value={featureFilter}
              onChange={(e) => setFeatureFilter(e.target.value)}
              placeholder="Filter genes (name / product / locus_tag)"
              style={{ marginBottom: 6 }}
            />
            <div
              style={{
                maxHeight: 150,
                overflowY: "auto",
                display: "flex",
                flexDirection: "column",
                gap: 4,
                marginBottom: 12,
              }}
            >
              {visibleFeatures.map((f, idx) => (
                <button
                  key={`${f.name ?? "feat"}-${f.start}-${idx}`}
                  type="button"
                  className="glass-card"
                  onClick={() => pickFeature(f)}
                  style={{
                    textAlign: "left",
                    padding: "6px 9px",
                    background: "var(--bg-tertiary)",
                    cursor: "pointer",
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 8,
                    fontSize: 11,
                  }}
                >
                  <span>
                    <span style={{ fontFamily: "monospace" }}>{f.name || f.locus_tag || "—"}</span>
                    {f.product ? (
                      <span style={{ color: "var(--text-muted)" }}> · {f.product}</span>
                    ) : null}
                  </span>
                  <span style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                    {f.length.toLocaleString()} bp · {f.strand === "minus" ? "−" : "+"}
                  </span>
                </button>
              ))}
            </div>
          </>
        )}

        {/* Step 3 — sub-range + strand */}
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
          <span style={{ fontSize: 11, color: "var(--text-muted)", minWidth: 70 }}>
            Sub-range
          </span>
          <input
            className="glass-input blast-small-input"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            placeholder="From"
            type="number"
            min={1}
          />
          <ArrowRight size={12} style={{ color: "var(--text-faint)" }} />
          <input
            className="glass-input blast-small-input"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="To"
            type="number"
            min={1}
          />
          <div style={{ display: "flex", gap: 4, marginLeft: "auto" }}>
            {((["plus", "minus"] as const).map((s) => {
              const selected = strand === s;
              return (
                <button
                  key={s}
                  type="button"
                  className="glass-button"
                  aria-pressed={selected}
                  onClick={() => setStrand(s)}
                  style={{
                    fontSize: 11,
                    fontWeight: selected ? 600 : 400,
                    color: selected ? "var(--text-primary)" : "var(--text-muted)",
                    background: selected ? "var(--bg-hover)" : undefined,
                    borderColor: selected ? "var(--border-focus)" : undefined,
                  }}
                >
                  {s === "plus" ? "Plus" : "Minus"}
                </button>
              );
            }))}
          </div>
        </div>

        <div
          style={{
            fontFamily: "monospace",
            fontSize: 11,
            color: "var(--text-muted)",
            marginBottom: 12,
            wordBreak: "break-all",
          }}
        >
          {previewHeader() || "Pick a record to preview the FASTA header"}
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="glass-button" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="glass-button blast-action-btn--example"
            type="button"
            onClick={() => void doInsert()}
            disabled={inserting || !accession.trim()}
          >
            {inserting ? (
              <Loader2 size={13} strokeWidth={1.5} className="spin" />
            ) : (
              <Dna size={13} strokeWidth={1.5} />
            )}{" "}
            Insert sequence
          </button>
        </div>
      </div>
    </div>
  );
}
