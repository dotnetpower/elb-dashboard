import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Play, Upload, ChevronDown, ChevronUp, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";

import {
  type BlastSubmitRequest,
  type BlastProgram,
  blastApi,
} from "@/api/endpoints";
import { SubscriptionPicker } from "@/components/SubscriptionPicker";
import { MAX_UPLOAD_BYTES } from "@/constants";

const PROGRAMS: { value: BlastProgram; label: string; desc: string }[] = [
  { value: "blastn", label: "blastn", desc: "Nucleotide → Nucleotide" },
  { value: "blastp", label: "blastp", desc: "Protein → Protein" },
  { value: "blastx", label: "blastx", desc: "Translated Nucleotide → Protein" },
  { value: "tblastn", label: "tblastn", desc: "Protein → Translated Nucleotide" },
  { value: "tblastx", label: "tblastx", desc: "Translated Nucl. → Translated Nucl." },
];

const EXAMPLE_FASTA = `>example_query Human insulin mRNA
ATGGCCCTGTGGATGCGCCTCCTGCCCCTGCTGGCGCTGCTGGCCCTCTGGGGACCTGAC
CCAGCCGCAGCCTTTGTGAACCAACACCTGTGCGGCTCACACCTGGTGGAAGCTCTCTAC
CTAGTGTGCGGGGAACGAGGCTTCTTCTACACACCCAAGACCCGCCGGGAGGCAGAGGAC
CTGCAGGTGGGGCAGGTGGAGCTGGGCGGGGGCCCTGGTGCAGGCAGCCTGCAGCCCTTG
GCCCTGGAGGGGTCCCTGCAGAAGCGTGGCATTGTGGAACAATGCTGTACCAGCATCTGC
TCCCTCTACCAGCTGGAGAACTACTGCAACTAGACGCAGCCCGCAGGCAGCCCCACACCCG
CCGCCTCCTGCACCGAGAGAGATGGAATAAAGCCCTTGAACCAGC`;

interface FormState {
  subscription_id: string;
  resource_group: string;
  region: string;
  program: BlastProgram;
  db: string;
  query_data: string;
  job_title: string;
  evalue: number;
  max_target_seqs: number;
  outfmt: number;
  word_size: string;
  gap_open: string;
  gap_extend: string;
  additional_options: string;
  machine_type: string;
  num_nodes: number;
  pd_size: string;
  mem_request: string;
  mem_limit: string;
  acr_resource_group: string;
  acr_name: string;
  storage_account: string;
  terminal_resource_group: string;
  terminal_vm_name: string;
}

const INITIAL: FormState = {
  subscription_id: "",
  resource_group: "rg-elb",
  region: "koreacentral",
  program: "blastn",
  db: "",
  query_data: "",
  job_title: "",
  evalue: 10,
  max_target_seqs: 500,
  outfmt: 7,
  word_size: "",
  gap_open: "",
  gap_extend: "",
  additional_options: "",
  machine_type: "Standard_D8s_v3",
  num_nodes: 1,
  pd_size: "3000Gi",
  mem_request: "16Gi",
  mem_limit: "32Gi",
  acr_resource_group: "",
  acr_name: "",
  storage_account: "",
  terminal_resource_group: "rg-elb-terminal",
  terminal_vm_name: "vm-elb-terminal",
};

export function BlastSubmit() {
  const [form, setForm] = useState<FormState>(INITIAL);
  const [showParams, setShowParams] = useState(false);
  const [showCluster, setShowCluster] = useState(false);
  const navigate = useNavigate();

  const dbQuery = useQuery({
    queryKey: ["blast-databases", form.subscription_id, form.storage_account],
    queryFn: () => blastApi.listDatabases(form.subscription_id, form.storage_account),
    enabled: Boolean(form.subscription_id && form.storage_account),
  });

  const submitMutation = useMutation({
    mutationFn: (req: BlastSubmitRequest) => blastApi.submit(req),
    onSuccess: () => {
      navigate("/blast/jobs");
    },
  });

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const handleSubmit = () => {
    const req: BlastSubmitRequest = {
      subscription_id: form.subscription_id,
      resource_group: form.resource_group,
      region: form.region,
      program: form.program,
      db: form.db,
      query_data: form.query_data || undefined,
      job_title: form.job_title || undefined,
      evalue: form.evalue,
      max_target_seqs: form.max_target_seqs,
      outfmt: form.outfmt,
      word_size: form.word_size ? parseInt(form.word_size, 10) : undefined,
      gap_open: form.gap_open ? parseInt(form.gap_open, 10) : undefined,
      gap_extend: form.gap_extend ? parseInt(form.gap_extend, 10) : undefined,
      additional_options: form.additional_options || undefined,
      machine_type: form.machine_type,
      num_nodes: form.num_nodes,
      pd_size: form.pd_size,
      mem_request: form.mem_request,
      mem_limit: form.mem_limit,
      acr_resource_group: form.acr_resource_group || undefined,
      acr_name: form.acr_name || undefined,
      storage_account: form.storage_account,
      terminal_resource_group: form.terminal_resource_group,
      terminal_vm_name: form.terminal_vm_name,
    };
    submitMutation.mutate(req);
  };

  const canSubmit =
    form.subscription_id &&
    form.resource_group &&
    form.program &&
    form.db &&
    form.query_data &&
    form.storage_account &&
    !submitMutation.isPending;

  return (
    <div className="page-stack">
      <header>
        <h1 style={{ margin: 0 }}>BLAST Search</h1>
        <p className="muted" style={{ marginTop: "var(--space-2)" }}>
          Submit a sequence search using ElasticBLAST on AKS.
        </p>
      </header>

      {/* Query Input */}
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>Enter Query Sequence</h3>
        <p className="muted" style={{ fontSize: 12, marginBottom: "var(--space-3)" }}>
          Paste FASTA sequence(s) or upload a file (.fa, .fasta, .fna, .faa).
        </p>
        <textarea
          className="glass-input"
          rows={10}
          value={form.query_data}
          onChange={(e) => set("query_data", e.target.value)}
          placeholder=">sequence_id description&#10;ATCGATCG..."
          spellCheck={false}
          style={{ fontFamily: "monospace", fontSize: 13, resize: "vertical" }}
        />
        <div
          style={{
            display: "flex",
            gap: "var(--space-3)",
            marginTop: "var(--space-3)",
            alignItems: "center",
          }}
        >
          <label className="glass-button" style={{ cursor: "pointer" }}>
            <Upload size={14} strokeWidth={1.5} /> Upload file
            <input
              type="file"
              accept=".fa,.fasta,.fna,.faa,.fa.gz"
              style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (file.size > MAX_UPLOAD_BYTES) {
                  alert(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max ${MAX_UPLOAD_BYTES / 1024 / 1024} MB.`);
                  return;
                }
                const reader = new FileReader();
                reader.onload = () => {
                  if (typeof reader.result === "string") {
                    set("query_data", reader.result);
                  }
                };
                reader.readAsText(file);
              }}
            />
          </label>
          <button
            className="glass-button"
            onClick={() => set("query_data", EXAMPLE_FASTA)}
            type="button"
          >
            Load example
          </button>
          {form.query_data && (
            <span className="muted" style={{ fontSize: 12 }}>
              {form.query_data.split("\n").filter((l) => l.startsWith(">")).length} sequence(s),{" "}
              {form.query_data.length.toLocaleString()} chars
            </span>
          )}
        </div>

        <label style={{ marginTop: "var(--space-4)", display: "block" }}>
          <span className="glass-label">Job Title</span>
          <input
            className="glass-input"
            value={form.job_title}
            onChange={(e) => set("job_title", e.target.value)}
            placeholder="My BLAST search"
            maxLength={200}
          />
        </label>
      </section>

      {/* Program & Database */}
      <section className="glass-card">
        <h3 style={{ marginTop: 0 }}>Search Set</h3>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: "var(--space-4)",
          }}
        >
          <label>
            <span className="glass-label">Program</span>
            <select
              className="glass-input"
              value={form.program}
              onChange={(e) => set("program", e.target.value as BlastProgram)}
            >
              {PROGRAMS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label} — {p.desc}
                </option>
              ))}
            </select>
          </label>

          <label>
            <span className="glass-label">Database</span>
            {dbQuery.data?.databases && dbQuery.data.databases.length > 0 ? (
              <select
                className="glass-input"
                value={form.db}
                onChange={(e) => set("db", e.target.value)}
              >
                <option value="">Select a database</option>
                {dbQuery.data.databases.map((d) => (
                  <option key={d.name} value={`${d.container}/${d.name}/${d.name}`}>
                    {d.name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                className="glass-input"
                value={form.db}
                onChange={(e) => set("db", e.target.value)}
                placeholder="blast-db/pdbnt/pdbnt"
                spellCheck={false}
              />
            )}
          </label>
        </div>
      </section>

      {/* Algorithm Parameters (collapsible) */}
      <section className="glass-card">
        <button
          onClick={() => setShowParams((v) => !v)}
          style={{
            background: "none",
            border: "none",
            color: "var(--text-primary)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: "var(--space-2)",
            width: "100%",
            padding: 0,
          }}
        >
          <h3 style={{ margin: 0, flex: 1, textAlign: "left" }}>Algorithm Parameters</h3>
          {showParams ? (
            <ChevronUp size={16} strokeWidth={1.5} />
          ) : (
            <ChevronDown size={16} strokeWidth={1.5} />
          )}
        </button>
        {showParams && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
              gap: "var(--space-4)",
              marginTop: "var(--space-4)",
            }}
          >
            <label>
              <span className="glass-label">E-value</span>
              <input
                className="glass-input"
                type="number"
                step="any"
                value={form.evalue}
                onChange={(e) => set("evalue", parseFloat(e.target.value) || 10)}
              />
            </label>
            <label>
              <span className="glass-label">Max target sequences</span>
              <input
                className="glass-input"
                type="number"
                value={form.max_target_seqs}
                onChange={(e) => set("max_target_seqs", parseInt(e.target.value, 10) || 500)}
              />
            </label>
            <label>
              <span className="glass-label">Output format</span>
              <select
                className="glass-input"
                value={form.outfmt}
                onChange={(e) => set("outfmt", parseInt(e.target.value, 10))}
              >
                <option value={7}>7 — Tabular with comments</option>
                <option value={6}>6 — Tabular</option>
                <option value={0}>0 — Pairwise text</option>
                <option value={11}>11 — ASN.1 (archive)</option>
              </select>
            </label>
            <label>
              <span className="glass-label">Word size</span>
              <input
                className="glass-input"
                type="number"
                value={form.word_size}
                onChange={(e) => set("word_size", e.target.value)}
                placeholder="Auto"
              />
            </label>
            <label>
              <span className="glass-label">Gap open</span>
              <input
                className="glass-input"
                type="number"
                value={form.gap_open}
                onChange={(e) => set("gap_open", e.target.value)}
                placeholder="Auto"
              />
            </label>
            <label>
              <span className="glass-label">Gap extend</span>
              <input
                className="glass-input"
                type="number"
                value={form.gap_extend}
                onChange={(e) => set("gap_extend", e.target.value)}
                placeholder="Auto"
              />
            </label>
            <label style={{ gridColumn: "1 / -1" }}>
              <span className="glass-label">Additional BLAST options</span>
              <input
                className="glass-input"
                value={form.additional_options}
                onChange={(e) => set("additional_options", e.target.value)}
                placeholder="-max_hsps 1 -dust no"
                spellCheck={false}
              />
            </label>
          </div>
        )}
      </section>

      {/* Cluster Configuration (collapsible) */}
      <section className="glass-card">
        <button
          onClick={() => setShowCluster((v) => !v)}
          style={{
            background: "none",
            border: "none",
            color: "var(--text-primary)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: "var(--space-2)",
            width: "100%",
            padding: 0,
          }}
        >
          <h3 style={{ margin: 0, flex: 1, textAlign: "left" }}>Cluster &amp; Azure Resources</h3>
          {showCluster ? (
            <ChevronUp size={16} strokeWidth={1.5} />
          ) : (
            <ChevronDown size={16} strokeWidth={1.5} />
          )}
        </button>
        {showCluster && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: "var(--space-4)",
              marginTop: "var(--space-4)",
            }}
          >
            <SubscriptionPicker
              value={form.subscription_id}
              onChange={(id) => set("subscription_id", id)}
            />
            {(
              [
                ["resource_group", "Resource Group"],
                ["region", "Region"],
                ["storage_account", "Storage Account"],
                ["acr_resource_group", "ACR Resource Group"],
                ["acr_name", "ACR Name"],
                ["machine_type", "Machine Type"],
                ["pd_size", "Persistent Disk Size"],
                ["mem_request", "Memory Request"],
                ["mem_limit", "Memory Limit"],
                ["terminal_resource_group", "Terminal RG"],
                ["terminal_vm_name", "Terminal VM"],
              ] as const
            ).map(([key, label]) => (
              <label key={key}>
                <span className="glass-label">{label}</span>
                <input
                  className="glass-input"
                  value={String(form[key] ?? "")}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  spellCheck={false}
                />
              </label>
            ))}
            <label>
              <span className="glass-label">Num Nodes</span>
              <input
                className="glass-input"
                type="number"
                min={1}
                value={form.num_nodes}
                onChange={(e) => set("num_nodes", parseInt(e.target.value, 10) || 1)}
              />
            </label>
          </div>
        )}
      </section>

      {/* Submit */}
      <div
        style={{
          display: "flex",
          gap: "var(--space-3)",
          alignItems: "center",
        }}
      >
        <button
          className="glass-button glass-button--primary"
          onClick={handleSubmit}
          disabled={!canSubmit}
          style={{ padding: "12px 24px", fontSize: 15 }}
        >
          {submitMutation.isPending ? (
            <Loader2 size={18} strokeWidth={1.5} className="spin" />
          ) : (
            <Play size={18} strokeWidth={1.5} />
          )}
          BLAST
        </button>
        {submitMutation.isError && (
          <span className="muted" style={{ color: "var(--danger)" }}>
            {(submitMutation.error as Error).message}
          </span>
        )}
      </div>
    </div>
  );
}
