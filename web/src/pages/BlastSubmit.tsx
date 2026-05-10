import { useState, useEffect } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Play, Upload, ChevronDown, ChevronUp, Loader2, Server, HelpCircle, RotateCcw, X, Dna } from "lucide-react";
import { useNavigate, Link } from "react-router-dom";
import { useToast } from "@/components/Toast";

import {
  type BlastSubmitRequest,
  type BlastProgram,
  type AksClusterSummary,
  blastApi,
  monitoringApi,
} from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { MAX_UPLOAD_BYTES } from "@/constants";

// ---------------------------------------------------------------------------
// Program metadata (NCBI-style)
// ---------------------------------------------------------------------------
const PROGRAMS: {
  value: BlastProgram;
  label: string;
  desc: string;
  longDesc: string;
  dbType: "nucl" | "prot";
  defaultWordSize: number;
}[] = [
  { value: "blastn", label: "blastn", desc: "Nucleotide → Nucleotide", longDesc: "Search nucleotide databases using a nucleotide query.", dbType: "nucl", defaultWordSize: 28 },
  { value: "blastp", label: "blastp", desc: "Protein → Protein", longDesc: "Search protein databases using a protein query.", dbType: "prot", defaultWordSize: 6 },
  { value: "blastx", label: "blastx", desc: "Translated Nucleotide → Protein", longDesc: "Search protein databases using a translated nucleotide query.", dbType: "prot", defaultWordSize: 6 },
  { value: "tblastn", label: "tblastn", desc: "Protein → Translated Nucleotide", longDesc: "Search translated nucleotide databases using a protein query.", dbType: "nucl", defaultWordSize: 6 },
  { value: "tblastx", label: "tblastx", desc: "Translated Nucl. → Translated Nucl.", longDesc: "Search translated nucleotide databases using a translated nucleotide query.", dbType: "nucl", defaultWordSize: 3 },
];

// #32: Parameter presets
const PRESETS: { label: string; desc: string; evalue: number; max_target_seqs: number }[] = [
  { label: "Quick scan", desc: "Fast, fewer results", evalue: 10, max_target_seqs: 50 },
  { label: "Standard", desc: "Balanced (default)", evalue: 0.05, max_target_seqs: 100 },
  { label: "Thorough", desc: "Low E-value, more targets", evalue: 1e-5, max_target_seqs: 500 },
  { label: "Publication", desc: "Stringent parameters", evalue: 1e-10, max_target_seqs: 1000 },
];

// #31: Human-friendly database descriptions
const DB_DESCRIPTIONS: Record<string, { label: string; size: string; type: "nucl" | "prot" }> = {
  "core_nt": { label: "Core Nucleotide", size: "~250 GB", type: "nucl" },
  "nt": { label: "Nucleotide collection", size: "~400 GB", type: "nucl" },
  "nr": { label: "Non-redundant protein", size: "~300 GB", type: "prot" },
  "swissprot": { label: "SwissProt", size: "~300 MB", type: "prot" },
  "pdbnt": { label: "PDB nucleotide", size: "~200 MB", type: "nucl" },
  "refseq_protein": { label: "RefSeq protein", size: "~100 GB", type: "prot" },
  "16S_ribosomal_RNA": { label: "16S ribosomal RNA", size: "~18 MB", type: "nucl" },
  "ITS_RefSeq_Fungi": { label: "ITS RefSeq Fungi", size: "~8 MB", type: "nucl" },
};

const EXAMPLE_FASTA = `>example_query Human insulin mRNA
ATGGCCCTGTGGATGCGCCTCCTGCCCCTGCTGGCGCTGCTGGCCCTCTGGGGACCTGAC
CCAGCCGCAGCCTTTGTGAACCAACACCTGTGCGGCTCACACCTGGTGGAAGCTCTCTAC
CTAGTGTGCGGGGAACGAGGCTTCTTCTACACACCCAAGACCCGCCGGGAGGCAGAGGAC
CTGCAGGTGGGGCAGGTGGAGCTGGGCGGGGGCCCTGGTGCAGGCAGCCTGCAGCCCTTG
GCCCTGGAGGGGTCCCTGCAGAAGCGTGGCATTGTGGAACAATGCTGTACCAGCATCTGC
TCCCTCTACCAGCTGGAGAACTACTGCAACTAGACGCAGCCCGCAGGCAGCCCCACACCCG
CCGCCTCCTGCACCGAGAGAGATGGAATAAAGCCCTTGAACCAGC`;

interface FormState {
  program: BlastProgram;
  db: string;
  query_data: string;
  query_from: string;
  query_to: string;
  job_title: string;
  evalue: number;
  max_target_seqs: number;
  outfmt: number;
  word_size: string;
  gap_open: string;
  gap_extend: string;
  match_score: string;
  mismatch_score: string;
  low_complexity_filter: boolean;
  additional_options: string;
  selectedCluster: string;
}

const INITIAL: FormState = {
  program: "blastn",
  db: "",
  query_data: "",
  query_from: "",
  query_to: "",
  job_title: "",
  evalue: 0.05,
  max_target_seqs: 100,
  outfmt: 7,
  word_size: "",
  gap_open: "",
  gap_extend: "",
  match_score: "",
  mismatch_score: "",
  low_complexity_filter: true,
  additional_options: "",
  selectedCluster: "",
};

function Tip({ text }: { text: string }) {
  return (
    <span title={text} style={{ cursor: "help", marginLeft: 4, color: "var(--text-faint)", verticalAlign: "middle" }}>
      <HelpCircle size={12} strokeWidth={1.5} />
    </span>
  );
}

export function BlastSubmit() {
  // #34 Form auto-save: restore draft from sessionStorage
  const [form, setForm] = useState<FormState>(() => {
    try {
      const saved = sessionStorage.getItem("elb-blast-draft");
      if (saved) return { ...INITIAL, ...JSON.parse(saved) };
    } catch { /* ignore */ }
    return INITIAL;
  });
  const [showParams, setShowParams] = useState(false);
  const navigate = useNavigate();
  const { toast } = useToast();

  // #34 Auto-save draft on change
  useEffect(() => {
    sessionStorage.setItem("elb-blast-draft", JSON.stringify(form));
  }, [form]);

  const [config] = useState(() => loadSavedConfig());
  const subId = config?.subscriptionId ?? "";
  const workloadRg = config?.workloadResourceGroup ?? "";
  const storageAccount = config?.storageAccountName ?? "";
  const acrRg = config?.acrResourceGroup ?? "";
  const acrName = config?.acrName ?? "";
  const terminalRg = config?.terminalResourceGroup ?? "rg-elb-terminal";
  const terminalVm = config?.terminalVmName ?? "vm-elb-terminal";
  const region = config?.region ?? "koreacentral";

  const programMeta = PROGRAMS.find((p) => p.value === form.program) ?? PROGRAMS[0];

  const clusterQuery = useQuery({
    queryKey: ["aks-clusters", subId, workloadRg],
    queryFn: () => monitoringApi.aks(subId, workloadRg),
    enabled: Boolean(subId && workloadRg),
    refetchInterval: 30_000,
  });

  const clusters = clusterQuery.data?.clusters ?? [];
  const selectedCluster: AksClusterSummary | undefined = clusters.find(
    (c) => c.name === form.selectedCluster,
  );

  useEffect(() => {
    if (!form.selectedCluster && clusters.length > 0) {
      const running = clusters.find((c) => c.power_state === "Running");
      setForm((f) => ({ ...f, selectedCluster: running?.name ?? clusters[0].name }));
    }
  }, [clusters, form.selectedCluster]);

  const dbQuery = useQuery({
    queryKey: ["blast-databases", subId, storageAccount],
    queryFn: () => blastApi.listDatabases(subId, storageAccount, workloadRg),
    enabled: Boolean(subId && storageAccount && workloadRg),
  });

  // Check Remote Terminal VM status (needed for elastic-blast CLI)
  const vmQuery = useQuery({
    queryKey: ["terminal-vm", subId, terminalRg, terminalVm],
    queryFn: () => monitoringApi.terminal(subId, terminalRg, terminalVm),
    enabled: Boolean(subId && terminalRg && terminalVm),
    staleTime: 30_000,
  });
  const vmRunning = vmQuery.data?.power_state?.toLowerCase().includes("running") ?? false;

  const submitMutation = useMutation({
    mutationFn: (req: BlastSubmitRequest) => blastApi.submit(req),
    onSuccess: (resp) => {
      // Clear draft on successful submission
      sessionStorage.removeItem("elb-blast-draft");
      toast("BLAST search submitted! Tracking your job…", "success");
      const jobId = resp?.job_id || resp?.instance_id;
      if (jobId) navigate(`/blast/jobs/${encodeURIComponent(jobId)}`);
      else navigate("/blast/jobs");
    },
    onError: (err: Error) => {
      // #45: Parse common errors into user-friendly messages
      const msg = err.message.toLowerCase();
      let friendly = err.message;
      if (msg.includes("storage") && msg.includes("not found")) friendly = "Storage account not found. Set it up on the Dashboard first.";
      else if (msg.includes("cluster") && msg.includes("not found")) friendly = "AKS cluster not found. Create one on the Dashboard.";
      else if (msg.includes("unauthorized") || msg.includes("403")) friendly = "Permission denied. Check your Azure RBAC permissions.";
      else if (msg.includes("quota")) friendly = "Azure quota exceeded. Try a smaller cluster or different region.";
      toast(`Submission failed: ${friendly}`, "error");
    },
  });

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const handleSubmit = () => {
    if (!selectedCluster) return;
    // Refresh VM status before submitting to ensure it's still running
    vmQuery.refetch();
    let opts = form.additional_options || "";
    if (form.low_complexity_filter && form.program === "blastn" && !opts.includes("-dust")) opts += " -dust yes";
    if (form.query_from && form.query_to) opts += ` -query_loc ${form.query_from}-${form.query_to}`;
    if (form.match_score) opts += ` -reward ${form.match_score}`;
    if (form.mismatch_score) opts += ` -penalty ${form.mismatch_score}`;

    submitMutation.mutate({
      subscription_id: subId,
      resource_group: workloadRg,
      region: selectedCluster.region || region,
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
      additional_options: opts.trim() || undefined,
      machine_type: selectedCluster.node_sku || "Standard_E16s_v5",
      num_nodes: selectedCluster.node_count || 3,
      pd_size: "3000Gi",
      mem_request: "16Gi",
      mem_limit: "32Gi",
      acr_resource_group: acrRg || undefined,
      acr_name: acrName || undefined,
      storage_account: storageAccount,
      terminal_resource_group: terminalRg,
      terminal_vm_name: terminalVm,
    });
  };

  const canSubmit = subId && workloadRg && form.program && form.db && form.query_data
    && storageAccount && selectedCluster && selectedCluster.power_state === "Running"
    && !submitMutation.isPending;

  const missing: { text: string; link?: string }[] = [];
  if (!subId || !workloadRg) missing.push({ text: "Azure resources not configured", link: "/" });
  if (!form.query_data) missing.push({ text: "Query sequence" });
  else if (!form.query_data.trim().startsWith(">")) missing.push({ text: "Query must be in FASTA format (start with '>')" });
  if (!form.db) missing.push({ text: "Database" });
  if (!storageAccount) missing.push({ text: "Storage account", link: "/" });
  if (!selectedCluster) missing.push({ text: "AKS cluster — create one on the Dashboard", link: "/" });
  else if (selectedCluster.power_state !== "Running") missing.push({ text: "AKS cluster must be running" });
  if (!vmRunning) missing.push({ text: "Remote Terminal VM must be running — go to Terminal page", link: "/terminal" });

  const isNuclDb = form.db && /\b(nt|core_nt)\b/.test(form.db);
  const isProtDb = form.db && /\b(nr|swissprot|refseq_protein|pdb)\b/.test(form.db);
  const dbWarning =
    (programMeta.dbType === "prot" && isNuclDb) ? `${form.program} expects a protein database, but "${form.db.split("/").pop()}" appears to be nucleotide.` :
    (programMeta.dbType === "nucl" && isProtDb) ? `${form.program} expects a nucleotide database, but "${form.db.split("/").pop()}" appears to be protein.` :
    null;

  const paramsSummary = `E-value: ${form.evalue} · Max: ${form.max_target_seqs} · Fmt: ${form.outfmt}`;
  const searchSummary = form.db ? `Search ${form.db.split("/").pop() || form.db} using ${form.program}` : "";

  return (
    <div className="page-stack">
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h1 style={{ margin: 0 }}>BLAST Search</h1>
          <p className="muted" style={{ marginTop: "var(--space-2)" }}>
            Submit a sequence search using ElasticBLAST on AKS.
          </p>
        </div>
        <button className="glass-button" onClick={() => setForm(INITIAL)} style={{ fontSize: 11 }}>
          <RotateCcw size={12} strokeWidth={1.5} /> Reset
        </button>
      </header>

      {/* Program Selection — Tab-style (#1, #14) */}
      <section className="glass-card">
        <div style={{ display: "flex", gap: 0, borderBottom: "1px solid var(--border-weak)", marginBottom: "var(--space-3)" }}>
          {PROGRAMS.map((p) => (
            <button
              key={p.value}
              onClick={() => set("program", p.value)}
              style={{
                flex: 1, padding: "10px 8px",
                background: form.program === p.value ? "rgba(110,159,255,0.1)" : "transparent",
                border: "none",
                borderBottom: form.program === p.value ? "2px solid var(--accent)" : "2px solid transparent",
                color: form.program === p.value ? "var(--accent)" : "var(--text-muted)",
                cursor: "pointer", fontSize: 13,
                fontWeight: form.program === p.value ? 600 : 400,
                transition: "all 0.15s",
              }}
            >
              {p.label}
            </button>
          ))}
        </div>
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>{programMeta.longDesc}</p>
      </section>

      {/* Query Input (#2, #3, #11) */}
      <section className="glass-card glass-card--strong">
        <h3 style={{ marginTop: 0 }}>
          Enter Query Sequence
          <Tip text="Enter accession number(s), gi(s), or FASTA sequence(s)." />
        </h3>
        <textarea
          className="glass-input" rows={8} value={form.query_data}
          onChange={(e) => set("query_data", e.target.value)}
          placeholder={">sequence_id description\nATCGATCG..."}
          spellCheck={false}
          style={{ fontFamily: "monospace", fontSize: 13, resize: "vertical" }}
        />
        <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-3)", alignItems: "center", flexWrap: "wrap" }}>
          <label className="glass-button" style={{ cursor: "pointer", fontSize: 11 }}>
            <Upload size={12} strokeWidth={1.5} /> Upload
            <input type="file" accept=".fa,.fasta,.fna,.faa" style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (file.size > MAX_UPLOAD_BYTES) { toast(`File too large. Max ${MAX_UPLOAD_BYTES / 1024 / 1024} MB.`, "error"); return; }
                const reader = new FileReader();
                reader.onload = () => { if (typeof reader.result === "string") set("query_data", reader.result); };
                reader.readAsText(file);
              }}
            />
          </label>
          <button className="glass-button" onClick={() => {
            set("query_data", EXAMPLE_FASTA);
            set("program", "blastn");
            toast("Example loaded — Human insulin mRNA (nucleotide sequence)", "info");
          }} type="button" style={{ fontSize: 11 }}>
            <Dna size={12} /> Load example
          </button>
          {form.query_data && (
            <button className="glass-button" onClick={() => set("query_data", "")} type="button" style={{ fontSize: 11 }}>
              <X size={12} strokeWidth={1.5} /> Clear
            </button>
          )}
          {form.query_data && (
            <span className="muted" style={{ fontSize: 11 }}>
              {form.query_data.split("\n").filter((l) => l.startsWith(">")).length} seq · {form.query_data.length.toLocaleString()} chars
            </span>
          )}
        </div>

        {/* Query Subrange (#3) */}
        <div style={{ display: "flex", gap: "var(--space-3)", marginTop: "var(--space-3)", alignItems: "center" }}>
          <span className="glass-label" style={{ fontSize: 11, minWidth: "fit-content", marginBottom: 0 }}>
            Subrange <Tip text="Restrict search to a range of the query (1-based)." />
          </span>
          <input className="glass-input" value={form.query_from} onChange={(e) => set("query_from", e.target.value)}
            placeholder="From" type="number" min={1} style={{ width: 80, fontSize: 12, padding: "4px 8px" }} />
          <input className="glass-input" value={form.query_to} onChange={(e) => set("query_to", e.target.value)}
            placeholder="To" type="number" min={1} style={{ width: 80, fontSize: 12, padding: "4px 8px" }} />
        </div>

        <label style={{ marginTop: "var(--space-3)", display: "block" }}>
          <span className="glass-label">Job Title</span>
          <input className="glass-input" value={form.job_title} onChange={(e) => set("job_title", e.target.value)}
            placeholder="My BLAST search" maxLength={200} />
        </label>
      </section>

      {/* Choose Search Set (#4, #18) */}
      <section className="glass-card">
        <h3 style={{ marginTop: 0 }}>Choose Search Set</h3>
        <label>
          <span className="glass-label">Database <Tip text="Select a BLAST database from your storage account." /></span>
          {dbQuery.data?.databases && dbQuery.data.databases.length > 0 ? (
            <select className="glass-input" value={form.db} onChange={(e) => set("db", e.target.value)}>
              <option value="">Select a database</option>
              {dbQuery.data.databases.map((d) => {
                const info = DB_DESCRIPTIONS[d.name];
                const label = info ? `${info.label} (${d.name}) — ${info.size}` : d.name;
                return <option key={d.name} value={`${d.container}/${d.name}/${d.name}`}>{label}</option>;
              })}
            </select>
          ) : (
            <input className="glass-input" value={form.db} onChange={(e) => set("db", e.target.value)}
              placeholder="blast-db/core_nt/core_nt" spellCheck={false} />
          )}
        </label>
        {/* #34: Auto-suggest DB based on program */}
        {!form.db && dbQuery.data?.databases && dbQuery.data.databases.length > 0 && (
          <div style={{ marginTop: "var(--space-2)", fontSize: 11, color: "var(--text-muted)" }}>
            Suggested for {form.program}: {programMeta.dbType === "nucl" ? "nucleotide" : "protein"} databases
            {dbQuery.data.databases
              .filter((d) => DB_DESCRIPTIONS[d.name]?.type === programMeta.dbType)
              .slice(0, 3)
              .map((d) => (
                <button key={d.name} className="glass-button" style={{ fontSize: 10, padding: "1px 6px", marginLeft: 4 }}
                  onClick={() => set("db", `${d.container}/${d.name}/${d.name}`)}>
                  {d.name}
                </button>
              ))}
          </div>
        )}
        {/* #51: Warning if DB not in storage */}
        {form.db && dbQuery.data?.databases && !dbQuery.data.databases.some((d) => form.db.includes(d.name)) && (
          <div style={{ marginTop: "var(--space-2)", padding: "8px 12px", background: "rgba(240,198,116,0.06)", border: "1px solid rgba(240,198,116,0.18)", borderRadius: 6, fontSize: 12, color: "var(--warning)" }}>
            This database doesn't appear to be downloaded yet.{" "}
            <Link to="/" style={{ color: "var(--accent)" }}>Download it from the Dashboard</Link>.
          </div>
        )}
        {dbWarning && (
          <div style={{ marginTop: "var(--space-3)", padding: "8px 12px", background: "rgba(240,198,116,0.06)", border: "1px solid rgba(240,198,116,0.18)", borderRadius: 6, fontSize: 12, color: "var(--warning)" }}>
            {dbWarning}
          </div>
        )}
      </section>

      {/* Algorithm Parameters (#6, #7, #8, #10, #15-17) */}
      <section className="glass-card">
        <button onClick={() => setShowParams((v) => !v)}
          style={{ background: "none", border: "none", color: "var(--text-primary)", cursor: "pointer", display: "flex", alignItems: "center", gap: "var(--space-2)", width: "100%", padding: 0 }}>
          <h3 style={{ margin: 0, flex: 1, textAlign: "left" }}>Algorithm Parameters</h3>
          <span className="muted" style={{ fontSize: 11, marginRight: 8 }}>{!showParams && paramsSummary}</span>
          {showParams ? <ChevronUp size={16} strokeWidth={1.5} /> : <ChevronDown size={16} strokeWidth={1.5} />}
        </button>
        {showParams && (
          <div style={{ marginTop: "var(--space-4)" }}>
            {/* #32: Parameter presets */}
            <div style={{ display: "flex", gap: 6, marginBottom: "var(--space-3)", flexWrap: "wrap" }}>
              {PRESETS.map((p) => (
                <button key={p.label} className={`glass-button${form.evalue === p.evalue && form.max_target_seqs === p.max_target_seqs ? " glass-button--primary" : ""}`}
                  style={{ fontSize: 11, padding: "3px 10px" }}
                  onClick={() => { set("evalue", p.evalue); set("max_target_seqs", p.max_target_seqs); }}>
                  {p.label}
                  <span style={{ fontSize: 9, color: "var(--text-faint)", marginLeft: 4 }}>{p.desc}</span>
                </button>
              ))}
            </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "var(--space-4)" }}>
            <label>
              <span className="glass-label">E-value <Tip text="Expected number of chance matches. Lower = more stringent. NCBI default: 0.05" /></span>
              <input className="glass-input" type="number" step="any" value={form.evalue}
                onChange={(e) => set("evalue", parseFloat(e.target.value) || 0.05)} />
            </label>
            <label>
              <span className="glass-label">Max target seqs <Tip text="Maximum number of aligned sequences to keep. NCBI default: 100" /></span>
              <input className="glass-input" type="number" value={form.max_target_seqs}
                onChange={(e) => set("max_target_seqs", parseInt(e.target.value, 10) || 100)} />
            </label>
            <label>
              <span className="glass-label">Word size <Tip text="Length of initial exact match. blastn: 28 (megablast), blastp: 6" /></span>
              <input className="glass-input" type="number" value={form.word_size}
                onChange={(e) => set("word_size", e.target.value)} placeholder={String(programMeta.defaultWordSize)} />
            </label>
            <label>
              <span className="glass-label">Output format</span>
              <select className="glass-input" value={form.outfmt} onChange={(e) => set("outfmt", parseInt(e.target.value, 10))}>
                <option value={7}>7 — Tabular + comments</option>
                <option value={6}>6 — Tabular</option>
                <option value={0}>0 — Pairwise text</option>
                <option value={11}>11 — ASN.1 (archive)</option>
              </select>
            </label>
            {form.program === "blastn" && (<>
              <label>
                <span className="glass-label">Match score <Tip text="Reward for a nucleotide match. Default: 1" /></span>
                <input className="glass-input" type="number" value={form.match_score}
                  onChange={(e) => set("match_score", e.target.value)} placeholder="1" />
              </label>
              <label>
                <span className="glass-label">Mismatch score <Tip text="Penalty for a mismatch. Default: -2" /></span>
                <input className="glass-input" type="number" value={form.mismatch_score}
                  onChange={(e) => set("mismatch_score", e.target.value)} placeholder="-2" />
              </label>
            </>)}
            <label>
              <span className="glass-label">Gap open <Tip text="Cost to open a gap." /></span>
              <input className="glass-input" type="number" value={form.gap_open}
                onChange={(e) => set("gap_open", e.target.value)} placeholder="Auto" />
            </label>
            <label>
              <span className="glass-label">Gap extend <Tip text="Cost to extend a gap." /></span>
              <input className="glass-input" type="number" value={form.gap_extend}
                onChange={(e) => set("gap_extend", e.target.value)} placeholder="Auto" />
            </label>
            <div style={{ gridColumn: "1 / -1", display: "flex", gap: "var(--space-4)", alignItems: "center", flexWrap: "wrap" }}>
              <span className="glass-label" style={{ marginBottom: 0 }}>Filters:</span>
              <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12 }}>
                <input type="checkbox" checked={form.low_complexity_filter}
                  onChange={(e) => set("low_complexity_filter", e.target.checked)} />
                Low complexity filter <Tip text="Mask low-complexity regions (DUST for nucleotide, SEG for protein)." />
              </label>
            </div>
            <label style={{ gridColumn: "1 / -1" }}>
              <span className="glass-label">Additional options <Tip text="Extra command-line flags for BLAST." /></span>
              <input className="glass-input" value={form.additional_options}
                onChange={(e) => set("additional_options", e.target.value)}
                placeholder="-max_hsps 1 -num_threads 4" spellCheck={false} />
            </label>
          </div>
          </div>
        )}
      </section>

      {/* AKS Cluster */}
      <section className="glass-card">
        <h3 style={{ marginTop: 0 }}>
          <Server size={16} strokeWidth={1.5} style={{ verticalAlign: "middle", marginRight: 6 }} /> AKS Cluster
        </h3>
        {!subId && <div className="muted">Configure your Azure resources on the Dashboard first.</div>}
        {subId && clusterQuery.isLoading && (
          <div className="muted"><Loader2 size={12} className="spin" style={{ display: "inline", verticalAlign: "middle" }} /> Loading clusters...</div>
        )}
        {subId && clusters.length === 0 && !clusterQuery.isLoading && (
          <div className="muted">No AKS clusters in <strong>{workloadRg}</strong>. Create one on the Dashboard.</div>
        )}
        {clusters.length > 0 && (<>
          <select className="glass-input" value={form.selectedCluster}
            onChange={(e) => set("selectedCluster", e.target.value)} style={{ marginBottom: "var(--space-3)" }}>
            <option value="">Select cluster</option>
            {clusters.map((c) => (
              <option key={c.name} value={c.name}>{c.name} — {c.region} ({c.power_state ?? "?"})</option>
            ))}
          </select>
          {selectedCluster && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: "var(--space-2)", padding: "var(--space-3)", background: "var(--glass-bg)", borderRadius: 8, border: "1px solid var(--glass-border)", fontSize: 12 }}>
              {([
                ["Status", selectedCluster.power_state, selectedCluster.power_state === "Running" ? "var(--success)" : "var(--warning)"],
                ["State", selectedCluster.provisioning_state, undefined],
                ["SKU", selectedCluster.node_sku, undefined],
                ["Nodes", selectedCluster.node_count, undefined],
                ["K8s", selectedCluster.k8s_version, undefined],
                ["Region", selectedCluster.region, undefined],
              ] as [string, unknown, string | undefined][]).map(([label, val, color]) => (
                <div key={label}>
                  <div className="muted" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.5px" }}>{label}</div>
                  <div style={{ fontWeight: color ? 600 : 400, color }}>{String(val ?? "?")}</div>
                </div>
              ))}
            </div>
          )}
        </>)}
      </section>

      {/* Submit (#9, #13) */}
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
        {missing.length > 0 && !submitMutation.isPending && (
          <div style={{ padding: "10px 14px", background: "rgba(240,198,116,0.06)", border: "1px solid rgba(240,198,116,0.18)", borderRadius: 8, fontSize: 12, color: "var(--warning)" }}>
            <strong>Required before submitting:</strong>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18, lineHeight: 1.8 }}>
              {missing.map((m) => (
                <li key={m.text}>
                  {m.text}
                  {m.link && <Link to={m.link} style={{ marginLeft: 6, color: "var(--accent)", fontSize: 11 }}>Go to Dashboard →</Link>}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
          <button className="glass-button glass-button--primary" onClick={handleSubmit}
            disabled={!canSubmit} style={{ padding: "12px 24px", fontSize: 15 }}>
            {submitMutation.isPending ? <Loader2 size={18} strokeWidth={1.5} className="spin" /> : <Play size={18} strokeWidth={1.5} />}
            BLAST
          </button>
          {searchSummary && <span className="muted" style={{ fontSize: 12 }}>{searchSummary}</span>}
          {submitMutation.isError && (
            <span className="muted" style={{ color: "var(--danger)" }}>{(submitMutation.error as Error).message}</span>
          )}
        </div>
      </div>
    </div>
  );
}
