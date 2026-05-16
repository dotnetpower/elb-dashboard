import { useState, useEffect, useMemo, useRef } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Play,
  Loader2,
  RotateCcw,
  Dna,
  CheckCircle2,
  AlertTriangle,
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
import { AlgorithmParametersSection } from "@/pages/blastSubmit/AlgorithmParametersSection";
import { ComputeSection } from "@/pages/blastSubmit/ComputeSection";
import {
  getWorkloadNodeCount,
  getWorkloadNodeSku,
} from "@/pages/blastSubmit/computeEnvironment";
import { DatabaseSection } from "@/pages/blastSubmit/DatabaseSection";
import {
  databaseExists,
  getDatabaseWarning,
  getDbBaseName,
  getSequenceStats,
} from "@/pages/blastSubmit/helpers";
import { useDbWithWarmupPlan } from "@/pages/blastSubmit/useDbWithWarmupPlan";
import { ProgramSection } from "@/pages/blastSubmit/ProgramSection";
import { QuerySection } from "@/pages/blastSubmit/QuerySection";
import { BlastCommandPreview } from "@/pages/blastSubmit/ui";
import {
  BLASTN_OPTIMIZE,
  INITIAL,
  PROGRAMS,
  type FormState,
} from "@/pages/blastSubmitModel";

const DRAFT_SCHEMA_VERSION = 3;

export function BlastSubmit() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  // #34 Form auto-save: restore draft from sessionStorage
  const [form, setForm] = useState<FormState>(() => {
    try {
      const saved = sessionStorage.getItem("elb-blast-draft");
      if (saved) {
        const parsed = JSON.parse(saved) as Partial<FormState> & { draft_version?: number };
        const restored = { ...INITIAL, ...parsed };
        if (parsed.draft_version !== DRAFT_SCHEMA_VERSION) {
          restored.db_auto_partition = INITIAL.db_auto_partition;
          restored.sharding_mode = INITIAL.sharding_mode;
        }
        return restored;
      }
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
    sessionStorage.setItem(
      "elb-blast-draft",
      JSON.stringify({ ...form, draft_version: DRAFT_SCHEMA_VERSION }),
    );
  }, [form]);

  const [config] = useState(() => loadSavedConfig());
  const subId = config?.subscriptionId ?? "";
  const workloadRg = config?.workloadResourceGroup ?? "";
  const storageAccount = config?.storageAccountName ?? "";
  const acrRg = config?.acrResourceGroup ?? "";
  const acrName = config?.acrName ?? "";
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

  // Database listing + warmup-feasibility plan, scoped to the selected
  // cluster's topology. See `useDbWithWarmupPlan` for the contract — the
  // hook is the single owner of:
  //   * the `/api/blast/databases` query (keyed by topology so a cluster
  //     switch triggers a fresh fetch and isolated cache entry),
  //   * `selectedDbInfo` memoisation,
  //   * `selectedDbPlan` (server-computed warmup feasibility), and
  //   * `warmupBlocked` (the single boolean the submit gating reads).
  const {
    dbQuery,
    selectedDbInfo,
    selectedDbPlan,
    warmupBlocked,
  } = useDbWithWarmupPlan({
    subId,
    storageAccount,
    workloadRg,
    selectedCluster,
    selectedDbShortName,
    warmupRequested: form.enable_warmup && !isDbAlreadyWarm,
  });
  const dbSharded = selectedDbInfo?.sharded === true;
  const dbShardSets = selectedDbInfo?.shard_sets ?? [];
  const dbTotalBytes = selectedDbInfo?.total_bytes;

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
    if (warmupBlocked) {
      // Defence in depth — the Run BLAST button is already disabled when
      // the planner says no, but a keyboard / programmatic activation
      // could slip through. Surface the planner verdict immediately.
      toast(
        `Warmup blocked by feasibility planner: ${selectedDbPlan?.message ?? "infeasible"}`,
        "error",
      );
      return;
    }
    const workloadNodeSku = getWorkloadNodeSku(selectedCluster);
    const workloadNodeCount = getWorkloadNodeCount(selectedCluster);
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
      machine_type: workloadNodeSku || "Standard_E32s_v5",
      num_nodes: workloadNodeCount || 3,
      pd_size: "1000Gi",
      mem_request: "8Gi",
      mem_limit: "24Gi",
      enable_warmup: form.enable_warmup,
      db_auto_partition: form.sharding_mode !== "off",
      sharding_mode: form.sharding_mode,
      allow_approximate_sharding: form.sharding_mode === "approximate" || undefined,
      disable_sharding: form.disable_sharding,
      acr_resource_group: acrRg || undefined,
      acr_name: acrName || undefined,
      storage_account: storageAccount,
      aks_cluster_name: selectedCluster.name,
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
  const dbBaseName = getDbBaseName(form.db);
  const dbMissingFromStorage =
    Boolean(form.db) && dbListResolved && !databaseExists(knownDbs, form.db);

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
    !warmupBlocked &&
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
  if (warmupBlocked)
    missing.push({
      text:
        selectedDbPlan?.message ??
        "Warmup is not feasible on this cluster — disable warmup or upgrade the cluster",
    });

  const dbWarning = getDatabaseWarning(form, programMeta);

  const paramsSummary = `E-value: ${form.evalue} · Max: ${form.max_target_seqs} · Fmt: ${form.outfmt}`;
  const searchSummary = form.db
    ? `Search ${form.db.split("/").pop() || form.db} using ${programMeta.label}`
    : "";

  const { seqCount, charCount, isFasta } = getSequenceStats(form.query_data);

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
              ElasticBLAST New Search · {programMeta.label === "blastn"
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

      <ProgramSection form={form} set={set} programMeta={programMeta} />

      <QuerySection
        form={form}
        set={set}
        fileInputRef={fileInputRef}
        toast={toast}
        isFasta={isFasta}
        seqCount={seqCount}
        charCount={charCount}
      />

      <DatabaseSection
        form={form}
        set={set}
        programMeta={programMeta}
        databases={dbQuery.data?.databases}
        dbWarning={dbWarning}
        dbMissingFromStorage={dbMissingFromStorage}
        dbBaseName={dbBaseName}
      />

      <ComputeSection
        subId={subId}
        workloadRg={workloadRg}
        clusters={clusters}
        selectedCluster={selectedCluster}
        clusterLoading={clusterQuery.isLoading}
        form={form}
        set={set}
        isDbAlreadyWarm={isDbAlreadyWarm}
        warmDbInfo={warmDbInfo}
        selectedDbShortName={selectedDbShortName}
        dbSharded={dbSharded}
        dbShardSets={dbShardSets}
        dbTotalBytes={dbTotalBytes}
        warmupPlan={selectedDbPlan}
      />

      <AlgorithmParametersSection
        form={form}
        set={set}
        showParams={showParams}
        setShowParams={setShowParams}
        paramsSummary={paramsSummary}
        programMeta={programMeta}
      />

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
                <Loader2 size={16} strokeWidth={1.5} className="spin" />
              ) : (
                <Play size={15} strokeWidth={1.5} />
              )}
              <span>{submitMutation.isPending ? "Submitting" : "Run BLAST"}</span>
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
