/**
 * Molecular-diagnostics target-analysis card for the Sequence Detail page.
 *
 * Responsibility: Answer the questions a diagnostics researcher actually has
 * when they arrive on an accession from a BLAST hit — is the hit window a
 * usable assay target? It surfaces base composition + GC% (whole record and the
 * highlighted hit window), N / IUPAC-ambiguous warnings, whether the hit
 * overlaps or sits next to an assembly gap, which annotated feature(s) the hit
 * falls inside, a reverse-complement view, a one-click sub-range FASTA extract,
 * an assembly-quality (gap) summary, and a record-freshness note. All heavy
 * logic lives in `sequenceAnalysis.ts`; this component only arranges and labels
 * it.
 * Edit boundaries: Presentational. Pull analytics from `sequenceAnalysis`; do
 * not embed sequence math here. No network.
 * Key entry points: `TargetAnalysisCard`.
 * Risky contracts: coordinates are 1-based inclusive subject coordinates,
 * matching `hl_start`/`hl_stop`.
 * Validation: `cd web && npm run build` + open a record with `?hl_start&hl_stop`.
 */
import { useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowLeftRight,
  Check,
  CheckCircle2,
  Copy,
  Crosshair,
  Layers,
} from "lucide-react";

import type { NuccoreFeature, NuccoreGenBank, NuccoreSummary } from "@/api/ncbi";
import { useTransientState } from "../../hooks/useTransientState";
import {
  baseComposition,
  collectAssemblyGaps,
  extractSubrange,
  featuresOverlappingRange,
  gapSummary,
  hitGapRelation,
  reverseComplement,
  subrangeFasta,
} from "./sequenceAnalysis";

function pct(value: number | null): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function featureName(feature: NuccoreFeature): string {
  const get = (name: string) =>
    feature.qualifiers.find((q) => q.name === name && q.value)?.value ?? null;
  const gene = get("gene");
  const product = get("product");
  const label = [gene, product].filter(Boolean).join(" · ");
  return label || feature.key || "feature";
}

function recordAgeDays(updateDate: string | null | undefined): number | null {
  if (!updateDate) return null;
  // Accept "2025/05/01", "2025-05-01", and GenBank "01-MAY-2025".
  const ts = Date.parse(updateDate.replace(/\//g, "-"));
  if (Number.isNaN(ts)) return null;
  return Math.floor((Date.now() - ts) / 86_400_000);
}

function CopyChip({ value, label }: { value: string; label: string }) {
  const [copied, flashCopied] = useTransientState(false);
  const onCopy = () => {
    if (!navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(value).then(() => flashCopied(true, 1500));
  };
  return (
    <button
      type="button"
      className="glass-button glass-button--ghost target-analysis__chip"
      onClick={onCopy}
      title={`Copy ${label}`}
    >
      {copied ? <Check size={12} strokeWidth={1.5} /> : <Copy size={12} strokeWidth={1.5} />}
      {copied ? "Copied" : label}
    </button>
  );
}

function CompositionBar({
  label,
  comp,
}: {
  label: string;
  comp: ReturnType<typeof baseComposition>;
}) {
  return (
    <div className="target-analysis__comp">
      <div className="target-analysis__comp-head">
        <span>{label}</span>
        <span className="target-analysis__len">
          {comp.length.toLocaleString()} bp · GC {pct(comp.gc)}
        </span>
      </div>
      <dl className="target-analysis__counts">
        {(["A", "C", "G", "T"] as const).map((b) => (
          <div key={b} className="target-analysis__count">
            <dt>{b}</dt>
            <dd>{comp.counts[b].toLocaleString()}</dd>
          </div>
        ))}
        {comp.counts.U > 0 && (
          <div className="target-analysis__count">
            <dt>U</dt>
            <dd>{comp.counts.U.toLocaleString()}</dd>
          </div>
        )}
        <div
          className={`target-analysis__count${comp.n > 0 ? " target-analysis__count--warn" : ""}`}
        >
          <dt>N</dt>
          <dd>{comp.n.toLocaleString()}</dd>
        </div>
        {comp.ambiguous > 0 && (
          <div className="target-analysis__count target-analysis__count--warn">
            <dt>IUPAC</dt>
            <dd>{comp.ambiguous.toLocaleString()}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}

export function TargetAnalysisCard({
  accession,
  seq,
  highlight,
  features,
  summary,
  genbank,
  onJumpToHit,
}: {
  accession: string;
  seq: string | null;
  highlight: { start: number; stop: number } | null;
  features: NuccoreFeature[];
  summary: NuccoreSummary | undefined;
  genbank: NuccoreGenBank | undefined;
  onJumpToHit?: () => void;
}) {
  const [revComp, setRevComp] = useState(false);

  const totalLength = summary?.length ?? genbank?.length ?? (seq ? seq.length : null);
  const gaps = useMemo(() => collectAssemblyGaps(features), [features]);
  const gapStats = useMemo(() => gapSummary(gaps, totalLength), [gaps, totalLength]);

  const wholeComp = useMemo(() => (seq ? baseComposition(seq) : null), [seq]);

  const targetSeq = useMemo(
    () => (seq && highlight ? extractSubrange(seq, highlight.start, highlight.stop) : ""),
    [seq, highlight],
  );
  const targetComp = useMemo(
    () => (targetSeq ? baseComposition(targetSeq) : null),
    [targetSeq],
  );
  const gapRelation = useMemo(
    () => (highlight ? hitGapRelation(highlight, gaps) : null),
    [highlight, gaps],
  );
  const containingFeatures = useMemo(
    () => (highlight ? featuresOverlappingRange(features, highlight) : []),
    [features, highlight],
  );

  const ageDays = recordAgeDays(summary?.update_date ?? genbank?.update_date);
  const isRefSeq = (summary?.source_db ?? "").toLowerCase() === "refseq";

  // Nothing to analyse yet (FASTA still loading and no gap/feature data).
  if (!seq && gaps.length === 0 && !highlight) return null;

  const lo = highlight ? Math.min(highlight.start, highlight.stop) : 0;
  const hi = highlight ? Math.max(highlight.start, highlight.stop) : 0;
  const displaySeq = targetSeq
    ? revComp
      ? reverseComplement(targetSeq)
      : targetSeq
    : "";

  return (
    <section
      className="glass-card glass-card--strong target-analysis"
      aria-labelledby="target-analysis-heading"
    >
      <div className="target-analysis__head">
        <h2 id="target-analysis-heading" className="target-analysis__title">
          <Activity size={15} strokeWidth={1.5} /> Target analysis
        </h2>
        <span className="target-analysis__note">
          Coordinates are 1-based, inclusive, on the subject (+) strand.
        </span>
      </div>

      {/* Hit-window verdict badges. */}
      {highlight && (
        <div className="target-analysis__verdicts">
          <span className="target-analysis__verdict">
            <Crosshair size={12} strokeWidth={1.5} />
            Hit {lo.toLocaleString()}–{hi.toLocaleString()} ({(hi - lo + 1).toLocaleString()} bp)
          </span>
          {onJumpToHit && (
            <button
              type="button"
              className="glass-button glass-button--ghost target-analysis__chip"
              onClick={onJumpToHit}
            >
              Jump to hit in sequence
            </button>
          )}
          {gapRelation?.kind === "overlap" && (
            <span className="target-analysis__badge target-analysis__badge--danger">
              <AlertTriangle size={12} strokeWidth={1.5} />
              Overlaps assembly gap — not a usable target
            </span>
          )}
          {gapRelation?.kind === "adjacent" && (
            <span className="target-analysis__badge target-analysis__badge--warn">
              <AlertTriangle size={12} strokeWidth={1.5} />
              {gapRelation.nearestDistance} bp from an assembly gap
            </span>
          )}
          {gapRelation?.kind === "clear" && gaps.length > 0 && (
            <span className="target-analysis__badge target-analysis__badge--ok">
              <CheckCircle2 size={12} strokeWidth={1.5} />
              Clear of assembly gaps
            </span>
          )}
          {targetComp?.hasUncertain && (
            <span className="target-analysis__badge target-analysis__badge--warn">
              <AlertTriangle size={12} strokeWidth={1.5} />
              {targetComp.n > 0 ? `${targetComp.n} N` : ""}
              {targetComp.n > 0 && targetComp.ambiguous > 0 ? " · " : ""}
              {targetComp.ambiguous > 0 ? `${targetComp.ambiguous} ambiguous` : ""} in window
            </span>
          )}
        </div>
      )}

      {/* Containing features. */}
      {highlight && containingFeatures.length > 0 && (
        <div className="target-analysis__features">
          <span className="target-analysis__sublabel">Hit falls inside</span>
          <div className="target-analysis__feature-list">
            {containingFeatures.slice(0, 6).map((f, i) => (
              <span key={`${f.key}-${i}`} className="target-analysis__feature">
                <Layers size={11} strokeWidth={1.5} />
                {f.key}: {featureName(f)}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Composition: whole record + target window. */}
      <div className="target-analysis__comps">
        {wholeComp && <CompositionBar label="Whole record" comp={wholeComp} />}
        {targetComp && <CompositionBar label="Hit window" comp={targetComp} />}
        {highlight && seq && !targetSeq && (
          <p className="target-analysis__empty">
            Hit window {lo}–{hi} is outside the resolved sequence.
          </p>
        )}
      </div>

      {/* Target sub-sequence + extraction. */}
      {targetSeq && (
        <div className="target-analysis__extract">
          <div className="target-analysis__extract-head">
            <span className="target-analysis__sublabel">
              Hit window FASTA{revComp ? " (reverse-complement)" : ""}
            </span>
            <div className="target-analysis__extract-tools">
              <button
                type="button"
                className="glass-button glass-button--ghost target-analysis__chip"
                aria-pressed={revComp}
                onClick={() => setRevComp((v) => !v)}
                title="Show the reverse complement of the hit window"
              >
                <ArrowLeftRight size={12} strokeWidth={1.5} />
                {revComp ? "Forward" : "Rev-comp"}
              </button>
              <CopyChip
                value={subrangeFasta(accession, seq ?? "", lo, hi, {
                  reverseComplement: revComp,
                })}
                label="Copy FASTA"
              />
            </div>
          </div>
          <pre className="target-analysis__seq">{displaySeq}</pre>
        </div>
      )}

      {/* Assembly quality. */}
      <div className="target-analysis__quality">
        {gaps.length === 0 ? (
          <span className="target-analysis__badge target-analysis__badge--ok">
            <CheckCircle2 size={12} strokeWidth={1.5} />
            No assembly gaps reported
          </span>
        ) : (
          <span className="target-analysis__badge target-analysis__badge--warn">
            <AlertTriangle size={12} strokeWidth={1.5} />
            Draft assembly · {gapStats.count} gaps · {gapStats.totalBp.toLocaleString()} bp
            {gapStats.fraction != null ? ` (${pct(gapStats.fraction)})` : ""}
          </span>
        )}
        {!isRefSeq && (
          <span className="target-analysis__badge">
            {(summary?.source_db ?? "GenBank").toUpperCase()} record — prefer a RefSeq
            reference for a validated assay
          </span>
        )}
        {ageDays != null && ageDays > 365 && (
          <span className="target-analysis__badge target-analysis__badge--warn">
            <AlertTriangle size={12} strokeWidth={1.5} />
            Updated {Math.floor(ageDays / 365)}y ago — confirm against current variants
          </span>
        )}
      </div>
    </section>
  );
}
