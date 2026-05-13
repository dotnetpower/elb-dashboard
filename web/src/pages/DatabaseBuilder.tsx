import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Database,
  Upload,
  CheckCircle2,
  AlertTriangle,
  Loader2,
  FileText,
  Dna,
  FlaskConical,
  HardDrive,
  Settings,
  Sparkles,
  RefreshCw,
  Copy,
  Check,
  ArrowRight,
} from "lucide-react";
import { Link } from "react-router-dom";

import { blastApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";

const EXAMPLE_NUCL_FASTA = `>my_seq_1 Example nucleotide sequence
ATGCGATCGATCGATCGATCGATCGATCGATCGATCGATCG
ATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGA
>my_seq_2 Another sequence
GCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAG
CTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGC`;

const EXAMPLE_PROT_FASTA = `>protein_1 Example protein sequence
MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTK
TYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALS
>protein_2 Another protein
MGLSDGEWQLVLNVWGKVEADIPGHGQEVLIRLFKGHPETL`;

const MAX_INLINE_BYTES = 50 * 1024 * 1024;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function SectionHeader({
  step,
  icon,
  title,
  subtitle,
  rightSlot,
}: {
  step: number;
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  rightSlot?: React.ReactNode;
}) {
  return (
    <div className="blast-section-hd" style={{ justifyContent: "space-between" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="blast-step-badge">{step}</span>
        <span className="blast-section-icon">{icon}</span>
        <div>
          <div className="blast-section-title">{title}</div>
          {subtitle && <div className="blast-section-sub">{subtitle}</div>}
        </div>
      </div>
      {rightSlot && <div>{rightSlot}</div>}
    </div>
  );
}

export function DatabaseBuilder() {
  const cfg = loadSavedConfig();
  const { toast } = useToast();

  const [dbName, setDbName] = useState("");
  const [dbType, setDbType] = useState<"nucl" | "prot">("nucl");
  const [title, setTitle] = useState("");
  const [fastaData, setFastaData] = useState("");
  const [inputMode, setInputMode] = useState<"paste" | "file">("paste");
  const [fileName, setFileName] = useState("");
  const [copied, setCopied] = useState(false);

  // FASTA validation
  const fastaStats = useMemo(() => {
    const lines = fastaData.trim().split("\n");
    const seqCount = lines.filter((l) => l.startsWith(">")).length;
    const totalBases = lines
      .filter((l) => !l.startsWith(">") && l.trim())
      .join("").length;
    const isValid = seqCount > 0 && fastaData.trim().startsWith(">");
    return { seqCount, totalBases, isValid };
  }, [fastaData]);

  const isValidDbName = /^[a-zA-Z0-9_-]{1,50}$/.test(dbName);

  const dbListQuery = useQuery({
    queryKey: ["blast-databases", cfg?.storageAccountName],
    queryFn: () =>
      blastApi.listDatabases(
        cfg?.subscriptionId ?? "",
        cfg?.storageAccountName ?? "",
        cfg?.workloadResourceGroup ?? "",
      ),
    enabled: !!cfg?.subscriptionId && !!cfg?.storageAccountName,
    staleTime: 30_000,
  });

  const existingDbs = dbListQuery.data?.databases ?? [];
  const nameClash = !!dbName && existingDbs.some((d) => d.name === dbName);

  const buildMutation = useMutation({
    mutationFn: () =>
      blastApi.buildCustomDb({
        subscription_id: cfg?.subscriptionId ?? "",
        resource_group: cfg?.workloadResourceGroup ?? "",
        storage_account: cfg?.storageAccountName ?? "",
        db_name: dbName,
        db_type: dbType,
        title: title || dbName,
        fasta_data: fastaData,
      }),
    onSuccess: (data) => {
      toast(`Database "${data.db_name}" created (${data.file_count} files)`, "success");
      setFastaData("");
      setDbName("");
      setTitle("");
      setFileName("");
      dbListQuery.refetch();
    },
    onError: (err: unknown) => {
      toast(`Build failed: ${formatApiError(err, "blast")}`, "error");
    },
  });

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_INLINE_BYTES) {
      toast(
        `File too large (max ${formatBytes(MAX_INLINE_BYTES)} for inline upload)`,
        "error",
      );
      return;
    }
    setFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => setFastaData(reader.result as string);
    reader.readAsText(file);
  };

  const readiness = [
    { ok: !!cfg?.subscriptionId, label: "Workspace" },
    { ok: isValidDbName, label: "Database name" },
    { ok: fastaStats.isValid, label: "FASTA input" },
  ];
  const readyCount = readiness.filter((r) => r.ok).length;
  const allReady = readyCount === readiness.length && !buildMutation.isPending;
  const successPath = buildMutation.data
    ? `blast-db/${buildMutation.data.db_name}/${buildMutation.data.db_name}`
    : "";

  const handleCopyPath = () => {
    if (!successPath) return;
    navigator.clipboard.writeText(successPath).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  };

  return (
    <div className="page-stack">
      {/* ── Premium header ── */}
      <header
        className="page-header"
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
          marginBottom: 0,
        }}
      >
        <div>
          <div
            className="page-header__title"
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <Database size={22} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
            Custom Database Builder
          </div>
          <div className="page-header__desc">
            Upload FASTA sequences, run <code className="code-val">makeblastdb</code> on
            the Remote Terminal VM, and publish a private BLAST database to Azure Blob
            Storage.
          </div>
        </div>
        <div
          className="blast-readiness"
          aria-label="Builder readiness"
          title={`${readyCount} of ${readiness.length} prerequisites ready`}
        >
          {readiness.map((r) => (
            <span
              key={r.label}
              className={`blast-readiness__dot${r.ok ? " blast-readiness__dot--ok" : ""}`}
              title={r.label}
            />
          ))}
          <span className="muted" style={{ fontSize: 10 }}>
            {readyCount}/{readiness.length}
          </span>
        </div>
      </header>

      {/* ── Setup-required banner ── */}
      {!cfg?.subscriptionId && (
        <section
          className="glass-card"
          style={{
            padding: 16,
            display: "flex",
            alignItems: "center",
            gap: 12,
            borderColor: "var(--warning)",
          }}
        >
          <AlertTriangle size={18} style={{ color: "var(--warning)", flexShrink: 0 }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>Workspace not configured</div>
            <div className="muted" style={{ fontSize: 12 }}>
              Pick a subscription, storage account, and Remote Terminal on the Dashboard
              before building a custom database.
            </div>
          </div>
          <Link to="/" className="btn btn--primary btn--sm">
            Open Dashboard <ArrowRight size={12} />
          </Link>
        </section>
      )}

      {/* ── Step 1: Configuration ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={1}
          icon={<Settings size={16} strokeWidth={1.5} />}
          title="Database Configuration"
          subtitle="Name, sequence type, and human-readable title"
        />

        <div style={{ display: "grid", gap: 16 }}>
          <div>
            <label className="form-label" htmlFor="db-name">
              Database name *
            </label>
            <input
              id="db-name"
              type="text"
              className="form-input"
              placeholder="e.g. my_pathogen_db"
              value={dbName}
              onChange={(e) => setDbName(e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))}
              maxLength={50}
              style={{ width: "100%", maxWidth: 420 }}
            />
            {dbName && !isValidDbName && (
              <div className="form-hint" style={{ color: "var(--danger)", marginTop: 4 }}>
                Only letters, digits, _ and - allowed (1-50 chars)
              </div>
            )}
            {nameClash && (
              <div
                className="form-hint"
                style={{ color: "var(--warning)", marginTop: 4 }}
              >
                A database with this name already exists — it will be overwritten on
                rebuild.
              </div>
            )}
          </div>

          <div>
            <label className="form-label">Sequence type *</label>
            <div className="blast-program-tabs" style={{ maxWidth: 420 }}>
              <button
                type="button"
                onClick={() => setDbType("nucl")}
                className={`blast-program-tab${dbType === "nucl" ? " blast-program-tab--active" : ""}`}
              >
                <span className="blast-program-tab__name">
                  <Dna size={13} style={{ verticalAlign: "-2px", marginRight: 4 }} />
                  Nucleotide
                </span>
                <span className="blast-program-tab__desc">DNA / RNA · -dbtype nucl</span>
              </button>
              <button
                type="button"
                onClick={() => setDbType("prot")}
                className={`blast-program-tab${dbType === "prot" ? " blast-program-tab--active" : ""}`}
              >
                <span className="blast-program-tab__name">
                  <FlaskConical
                    size={13}
                    style={{ verticalAlign: "-2px", marginRight: 4 }}
                  />
                  Protein
                </span>
                <span className="blast-program-tab__desc">
                  Amino acids · -dbtype prot
                </span>
              </button>
            </div>
          </div>

          <div>
            <label className="form-label" htmlFor="db-title">
              Title (optional)
            </label>
            <input
              id="db-title"
              type="text"
              className="form-input"
              placeholder="Human-readable description shown in BLAST results"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={200}
              style={{ width: "100%", maxWidth: 520 }}
            />
          </div>
        </div>
      </section>

      {/* ── Step 2: FASTA Input ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={2}
          icon={<FileText size={16} strokeWidth={1.5} />}
          title="FASTA Input"
          subtitle="Paste sequences or upload a FASTA file (≤ 50 MB inline)"
        />

        <div className="blast-program-tabs" style={{ maxWidth: 360 }}>
          <button
            type="button"
            onClick={() => setInputMode("paste")}
            className={`blast-program-tab${inputMode === "paste" ? " blast-program-tab--active" : ""}`}
          >
            <span className="blast-program-tab__name">Paste sequence</span>
            <span className="blast-program-tab__desc">Quick prototyping</span>
          </button>
          <button
            type="button"
            onClick={() => setInputMode("file")}
            className={`blast-program-tab${inputMode === "file" ? " blast-program-tab--active" : ""}`}
          >
            <span className="blast-program-tab__name">
              <Upload size={12} style={{ verticalAlign: "-1px", marginRight: 4 }} />
              Upload file
            </span>
            <span className="blast-program-tab__desc">.fa .fasta .fna .faa</span>
          </button>
        </div>

        {inputMode === "paste" ? (
          <div className="blast-textarea-wrap" style={{ marginTop: 12 }}>
            <textarea
              className="form-input blast-textarea"
              rows={12}
              placeholder=">sequence_id Description&#10;ATGCGATCGA..."
              value={fastaData}
              onChange={(e) => setFastaData(e.target.value)}
              style={{ width: "100%" }}
            />
            <div className="blast-textarea-stats">
              {fastaStats.seqCount > 0 ? (
                <>
                  {fastaStats.isValid ? (
                    <CheckCircle2 size={12} style={{ color: "var(--success)" }} />
                  ) : (
                    <AlertTriangle size={12} style={{ color: "var(--danger)" }} />
                  )}
                  <span>
                    {fastaStats.seqCount} sequence
                    {fastaStats.seqCount !== 1 ? "s" : ""}
                  </span>
                  <span>·</span>
                  <span>{fastaStats.totalBases.toLocaleString()} residues</span>
                  <span>·</span>
                  <span>{(fastaData.length / 1024).toFixed(1)} KB</span>
                </>
              ) : (
                <span>Paste a FASTA-formatted sequence to begin.</span>
              )}
              <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() =>
                    setFastaData(
                      dbType === "nucl" ? EXAMPLE_NUCL_FASTA : EXAMPLE_PROT_FASTA,
                    )
                  }
                >
                  <Sparkles size={12} /> Load example
                </button>
                {fastaData && (
                  <button
                    type="button"
                    className="btn btn--ghost btn--sm"
                    onClick={() => {
                      setFastaData("");
                      setFileName("");
                    }}
                    style={{ color: "var(--danger)" }}
                  >
                    Clear
                  </button>
                )}
              </span>
            </div>
          </div>
        ) : (
          <label
            htmlFor="fasta-file"
            className="empty-state"
            style={{
              marginTop: 12,
              borderRadius: 12,
              border: "2px dashed var(--border-medium)",
              cursor: "pointer",
              minHeight: 160,
            }}
          >
            <div className="empty-state__icon">
              <Upload size={24} strokeWidth={1.5} />
            </div>
            <div className="empty-state__title">Drop a FASTA file here</div>
            <div className="empty-state__desc">
              Accepted: .fa, .fasta, .fna, .faa, .txt — up to{" "}
              {formatBytes(MAX_INLINE_BYTES)}
            </div>
            <input
              id="fasta-file"
              type="file"
              accept=".fa,.fasta,.fna,.faa,.txt"
              onChange={handleFileUpload}
              style={{ display: "none" }}
            />
            {fileName && (
              <div
                style={{
                  marginTop: 12,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  fontSize: 12,
                }}
              >
                <FileText size={13} style={{ color: "var(--accent)" }} />
                <code className="code-val">{fileName}</code>
                <span className="muted">
                  · {(fastaData.length / 1024).toFixed(1)} KB · {fastaStats.seqCount}{" "}
                  sequence
                  {fastaStats.seqCount !== 1 ? "s" : ""}
                </span>
              </div>
            )}
          </label>
        )}
      </section>

      {/* ── Step 3: Build & Submit ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={3}
          icon={<FlaskConical size={16} strokeWidth={1.5} />}
          title="Build database"
          subtitle="Runs makeblastdb on the Remote Terminal VM, then publishes to blob storage"
        />

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <div className="muted" style={{ fontSize: 12, maxWidth: 520 }}>
            {allReady
              ? `Ready to build "${dbName}" (${dbType === "nucl" ? "nucleotide" : "protein"}) with ${fastaStats.seqCount} sequence${fastaStats.seqCount !== 1 ? "s" : ""}.`
              : `Complete ${readiness.length - readyCount} more step${readiness.length - readyCount !== 1 ? "s" : ""} to enable building.`}
          </div>
          <button
            type="button"
            className="blast-submit-btn"
            disabled={!allReady}
            onClick={() => buildMutation.mutate()}
          >
            {buildMutation.isPending ? (
              <>
                <Loader2 size={16} className="spin" />
                Building database…
              </>
            ) : (
              <>
                <Database size={16} />
                Build database
              </>
            )}
          </button>
        </div>

        {buildMutation.isPending && (
          <div
            className="muted"
            style={{
              marginTop: 12,
              fontSize: 12,
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Loader2 size={12} className="spin" />
            Building may take a few minutes depending on input size — keep this tab open.
          </div>
        )}

        {buildMutation.isSuccess && buildMutation.data && (
          <div
            style={{
              marginTop: 16,
              padding: 16,
              borderRadius: 12,
              background: "rgba(106,214,163,0.06)",
              border: "1px solid rgba(106,214,163,0.25)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginBottom: 10,
                color: "var(--success)",
                fontWeight: 600,
              }}
            >
              <CheckCircle2 size={16} /> Database created
            </div>
            <div className="metric-grid" style={{ marginTop: 0 }}>
              <div className="metric-block">
                <div className="mv">{buildMutation.data.db_name}</div>
                <div className="mu">Name</div>
              </div>
              <div className="metric-block">
                <div className="mv">
                  {buildMutation.data.db_type === "prot" ? "Protein" : "Nucleotide"}
                </div>
                <div className="mu">Type</div>
              </div>
              <div className="metric-block">
                <div className="mv">{buildMutation.data.file_count}</div>
                <div className="mu">Files</div>
              </div>
            </div>
            <div
              style={{
                marginTop: 12,
                display: "flex",
                alignItems: "center",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <span className="muted" style={{ fontSize: 12 }}>
                Use this path when submitting a job:
              </span>
              <code className="code-val">{successPath}</code>
              <button
                type="button"
                className={`copy-btn${copied ? " copy-btn--copied" : ""}`}
                onClick={handleCopyPath}
                aria-label="Copy database path"
              >
                {copied ? <Check size={12} /> : <Copy size={12} />}{" "}
                {copied ? "Copied" : "Copy"}
              </button>
              <Link
                to="/blast/submit"
                className="btn btn--primary btn--sm"
                style={{ marginLeft: "auto" }}
              >
                Run a search <ArrowRight size={12} />
              </Link>
            </div>
          </div>
        )}

        {buildMutation.isError && (
          <div
            style={{
              marginTop: 16,
              padding: 12,
              borderRadius: 10,
              background: "rgba(224,123,138,0.08)",
              border: "1px solid rgba(224,123,138,0.25)",
              color: "var(--danger)",
              fontSize: 12,
            }}
          >
            <AlertTriangle size={13} style={{ verticalAlign: "-2px", marginRight: 6 }} />
            {formatApiError(buildMutation.error, "blast")}
          </div>
        )}
      </section>

      {/* ── Step 4: Existing databases ── */}
      <section className="glass-card blast-section">
        <SectionHeader
          step={4}
          icon={<HardDrive size={16} strokeWidth={1.5} />}
          title="Existing databases"
          subtitle="All BLAST databases discovered in the configured storage account"
          rightSlot={
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => dbListQuery.refetch()}
              disabled={dbListQuery.isFetching}
              title="Refresh list"
            >
              <RefreshCw size={12} className={dbListQuery.isFetching ? "spin" : ""} />
            </button>
          }
        />

        {dbListQuery.isLoading ? (
          <div className="empty-state">
            <div className="empty-state__icon">
              <Loader2 size={20} className="spin" />
            </div>
            <div className="empty-state__title">Loading databases…</div>
          </div>
        ) : !cfg?.subscriptionId ? (
          <div className="empty-state">
            <div className="empty-state__icon">
              <HardDrive size={20} strokeWidth={1.5} />
            </div>
            <div className="empty-state__title">Workspace not configured</div>
            <div className="empty-state__desc">
              Configure a subscription and storage account to see your databases.
            </div>
          </div>
        ) : existingDbs.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state__icon">
              <Database size={20} strokeWidth={1.5} />
            </div>
            <div className="empty-state__title">No databases yet</div>
            <div className="empty-state__desc">
              Build your first custom database above, or download a public NCBI database
              from the Storage card on the Dashboard.
            </div>
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Files</th>
                  <th>Size</th>
                  <th>Last modified</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {existingDbs.map((db) => (
                  <tr key={db.name}>
                    <td>
                      <code className="code-val">{db.name}</code>
                    </td>
                    <td>{db.file_count ?? "—"}</td>
                    <td>{db.total_bytes ? formatBytes(db.total_bytes) : "—"}</td>
                    <td className="muted">
                      {db.last_modified
                        ? new Date(db.last_modified).toLocaleDateString()
                        : "—"}
                    </td>
                    <td>
                      <span
                        className={`badge badge--${db.source_version ? "info" : "muted"}`}
                      >
                        {db.source_version ?? "custom"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
