import { BookOpen, FlaskConical } from "lucide-react";

import { PROGRAMS } from "@/pages/blastSubmitModel";
import type { ProgramSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader } from "@/pages/blastSubmit/ui";

const PROGRAM_SHORT_DESCRIPTIONS: Record<string, string> = {
  blastn: "Nucl -> Nucl",
  blastp: "Protein -> Protein",
  blastx: "tNucl -> Protein",
  tblastn: "Protein -> tNucl",
  tblastx: "tNucl -> tNucl",
};

export function ProgramSection({ form, set, programMeta }: ProgramSectionProps) {
  return (
    <section className="glass-card blast-section bsl-input bsl-done">
      <SectionHeader
        step={1}
        icon={<FlaskConical size={16} strokeWidth={1.5} />}
        title="Program Selection"
        subtitle="Choose a BLAST algorithm"
      />
      <div className="blast-program-tabs">
        {PROGRAMS.map((program) => (
          <button
            type="button"
            key={program.value}
            onClick={() => set("program", program.value)}
            className={`blast-program-tab${form.program === program.value ? " blast-program-tab--active" : ""}`}
            title={`${program.label}: ${program.longDesc}`}
          >
            <span className="blast-program-tab__name">{program.label}</span>
            <span className="blast-program-tab__desc">
              {PROGRAM_SHORT_DESCRIPTIONS[program.value] ?? program.desc}
            </span>
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
