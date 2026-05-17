import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, Copy, Loader2, Scissors } from "lucide-react";

import { preprocessApi } from "@/api/endpoints";
import { ExamplePicker } from "@/components/ExamplePicker";
import {
  PREPROCESS_EXAMPLES,
  type PreprocessExampleValues,
} from "@/data/labToolExamples";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";
import {
  NotImplementedBanner,
  SectionHeader,
  StatBox,
} from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function PreprocessorTab({ meta }: { meta: TabMeta }) {
  const [inputData, setInputData] = useState("");
  const [format, setFormat] = useState<"auto" | "fastq" | "fasta">("auto");
  const [minLength, setMinLength] = useState(0);
  const [minQuality, setMinQuality] = useState(0);
  const { copied, copyText } = useClipboardFeedback();

  const mutation = useMutation({
    mutationFn: () =>
      preprocessApi.process({
        input_data: inputData,
        format,
        min_length: minLength,
        min_quality: minQuality,
      }),
  });

  const stats = mutation.data?.stats;

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Scissors size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
      />
      <NotImplementedBanner feature="Preprocessor" />

      <ExamplePicker<PreprocessExampleValues>
        examples={PREPROCESS_EXAMPLES}
        onSelect={(v) => {
          setInputData(v.inputData);
          setFormat(v.format);
          setMinLength(v.minLength);
          setMinQuality(v.minQuality);
        }}
      />

      <div className="form-row" style={{ marginBottom: 16 }}>
        <label className="form-label">Input sequences (FASTA or FASTQ)</label>
        <textarea
          className="form-input blast-textarea"
          rows={8}
          value={inputData}
          onChange={(e) => setInputData(e.target.value)}
          placeholder="Paste FASTA (>header...) or FASTQ (@header...) sequences"
        />
      </div>

      <div className="form-grid form-grid--cols-3" style={{ marginBottom: 16 }}>
        <div className="form-row">
          <label className="form-label">Format</label>
          <select
            className="form-input"
            value={format}
            onChange={(e) => setFormat(e.target.value as typeof format)}
          >
            <option value="auto">Auto-detect</option>
            <option value="fasta">FASTA</option>
            <option value="fastq">FASTQ</option>
          </select>
        </div>
        <div className="form-row">
          <label className="form-label">Min length</label>
          <input
            className="form-input"
            type="number"
            min={0}
            value={minLength}
            onChange={(e) => setMinLength(+e.target.value)}
          />
        </div>
        <div className="form-row">
          <label className="form-label">Min quality (FASTQ)</label>
          <input
            className="form-input"
            type="number"
            min={0}
            max={40}
            value={minQuality}
            onChange={(e) => setMinQuality(+e.target.value)}
          />
        </div>
      </div>

      <button
        className="btn btn--primary"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !inputData.trim()}
      >
        {mutation.isPending ? (
          <Loader2 size={14} className="spin" />
        ) : (
          <Scissors size={14} />
        )}{" "}
        Process
      </button>

      {stats && (
        <div style={{ marginTop: 20 }}>
          <div className="metric-grid">
            <StatBox label="Input seqs" value={stats.input_sequences} />
            <StatBox label="Output seqs" value={stats.output_sequences} />
            <StatBox label="Total bases" value={stats.total_bases.toLocaleString()} />
            <StatBox label="Avg length" value={stats.avg_length} />
            <StatBox label="GC %" value={`${stats.gc_content}%`} />
            <StatBox
              label="Filtered"
              value={stats.filtered_short + stats.filtered_quality}
            />
          </div>

          {mutation.data?.fasta_output && (
            <div style={{ marginTop: 16 }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 6,
                }}
              >
                <label className="form-label" style={{ margin: 0 }}>
                  Output FASTA
                </label>
                <button
                  className={`copy-btn${copied === "fasta" ? " copy-btn--copied" : ""}`}
                  onClick={() => copyText(mutation.data!.fasta_output, "fasta")}
                >
                  {copied === "fasta" ? <Check size={12} /> : <Copy size={12} />} {" "}
                  {copied === "fasta" ? "Copied" : "Copy"}
                </button>
              </div>
              <textarea
                className="form-input blast-textarea"
                rows={6}
                readOnly
                value={mutation.data.fasta_output}
                style={{ width: "100%" }}
              />
            </div>
          )}
        </div>
      )}
    </section>
  );
}
