import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, FlaskConical, Loader2 } from "lucide-react";

import { formatApiError } from "@/api/client";
import { primerApi } from "@/api/endpoints";
import { ExamplePicker } from "@/components/ExamplePicker";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { PRIMER_EXAMPLES, type PrimerExampleValues } from "@/data/labToolExamples";
import { useTerminalSidecarHealth } from "@/hooks/usePrerequisites";
import {
  NotImplementedBanner,
  SectionHeader,
  SetupRequired,
  SidecarRequired,
} from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function PrimerDesignTab({
  meta,
  hasConfig,
}: {
  meta: TabMeta;
  hasConfig: boolean;
}) {
  const cfg = loadSavedConfig();
  const { toast } = useToast();
  const terminalSidecar = useTerminalSidecarHealth();
  const [sequence, setSequence] = useState("");
  const [targetStart, setTargetStart] = useState(100);
  const [targetLength, setTargetLength] = useState(200);
  const [productMin, setProductMin] = useState(100);
  const [productMax, setProductMax] = useState(1000);

  const mutation = useMutation({
    mutationFn: () =>
      primerApi.design({
        sequence,
        subscription_id: cfg?.subscriptionId ?? "",
        target_start: targetStart,
        target_length: targetLength,
        product_size_min: productMin,
        product_size_max: productMax,
      }),
    onError: (err: unknown) => toast(formatApiError(err, "blast"), "error"),
  });

  if (!hasConfig) {
    return (
      <section className="glass-card blast-section">
        <SectionHeader
          icon={<FlaskConical size={16} strokeWidth={1.5} />}
          title={meta.label}
          subtitle={meta.desc}
        />
        <SetupRequired feature="Primer Design" />
      </section>
    );
  }

  if (!terminalSidecar.isHealthy) {
    return (
      <section className="glass-card blast-section">
        <SectionHeader
          icon={<FlaskConical size={16} strokeWidth={1.5} />}
          title={meta.label}
          subtitle={meta.desc}
        />
        <SidecarRequired feature="Primer Design" />
      </section>
    );
  }

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<FlaskConical size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
      />
      <NotImplementedBanner feature="Primer Design" />

      <ExamplePicker<PrimerExampleValues>
        examples={PRIMER_EXAMPLES}
        onSelect={(v) => {
          setSequence(v.sequence);
          setTargetStart(v.targetStart);
          setTargetLength(v.targetLength);
          setProductMin(v.productMin);
          setProductMax(v.productMax);
        }}
      />

      <div className="form-row" style={{ marginBottom: 16 }}>
        <label className="form-label">Template sequence (nucleotide, min 50 bp)</label>
        <textarea
          className="form-input blast-textarea"
          rows={5}
          value={sequence}
          onChange={(e) => setSequence(e.target.value)}
          placeholder="ATGCGATCGATCGATCG..."
        />
      </div>

      <div className="form-grid form-grid--cols-4" style={{ marginBottom: 16 }}>
        <div className="form-row">
          <label className="form-label">Target start</label>
          <input
            className="form-input"
            type="number"
            value={targetStart}
            onChange={(e) => setTargetStart(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Target length</label>
          <input
            className="form-input"
            type="number"
            value={targetLength}
            onChange={(e) => setTargetLength(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Product min</label>
          <input
            className="form-input"
            type="number"
            value={productMin}
            onChange={(e) => setProductMin(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Product max</label>
          <input
            className="form-input"
            type="number"
            value={productMax}
            onChange={(e) => setProductMax(+e.target.value)}
          />
        </div>
      </div>

      <button
        className="btn btn--primary"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || sequence.length < 50}
      >
        {mutation.isPending ? (
          <Loader2 size={14} className="spin" />
        ) : (
          <FlaskConical size={14} />
        )}{" "}
        Design primers
      </button>

      {mutation.data?.primers && mutation.data.primers.length > 0 && (
        <div style={{ marginTop: 20, overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>#</th>
                <th>Forward primer</th>
                <th>Reverse primer</th>
                <th>Tm (F / R)</th>
                <th>GC% (F / R)</th>
                <th>Product</th>
                <th>Penalty</th>
              </tr>
            </thead>
            <tbody>
              {mutation.data.primers.map((p) => (
                <tr key={p.pair_index}>
                  <td>{p.pair_index + 1}</td>
                  <td>
                    <code className="code-val">{p.left_sequence}</code>
                  </td>
                  <td>
                    <code className="code-val">{p.right_sequence}</code>
                  </td>
                  <td>
                    {p.left_tm?.toFixed(1)} / {p.right_tm?.toFixed(1)}
                  </td>
                  <td>
                    {p.left_gc?.toFixed(1)} / {p.right_gc?.toFixed(1)}
                  </td>
                  <td>{p.product_size ?? "—"}</td>
                  <td>{p.pair_penalty?.toFixed(2) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {mutation.data?.primers?.length === 0 && (
        <div
          className="muted"
          style={{
            marginTop: 16,
            fontSize: 12,
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
          }}
        >
          <AlertTriangle size={13} /> No primer pairs found for the given parameters.
        </div>
      )}
    </section>
  );
}
