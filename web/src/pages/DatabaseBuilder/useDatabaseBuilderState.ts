import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { blastApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { useTerminalSidecarHealth } from "@/hooks/usePrerequisites";

import { MAX_INLINE_BYTES, formatBytes } from "./formatBytes";

export type DbType = "nucl" | "prot";
export type InputMode = "paste" | "file";

export function useDatabaseBuilderState() {
  const cfg = loadSavedConfig();
  const { toast } = useToast();
  const terminalSidecar = useTerminalSidecarHealth();

  const [dbName, setDbName] = useState("");
  const [dbType, setDbType] = useState<DbType>("nucl");
  const [title, setTitle] = useState("");
  const [fastaData, setFastaData] = useState("");
  const [inputMode, setInputMode] = useState<InputMode>("paste");
  const [fileName, setFileName] = useState("");
  const [copied, setCopied] = useState(false);

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
    { ok: terminalSidecar.isHealthy, label: "Terminal sidecar" },
  ];
  const readyCount = readiness.filter((r) => r.ok).length;
  const allReady = readyCount === readiness.length && !buildMutation.isPending;

  const successPath = buildMutation.data
    ? `blast-db/custom_db/${buildMutation.data.db_name}/${buildMutation.data.db_name}`
    : "";

  const handleCopyPath = () => {
    if (!successPath) return;
    navigator.clipboard.writeText(successPath).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  };

  return {
    cfg,
    dbName,
    setDbName,
    dbType,
    setDbType,
    title,
    setTitle,
    fastaData,
    setFastaData,
    inputMode,
    setInputMode,
    fileName,
    setFileName,
    copied,
    fastaStats,
    isValidDbName,
    nameClash,
    dbListQuery,
    existingDbs,
    buildMutation,
    handleFileUpload,
    readiness,
    readyCount,
    allReady,
    successPath,
    handleCopyPath,
  } as const;
}

export type DatabaseBuilderState = ReturnType<typeof useDatabaseBuilderState>;
