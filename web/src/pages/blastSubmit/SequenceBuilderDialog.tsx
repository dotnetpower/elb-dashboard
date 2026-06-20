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
import { useEffect, useRef, useState } from "react";
import { ArrowRight, Check, Dna, Loader2, Search, X } from "lucide-react";

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

  const searchInputRef = useRef<HTMLInputElement>(null);

  // Focus the first field on open and wire Escape-to-close so the dialog is
  // fully keyboard-operable (matches native dialog expectations; backdrop click
  // and Cancel remain).
  useEffect(() => {
    searchInputRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

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

  const accSelected = accession.trim().length > 0;
  const headerText = previewHeader();

  return (
    <div className="glass-dialog-backdrop" onClick={onClose}>
      <div
        className="glass-card glass-card--strong seqbuilder-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sequence-builder-title"
        onClick={(event) => event.stopPropagation()}
      >
        {/* Pinned header */}
        <div className="seqbuilder-dialog__header">
          <div>
            <span className="seqbuilder-badge">
              <Dna size={11} strokeWidth={1.5} /> NCBI
            </span>
            <h3 id="sequence-builder-title" className="seqbuilder-dialog__title">
              Generate query from NCBI
            </h3>
            <div className="seqbuilder-dialog__subtitle">
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

        {/* Scrollable body */}
        <div className="seqbuilder-dialog__body">
          {/* Step 1 — search */}
          <div className="seqbuilder-step">
            <div className="seqbuilder-step__head">
              <span className="seqbuilder-step__num">1</span>
              <span className="seqbuilder-step__label">Find a record</span>
            </div>
            <div className="seqbuilder-row">
              <input
                ref={searchInputRef}
                className="glass-input"
                value={term}
                onChange={(e) => setTerm(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void runSearch();
                  }
                }}
                aria-label="Search NCBI nucleotide database"
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

            {(searching || results) && (
              <div
                className="seqbuilder-list"
                role="listbox"
                aria-label="NCBI search results"
              >
                {searching ? (
                  <div className="seqbuilder-hint">Searching NCBI…</div>
                ) : results && results.length > 0 ? (
                  results.map((r) => {
                    const selected = accession.trim() === r.accession_version;
                    return (
                      <button
                        key={r.accession_version}
                        type="button"
                        role="option"
                        aria-selected={selected}
                        className={
                          "seqbuilder-item" +
                          (selected ? " seqbuilder-item--selected" : "")
                        }
                        onClick={() => selectAccession(r.accession_version)}
                      >
                        <div className="seqbuilder-item__top">
                          <span className="seqbuilder-item__acc">
                            {selected && (
                              <Check
                                className="seqbuilder-item__check"
                                size={13}
                                strokeWidth={2}
                              />
                            )}
                            {r.accession_version}
                          </span>
                          <span className="seqbuilder-item__meta">
                            {r.length != null ? `${r.length.toLocaleString()} bp` : ""}
                            {r.is_refseq ? " · RefSeq" : ""}
                          </span>
                        </div>
                        <div className="seqbuilder-item__sub">
                          {r.title || r.organism}
                        </div>
                      </button>
                    );
                  })
                ) : (
                  <div className="seqbuilder-hint">
                    No NCBI records matched that search.
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="seqbuilder-divider" />

          {/* Step 2 — accession + features */}
          <div className="seqbuilder-step">
            <div className="seqbuilder-step__head">
              <span className="seqbuilder-step__num">2</span>
              <span className="seqbuilder-step__label">Accession &amp; genes</span>
            </div>
            <div className="seqbuilder-row">
              <input
                className="glass-input"
                value={accession}
                onChange={(e) => setAccession(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void loadFeatures();
                  }
                }}
                aria-label="NCBI accession"
                placeholder="Accession (e.g. NC_063383.1)"
                style={{ flex: 1, fontFamily: "var(--font-mono)" }}
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

            {accSelected && (
              <span className="seqbuilder-selected-chip">
                <Check size={12} strokeWidth={2} /> {accession.trim()} selected
              </span>
            )}

            {(featuresLoading || features) && (
              <>
                {features && features.length > 0 && (
                  <div className="seqbuilder-row">
                    <input
                      className="glass-input"
                      value={featureFilter}
                      onChange={(e) => setFeatureFilter(e.target.value)}
                      aria-label="Filter gene features"
                      placeholder="Filter genes (name / product / locus_tag)"
                      style={{ flex: 1 }}
                    />
                    <span className="seqbuilder-caption">
                      {visibleFeatures.length}/{features.length}
                    </span>
                  </div>
                )}
                <div className="seqbuilder-list" aria-label="Gene features">
                  {featuresLoading ? (
                    <div className="seqbuilder-hint">Loading gene features…</div>
                  ) : features && features.length > 0 ? (
                    visibleFeatures.length > 0 ? (
                      visibleFeatures.map((f, idx) => (
                        <button
                          key={`${f.name ?? "feat"}-${f.start}-${idx}`}
                          type="button"
                          className="seqbuilder-item seqbuilder-feature"
                          onClick={() => pickFeature(f)}
                        >
                          <span className="seqbuilder-feature__name">
                            {f.name || f.locus_tag || "—"}
                            {f.product ? (
                              <span
                                style={{
                                  color: "var(--text-muted)",
                                  fontFamily: "var(--font-sans)",
                                }}
                              >
                                {" "}
                                · {f.product}
                              </span>
                            ) : null}
                          </span>
                          <span className="seqbuilder-item__meta">
                            {f.length.toLocaleString()} bp ·{" "}
                            {f.strand === "minus" ? "−" : "+"}
                          </span>
                        </button>
                      ))
                    ) : (
                      <div className="seqbuilder-hint">
                        No genes match that filter.
                      </div>
                    )
                  ) : (
                    <div className="seqbuilder-hint">
                      No gene features found for this record.
                    </div>
                  )}
                </div>
              </>
            )}
          </div>

          <div className="seqbuilder-divider" />

          {/* Step 3 — sub-range + strand */}
          <div className="seqbuilder-step">
            <div className="seqbuilder-step__head">
              <span className="seqbuilder-step__num">3</span>
              <span className="seqbuilder-step__label">Sub-range &amp; strand</span>
            </div>
            <div className="seqbuilder-row">
              <input
                className="glass-input blast-small-input"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
                aria-label="Sub-range start"
                placeholder="From"
                type="number"
                min={1}
              />
              <ArrowRight
                size={12}
                style={{ color: "var(--text-faint)", flex: "none" }}
              />
              <input
                className="glass-input blast-small-input"
                value={to}
                onChange={(e) => setTo(e.target.value)}
                aria-label="Sub-range end"
                placeholder="To"
                type="number"
                min={1}
              />
              <div className="seqbuilder-strand" role="group" aria-label="Strand">
                {(["plus", "minus"] as const).map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="seqbuilder-strand__btn"
                    aria-pressed={strand === s}
                    onClick={() => setStrand(s)}
                  >
                    {s === "plus" ? "Plus" : "Minus"}
                  </button>
                ))}
              </div>
            </div>

            <div
              className={
                "seqbuilder-preview" +
                (headerText ? "" : " seqbuilder-preview--placeholder")
              }
              aria-live="polite"
            >
              {headerText || "Pick a record to preview the FASTA header"}
            </div>
          </div>
        </div>

        {/* Pinned footer */}
        <div className="seqbuilder-dialog__footer">
          <button className="glass-button" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="glass-button glass-button--primary"
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
