import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Calendar,
  Check,
  Clock,
  Copy,
  Database,
  DollarSign,
  FlaskConical,
  Loader2,
  Play,
  RefreshCw,
  Scissors,
  Search,
  Shield,
  ToggleLeft,
  ToggleRight,
  Trash2,
} from "lucide-react";
import { Link } from "react-router-dom";

import { formatApiError } from "@/api/client";
import {
  auditApi,
  costApi,
  dbVersionApi,
  preprocessApi,
  primerApi,
  scheduleApi,
  taxonomyApi,
} from "@/api/endpoints";
import { ExamplePicker } from "@/components/ExamplePicker";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import {
  COST_EXAMPLES,
  PREPROCESS_EXAMPLES,
  PRIMER_EXAMPLES,
  TAXONOMY_EXAMPLES,
  type CostExampleValues,
  type PreprocessExampleValues,
  type PrimerExampleValues,
  type TaxonomyExampleValues,
} from "@/data/labToolExamples";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";
import { SectionHeader, SetupRequired, StatBox } from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function CostEstimatorTab({ meta }: { meta: TabMeta }) {
  const [sku, setSku] = useState("Standard_E32s_v5");
  const [nodes, setNodes] = useState(3);
  const [hours, setHours] = useState(2);
  const [pdSize, setPdSize] = useState(1000);
  const [dbSize, setDbSize] = useState(50);

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
            {/* SKUs MUST come from api/services/aks_skus.py::ALLOWED_SKUS
                (mirror of sibling AZURE_HPC_MACHINES). Picking anything
                outside that allow-list breaks BLAST submit. */}
            {[
              "Standard_D8s_v3",
              "Standard_D16s_v3",
              "Standard_E16s_v3",
              "Standard_E32s_v3",
              "Standard_E16s_v5",
              "Standard_E32s_v5",
              "Standard_E48s_v5",
              "Standard_E64s_v5",
              "Standard_E32bs_v5",
              "Standard_E64bs_v5",
              "Standard_L32s_v3",
              "Standard_L64s_v3",
            ].map((s) => (
              <option key={s} value={s}>
                {s}
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

export function PrimerDesignTab({
  meta,
  hasConfig,
}: {
  meta: TabMeta;
  hasConfig: boolean;
}) {
  const cfg = loadSavedConfig();
  const { toast } = useToast();
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
        terminal_resource_group: cfg?.terminalResourceGroup,
        terminal_vm_name: cfg?.terminalVmName,
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

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<FlaskConical size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
      />

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

export function TaxonomyTab({ meta }: { meta: TabMeta }) {
  const [accInput, setAccInput] = useState("");

  const mutation = useMutation({
    mutationFn: () => {
      const accessions = accInput
        .split(/[\s,;]+/)
        .filter(Boolean)
        .slice(0, 50);
      return taxonomyApi.lookup(accessions);
    },
  });

  const annotations = mutation.data?.annotations ?? {};

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Search size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
      />

      <ExamplePicker<TaxonomyExampleValues>
        examples={TAXONOMY_EXAMPLES}
        onSelect={(v) => setAccInput(v.accessions)}
      />

      <div className="form-row" style={{ marginBottom: 16 }}>
        <label className="form-label">
          Accessions (space, comma, or newline separated; max 50)
        </label>
        <textarea
          className="form-input blast-textarea"
          rows={3}
          value={accInput}
          onChange={(e) => setAccInput(e.target.value)}
          placeholder="NR_123456.1 NR_789012.1 XP_001234.2"
        />
      </div>

      <button
        className="btn btn--primary"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !accInput.trim()}
      >
        {mutation.isPending ? (
          <Loader2 size={14} className="spin" />
        ) : (
          <Search size={14} />
        )}{" "}
        Look up
      </button>

      {Object.keys(annotations).length > 0 && (
        <div style={{ marginTop: 20, overflowX: "auto" }}>
          <p className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            Found {mutation.data?.found} of {mutation.data?.requested}
          </p>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Accession</th>
                <th>Organism</th>
                <th>Title</th>
                <th>Tax ID</th>
                <th>Length</th>
              </tr>
            </thead>
            <tbody>
              {Object.values(annotations).map((a) => (
                <tr key={a.accession}>
                  <td>
                    <code className="code-val">{a.accession}</code>
                  </td>
                  <td style={{ fontWeight: 600 }}>{a.organism}</td>
                  <td
                    className="muted"
                    style={{
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {a.title}
                  </td>
                  <td>{a.taxid}</td>
                  <td>{a.seq_length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function SchedulesTab({ meta }: { meta: TabMeta }) {
  const { toast } = useToast();
  const listQuery = useQuery({
    queryKey: ["blast-schedules"],
    queryFn: () => scheduleApi.list(),
    staleTime: 10_000,
  });

  const runMutation = useMutation({
    mutationFn: (id: string) => scheduleApi.run(id),
    onSuccess: (data) => toast(`Job started: ${data.job_id}`, "success"),
    onError: (err: unknown) => toast(formatApiError(err, "blast"), "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => scheduleApi.remove(id),
    onSuccess: () => {
      toast("Schedule deleted", "info");
      listQuery.refetch();
    },
  });

  const schedules = listQuery.data?.schedules ?? [];

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Calendar size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
        rightSlot={
          <button
            className="btn btn--ghost btn--sm"
            onClick={() => listQuery.refetch()}
            disabled={listQuery.isFetching}
            title="Refresh"
          >
            <RefreshCw size={12} className={listQuery.isFetching ? "spin" : ""} />
          </button>
        }
      />

      {listQuery.isLoading ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Loader2 size={20} className="spin" />
          </div>
          <div className="empty-state__title">Loading schedules…</div>
        </div>
      ) : schedules.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Calendar size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No schedules yet</div>
          <div className="empty-state__desc">
            Save a search from the New Search page to add it here for one-click or
            scheduled re-runs.
          </div>
          <Link
            to="/blast/submit"
            className="btn btn--primary btn--sm"
            style={{ marginTop: 12 }}
          >
            New BLAST search <ArrowRight size={12} />
          </Link>
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 13 }}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Trigger</th>
                <th>Runs</th>
                <th>Last run</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {schedules.map((s) => (
                <tr key={s.schedule_id}>
                  <td style={{ fontWeight: 600 }}>{s.name}</td>
                  <td>
                    <span className="badge badge--info">{s.trigger_type}</span>
                  </td>
                  <td>{s.run_count}</td>
                  <td className="muted">
                    {s.last_run ? new Date(s.last_run).toLocaleString() : "Never"}
                  </td>
                  <td>
                    {s.enabled ? (
                      <span
                        style={{
                          color: "var(--success)",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                        }}
                      >
                        <ToggleRight size={14} /> Active
                      </span>
                    ) : (
                      <span
                        className="muted"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                        }}
                      >
                        <ToggleLeft size={14} /> Paused
                      </span>
                    )}
                  </td>
                  <td style={{ display: "flex", gap: 4 }}>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => runMutation.mutate(s.schedule_id)}
                      disabled={runMutation.isPending}
                      title="Run now"
                    >
                      <Play size={12} />
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      onClick={() => deleteMutation.mutate(s.schedule_id)}
                      style={{ color: "var(--danger)" }}
                      title="Delete"
                    >
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function DbVersionsTab({ meta, hasConfig }: { meta: TabMeta; hasConfig: boolean }) {
  const cfg = loadSavedConfig();
  const listQuery = useQuery({
    queryKey: ["db-versions", cfg?.storageAccountName],
    queryFn: () =>
      dbVersionApi.list(
        cfg?.subscriptionId ?? "",
        cfg?.storageAccountName ?? "",
        cfg?.workloadResourceGroup ?? "",
      ),
    enabled: !!cfg?.subscriptionId && !!cfg?.storageAccountName,
    staleTime: 30_000,
  });

  const versions = listQuery.data?.versions ?? [];

  if (!hasConfig) {
    return (
      <section className="glass-card blast-section">
        <SectionHeader
          icon={<Clock size={16} strokeWidth={1.5} />}
          title={meta.label}
          subtitle={meta.desc}
        />
        <SetupRequired feature="DB version registry" />
      </section>
    );
  }

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Clock size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
        rightSlot={
          <button
            className="btn btn--ghost btn--sm"
            onClick={() => listQuery.refetch()}
            disabled={listQuery.isFetching}
            title="Refresh"
          >
            <RefreshCw size={12} className={listQuery.isFetching ? "spin" : ""} />
          </button>
        }
      />

      {listQuery.isLoading ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Loader2 size={20} className="spin" />
          </div>
          <div className="empty-state__title">Loading database metadata…</div>
        </div>
      ) : versions.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Database size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No database metadata yet</div>
          <div className="empty-state__desc">
            Build a custom database or download a public NCBI database to populate the
            registry.
          </div>
          <Link
            to="/blast/databases/build"
            className="btn btn--primary btn--sm"
            style={{ marginTop: 12 }}
          >
            Build custom database <ArrowRight size={12} />
          </Link>
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Database</th>
                <th>Type</th>
                <th>Source</th>
                <th>Version</th>
                <th>Created</th>
                <th>By</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((v, i) => (
                <tr key={i}>
                  <td>
                    <code className="code-val" style={{ fontWeight: 600 }}>
                      {v.db_name}
                    </code>
                  </td>
                  <td>{v.db_type ?? "—"}</td>
                  <td>
                    <span
                      className={`badge badge--${v.source === "ncbi" ? "info" : "muted"}`}
                    >
                      {v.source ?? "custom"}
                    </span>
                  </td>
                  <td>{v.source_version || v.version_tag || "—"}</td>
                  <td className="muted">
                    {v.created_at ? new Date(v.created_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="muted">{v.created_by ?? "—"}</td>
                  <td
                    className="muted"
                    style={{
                      maxWidth: 220,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {v.notes ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function AuditTrailTab({ meta }: { meta: TabMeta }) {
  const [actionFilter, setActionFilter] = useState("");

  const listQuery = useQuery({
    queryKey: ["audit-trail", actionFilter],
    queryFn: () => auditApi.listEvents(200, actionFilter || undefined),
    staleTime: 15_000,
  });

  const events = useMemo(() => listQuery.data?.events ?? [], [listQuery.data?.events]);
  const summary = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const ev of events) {
      const action = ev.action ?? "unknown";
      counts[action] = (counts[action] ?? 0) + 1;
    }
    return counts;
  }, [events]);

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Shield size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
        rightSlot={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              className="form-input form-input--compact"
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
            >
              <option value="">All actions</option>
              <option value="blast_submit">BLAST Submit</option>
              <option value="blast_delete">BLAST Delete</option>
              <option value="db_build">DB Build</option>
              <option value="terminal_provision">Terminal Provision</option>
            </select>
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => listQuery.refetch()}
              disabled={listQuery.isFetching}
              title="Refresh"
            >
              <RefreshCw size={12} className={listQuery.isFetching ? "spin" : ""} />
            </button>
          </div>
        }
      />

      {events.length > 0 && (
        <div className="metric-grid" style={{ marginBottom: 16 }}>
          <StatBox label="Events" value={events.length} accent />
          {Object.entries(summary)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 3)
            .map(([action, count]) => (
              <StatBox key={action} label={action} value={count} />
            ))}
        </div>
      )}

      {listQuery.isLoading ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Loader2 size={20} className="spin" />
          </div>
          <div className="empty-state__title">Loading audit events…</div>
        </div>
      ) : events.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state__icon">
            <Shield size={20} strokeWidth={1.5} />
          </div>
          <div className="empty-state__title">No audit events yet</div>
          <div className="empty-state__desc">
            Operations like BLAST submissions, database builds, and terminal provisioning
            will appear here automatically.
          </div>
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>User</th>
                <th>Job ID</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev, i) => (
                <tr key={i}>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>
                    {ev.timestamp ? new Date(ev.timestamp).toLocaleString() : "—"}
                  </td>
                  <td>
                    <span className="badge badge--info">{ev.action}</span>
                  </td>
                  <td>{ev.user ?? "—"}</td>
                  <td>
                    <code className="code-val">{ev.job_id ?? "—"}</code>
                  </td>
                  <td
                    className="muted"
                    style={{
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {ev.details ? JSON.stringify(ev.details).slice(0, 100) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}