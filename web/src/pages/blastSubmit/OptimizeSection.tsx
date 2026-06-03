import { SlidersHorizontal } from "lucide-react";

import { BLASTN_OPTIMIZE } from "@/pages/blastSubmitModel";
import type { OptimizeSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader } from "@/pages/blastSubmit/ui";

export function OptimizeSection({ form, set }: OptimizeSectionProps) {
  if (form.program !== "blastn") {
    return null;
  }

  return (
    <section className="glass-card blast-section bsl-runtime bsl-done">
      <SectionHeader
        step={5}
        icon={<SlidersHorizontal size={16} strokeWidth={1.5} />}
        title="Program Selection"
        subtitle="Choose the blastn task profile"
      />
      <div className="blast-optimize-group blast-optimize-group--cards">
        {BLASTN_OPTIMIZE.map((option) => (
          <label
            key={option.value}
            className={`blast-optimize-option${form.optimize === option.value ? " blast-optimize-option--active" : ""}`}
          >
            <input
              type="radio"
              name="optimize"
              value={option.value}
              checked={form.optimize === option.value}
              onChange={() => {
                set("optimize", option.value);
                set("word_size", String(option.wordSize));
                set("evalue", option.evalue);
              }}
              style={{ display: "none" }}
            />
            <span className="blast-optimize-radio" />
            <div className="blast-optimize-copy">
              <div className="blast-optimize-title">{option.label}</div>
              <div className="muted">{option.desc}</div>
              <div className="blast-option-diff">
                <span>word size {option.wordSize}</span>
                <span>E-value {option.evalue}</span>
              </div>
            </div>
          </label>
        ))}
      </div>
    </section>
  );
}