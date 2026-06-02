import { useState } from "react";
import { BadgeCheck, Copy, FileText } from "lucide-react";

import type { BlastJobSummary } from "@/api/endpoints";
import {
  buildMethodsText,
  parityVerdict,
  searchSpacePin,
  type ParityState,
} from "./derived";

export interface ResultPassportCardProps {
  job: BlastJobSummary | null | undefined;
}

const PARITY_COLOR: Record<ParityState, string> = {
  equivalent: "#3fbf6a",
  drift: "#f0c674",
  approximate: "#7aa7ff",
  unknown: "#7a8290",
};

/**
 * Result Passport — a one-glance provenance + reproducibility card that NCBI
 * Web BLAST has no equivalent for. It answers "can I trust and cite this
 * run?" by combining the compatibility parity verdict, the pinned effective
 * search space, and an auto-generated, copy-pasteable Methods sentence.
 */
export function ResultPassportCard({ job }: ResultPassportCardProps) {
  const [copied, setCopied] = useState(false);
  const verdict = parityVerdict(job);
  const pin = searchSpacePin(job);
  const methods = buildMethodsText(job);
  const color = PARITY_COLOR[verdict.state];

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(methods);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="glass-card" style={{ padding: 16, marginBottom: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <FileText size={15} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
        <h3 style={{ margin: 0, fontSize: 14 }}>Result passport</h3>
        <span
          title={verdict.detail}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
            padding: "2px 10px",
            borderRadius: 999,
            background: `color-mix(in srgb, ${color} 16%, transparent)`,
            color,
            fontWeight: 600,
          }}
        >
          <BadgeCheck size={13} />
          {verdict.label}
        </span>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 6 }}
          onClick={handleCopy}
        >
          <Copy size={13} />
          {copied ? "Copied" : "Copy methods"}
        </button>
      </div>

      <p className="muted" style={{ margin: "8px 0 0", fontSize: 12 }}>
        {verdict.detail}
      </p>

      {pin.searchSpace !== null && (
        <p className="muted" style={{ margin: "4px 0 0", fontSize: 12 }}>
          {pin.text}
        </p>
      )}

      <div
        style={{
          marginTop: 12,
          padding: 12,
          borderRadius: 8,
          background: "var(--bg-tertiary)",
          border: "1px solid var(--glass-border)",
          fontSize: 13,
          lineHeight: 1.55,
          color: "var(--text-primary)",
        }}
      >
        {methods}
      </div>
    </div>
  );
}
