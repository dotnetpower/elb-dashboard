import { BookOpen, FlaskConical } from "lucide-react";

import { BLASTN_OPTIMIZE, PROGRAMS } from "@/pages/blastSubmitModel";
import type { ProgramSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader } from "@/pages/blastSubmit/ui";

export function ProgramSection({ form, set, programMeta }: ProgramSectionProps) {
  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={1}
        icon={<FlaskConical size={16} strokeWidth={1.5} />}
        title="Program Selection"
        subtitle="Choose a BLAST algorithm"
      />
      <div className="blast-program-tabs">
        {PROGRAMS.map((program) => (
          <button
            key={program.value}
            onClick={() => set("program", program.value)}
            className={`blast-program-tab${form.program === program.value ? " blast-program-tab--active" : ""}`}
          >
            <span className="blast-program-tab__name">{program.label}</span>
            <span className="blast-program-tab__desc">{program.desc}</span>
          </button>
        ))}
      </div>
      <div className="blast-program-info">
        <BookOpen size={14} strokeWidth={1.5} style={{ color: "var(--accent)", flexShrink: 0 }} />
        <span>{programMeta.longDesc}</span>
      </div>

      {form.program === "blastn" && (
        <div style={{ marginTop: 12 }}>
          <span className="glass-label" style={{ marginBottom: 6 }}>
            Optimize for
          </span>
          <div className="blast-optimize-group">
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
                  }}
                  style={{ display: "none" }}
                />
                <span className="blast-optimize-radio" />
                <div>
                  <div style={{ fontSize: 12 }}>{option.label}</div>
                  <div className="muted" style={{ fontSize: 10 }}>
                    {option.desc}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
