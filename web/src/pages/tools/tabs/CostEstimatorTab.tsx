import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { DollarSign, Loader2 } from "lucide-react";

import { costApi } from "@/api/endpoints";
import { ExamplePicker } from "@/components/ExamplePicker";
import { COST_EXAMPLES, type CostExampleValues } from "@/data/labToolExamples";
import { formatAksSkuOption, useAksSkus } from "@/hooks/useAksSkus";
import {
  NotImplementedBanner,
  SectionHeader,
  StatBox,
} from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function CostEstimatorTab({ meta }: { meta: TabMeta }) {
  const [sku, setSku] = useState("Standard_E32s_v5");
  const [nodes, setNodes] = useState(3);
  const [hours, setHours] = useState(2);
  const [pdSize, setPdSize] = useState(1000);
  const [dbSize, setDbSize] = useState(50);
  const { skus: skuOptions } = useAksSkus();

  const mutation = useMutation({
    mutationFn: () =>
      costApi.estimate({
        machine_type: sku,
        num_nodes: nodes,
        estimated_hours: hours,
        pd_size_gb: pdSize,
        db_size_gb: dbSize,
      }),
  });

  const est = mutation.data?.estimate;

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<DollarSign size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
      />
      <NotImplementedBanner feature="Cost Estimator" />

      <ExamplePicker<CostExampleValues>
        examples={COST_EXAMPLES}
        label="Load a scenario"
        onSelect={(v) => {
          setSku(v.sku);
          setNodes(v.nodes);
          setHours(v.hours);
          setPdSize(v.pdSize);
          setDbSize(v.dbSize);
        }}
      />

      <div className="form-grid form-grid--cols-5" style={{ marginBottom: 16 }}>
        <div className="form-row">
          <label className="form-label">Node SKU</label>
          <select
            className="form-input"
            value={sku}
            onChange={(e) => setSku(e.target.value)}
          >
            {skuOptions.map((option) => (
              <option key={option.name} value={option.name}>
                {formatAksSkuOption(option)}
              </option>
            ))}
          </select>
        </div>
        <div className="form-row">
          <label className="form-label">Nodes</label>
          <input
            className="form-input"
            type="number"
            min={1}
            max={100}
            value={nodes}
            onChange={(e) => setNodes(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Estimated hours</label>
          <input
            className="form-input"
            type="number"
            min={0.1}
            max={168}
            step={0.5}
            value={hours}
            onChange={(e) => setHours(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Persistent disk (GB)</label>
          <input
            className="form-input"
            type="number"
            min={10}
            max={10000}
            value={pdSize}
            onChange={(e) => setPdSize(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Database size (GB)</label>
          <input
            className="form-input"
            type="number"
            min={1}
            max={5000}
            value={dbSize}
            onChange={(e) => setDbSize(+e.target.value)}
          />
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <button
          className="btn btn--primary"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
        >
          {mutation.isPending ? (
            <Loader2 size={14} className="spin" />
          ) : (
            <DollarSign size={14} />
          )}{" "}
          Calculate estimate
        </button>
        <span className="muted" style={{ fontSize: 12 }}>
          USD, Pay-As-You-Go retail pricing in{" "}
          <code className="code-val">koreacentral</code>.
        </span>
      </div>

      {est && (
        <div className="metric-grid" style={{ marginTop: 16 }}>
          <StatBox label="Compute" value={`$${est.compute_usd}`} />
          <StatBox label="Disk" value={`$${est.disk_usd}`} />
          <StatBox label="Storage" value={`$${est.storage_usd}`} />
          <StatBox label="Total" value={`$${est.total_usd}`} accent />
        </div>
      )}
    </section>
  );
}
