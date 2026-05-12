import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  DollarSign, Clock, Shield, Scissors, FlaskConical,
  Loader2, AlertTriangle,
  Play, Trash2, ToggleLeft, ToggleRight, Calendar,
  Search, RefreshCw, Copy, Check,
} from "lucide-react";

import {
  costApi, preprocessApi, primerApi, auditApi, scheduleApi,
  dbVersionApi, taxonomyApi,
} from "@/api/endpoints";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";

// ═══════════════════════════════════════════════════════════════
// Tab definitions
// ═══════════════════════════════════════════════════════════════
type TabKey = "cost" | "preprocess" | "primer" | "audit" | "schedules" | "versions" | "taxonomy";

const TABS: { key: TabKey; label: string; icon: React.ReactNode }[] = [
  { key: "cost", label: "Cost Estimator", icon: <DollarSign size={14} /> },
  { key: "preprocess", label: "Preprocessor", icon: <Scissors size={14} /> },
  { key: "primer", label: "Primer Design", icon: <FlaskConical size={14} /> },
  { key: "taxonomy", label: "Taxonomy", icon: <Search size={14} /> },
  { key: "schedules", label: "Schedules", icon: <Calendar size={14} /> },
  { key: "versions", label: "DB Versions", icon: <Clock size={14} /> },
  { key: "audit", label: "Audit Trail", icon: <Shield size={14} /> },
];

export function ToolsPage() {
  const [activeTab, setActiveTab] = useState<TabKey>("cost");

  return (
    <div style={{ maxWidth: 1000, margin: "0 auto" }}>
      <h1 style={{ fontSize: 22, marginBottom: 20 }}>Tools</h1>

      {/* Tab bar */}
      <div style={{ display: "flex", gap: 4, marginBottom: 24, overflowX: "auto", flexWrap: "wrap" }}>
        {TABS.map(t => (
          <button
            key={t.key}
            className={`btn btn--sm ${activeTab === t.key ? "btn--primary" : "btn--ghost"}`}
            onClick={() => setActiveTab(t.key)}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      {activeTab === "cost" && <CostEstimatorTab />}
      {activeTab === "preprocess" && <PreprocessorTab />}
      {activeTab === "primer" && <PrimerDesignTab />}
      {activeTab === "taxonomy" && <TaxonomyTab />}
      {activeTab === "schedules" && <SchedulesTab />}
      {activeTab === "versions" && <DbVersionsTab />}
      {activeTab === "audit" && <AuditTrailTab />}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P11 — Cost Estimator
// ═══════════════════════════════════════════════════════════════
function CostEstimatorTab() {
  const [sku, setSku] = useState("Standard_E16s_v5");
  const [nodes, setNodes] = useState(3);
  const [hours, setHours] = useState(2);
  const [pdSize, setPdSize] = useState(1000);
  const [dbSize, setDbSize] = useState(50);

  const mutation = useMutation({
    mutationFn: () => costApi.estimate({
      machine_type: sku, num_nodes: nodes,
      estimated_hours: hours, pd_size_gb: pdSize, db_size_gb: dbSize,
    }),
  });

  const est = mutation.data?.estimate;

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <h3 style={{ margin: "0 0 16px" }}><DollarSign size={16} /> Cost Estimator</h3>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Estimate Azure costs before submitting a BLAST job.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 20 }}>
        <div>
          <label className="form-label">Node SKU</label>
          <select className="form-input" value={sku} onChange={e => setSku(e.target.value)} style={{ width: "100%" }}>
            {["Standard_D2s_v5","Standard_D4s_v5","Standard_D8s_v5","Standard_D16s_v5",
              "Standard_E4s_v5","Standard_E8s_v5","Standard_E16s_v5","Standard_E32s_v5"].map(s =>
              <option key={s} value={s}>{s}</option>
            )}
          </select>
        </div>
        <div>
          <label className="form-label">Number of Nodes</label>
          <input className="form-input" type="number" min={1} max={100}
            value={nodes} onChange={e => setNodes(+e.target.value)} style={{ width: "100%" }} />
        </div>
        <div>
          <label className="form-label">Estimated Hours</label>
          <input className="form-input" type="number" min={0.1} max={168} step={0.5}
            value={hours} onChange={e => setHours(+e.target.value)} style={{ width: "100%" }} />
        </div>
        <div>
          <label className="form-label">Persistent Disk (GB)</label>
          <input className="form-input" type="number" min={10} max={10000}
            value={pdSize} onChange={e => setPdSize(+e.target.value)} style={{ width: "100%" }} />
        </div>
        <div>
          <label className="form-label">Database Size (GB)</label>
          <input className="form-input" type="number" min={1} max={5000}
            value={dbSize} onChange={e => setDbSize(+e.target.value)} style={{ width: "100%" }} />
        </div>
      </div>

      <button className="btn btn--primary" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? <Loader2 size={14} className="spin" /> : <DollarSign size={14} />} Calculate
      </button>

      {est && (
        <div style={{ marginTop: 20, display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
          <CostCard label="Compute" value={`$${est.compute_usd}`} />
          <CostCard label="Disk" value={`$${est.disk_usd}`} />
          <CostCard label="Storage" value={`$${est.storage_usd}`} />
          <CostCard label="Total" value={`$${est.total_usd}`} accent />
        </div>
      )}
    </div>
  );
}

function CostCard({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div style={{ padding: 14, background: "var(--glass-bg)", borderRadius: 10, textAlign: "center" }}>
      <div style={{ fontSize: 20, fontWeight: 700, color: accent ? "var(--accent)" : "var(--text-primary)" }}>{value}</div>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P5 — Query Preprocessor
// ═══════════════════════════════════════════════════════════════
function PreprocessorTab() {
  const [inputData, setInputData] = useState("");
  const [format, setFormat] = useState<"auto" | "fastq" | "fasta">("auto");
  const [minLength, setMinLength] = useState(0);
  const [minQuality, setMinQuality] = useState(0);
  const [copied, setCopied] = useState(false);

  const mutation = useMutation({
    mutationFn: () => preprocessApi.process({
      input_data: inputData, format, min_length: minLength, min_quality: minQuality,
    }),
  });

  const stats = mutation.data?.stats;

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <h3 style={{ margin: "0 0 16px" }}><Scissors size={16} /> Query Preprocessor</h3>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Convert FASTQ to FASTA, filter by length/quality, and compute sequence statistics.
      </p>

      <div style={{ marginBottom: 16 }}>
        <label className="form-label">Input Sequences (FASTA or FASTQ)</label>
        <textarea
          className="form-input" rows={8} value={inputData}
          onChange={e => setInputData(e.target.value)}
          placeholder="Paste FASTA (>header...) or FASTQ (@header...) sequences"
          style={{ width: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
        />
      </div>

      <div style={{ display: "flex", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
        <div>
          <label className="form-label">Format</label>
          <select className="form-input" value={format} onChange={e => setFormat(e.target.value as typeof format)}>
            <option value="auto">Auto-detect</option>
            <option value="fasta">FASTA</option>
            <option value="fastq">FASTQ</option>
          </select>
        </div>
        <div>
          <label className="form-label">Min Length</label>
          <input className="form-input" type="number" min={0} value={minLength}
            onChange={e => setMinLength(+e.target.value)} style={{ width: 100 }} />
        </div>
        <div>
          <label className="form-label">Min Quality (FASTQ)</label>
          <input className="form-input" type="number" min={0} max={40} value={minQuality}
            onChange={e => setMinQuality(+e.target.value)} style={{ width: 100 }} />
        </div>
      </div>

      <button className="btn btn--primary" onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !inputData.trim()}>
        {mutation.isPending ? <Loader2 size={14} className="spin" /> : <Scissors size={14} />} Process
      </button>

      {stats && (
        <div style={{ marginTop: 20 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 8, marginBottom: 16 }}>
            <StatBox label="Input Seqs" value={stats.input_sequences} />
            <StatBox label="Output Seqs" value={stats.output_sequences} />
            <StatBox label="Total Bases" value={stats.total_bases.toLocaleString()} />
            <StatBox label="Avg Length" value={stats.avg_length} />
            <StatBox label="GC %" value={`${stats.gc_content}%`} />
            <StatBox label="Filtered" value={stats.filtered_short + stats.filtered_quality} />
          </div>

          {mutation.data?.fasta_output && (
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <label className="form-label" style={{ margin: 0 }}>Output FASTA</label>
                <button className="btn btn--ghost btn--sm" onClick={() => {
                  navigator.clipboard.writeText(mutation.data!.fasta_output);
                  setCopied(true); setTimeout(() => setCopied(false), 2000);
                }}>
                  {copied ? <Check size={12} /> : <Copy size={12} />} {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <textarea
                className="form-input" rows={6} readOnly
                value={mutation.data.fasta_output}
                style={{ width: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 11 }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatBox({ label, value }: { label: string; value: string | number }) {
  return (
    <div style={{ padding: 8, background: "var(--glass-bg)", borderRadius: 8, textAlign: "center" }}>
      <div style={{ fontSize: 16, fontWeight: 600 }}>{value}</div>
      <div className="muted" style={{ fontSize: 11 }}>{label}</div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P12 — Primer Design
// ═══════════════════════════════════════════════════════════════
function PrimerDesignTab() {
  const cfg = loadSavedConfig();
  const { toast } = useToast();
  const [sequence, setSequence] = useState("");
  const [targetStart, setTargetStart] = useState(100);
  const [targetLength, setTargetLength] = useState(200);
  const [productMin, setProductMin] = useState(100);
  const [productMax, setProductMax] = useState(1000);

  const mutation = useMutation({
    mutationFn: () => primerApi.design({
      sequence, subscription_id: cfg?.subscriptionId ?? "",
      terminal_resource_group: cfg?.terminalResourceGroup,
      terminal_vm_name: cfg?.terminalVmName,
      target_start: targetStart, target_length: targetLength,
      product_size_min: productMin, product_size_max: productMax,
    }),
    onError: (err: Error) => toast(err.message, "error"),
  });

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <h3 style={{ margin: "0 0 16px" }}><FlaskConical size={16} /> Primer Design (Primer3)</h3>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Design PCR primers using Primer3 on the Remote Terminal VM.
      </p>

      <div style={{ marginBottom: 16 }}>
        <label className="form-label">Template Sequence (nucleotide, min 50 bp)</label>
        <textarea className="form-input" rows={5} value={sequence}
          onChange={e => setSequence(e.target.value)}
          placeholder="ATGCGATCGATCGATCG..."
          style={{ width: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
        />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 16 }}>
        <div>
          <label className="form-label">Target Start</label>
          <input className="form-input" type="number" value={targetStart}
            onChange={e => setTargetStart(+e.target.value)} style={{ width: "100%" }} />
        </div>
        <div>
          <label className="form-label">Target Length</label>
          <input className="form-input" type="number" value={targetLength}
            onChange={e => setTargetLength(+e.target.value)} style={{ width: "100%" }} />
        </div>
        <div>
          <label className="form-label">Product Min</label>
          <input className="form-input" type="number" value={productMin}
            onChange={e => setProductMin(+e.target.value)} style={{ width: "100%" }} />
        </div>
        <div>
          <label className="form-label">Product Max</label>
          <input className="form-input" type="number" value={productMax}
            onChange={e => setProductMax(+e.target.value)} style={{ width: "100%" }} />
        </div>
      </div>

      <button className="btn btn--primary" onClick={() => mutation.mutate()}
        disabled={mutation.isPending || sequence.length < 50}>
        {mutation.isPending ? <Loader2 size={14} className="spin" /> : <FlaskConical size={14} />} Design Primers
      </button>

      {mutation.data?.primers && mutation.data.primers.length > 0 && (
        <div style={{ marginTop: 20, overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>#</th><th>Forward Primer</th><th>Reverse Primer</th>
                <th>Tm (F/R)</th><th>GC% (F/R)</th><th>Product</th><th>Penalty</th>
              </tr>
            </thead>
            <tbody>
              {mutation.data.primers.map(p => (
                <tr key={p.pair_index}>
                  <td>{p.pair_index + 1}</td>
                  <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{p.left_sequence}</td>
                  <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{p.right_sequence}</td>
                  <td>{p.left_tm?.toFixed(1)} / {p.right_tm?.toFixed(1)}</td>
                  <td>{p.left_gc?.toFixed(1)} / {p.right_gc?.toFixed(1)}</td>
                  <td>{p.product_size ?? "—"}</td>
                  <td>{p.pair_penalty?.toFixed(2) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {mutation.data?.primers?.length === 0 && (
        <div className="muted" style={{ marginTop: 16 }}>
          <AlertTriangle size={14} /> No primer pairs found for the given parameters.
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P4 — Taxonomy Lookup
// ═══════════════════════════════════════════════════════════════
function TaxonomyTab() {
  const [accInput, setAccInput] = useState("");

  const mutation = useMutation({
    mutationFn: () => {
      const accessions = accInput.split(/[\s,;]+/).filter(Boolean).slice(0, 50);
      return taxonomyApi.lookup(accessions);
    },
  });

  const annotations = mutation.data?.annotations ?? {};

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <h3 style={{ margin: "0 0 16px" }}><Search size={16} /> Taxonomy Annotation</h3>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Look up organism, taxonomy ID, and sequence info from NCBI for BLAST hit accessions.
      </p>

      <div style={{ marginBottom: 16 }}>
        <label className="form-label">Accessions (space, comma, or newline separated; max 50)</label>
        <textarea className="form-input" rows={3} value={accInput}
          onChange={e => setAccInput(e.target.value)}
          placeholder="NR_123456.1 NR_789012.1 XP_001234.2"
          style={{ width: "100%", fontFamily: "var(--font-mono, monospace)", fontSize: 13 }}
        />
      </div>

      <button className="btn btn--primary" onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !accInput.trim()}>
        {mutation.isPending ? <Loader2 size={14} className="spin" /> : <Search size={14} />} Look Up
      </button>

      {Object.keys(annotations).length > 0 && (
        <div style={{ marginTop: 20, overflowX: "auto" }}>
          <p className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            Found {mutation.data?.found} of {mutation.data?.requested}
          </p>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr><th>Accession</th><th>Organism</th><th>Title</th><th>Tax ID</th><th>Length</th></tr>
            </thead>
            <tbody>
              {Object.values(annotations).map(a => (
                <tr key={a.accession}>
                  <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{a.accession}</td>
                  <td style={{ fontWeight: 600 }}>{a.organism}</td>
                  <td className="muted" style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.title}</td>
                  <td>{a.taxid}</td>
                  <td>{a.seq_length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P7 — Scheduled BLAST
// ═══════════════════════════════════════════════════════════════
function SchedulesTab() {
  const { toast } = useToast();
  const listQuery = useQuery({
    queryKey: ["blast-schedules"],
    queryFn: () => scheduleApi.list(),
    staleTime: 10_000,
  });

  const runMutation = useMutation({
    mutationFn: (id: string) => scheduleApi.run(id),
    onSuccess: (data) => toast(`Job started: ${data.job_id}`, "success"),
    onError: (err: Error) => toast(err.message, "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => scheduleApi.remove(id),
    onSuccess: () => { toast("Schedule deleted", "info"); listQuery.refetch(); },
  });

  const schedules = listQuery.data?.schedules ?? [];

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h3 style={{ margin: 0 }}><Calendar size={16} /> Scheduled BLAST Jobs</h3>
        <button className="btn btn--ghost btn--sm" onClick={() => listQuery.refetch()}>
          <RefreshCw size={14} className={listQuery.isFetching ? "spin" : ""} />
        </button>
      </div>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Create saved BLAST configurations that can be re-run with one click or triggered automatically.
      </p>

      {schedules.length === 0 ? (
        <p className="muted">No schedules configured. Create one from the New Search page.</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 13 }}>
            <thead>
              <tr><th>Name</th><th>Trigger</th><th>Runs</th><th>Last Run</th><th>Status</th><th>Actions</th></tr>
            </thead>
            <tbody>
              {schedules.map(s => (
                <tr key={s.schedule_id}>
                  <td style={{ fontWeight: 600 }}>{s.name}</td>
                  <td><span className="badge badge--info">{s.trigger_type}</span></td>
                  <td>{s.run_count}</td>
                  <td className="muted">{s.last_run ? new Date(s.last_run).toLocaleString() : "Never"}</td>
                  <td>{s.enabled
                    ? <span style={{ color: "var(--success)" }}><ToggleRight size={14} /> Active</span>
                    : <span className="muted"><ToggleLeft size={14} /> Paused</span>
                  }</td>
                  <td style={{ display: "flex", gap: 4 }}>
                    <button className="btn btn--ghost btn--sm" onClick={() => runMutation.mutate(s.schedule_id)}
                      disabled={runMutation.isPending} title="Run now">
                      <Play size={12} />
                    </button>
                    <button className="btn btn--ghost btn--sm" onClick={() => deleteMutation.mutate(s.schedule_id)}
                      style={{ color: "var(--danger)" }} title="Delete">
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P2 — DB Version Registry
// ═══════════════════════════════════════════════════════════════
function DbVersionsTab() {
  const cfg = loadSavedConfig();

  const listQuery = useQuery({
    queryKey: ["db-versions", cfg?.storageAccountName],
    queryFn: () => dbVersionApi.list(
      cfg?.subscriptionId ?? "",
      cfg?.storageAccountName ?? "",
      cfg?.workloadResourceGroup ?? "",
    ),
    enabled: !!cfg?.subscriptionId && !!cfg?.storageAccountName,
    staleTime: 30_000,
  });

  const versions = listQuery.data?.versions ?? [];

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h3 style={{ margin: 0 }}><Clock size={16} /> Database Version Registry</h3>
        <button className="btn btn--ghost btn--sm" onClick={() => listQuery.refetch()}>
          <RefreshCw size={14} className={listQuery.isFetching ? "spin" : ""} />
        </button>
      </div>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Track versions and metadata for all BLAST databases in your storage account.
      </p>

      {!cfg?.subscriptionId && (
        <p className="muted"><AlertTriangle size={14} /> Configure your resources on the Dashboard first.</p>
      )}

      {listQuery.isLoading && <p className="muted"><Loader2 size={14} className="spin" /> Loading...</p>}

      {versions.length === 0 && !listQuery.isLoading && (
        <p className="muted">No database metadata found. Build a custom database or download from NCBI to get started.</p>
      )}

      {versions.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Database</th><th>Type</th><th>Source</th><th>Version</th>
                <th>Created</th><th>By</th><th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: "var(--font-mono, monospace)", fontWeight: 600 }}>{v.db_name}</td>
                  <td>{v.db_type ?? "—"}</td>
                  <td><span className={`badge badge--${v.source === "ncbi" ? "info" : "muted"}`}>{v.source ?? "custom"}</span></td>
                  <td>{v.source_version || v.version_tag || "—"}</td>
                  <td className="muted">{v.created_at ? new Date(v.created_at).toLocaleDateString() : "—"}</td>
                  <td className="muted">{v.created_by ?? "—"}</td>
                  <td className="muted" style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis" }}>{v.notes ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════
// P9 — Audit Trail
// ═══════════════════════════════════════════════════════════════
function AuditTrailTab() {
  const [actionFilter, setActionFilter] = useState("");

  const listQuery = useQuery({
    queryKey: ["audit-trail", actionFilter],
    queryFn: () => auditApi.listEvents(200, actionFilter || undefined),
    staleTime: 15_000,
  });

  const events = listQuery.data?.events ?? [];

  return (
    <div className="glass-card" style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h3 style={{ margin: 0 }}><Shield size={16} /> Audit Trail</h3>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select className="form-input" value={actionFilter} onChange={e => setActionFilter(e.target.value)}
            style={{ fontSize: 12, width: 150 }}>
            <option value="">All actions</option>
            <option value="blast_submit">BLAST Submit</option>
            <option value="blast_delete">BLAST Delete</option>
            <option value="db_build">DB Build</option>
            <option value="terminal_provision">Terminal Provision</option>
          </select>
          <button className="btn btn--ghost btn--sm" onClick={() => listQuery.refetch()}>
            <RefreshCw size={14} className={listQuery.isFetching ? "spin" : ""} />
          </button>
        </div>
      </div>
      <p className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
        Immutable log of all BLAST operations for regulatory compliance (GLP/CLIA).
      </p>

      {listQuery.isLoading && <p className="muted"><Loader2 size={14} className="spin" /> Loading...</p>}

      {events.length === 0 && !listQuery.isLoading && (
        <p className="muted">No audit events recorded yet.</p>
      )}

      {events.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr><th>Time</th><th>Action</th><th>User</th><th>Job ID</th><th>Details</th></tr>
            </thead>
            <tbody>
              {events.map((ev, i) => (
                <tr key={i}>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>
                    {ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "—"}
                  </td>
                  <td><span className="badge badge--info">{ev.action}</span></td>
                  <td>{ev.user ?? "—"}</td>
                  <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{ev.job_id ?? "—"}</td>
                  <td className="muted" style={{ maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {ev.details ? JSON.stringify(ev.details).slice(0, 100) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
