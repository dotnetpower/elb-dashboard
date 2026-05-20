import { FileText } from "lucide-react";

import { StepLogSection } from "@/components/BlastStepTimeline";

import type { BlastResultsState } from "./useBlastResultsState";

export interface ExecutionStepsCardProps {
  state: BlastResultsState;
}

export function ExecutionStepsCard({ state }: ExecutionStepsCardProps) {
  const { job, effectivePhase, subscriptionId, storageAccount, resourceGroup } = state;
  if (!job) return null;
  return (
    <section className="glass-card" style={{ padding: "14px 16px" }}>
      <h3
        style={{
          margin: "0 0 10px 0",
          fontSize: 14,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <FileText size={15} strokeWidth={1.5} /> Execution Steps
      </h3>
      <StepLogSection
        phase={effectivePhase}
        job={job as unknown as Record<string, unknown>}
        subscriptionId={subscriptionId}
        storageAccount={storageAccount}
        resourceGroup={resourceGroup}
      />
    </section>
  );
}
