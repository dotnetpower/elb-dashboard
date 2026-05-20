import { BookOpen, FlaskConical } from "lucide-react";

import { PROGRAMS } from "@/pages/blastSubmitModel";
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
    </section>
  );
}
