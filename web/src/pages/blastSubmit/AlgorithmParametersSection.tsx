import { ChevronDown, ChevronUp, Gauge, Zap } from "lucide-react";

import { PRESETS } from "@/pages/blastSubmitModel";
import type { FormState } from "@/pages/blastSubmitModel";
import type { ProgramMeta, SetBlastField } from "@/pages/blastSubmit/types";
import { Tip } from "@/pages/blastSubmit/ui";

const OUTPUT_FORMAT_OPTIONS = [
  { value: 0, label: "0 — Pairwise text" },
  { value: 5, label: "5 — BLAST XML" },
  { value: 6, label: "6 — Tabular" },
  { value: 7, label: "7 — Tabular + comments" },
  { value: 11, label: "11 — ASN.1 (archive)" },
];

export function AlgorithmParametersSection({
  form,
  set,
  showParams,
  setShowParams,
  paramsSummary,
  programMeta,
  webBlastSearchsp,
  webBlastSearchspScope,
}: {
  form: FormState;
  set: SetBlastField;
  showParams: boolean;
  setShowParams: (value: (current: boolean) => boolean) => void;
  paramsSummary: string;
  programMeta: ProgramMeta;
  webBlastSearchsp?: number;
  webBlastSearchspScope?: string;
}) {
  return (
    <section className="glass-card blast-section">
      <button onClick={() => setShowParams((value) => !value)} className="blast-params-toggle">
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="blast-step-badge" style={{ fontSize: 10, width: 20, height: 20 }}>
            6
          </span>
          <Gauge size={16} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
          <span style={{ fontWeight: 600, fontSize: 14 }}>Algorithm Parameters</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="muted" style={{ fontSize: 11 }}>
            {!showParams && paramsSummary}
          </span>
          {showParams ? <ChevronUp size={16} strokeWidth={1.5} /> : <ChevronDown size={16} strokeWidth={1.5} />}
        </div>
      </button>
      {showParams && (
        <div style={{ marginTop: 16 }}>
          <div className="blast-presets">
            {PRESETS.map((preset) => {
              const active = form.evalue === preset.evalue && form.max_target_seqs === preset.max_target_seqs;
              return (
                <button
                  key={preset.label}
                  className={`blast-preset${active ? " blast-preset--active" : ""}`}
                  onClick={() => {
                    set("evalue", preset.evalue);
                    set("max_target_seqs", preset.max_target_seqs);
                  }}
                >
                  <Zap size={12} />
                  <div>
                    <div style={{ fontWeight: 500 }}>{preset.label}</div>
                    <div className="muted" style={{ fontSize: 10 }}>
                      {preset.desc}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>

          <div className="blast-params-grid">
            <label>
              <span className="glass-label">
                E-value <Tip text="Expected number of chance matches. Lower = more stringent." />
              </span>
              <input
                className="glass-input"
                type="number"
                step="any"
                value={form.evalue}
                onChange={(event) => set("evalue", parseFloat(event.target.value) || 0.05)}
              />
            </label>
            <label>
              <span className="glass-label">
                Max target seqs <Tip text="Maximum number of aligned sequences to keep." />
              </span>
              <input
                className="glass-input"
                type="number"
                value={form.max_target_seqs}
                onChange={(event) => set("max_target_seqs", parseInt(event.target.value, 10) || 100)}
              />
            </label>
            <label>
              <span className="glass-label">
                Word size <Tip text="Length of initial exact match." />
              </span>
              <input
                className="glass-input"
                type="number"
                value={form.word_size}
                onChange={(event) => set("word_size", event.target.value)}
                placeholder={String(programMeta.defaultWordSize)}
              />
            </label>
            <label>
              <span className="glass-label">Output format</span>
              <select
                className="glass-input"
                value={form.outfmt}
                onChange={(event) => set("outfmt", parseInt(event.target.value, 10))}
              >
                {OUTPUT_FORMAT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            {webBlastSearchsp && (
              <label>
                <span className="glass-label">
                  Search space <Tip text={webBlastSearchspScope ?? "Verified Web BLAST calibration default."} />
                </span>
                <input className="glass-input" value={webBlastSearchsp.toString()} readOnly />
              </label>
            )}
            {form.program === "blastn" && (
              <>
                <label>
                  <span className="glass-label">
                    Match score <Tip text="Reward for a nucleotide match. Default: 1" />
                  </span>
                  <input
                    className="glass-input"
                    type="number"
                    value={form.match_score}
                    onChange={(event) => set("match_score", event.target.value)}
                    placeholder="1"
                  />
                </label>
                <label>
                  <span className="glass-label">
                    Mismatch score <Tip text="Penalty for a mismatch. Default: -2" />
                  </span>
                  <input
                    className="glass-input"
                    type="number"
                    value={form.mismatch_score}
                    onChange={(event) => set("mismatch_score", event.target.value)}
                    placeholder="-2"
                  />
                </label>
              </>
            )}
            <label>
              <span className="glass-label">
                Gap open <Tip text="Cost to open a gap." />
              </span>
              <input
                className="glass-input"
                type="number"
                value={form.gap_open}
                onChange={(event) => set("gap_open", event.target.value)}
                placeholder="Auto"
              />
            </label>
            <label>
              <span className="glass-label">
                Gap extend <Tip text="Cost to extend a gap." />
              </span>
              <input
                className="glass-input"
                type="number"
                value={form.gap_extend}
                onChange={(event) => set("gap_extend", event.target.value)}
                placeholder="Auto"
              />
            </label>
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
              <span className="glass-label" style={{ marginBottom: 0 }}>
                Filters:
              </span>
              <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12 }}>
                <input
                  type="checkbox"
                  checked={form.low_complexity_filter}
                  onChange={(event) => set("low_complexity_filter", event.target.checked)}
                />
                Low complexity filter <Tip text="Mask low-complexity regions (DUST for nucleotide, SEG for protein)." />
              </label>
            </div>
            <label style={{ gridColumn: "1 / -1" }}>
              <span className="glass-label">
                Additional options <Tip text="Extra command-line flags for BLAST." />
              </span>
              <input
                className="glass-input"
                value={form.additional_options}
                onChange={(event) => set("additional_options", event.target.value)}
                placeholder="-max_hsps 1 -num_threads 4"
                spellCheck={false}
              />
            </label>
          </div>
        </div>
      )}
    </section>
  );
}
