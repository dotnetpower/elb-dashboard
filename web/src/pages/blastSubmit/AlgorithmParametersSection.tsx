import { ChevronDown, ChevronUp, Gauge, TriangleAlert, Zap } from "lucide-react";

import { PRESETS } from "@/pages/blastSubmitModel";
import type { FormState } from "@/pages/blastSubmitModel";
import { parseNumericInput } from "@/pages/blastSubmit/numericInput";
import { parameterWarnings } from "@/pages/blastSubmit/parameterWarnings";
import type { ProgramMeta, SetBlastField } from "@/pages/blastSubmit/types";
import { Tip } from "@/pages/blastSubmit/ui";

const OUTPUT_FORMAT_OPTIONS = [
  { value: 0, label: "0 — Pairwise text" },
  { value: 5, label: "5 — BLAST XML" },
  { value: 6, label: "6 — Tabular" },
  { value: 7, label: "7 — Tabular + comments" },
  { value: 11, label: "11 — ASN.1 (archive)" },
  { value: 12, label: "12 — JSON Seq-align" },
  { value: 13, label: "13 — Multiple-file BLAST JSON" },
  { value: 14, label: "14 — Multiple-file BLAST XML2" },
  { value: 15, label: "15 — Single-file BLAST JSON" },
  { value: 16, label: "16 — Single-file BLAST XML2" },
  { value: 17, label: "17 — SAM" },
];

const GAP_COST_OPTIONS = [
  { value: "", label: "Linear" },
  { value: "5,2", label: "Existence 5, extension 2" },
  { value: "2,2", label: "Existence 2, extension 2" },
  { value: "1,2", label: "Existence 1, extension 2" },
  { value: "0,2", label: "Existence 0, extension 2" },
  { value: "3,1", label: "Existence 3, extension 1" },
  { value: "5,1", label: "Existence 5, extension 1" },
];

const MATCH_MISMATCH_OPTIONS = [
  { value: "1,-2", label: "1,-2" },
  { value: "1,-3", label: "1,-3" },
  { value: "2,-3", label: "2,-3" },
  { value: "4,-5", label: "4,-5" },
];

// Render the collapsed-section summary, highlighting the output-format ("Fmt: N")
// segment so it stands out from the other muted parameters.
function renderParamsSummary(summary: string) {
  return summary.split(" · ").map((segment, index) => {
    const node = segment.startsWith("Fmt:") ? (
      <span className="blast-params-summary__fmt">{segment}</span>
    ) : (
      segment
    );
    return (
      <span key={index}>
        {index > 0 && " · "}
        {node}
      </span>
    );
  });
}

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
  const gapCostValue = form.gap_open || form.gap_extend ? `${form.gap_open},${form.gap_extend}` : "";
  const matchMismatchValue = form.match_score || form.mismatch_score ? `${form.match_score || "1"},${form.mismatch_score || "-2"}` : "1,-2";
  const warnings = parameterWarnings(form);

  return (
    <section className="glass-card blast-section bsl-runtime bsl-done">
      <button onClick={() => setShowParams((value) => !value)} className="blast-params-toggle">
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="blast-step-badge" style={{ fontSize: 10, width: 20, height: 20 }}>
            7
          </span>
          <Gauge size={16} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
          <span style={{ fontWeight: 600, fontSize: 14 }}>Algorithm Parameters</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="muted" style={{ fontSize: 11 }}>
            {!showParams && renderParamsSummary(paramsSummary)}
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

          {warnings.length > 0 && (
            <div className="blast-parameter-warnings" role="status" aria-live="polite" aria-label="Custom parameter cautions">
              {warnings.map((warning) => (
                <span className="blast-parameter-warning" key={warning.key}>
                  <TriangleAlert size={12} strokeWidth={1.5} aria-hidden="true" />
                  <span><strong>Caution:</strong> {warning.message}</span>
                </span>
              ))}
            </div>
          )}

          <div className="blast-parameter-groups">
            <div className="blast-parameter-group">
              <div className="blast-parameter-group__title">General Parameters</div>
              <div className="blast-params-grid">
                <label>
                  <span className="glass-label">
                    Max target sequences <Tip text="Lowering this limit can change which hits appear, not only how many are returned, because BLAST prunes candidates during the search." />
                  </span>
                  <input
                    className="glass-input"
                    type="number"
                    value={form.max_target_seqs}
                    onChange={(event) =>
                      set("max_target_seqs", parseNumericInput(event.target.value, 100))
                    }
                  />
                </label>
                <label>
                  <span className="glass-label">
                    Expect threshold <Tip text="Lower values remove weaker matches; changing this value changes result membership and comparability with earlier searches." />
                  </span>
                  <input
                    className="glass-input"
                    type="number"
                    step="any"
                    value={form.evalue}
                    onChange={(event) =>
                      set("evalue", parseNumericInput(event.target.value, 0.05))
                    }
                  />
                </label>
                <label>
                  <span className="glass-label">
                    Word size <Tip text="Larger words usually run faster but can miss weaker similarities; smaller words increase sensitivity and runtime." />
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
                  <span className="glass-label">
                    Max matches in a query range <Tip text="Positive values suppress nearby lower-ranked matches and can remove otherwise reportable hits. Zero keeps the BLAST default." />
                  </span>
                  <input
                    className="glass-input"
                    type="number"
                    min={0}
                    value={form.max_matches_in_query_range}
                    onChange={(event) => set("max_matches_in_query_range", event.target.value)}
                  />
                </label>
                <label>
                  <span className="glass-label">
                    Output format <Tip text="The format changes downstream parsing and reformatting options, not the underlying alignments." />
                  </span>
                  <select
                    className="glass-input"
                    value={form.outfmt}
                    onChange={(event) => set("outfmt", parseInt(event.target.value, 10))}
                    disabled={form.outfmt_taxonomy_columns}
                  >
                    {OUTPUT_FORMAT_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="blast-checkbox-row">
                  <input
                    type="checkbox"
                    checked={form.outfmt_taxonomy_columns}
                    onChange={(event) => {
                      const on = event.target.checked;
                      set("outfmt_taxonomy_columns", on);
                      // Taxonomy columns require a tabular layout; force outfmt 7
                      // so the visible format matches the emitted specifier.
                      if (on) set("outfmt", 7);
                    }}
                  />
                  <span>
                    Include taxonomy &amp; description columns (taxid, scientific name, title, coverage){" "}
                    <Tip text="Adds the subject tax id, scientific name, title (Description), and query coverage (HSP Cover) columns to a tabular result via -outfmt 7 std staxids sscinames stitle qcovs. Works with sharding (the merge keeps the extended # Fields header). Requires a database that ships taxonomy data, e.g. core_nt." />
                  </span>
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
                  <label className="blast-checkbox-row">
                    <input
                      type="checkbox"
                      checked={form.short_query_adjust}
                      onChange={(event) => set("short_query_adjust", event.target.checked)}
                    />
                    <span>
                      Automatically adjust parameters for short input sequences <Tip text="For blastn queries up to 50 bases, use the BLASTN-short task unless you override it in Additional options." />
                    </span>
                  </label>
                )}
              </div>
            </div>

            <div className="blast-parameter-group">
              <div className="blast-parameter-group__title">Scoring Parameters</div>
              <div className="blast-params-grid">
                {form.program === "blastn" && (
                  <label>
                    <span className="glass-label">
                      Match/Mismatch scores <Tip text="Changing these scores can alter nucleotide alignment boundaries, bit scores, and hit ranking." />
                    </span>
                    <select
                      className="glass-input"
                      value={MATCH_MISMATCH_OPTIONS.some((option) => option.value === matchMismatchValue) ? matchMismatchValue : "custom"}
                      onChange={(event) => {
                        if (event.target.value === "custom") return;
                        const [match, mismatch] = event.target.value.split(",");
                        set("match_score", match ?? "");
                        set("mismatch_score", mismatch ?? "");
                      }}
                    >
                      {MATCH_MISMATCH_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                      <option value="custom">Custom</option>
                    </select>
                  </label>
                )}
                <label>
                  <span className="glass-label">
                    Gap costs <Tip text="Changing gap penalties can alter alignment boundaries, scores, and hit ranking. Linear leaves BLAST defaults untouched." />
                  </span>
                  <select
                    className="glass-input"
                    value={GAP_COST_OPTIONS.some((option) => option.value === gapCostValue) ? gapCostValue : "custom"}
                    onChange={(event) => {
                      if (event.target.value === "custom") return;
                      if (!event.target.value) {
                        set("gap_open", "");
                        set("gap_extend", "");
                        return;
                      }
                      const [open, extend] = event.target.value.split(",");
                      set("gap_open", open ?? "");
                      set("gap_extend", extend ?? "");
                    }}
                  >
                    {GAP_COST_OPTIONS.map((option) => (
                      <option key={option.value || "linear"} value={option.value}>{option.label}</option>
                    ))}
                    <option value="custom">Custom</option>
                  </select>
                </label>
                <label>
                  <span className="glass-label">
                    Gap open <Tip text="A higher opening penalty discourages new gaps and can change alignment boundaries and ranking." />
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
                    Gap extend <Tip text="A higher extension penalty shortens existing gaps and can change alignment boundaries and ranking." />
                  </span>
                  <input
                    className="glass-input"
                    type="number"
                    value={form.gap_extend}
                    onChange={(event) => set("gap_extend", event.target.value)}
                    placeholder="Auto"
                  />
                </label>
              </div>
            </div>

            <div className="blast-parameter-group">
              <div className="blast-parameter-group__title">Filters and Masking</div>
              <div className="blast-filter-grid">
                <label className="blast-checkbox-row">
                  <input
                    type="checkbox"
                    checked={form.low_complexity_filter}
                    onChange={(event) => set("low_complexity_filter", event.target.checked)}
                  />
                  <span>Low complexity regions <Tip text="Turning this off can increase repetitive or compositionally biased matches and substantially increase runtime." /></span>
                </label>
                <label className="blast-checkbox-row">
                  <input
                    type="checkbox"
                    checked={form.mask_lookup_table_only}
                    onChange={(event) => set("mask_lookup_table_only", event.target.checked)}
                  />
                  <span>Mask for lookup table only <Tip text="Use soft masking so masked regions do not seed hits but can still extend alignments." /></span>
                </label>
                <label className="blast-checkbox-row">
                  <input
                    type="checkbox"
                    checked={form.mask_lowercase}
                    onChange={(event) => set("mask_lowercase", event.target.checked)}
                  />
                  <span>Mask lower case letters <Tip text="Treat lower-case bases in the query as masked sequence." /></span>
                </label>
                <label className="blast-checkbox-row">
                  <input
                    type="checkbox"
                    checked={form.species_repeat_filter}
                    onChange={(event) => set("species_repeat_filter", event.target.checked)}
                  />
                  <span>Species-specific repeats <Tip text="Suppresses repeat-derived matches for the selected taxid; relevant hits overlapping repeats may also disappear." /></span>
                </label>
                {form.species_repeat_filter && (
                  <label>
                    <span className="glass-label">
                      Repeat taxid <Tip text="Chooses which species repeat library is masked; selecting the wrong taxid can suppress the wrong query regions. Homo sapiens is 9606." />
                    </span>
                    <input
                      className="glass-input"
                      value={form.repeat_filter_taxid}
                      onChange={(event) => set("repeat_filter_taxid", event.target.value)}
                      placeholder="9606"
                    />
                  </label>
                )}
              </div>
            </div>

            <label>
              <span className="glass-label">
                Additional options <Tip text="Extra flags can override form defaults and make this run difficult to compare with searches that used the recommended settings." />
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
