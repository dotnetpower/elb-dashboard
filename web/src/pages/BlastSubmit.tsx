import { useState, useEffect, useMemo, useRef } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Play,
  Upload,
  ChevronDown,
  ChevronUp,
  Loader2,
  Server,
  HelpCircle,
  RotateCcw,
  X,
  Dna,
  Database,
  Zap,
  Gauge,
  FlaskConical,
  BookOpen,
  CheckCircle2,
  AlertTriangle,
  ArrowRight,
  Copy,
  Terminal,
  Check,
} from "lucide-react";
import { useNavigate, Link } from "react-router-dom";

import { formatApiError } from "@/api/client";
import {
  type BlastSubmitRequest,
  type AksClusterSummary,
  blastApi,
  monitoringApi,
} from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { MAX_UPLOAD_BYTES } from "@/constants";
import {
  BLASTN_OPTIMIZE,
  buildCommandString,
  DB_DESCRIPTIONS,
  EXAMPLE_FASTA,
  INITIAL,
  PRESETS,
  PROGRAMS,
  type FormState,
} from "@/pages/blastSubmitModel";

function Tip({ text }: { text: string }) {
  return (
    <span
      title={text}
      style={{
        cursor: "help",
        marginLeft: 4,
        color: "var(--text-faint)",
        verticalAlign: "middle",
      }}
    >
      <HelpCircle size={12} strokeWidth={1.5} />
    </span>
  );
}

function SectionHeader({
  step,
  icon,
  title,
  subtitle,
}: {
  step: number;
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="blast-section-hd">
      <span className="blast-step-badge">{step}</span>
      <span className="blast-section-icon">{icon}</span>
      <div>
        <div className="blast-section-title">{title}</div>
        {subtitle && <div className="blast-section-sub">{subtitle}</div>}
      </div>
    </div>
  );
}

function BlastCommandPreview({
  form,
  programMeta,
  toast,
}: {
  form: FormState;
  programMeta: (typeof PROGRAMS)[0];
  toast: (msg: string, type: "info" | "success" | "error") => void;
}) {
  const [copied, setCopied] = useState(false);
  const cmd = buildCommandString(form, programMeta);

  const handleCopy = () => {
    navigator.clipboard.writeText(cmd).then(() => {
      setCopied(true);
      toast("Command copied to clipboard", "info");
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="blast-cmd-preview">
      <div className="blast-cmd-preview__header">
        <Terminal size={13} strokeWidth={1.5} />
        <span>Command Preview</span>
        <button className="blast-cmd-copy" onClick={handleCopy} title="Copy command">
          {copied ? (
            <Check size={12} strokeWidth={2} />
          ) : (
            <Copy size={12} strokeWidth={1.5} />
          )}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <code className="blast-cmd-preview__code">{cmd}</code>
    </div>
  );
}

export function BlastSubmit() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  // #34 Form auto-save: restore draft from sessionStorage
  const [form, setForm] = useState<FormState>(() => {
    try {
      const saved = sessionStorage.getItem("elb-blast-draft");
      if (saved) return { ...INITIAL, ...JSON.parse(saved) };
    } catch {
      /* ignore */
    }
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

  const clusters = useMemo(
    () => clusterQuery.data?.clusters ?? [],
    [clusterQuery.data?.clusters],
  );
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

  // Warmup status — which DBs are already cached on cluster nodes
  const warmupQuery = useQuery({
    queryKey: ["warmup-status-submit", subId, workloadRg, form.selectedCluster],
    queryFn: () => monitoringApi.warmupStatus(subId, workloadRg, form.selectedCluster!),
    enabled: Boolean(subId && workloadRg && form.selectedCluster && selectedCluster?.power_state === "Running"),
    staleTime: 30_000,
  });
  const warmDbs = useMemo(() => {
    const dbs = warmupQuery.data?.databases ?? [];
    return new Map(dbs.filter((d) => d.status === "Ready").map((d) => [d.name, d]));
  }, [warmupQuery.data]);

  // Derive the short DB name from the form.db path (e.g. "blast-db/core_nt" → "core_nt")
  const selectedDbShortName = useMemo(() => {
    const db = form.db;
    if (!db) return "";
    const parts = db.split("/");
    return parts[parts.length - 1];
  }, [form.db]);

  const isDbAlreadyWarm = warmDbs.has(selectedDbShortName);
  const warmDbInfo = warmDbs.get(selectedDbShortName);

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
      const friendly = formatApiError(err, "blast");
      toast(`Submission failed: ${friendly}`, "error");
    },
  });

  // Pre-flight readiness check
  const [preFlightResult, setPreFlightResult] = useState<{
    ready: boolean;
    checks: Array<{
      id: string;
      status: string;
      title: string;
      detail?: string;
      action?: string;
      action_type?: string;
      action_params?: Record<string, string>;
      severity?: string;
      suggested_dbs?: string[];
    }>;
    critical_blockers: number;
    summary: string;
  } | null>(null);

  const preFlightMutation = useMutation({
    mutationFn: () =>
      blastApi.preFlight({
        subscription_id: subId,
        resource_group: workloadRg,
        acr_resource_group: acrRg || undefined,
        acr_name: acrName || undefined,
        storage_account: storageAccount,
        aks_cluster_name: selectedCluster?.name || "",
        terminal_resource_group: terminalRg,
        terminal_vm_name: terminalVm,
        db: form.db,
        query_data: form.query_data || undefined,
      }),
    onSuccess: (result) => {
      setPreFlightResult(result);
      if (result.ready) {
        toast("All pre-flight checks passed", "success");
      }
    },
    onError: (err: Error) => {
      toast(`Pre-flight check failed: ${formatApiError(err, "blast")}`, "error");
    },
  });

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const handleSubmit = () => {
    if (!selectedCluster) return;
    // Refresh VM status before submitting to ensure it's still running
    vmQuery.refetch();
    let opts = form.additional_options || "";
    if (
      form.low_complexity_filter &&
      form.program === "blastn" &&
      !opts.includes("-dust")
    )
      opts += " -dust yes";
    if (form.query_from && form.query_to)
      opts += ` -query_loc ${form.query_from}-${form.query_to}`;
    if (form.match_score) opts += ` -reward ${form.match_score}`;
    if (form.mismatch_score) opts += ` -penalty ${form.mismatch_score}`;

    // Auto-generate job title if not provided
    const dbShort = form.db.split("/").pop() || form.db;
    const autoTitle = form.job_title || `${form.program} · ${dbShort}`;

    submitMutation.mutate({
      subscription_id: subId,
      resource_group: workloadRg,
      region: selectedCluster.region || region,
      program: form.program,
      db: form.db,
      query_data: form.query_data || undefined,
      job_title: autoTitle,
      evalue: form.evalue,
      max_target_seqs: form.max_target_seqs,
      outfmt: form.outfmt,
      word_size: form.word_size ? parseInt(form.word_size, 10) : undefined,
      gap_open: form.gap_open ? parseInt(form.gap_open, 10) : undefined,
      gap_extend: form.gap_extend ? parseInt(form.gap_extend, 10) : undefined,
      additional_options: opts.trim() || undefined,
      machine_type: selectedCluster.node_sku || "Standard_E16s_v5",
      num_nodes: selectedCluster.node_count || 3,
      pd_size: "1000Gi",
      mem_request: "8Gi",
      mem_limit: "24Gi",
      enable_warmup: form.enable_warmup,
      db_auto_partition: form.db_auto_partition,
      acr_resource_group: acrRg || undefined,
      acr_name: acrName || undefined,
      storage_account: storageAccount,
      aks_cluster_name: selectedCluster.name,
      terminal_resource_group: terminalRg,
      terminal_vm_name: terminalVm,
    });
  };

  // Hard guard: if we have a successful database listing AND the user-typed
  // db path doesn't match any returned database, the submission will fail at
  // the warmup step (`elastic-blast prepare` returns ERROR + EXIT_CODE=2).
  // Block submission proactively so the user gets a clear blocker instead of
  // wasting a 5-10min provision cycle.
  // Match by the final path segment (the BLAST db basename) — `includes`
  // on the full path would false-positive when one db name is a substring
  // of another (e.g. "nt" vs "core_nt").
  const knownDbs = dbQuery.data?.databases ?? [];
  const dbListResolved = dbQuery.isSuccess && knownDbs.length > 0;
  const dbBaseName = form.db ? (form.db.split("/").filter(Boolean).pop() ?? "") : "";
  const dbMissingFromStorage =
    Boolean(form.db) && dbListResolved && !knownDbs.some((d) => d.name === dbBaseName);

  const canSubmit =
    subId &&
    workloadRg &&
    form.program &&
    form.db &&
    form.query_data &&
    storageAccount &&
    selectedCluster &&
    selectedCluster.power_state === "Running" &&
    !dbMissingFromStorage &&
    !submitMutation.isPending;

  const missing: { text: string; link?: string }[] = [];
  if (!subId || !workloadRg)
    missing.push({ text: "Azure resources not configured", link: "/" });
  if (!form.query_data) missing.push({ text: "Query sequence" });
  else if (!form.query_data.trim().startsWith(">"))
    missing.push({ text: "Query must be in FASTA format (start with '>')" });
  if (!form.db) missing.push({ text: "Database" });
  else if (dbMissingFromStorage)
    missing.push({
      text: `Database '${form.db.split("/").pop()}' is not in storage — download it from the Dashboard first`,
      link: "/",
    });
  if (!storageAccount) missing.push({ text: "Storage account", link: "/" });
  if (!selectedCluster)
    missing.push({ text: "AKS cluster — create one on the Dashboard", link: "/" });
  else if (selectedCluster.power_state !== "Running")
    missing.push({ text: "AKS cluster must be running" });
  if (!vmRunning)
    missing.push({
      text: "Remote Terminal VM must be running — go to Terminal page",
      link: "/terminal",
    });

  const isNuclDb = form.db && /\b(nt|core_nt)\b/.test(form.db);
  const isProtDb = form.db && /\b(nr|swissprot|refseq_protein|pdb)\b/.test(form.db);
  const dbWarning =
    programMeta.dbType === "prot" && isNuclDb
      ? `${form.program} expects a protein database, but "${form.db.split("/").pop()}" appears to be nucleotide.`
      : programMeta.dbType === "nucl" && isProtDb
        ? `${form.program} expects a nucleotide database, but "${form.db.split("/").pop()}" appears to be protein.`
        : null;

  const paramsSummary = `E-value: ${form.evalue} · Max: ${form.max_target_seqs} · Fmt: ${form.outfmt}`;
  const searchSummary = form.db
    ? `Search ${form.db.split("/").pop() || form.db} using ${programMeta.label}`
    : "";

  // Sequence stats
  const seqCount = form.query_data
    ? form.query_data.split("\n").filter((l) => l.startsWith(">")).length
    : 0;
  const charCount = form.query_data.length;
  const isFasta = form.query_data.trim().startsWith(">");

  // Readiness indicators
  const readySteps = [
    { ok: Boolean(subId && workloadRg), label: "Config" },
    { ok: Boolean(form.query_data && isFasta), label: "Sequence" },
    { ok: Boolean(form.db), label: "Database" },
    { ok: Boolean(selectedCluster?.power_state === "Running"), label: "Cluster" },
  ];
  const readyCount = readySteps.filter((s) => s.ok).length;

  return (
    <div className="blast-page">
      {/* ── Header ── */}
      <header className="blast-header">
        <div>
          <div className="blast-header__title">
            <Dna size={24} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
            <h1 style={{ margin: 0 }}>
              {programMeta.label === "blastn"
                ? "Standard Nucleotide"
                : programMeta.label === "blastp"
                  ? "Standard Protein"
                  : programMeta.label.toUpperCase()}{" "}
              BLAST
            </h1>
          </div>
          <p className="muted" style={{ marginTop: 4, fontSize: 13 }}>
            Submit a sequence search using ElasticBLAST on AKS
          </p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {/* Readiness indicator */}
          <div className="blast-readiness">
            {readySteps.map((s) => (
              <span
                key={s.label}
                className={`blast-readiness__dot${s.ok ? " blast-readiness__dot--ok" : ""}`}
                title={s.label}
              />
            ))}
            <span className="muted" style={{ fontSize: 10 }}>
              {readyCount}/{readySteps.length}
            </span>
          </div>
          <button
            className="glass-button"
            onClick={() => setForm(INITIAL)}
            style={{ fontSize: 11 }}
          >
            <RotateCcw size={12} strokeWidth={1.5} /> Reset
          </button>
        </div>
      </header>

      {/* ── Step 1: Program Selection ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={1}
          icon={<FlaskConical size={16} strokeWidth={1.5} />}
          title="Program Selection"
          subtitle="Choose a BLAST algorithm"
        />
        <div className="blast-program-tabs">
          {PROGRAMS.map((p) => (
            <button
              key={p.value}
              onClick={() => set("program", p.value)}
              className={`blast-program-tab${form.program === p.value ? " blast-program-tab--active" : ""}`}
            >
              <span className="blast-program-tab__name">{p.label}</span>
              <span className="blast-program-tab__desc">{p.desc}</span>
            </button>
          ))}
        </div>
        <div className="blast-program-info">
          <BookOpen
            size={14}
            strokeWidth={1.5}
            style={{ color: "var(--accent)", flexShrink: 0 }}
          />
          <span>{programMeta.longDesc}</span>
        </div>

        {/* NCBI-style Optimize for — blastn only */}
        {form.program === "blastn" && (
          <div style={{ marginTop: 12 }}>
            <span className="glass-label" style={{ marginBottom: 6 }}>
              Optimize for
            </span>
            <div className="blast-optimize-group">
              {BLASTN_OPTIMIZE.map((opt) => (
                <label
                  key={opt.value}
                  className={`blast-optimize-option${form.optimize === opt.value ? " blast-optimize-option--active" : ""}`}
                >
                  <input
                    type="radio"
                    name="optimize"
                    value={opt.value}
                    checked={form.optimize === opt.value}
                    onChange={() => {
                      set("optimize", opt.value);
                      set("word_size", String(opt.wordSize));
                    }}
                    style={{ display: "none" }}
                  />
                  <span className="blast-optimize-radio" />
                  <div>
                    <div style={{ fontSize: 12 }}>{opt.label}</div>
                    <div className="muted" style={{ fontSize: 10 }}>
                      {opt.desc}
                    </div>
                  </div>
                </label>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* ── Step 2: Enter Query Sequence ── */}
      <section className="glass-card glass-card--strong blast-section">
        <SectionHeader
          step={2}
          icon={<Dna size={16} strokeWidth={1.5} />}
          title="Enter Query Sequence"
          subtitle="Paste FASTA sequence(s) or upload a file"
        />

        <div className="blast-textarea-wrap">
          <textarea
            className="glass-input blast-textarea"
            rows={10}
            value={form.query_data}
            onChange={(e) => set("query_data", e.target.value)}
            placeholder={
              ">sequence_id description\nATCGATCG...\n\nPaste your FASTA sequence here, or click 'Load example' below."
            }
            spellCheck={false}
          />
          {/* Live stats ribbon */}
          {form.query_data && (
            <div className="blast-textarea-stats">
              {isFasta ? (
                <span style={{ color: "var(--success)" }}>
                  <CheckCircle2 size={10} /> Valid FASTA
                </span>
              ) : (
                <span style={{ color: "var(--warning)" }}>
                  <AlertTriangle size={10} /> Not FASTA format
                </span>
              )}
              <span className="blast-textarea-stats__sep" />
              <span>
                {seqCount} sequence{seqCount !== 1 ? "s" : ""}
              </span>
              <span className="blast-textarea-stats__sep" />
              <span>{charCount.toLocaleString()} characters</span>
            </div>
          )}
        </div>

        <div className="blast-query-actions">
          <label className="glass-button blast-action-btn" style={{ cursor: "pointer" }}>
            <Upload size={13} strokeWidth={1.5} /> Upload file
            <input
              ref={fileInputRef}
              type="file"
              accept=".fa,.fasta,.fna,.faa"
              style={{ display: "none" }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                if (file.size > MAX_UPLOAD_BYTES) {
                  toast(
                    `File too large. Max ${MAX_UPLOAD_BYTES / 1024 / 1024} MB.`,
                    "error",
                  );
                  return;
                }
                const reader = new FileReader();
                reader.onload = () => {
                  if (typeof reader.result === "string") set("query_data", reader.result);
                };
                reader.readAsText(file);
              }}
            />
          </label>
          <button
            className="glass-button blast-action-btn"
            onClick={() => {
              set("query_data", EXAMPLE_FASTA);
              set("program", "blastn");
              toast(
                "Example loaded — E. coli 16S rRNA (matches 16S_ribosomal_RNA DB)",
                "info",
              );
            }}
            type="button"
          >
            <Dna size={13} /> Load example
          </button>
          {form.query_data && (
            <button
              className="glass-button blast-action-btn"
              onClick={() => set("query_data", "")}
              type="button"
            >
              <X size={13} strokeWidth={1.5} /> Clear
            </button>
          )}
        </div>

        {/* Query Subrange */}
        <div className="blast-subrange-row">
          <span
            className="glass-label"
            style={{ fontSize: 11, minWidth: "fit-content", marginBottom: 0 }}
          >
            Query subrange{" "}
            <Tip text="Restrict search to a range of the query (1-based)." />
          </span>
          <input
            className="glass-input blast-small-input"
            value={form.query_from}
            onChange={(e) => set("query_from", e.target.value)}
            placeholder="From"
            type="number"
            min={1}
          />
          <ArrowRight size={12} style={{ color: "var(--text-faint)" }} />
          <input
            className="glass-input blast-small-input"
            value={form.query_to}
            onChange={(e) => set("query_to", e.target.value)}
            placeholder="To"
            type="number"
            min={1}
          />
        </div>

        <label style={{ marginTop: 12, display: "block" }}>
          <span className="glass-label">Job Title</span>
          <input
            className="glass-input"
            value={form.job_title}
            onChange={(e) => set("job_title", e.target.value)}
            placeholder="Enter a descriptive title for your BLAST search"
            maxLength={200}
          />
        </label>
      </section>

      {/* ── Step 3: Choose Search Set ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={3}
          icon={<Database size={16} strokeWidth={1.5} />}
          title="Choose Search Set"
          subtitle="Select a BLAST database from your storage"
        />
        <label>
          <span className="glass-label">
            Database <Tip text="Select a BLAST database from your storage account." />
          </span>
          {dbQuery.data?.databases && dbQuery.data.databases.length > 0 ? (
            <>
              <select
                className="glass-input"
                value={form.db}
                onChange={(e) => set("db", e.target.value)}
              >
                <option value="">— Select a database —</option>
                {dbQuery.data.databases.map((d) => {
                  const info = DB_DESCRIPTIONS[d.name];
                  const isCustom = d.source === "custom";
                  const label = info
                    ? `${info.label} (${d.name}) — ${info.size}`
                    : isCustom
                      ? `${d.name} [Custom]`
                      : d.name;
                  // Use the prefix returned by the API to build the canonical
                  // path. NCBI DBs live at blast-db/{name}/, custom DBs at
                  // blast-db/custom_db/{name}/.
                  const prefix = d.prefix ?? d.name;
                  return (
                    <option key={d.name} value={`${d.container}/${prefix}/${d.name}`}>
                      {label}
                    </option>
                  );
                })}
              </select>
              {/* Quick-pick chips */}
              {!form.db && (
                <div className="blast-db-chips">
                  <span className="muted" style={{ fontSize: 11 }}>
                    Suggested for {form.program} (
                    {programMeta.dbType === "nucl" ? "nucleotide" : "protein"}):
                  </span>
                  {dbQuery.data.databases
                    .filter((d) => DB_DESCRIPTIONS[d.name]?.type === programMeta.dbType)
                    .slice(0, 4)
                    .map((d) => {
                      const info = DB_DESCRIPTIONS[d.name];
                      return (
                        <button
                          key={d.name}
                          className="blast-db-chip"
                          onClick={() => set("db", `${d.container}/${d.name}/${d.name}`)}
                        >
                          <Database size={10} />
                          <span>{d.name}</span>
                          {info && (
                            <span className="blast-db-chip__size">{info.size}</span>
                          )}
                        </button>
                      );
                    })}
                </div>
              )}
            </>
          ) : (
            <input
              className="glass-input"
              value={form.db}
              onChange={(e) => set("db", e.target.value)}
              placeholder="blast-db/core_nt/core_nt"
              spellCheck={false}
            />
          )}
        </label>
        {/* Warning if DB not in storage */}
        {form.db &&
          dbQuery.data?.databases &&
          !dbQuery.data.databases.some((d) => form.db.includes(d.name)) && (
            <div className="blast-warning-box">
              <AlertTriangle size={14} />
              This database doesn't appear to be downloaded yet.{" "}
              <Link to="/" style={{ color: "var(--accent)" }}>
                Download it from the Dashboard
              </Link>
              .
            </div>
          )}
        {dbWarning && (
          <div className="blast-warning-box">
            <AlertTriangle size={14} />
            {dbWarning}
          </div>
        )}
      </section>

      {/* ── Step 4: Program Selection / AKS Cluster ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={4}
          icon={<Server size={16} strokeWidth={1.5} />}
          title="Compute Environment"
          subtitle="Select an AKS cluster to run the search"
        />
        {!subId && (
          <div className="muted">
            Configure your Azure resources on the Dashboard first.
          </div>
        )}
        {subId && clusterQuery.isLoading && (
          <div className="muted">
            <Loader2
              size={12}
              className="spin"
              style={{ display: "inline", verticalAlign: "middle" }}
            />{" "}
            Loading clusters...
          </div>
        )}
        {subId && clusters.length === 0 && !clusterQuery.isLoading && (
          <div className="muted">
            No AKS clusters in <strong>{workloadRg}</strong>.{" "}
            <Link to="/" style={{ color: "var(--accent)" }}>
              Create one on the Dashboard
            </Link>
            .
          </div>
        )}
        {clusters.length > 0 && (
          <>
            <select
              className="glass-input"
              value={form.selectedCluster}
              onChange={(e) => set("selectedCluster", e.target.value)}
              style={{ marginBottom: 12 }}
            >
              <option value="">— Select cluster —</option>
              {clusters.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name} — {c.region} ({c.power_state ?? "?"})
                </option>
              ))}
            </select>
            {selectedCluster && (
              <div className="blast-cluster-info">
                {(
                  [
                    [
                      "Status",
                      selectedCluster.power_state,
                      selectedCluster.power_state === "Running"
                        ? "var(--success)"
                        : "var(--warning)",
                    ],
                    ["State", selectedCluster.provisioning_state, undefined],
                    ["SKU", selectedCluster.node_sku, undefined],
                    ["Nodes", String(selectedCluster.node_count), undefined],
                    ["K8s", selectedCluster.k8s_version, undefined],
                    ["Region", selectedCluster.region, undefined],
                  ] as [string, string | undefined, string | undefined][]
                ).map(([label, val, color]) => (
                  <div key={label} className="blast-cluster-info__cell">
                    <div className="blast-cluster-info__label">{label}</div>
                    <div
                      className="blast-cluster-info__value"
                      style={color ? { fontWeight: 600, color } : undefined}
                    >
                      {val ?? "?"}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {/* Warmup & DB Sharding */}
        {selectedCluster && (
          <div
            style={{
              marginTop: 12,
              padding: "10px 14px",
              background: "var(--glass-bg)",
              border: "1px solid var(--glass-border)",
              borderRadius: 8,
            }}
          >
            <div
              style={{
                fontSize: 12,
                fontWeight: 600,
                marginBottom: 8,
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <Zap size={14} style={{ color: "var(--warning)" }} />
              Performance
            </div>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                cursor: isDbAlreadyWarm ? "default" : "pointer",
                fontSize: 12,
                marginBottom: 6,
                opacity: isDbAlreadyWarm ? 0.8 : 1,
              }}
            >
              <input
                type="checkbox"
                checked={isDbAlreadyWarm || form.enable_warmup}
                disabled={isDbAlreadyWarm}
                onChange={(e) => set("enable_warmup", e.target.checked)}
                style={{ accentColor: isDbAlreadyWarm ? "var(--success)" : "var(--accent)" }}
              />
              <span>
                Warmup cluster{" "}
                {isDbAlreadyWarm ? (
                  <span style={{ color: "var(--success)", fontWeight: 500 }}>
                    — cached on {warmDbInfo?.nodes_ready}/{warmDbInfo?.total_jobs} nodes
                  </span>
                ) : (
                  <span className="muted">
                    (prepare DB shards on local SSD before BLAST)
                  </span>
                )}
              </span>
            </label>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                cursor: "pointer",
                fontSize: 12,
              }}
            >
              <input
                type="checkbox"
                checked={form.db_auto_partition}
                onChange={(e) => set("db_auto_partition", e.target.checked)}
                style={{ accentColor: "var(--accent)" }}
              />
              <span>
                DB auto-partition{" "}
                <span className="muted">(split DB into shards for parallel search)</span>
              </span>
            </label>
            {(form.enable_warmup || isDbAlreadyWarm) && (
              <div
                className="muted"
                style={{ fontSize: 10, marginTop: 6, lineHeight: 1.5 }}
              >
                {isDbAlreadyWarm
                  ? `${selectedDbShortName} is already loaded on all cluster nodes. BLAST will start immediately without download delay.`
                  : "The prepare step will create the cluster, download DB shards to node SSDs, then submit BLAST with reuse=true. This adds ~5-10 min setup but significantly improves search performance for large databases."}
              </div>
            )}
          </div>
        )}
      </section>

      {/* ── Step 5: Algorithm Parameters (collapsed) ── */}
      <section className="glass-card blast-section">
        <button onClick={() => setShowParams((v) => !v)} className="blast-params-toggle">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              className="blast-step-badge"
              style={{ fontSize: 10, width: 20, height: 20 }}
            >
              5
            </span>
            <Gauge size={16} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
            <span style={{ fontWeight: 600, fontSize: 14 }}>Algorithm Parameters</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="muted" style={{ fontSize: 11 }}>
              {!showParams && paramsSummary}
            </span>
            {showParams ? (
              <ChevronUp size={16} strokeWidth={1.5} />
            ) : (
              <ChevronDown size={16} strokeWidth={1.5} />
            )}
          </div>
        </button>
        {showParams && (
          <div style={{ marginTop: 16 }}>
            {/* Presets */}
            <div className="blast-presets">
              {PRESETS.map((p) => {
                const active =
                  form.evalue === p.evalue && form.max_target_seqs === p.max_target_seqs;
                return (
                  <button
                    key={p.label}
                    className={`blast-preset${active ? " blast-preset--active" : ""}`}
                    onClick={() => {
                      set("evalue", p.evalue);
                      set("max_target_seqs", p.max_target_seqs);
                    }}
                  >
                    <Zap size={12} />
                    <div>
                      <div style={{ fontWeight: 500 }}>{p.label}</div>
                      <div className="muted" style={{ fontSize: 10 }}>
                        {p.desc}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>

            <div className="blast-params-grid">
              <label>
                <span className="glass-label">
                  E-value{" "}
                  <Tip text="Expected number of chance matches. Lower = more stringent." />
                </span>
                <input
                  className="glass-input"
                  type="number"
                  step="any"
                  value={form.evalue}
                  onChange={(e) => set("evalue", parseFloat(e.target.value) || 0.05)}
                />
              </label>
              <label>
                <span className="glass-label">
                  Max target seqs{" "}
                  <Tip text="Maximum number of aligned sequences to keep." />
                </span>
                <input
                  className="glass-input"
                  type="number"
                  value={form.max_target_seqs}
                  onChange={(e) =>
                    set("max_target_seqs", parseInt(e.target.value, 10) || 100)
                  }
                />
              </label>
              <label>
                <span className="glass-label">
                  Word size <Tip text="Length of initial exact match." />
                </span>
                <input
                  className="glass-input"
                  type="number"
                  value={form.word_size}
                  onChange={(e) => set("word_size", e.target.value)}
                  placeholder={String(programMeta.defaultWordSize)}
                />
              </label>
              <label>
                <span className="glass-label">Output format</span>
                <select
                  className="glass-input"
                  value={form.outfmt}
                  onChange={(e) => set("outfmt", parseInt(e.target.value, 10))}
                >
                  <option value={7}>7 — Tabular + comments</option>
                  <option value={6}>6 — Tabular</option>
                  <option value={0}>0 — Pairwise text</option>
                  <option value={11}>11 — ASN.1 (archive)</option>
                </select>
              </label>
              {form.program === "blastn" && (
                <>
                  <label>
                    <span className="glass-label">
                      Match score <Tip text="Reward for a nucleotide match. Default: 1" />
                    </span>
                    <input
                      className="glass-input"
                      type="number"
                      value={form.match_score}
                      onChange={(e) => set("match_score", e.target.value)}
                      placeholder="1"
                    />
                  </label>
                  <label>
                    <span className="glass-label">
                      Mismatch score <Tip text="Penalty for a mismatch. Default: -2" />
                    </span>
                    <input
                      className="glass-input"
                      type="number"
                      value={form.mismatch_score}
                      onChange={(e) => set("mismatch_score", e.target.value)}
                      placeholder="-2"
                    />
                  </label>
                </>
              )}
              <label>
                <span className="glass-label">
                  Gap open <Tip text="Cost to open a gap." />
                </span>
                <input
                  className="glass-input"
                  type="number"
                  value={form.gap_open}
                  onChange={(e) => set("gap_open", e.target.value)}
                  placeholder="Auto"
                />
              </label>
              <label>
                <span className="glass-label">
                  Gap extend <Tip text="Cost to extend a gap." />
                </span>
                <input
                  className="glass-input"
                  type="number"
                  value={form.gap_extend}
                  onChange={(e) => set("gap_extend", e.target.value)}
                  placeholder="Auto"
                />
              </label>
              <div
                style={{
                  gridColumn: "1 / -1",
                  display: "flex",
                  gap: 16,
                  alignItems: "center",
                  flexWrap: "wrap",
                }}
              >
                <span className="glass-label" style={{ marginBottom: 0 }}>
                  Filters:
                </span>
                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    cursor: "pointer",
                    fontSize: 12,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={form.low_complexity_filter}
                    onChange={(e) => set("low_complexity_filter", e.target.checked)}
                  />
                  Low complexity filter{" "}
                  <Tip text="Mask low-complexity regions (DUST for nucleotide, SEG for protein)." />
                </label>
              </div>
              <label style={{ gridColumn: "1 / -1" }}>
                <span className="glass-label">
                  Additional options <Tip text="Extra command-line flags for BLAST." />
                </span>
                <input
                  className="glass-input"
                  value={form.additional_options}
                  onChange={(e) => set("additional_options", e.target.value)}
                  placeholder="-max_hsps 1 -num_threads 4"
                  spellCheck={false}
                />
              </label>
            </div>
          </div>
        )}
      </section>

      {/* ── Submit Footer ── */}
      <div className="blast-submit-footer">
        {missing.length > 0 && !submitMutation.isPending && (
          <div className="blast-checklist">
            <strong style={{ fontSize: 11 }}>Required before submitting:</strong>
            <ul>
              {missing.map((m) => (
                <li key={m.text}>
                  {m.text}
                  {m.link && (
                    <Link
                      to={m.link}
                      style={{ marginLeft: 6, color: "var(--accent)", fontSize: 11 }}
                    >
                      Go →
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Pre-flight readiness check results */}
        {preFlightResult && (
          <div
            style={{
              background: preFlightResult.ready
                ? "rgba(115,191,105,0.06)"
                : "rgba(242,153,74,0.06)",
              border: `1px solid ${preFlightResult.ready ? "rgba(115,191,105,0.2)" : "rgba(242,153,74,0.2)"}`,
              borderRadius: 8,
              padding: "12px 16px",
              marginBottom: 8,
            }}
          >
            <div
              style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}
            >
              {preFlightResult.ready ? (
                <CheckCircle2 size={14} style={{ color: "var(--success)" }} />
              ) : (
                <AlertTriangle size={14} style={{ color: "var(--warning)" }} />
              )}
              <span style={{ fontSize: 12, fontWeight: 600 }}>
                {preFlightResult.summary}
              </span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {preFlightResult.checks.map((c) => (
                <div
                  key={c.id}
                  style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}
                >
                  {c.status === "pass" ? (
                    <CheckCircle2 size={11} style={{ color: "var(--success)" }} />
                  ) : c.status === "fail" ? (
                    <AlertTriangle
                      size={11}
                      style={{
                        color:
                          c.severity === "critical" ? "var(--danger)" : "var(--warning)",
                      }}
                    />
                  ) : c.status === "warn" ? (
                    <AlertTriangle
                      size={11}
                      style={{ color: "var(--warning)", opacity: 0.7 }}
                    />
                  ) : (
                    <Check size={11} style={{ color: "var(--text-faint)" }} />
                  )}
                  <span
                    style={{
                      color:
                        c.status === "pass" ? "var(--text-muted)" : "var(--text-primary)",
                    }}
                  >
                    {c.title}
                  </span>
                  {c.detail && (
                    <span className="muted" style={{ fontSize: 10 }}>
                      — {c.detail}
                    </span>
                  )}
                  {c.action && c.status === "fail" && (
                    <span
                      style={{ fontSize: 10, color: "var(--accent)", marginLeft: "auto" }}
                    >
                      {c.action_type === "download_db" ? (
                        <Link to="/" style={{ color: "var(--accent)" }}>
                          {c.action} →
                        </Link>
                      ) : (
                        c.action
                      )}
                    </span>
                  )}
                </div>
              ))}
            </div>
            {/* Suggested databases if DB not found */}
            {preFlightResult.checks.some(
              (c) => c.id === "blast_db" && c.status === "fail" && c.suggested_dbs,
            ) && (
              <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-muted)" }}>
                <span style={{ fontWeight: 600 }}>Suggested databases to download: </span>
                {preFlightResult.checks
                  .find((c) => c.id === "blast_db")
                  ?.suggested_dbs?.map((db, i) => (
                    <span key={db}>
                      {i > 0 && ", "}
                      <button
                        style={{
                          background: "none",
                          border: "none",
                          color: "var(--accent)",
                          cursor: "pointer",
                          fontSize: 11,
                          padding: 0,
                          textDecoration: "underline",
                        }}
                        onClick={() => set("db", `blast-db/${db}/${db}`)}
                      >
                        {db}
                      </button>
                    </span>
                  ))}
              </div>
            )}
          </div>
        )}

        {/* Command preview — shown when ready to submit */}
        {canSubmit && (
          <BlastCommandPreview form={form} programMeta={programMeta} toast={toast} />
        )}
        <div className="blast-submit-bar">
          <div className="blast-submit-summary">
            {searchSummary && (
              <span className="blast-submit-summary__text">
                {searchSummary}
                {form.optimize && form.program === "blastn" && (
                  <span className="muted">
                    {" "}
                    ·{" "}
                    {BLASTN_OPTIMIZE.find((o) => o.value === form.optimize)?.value ?? ""}
                  </span>
                )}
              </span>
            )}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {/* Pre-flight check button */}
            {canSubmit && (
              <button
                className="glass-button"
                onClick={() => preFlightMutation.mutate()}
                disabled={preFlightMutation.isPending}
                style={{ fontSize: 12, gap: 5 }}
              >
                {preFlightMutation.isPending ? (
                  <>
                    <Loader2 size={13} className="spin" /> Checking...
                  </>
                ) : (
                  <>
                    <CheckCircle2 size={13} /> Check Readiness
                  </>
                )}
              </button>
            )}
            <button
              className="blast-submit-btn"
              onClick={handleSubmit}
              disabled={!canSubmit}
            >
              {submitMutation.isPending ? (
                <Loader2 size={20} strokeWidth={1.5} className="spin" />
              ) : (
                <Play size={20} strokeWidth={1.5} />
              )}
              <span>BLAST</span>
            </button>
          </div>
        </div>
        {submitMutation.isError && (
          <div style={{ color: "var(--danger)", fontSize: 12, marginTop: 6 }}>
            {formatApiError(submitMutation.error, "blast")}
          </div>
        )}
      </div>
    </div>
  );
}
