import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Database, Upload, CheckCircle2, AlertTriangle, Loader2, ArrowLeft,
  FileText, Dna, FlaskConical, HardDrive,
} from "lucide-react";
import { Link } from "react-router-dom";

import { blastApi } from "@/api/endpoints";
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

export function DatabaseBuilder() {
  const cfg = loadSavedConfig();
  const { toast } = useToast();

  const [dbName, setDbName] = useState("");
  const [dbType, setDbType] = useState<"nucl" | "prot">("nucl");
  const [title, setTitle] = useState("");
  const [fastaData, setFastaData] = useState("");
  const [inputMode, setInputMode] = useState<"paste" | "file">("paste");
  const [fileName, setFileName] = useState("");

  // Validate FASTA
  const fastaLines = fastaData.trim().split("\n");
  const seqCount = fastaLines.filter(l => l.startsWith(">")).length;
  const totalBases = fastaLines.filter(l => !l.startsWith(">") && l.trim()).join("").length;
  const isValidFasta = seqCount > 0 && fastaData.trim().startsWith(">");
  const isValidDbName = /^[a-zA-Z0-9_-]{1,50}$/.test(dbName);

  // List existing databases
  const dbListQuery = useQuery({
    queryKey: ["blast-databases", cfg?.storageAccountName],
    queryFn: () => blastApi.listDatabases(
      cfg?.subscriptionId ?? "",
      cfg?.storageAccountName ?? "",
      cfg?.workloadResourceGroup ?? "",
    ),
    enabled: !!cfg?.subscriptionId && !!cfg?.storageAccountName,
    staleTime: 30_000,
  });

  const existingDbs = dbListQuery.data?.databases ?? [];

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
      dbListQuery.refetch();
    },
    onError: (err: Error) => {
      toast(`Build failed: ${err.message}`, "error");
    },
  });

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) {
      toast("File too large (max 50 MB for inline upload)", "error");
      return;
    }
    setFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      setFastaData(reader.result as string);
    };
    reader.readAsText(file);
  };

  const canSubmit = isValidDbName && isValidFasta && !buildMutation.isPending && !!cfg?.subscriptionId;

  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
        <Link to="/" className="btn btn--ghost btn--sm" style={{ padding: "6px 8px" }}>
          <ArrowLeft size={16} />
        </Link>
        <Database size={22} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
        <h1 style={{ margin: 0, fontSize: 22 }}>Custom Database Builder</h1>
      </div>

      <p className="muted" style={{ marginBottom: 24, lineHeight: 1.6 }}>
        Upload your FASTA sequences to create a custom BLAST database. The database will be built
        on the Remote Terminal VM using <code className="code-val">makeblastdb</code> and stored in
        Azure Blob Storage for use with ElasticBLAST.
      </p>

      {/* Config check */}
      {!cfg?.subscriptionId && (
        <div className="glass-card" style={{ padding: 16, marginBottom: 20, borderColor: "var(--warning)" }}>
          <AlertTriangle size={16} style={{ color: "var(--warning)", marginRight: 8 }} />
          <span className="muted">Setup required — configure your Azure resources on the Dashboard first.</span>
        </div>
      )}

      <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
        <h3 style={{ margin: "0 0 20px", fontSize: 16 }}>
          <FlaskConical size={16} strokeWidth={1.5} style={{ marginRight: 8 }} />
          Database Configuration
        </h3>

        {/* DB Name */}
        <div style={{ marginBottom: 16 }}>
          <label className="form-label">Database Name *</label>
          <input
            type="text"
            className="form-input"
            placeholder="e.g. my_pathogen_db"
            value={dbName}
            onChange={e => setDbName(e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))}
            maxLength={50}
            style={{ width: "100%", maxWidth: 400 }}
          />
          {dbName && !isValidDbName && (
            <span className="form-hint" style={{ color: "var(--danger)" }}>
              Only letters, digits, _ and - allowed (1-50 chars)
            </span>
          )}
          {dbName && existingDbs.some(d => d.name === dbName) && (
            <span className="form-hint" style={{ color: "var(--warning)" }}>
              A database with this name already exists — it will be overwritten
            </span>
          )}
        </div>

        {/* DB Type */}
        <div style={{ marginBottom: 16 }}>
          <label className="form-label">Sequence Type *</label>
          <div style={{ display: "flex", gap: 12 }}>
            <button
              className={`btn btn--sm ${dbType === "nucl" ? "btn--primary" : "btn--ghost"}`}
              onClick={() => setDbType("nucl")}
              type="button"
            >
              <Dna size={14} /> Nucleotide
            </button>
            <button
              className={`btn btn--sm ${dbType === "prot" ? "btn--primary" : "btn--ghost"}`}
              onClick={() => setDbType("prot")}
              type="button"
            >
              <FlaskConical size={14} /> Protein
            </button>
          </div>
        </div>

        {/* Title */}
        <div style={{ marginBottom: 16 }}>
          <label className="form-label">Title (optional)</label>
          <input
            type="text"
            className="form-input"
            placeholder="Human-readable description"
            value={title}
            onChange={e => setTitle(e.target.value)}
            maxLength={200}
            style={{ width: "100%", maxWidth: 500 }}
          />
        </div>
      </div>

      {/* FASTA Input */}
      <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
        <h3 style={{ margin: "0 0 16px", fontSize: 16 }}>
          <FileText size={16} strokeWidth={1.5} style={{ marginRight: 8 }} />
          FASTA Input
        </h3>

        <div style={{ display: "flex", gap: 12, marginBottom: 16 }}>
          <button
            className={`btn btn--sm ${inputMode === "paste" ? "btn--primary" : "btn--ghost"}`}
            onClick={() => setInputMode("paste")}
            type="button"
          >
            Paste Sequence
          </button>
          <button
            className={`btn btn--sm ${inputMode === "file" ? "btn--primary" : "btn--ghost"}`}
            onClick={() => setInputMode("file")}
            type="button"
          >
            <Upload size={14} /> Upload File
          </button>
        </div>

        {inputMode === "paste" ? (
          <>
            <textarea
              className="form-input"
              rows={12}
              placeholder=">sequence_id Description\nATGCGATCGA..."
              value={fastaData}
              onChange={e => setFastaData(e.target.value)}
              style={{
                width: "100%",
                fontFamily: "var(--font-mono, monospace)",
                fontSize: 13,
                lineHeight: 1.5,
                resize: "vertical",
              }}
            />
            <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => setFastaData(dbType === "nucl" ? EXAMPLE_NUCL_FASTA : EXAMPLE_PROT_FASTA)}
                type="button"
              >
                Load Example
              </button>
              {fastaData && (
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => setFastaData("")}
                  type="button"
                  style={{ color: "var(--danger)" }}
                >
                  Clear
                </button>
              )}
            </div>
          </>
        ) : (
          <div style={{ padding: 24, border: "2px dashed var(--glass-border)", borderRadius: 12, textAlign: "center" }}>
            <Upload size={32} className="muted" style={{ marginBottom: 8 }} />
            <p className="muted">Upload a FASTA file (.fa, .fasta, .fna, .faa)</p>
            <input
              type="file"
              accept=".fa,.fasta,.fna,.faa,.txt"
              onChange={handleFileUpload}
              style={{ margin: "12px auto" }}
            />
            {fileName && (
              <p style={{ marginTop: 8 }}>
                <FileText size={14} style={{ marginRight: 4 }} />
                {fileName}
              </p>
            )}
          </div>
        )}

        {/* FASTA stats */}
        {fastaData && (
          <div
            style={{
              marginTop: 12,
              padding: "8px 12px",
              background: "var(--glass-bg)",
              borderRadius: 8,
              fontSize: 13,
              display: "flex",
              gap: 20,
            }}
          >
            <span>
              {isValidFasta ? (
                <CheckCircle2 size={14} style={{ color: "var(--success)", marginRight: 4 }} />
              ) : (
                <AlertTriangle size={14} style={{ color: "var(--danger)", marginRight: 4 }} />
              )}
              {seqCount} sequence{seqCount !== 1 ? "s" : ""}
            </span>
            <span className="muted">{totalBases.toLocaleString()} residues</span>
            <span className="muted">{(fastaData.length / 1024).toFixed(1)} KB</span>
          </div>
        )}
      </div>

      {/* Submit */}
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 32 }}>
        <button
          className="btn btn--primary"
          disabled={!canSubmit}
          onClick={() => buildMutation.mutate()}
          type="button"
        >
          {buildMutation.isPending ? (
            <>
              <Loader2 size={16} className="spin" /> Building Database...
            </>
          ) : (
            <>
              <Database size={16} /> Build Database
            </>
          )}
        </button>
        {buildMutation.isPending && (
          <span className="muted" style={{ fontSize: 13 }}>
            This may take a few minutes depending on the input size
          </span>
        )}
      </div>

      {/* Success */}
      {buildMutation.isSuccess && buildMutation.data && (
        <div className="glass-card" style={{ padding: 20, marginBottom: 24, borderColor: "var(--success)" }}>
          <h4 style={{ margin: "0 0 8px", color: "var(--success)" }}>
            <CheckCircle2 size={16} style={{ marginRight: 8 }} />
            Database Created
          </h4>
          <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 16px", fontSize: 14 }}>
            <span className="muted">Name:</span><span>{buildMutation.data.db_name}</span>
            <span className="muted">Type:</span><span>{buildMutation.data.db_type}</span>
            <span className="muted">Files:</span><span>{buildMutation.data.file_count}</span>
            <span className="muted">Path:</span><code className="code-val">{buildMutation.data.path}</code>
          </div>
          <p className="muted" style={{ marginTop: 12, fontSize: 13 }}>
            Use <code className="code-val">blast-db/{buildMutation.data.db_name}/{buildMutation.data.db_name}</code> as
            the database path when submitting a BLAST job.
          </p>
        </div>
      )}

      {/* Existing databases */}
      <div className="glass-card" style={{ padding: 24 }}>
        <h3 style={{ margin: "0 0 16px", fontSize: 16 }}>
          <HardDrive size={16} strokeWidth={1.5} style={{ marginRight: 8 }} />
          Existing Databases ({existingDbs.length})
        </h3>
        {dbListQuery.isLoading ? (
          <p className="muted"><Loader2 size={14} className="spin" /> Loading...</p>
        ) : existingDbs.length === 0 ? (
          <p className="muted">No databases found in blob storage</p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Files</th>
                  <th>Size</th>
                  <th>Last Modified</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {existingDbs.map(db => (
                  <tr key={db.name}>
                    <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{db.name}</td>
                    <td>{db.file_count ?? "—"}</td>
                    <td>{db.total_bytes ? formatBytes(db.total_bytes) : "—"}</td>
                    <td className="muted">{db.last_modified ? new Date(db.last_modified).toLocaleDateString() : "—"}</td>
                    <td>
                      <span className={`badge badge--${db.source_version ? "info" : "muted"}`}>
                        {db.source_version ?? "custom"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
