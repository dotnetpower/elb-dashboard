import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { blastApi, type BlastExportFormat, type BlastResultFile } from "@/api/endpoints";
import { useToast } from "@/components/Toast";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";

/** Live byte progress for an in-flight result-file download. */
export interface BlastDownloadProgress {
  received: number;
  total: number | null;
}

export function useBlastResultActions({
  jobId,
  subscriptionId,
  resourceGroup,
  clusterName,
  storageAccount,
}: {
  jobId: string | undefined;
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  storageAccount: string;
}) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { copied, copyText } = useClipboardFeedback();
  const [downloadingFile, setDownloadingFile] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState<BlastDownloadProgress | null>(null);
  const [exportingFormat, setExportingFormat] = useState<BlastExportFormat | null>(null);

  const handleDownload = async (file: BlastResultFile) => {
    if (!jobId) return;
    if (!file.file_id) {
      // Every result file from the listing carries a file_id: local result
      // blobs get a deterministic base64 encoding of their path, and external
      // (OpenAPI / Service Bus) jobs keep their sibling-generated `result-NNN`
      // id. A missing file_id therefore means a malformed listing entry. The
      // removed legacy fallback POSTed to `/results/download` expecting a SAS
      // `download_url`, but that endpoint now streams bytes through the api
      // sidecar (charter §9, no SAS to the browser), so it could never succeed.
      // Surface a clear error instead of silently opening `undefined`.
      toast("This result file cannot be downloaded (missing file id).", "error");
      return;
    }
    setDownloadingFile(file.name);
    setDownloadProgress(null);
    let url: string | null = null;
    try {
      const response = await blastApi.downloadResultFile(
        jobId,
        file.file_id,
        subscriptionId,
        storageAccount,
        resourceGroup,
        (received, total) => setDownloadProgress({ received, total }),
      );
      url = URL.createObjectURL(response.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = response.filename ?? file.name.split("/").pop() ?? `${jobId}-result`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    } catch (e) {
      toast(`Download failed: ${(e as Error).message}`, "error");
    } finally {
      if (url) URL.revokeObjectURL(url);
      setDownloadingFile(null);
      setDownloadProgress(null);
    }
  };

  const handleExport = async (format: BlastExportFormat) => {
    if (!jobId || !subscriptionId || !storageAccount) return;
    setExportingFormat(format);
    let url: string | null = null;
    try {
      const response = await blastApi.exportResults(
        jobId,
        subscriptionId,
        storageAccount,
        format,
        resourceGroup,
      );
      const blob = new Blob([response.text], { type: response.contentType });
      url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = response.filename ?? `${jobId}_results.${format}`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      toast(`Exported ${blastExportFormatLabel(format)} report.`, "success");
    } catch (e) {
      toast(`Export failed: ${(e as Error).message}`, "error");
    } finally {
      if (url) URL.revokeObjectURL(url);
      setExportingFormat(null);
    }
  };

  const cancelMutation = useMutation({
    mutationFn: () =>
      blastApi.cancelJob(jobId!, {
        subscriptionId,
        resourceGroup,
        clusterName,
        storageAccount,
      }),
    onSuccess: () => {
      toast("Job cancelled.", "success");
      queryClient.invalidateQueries({ queryKey: ["blast-job", jobId] });
    },
    onError: (e) => toast(`Cancel failed: ${(e as Error).message}`, "error"),
  });

  const copyJobId = () => {
    if (jobId) copyText(jobId, "jobId");
  };

  return {
    copiedId: copied === "jobId",
    copyJobId,
    downloadingFile,
    downloadProgress,
    exportingFormat,
    handleDownload,
    handleExport,
    cancelMutation,
  };
}

function blastExportFormatLabel(format: BlastExportFormat): string {
  switch (format) {
    case "hit-table-text":
      return "Hit Table (text)";
    case "hit-table-csv":
      return "Hit Table (CSV)";
    case "ncbi-hit-table-text":
      return "NCBI Descriptions (text)";
    case "ncbi-hit-table-csv":
      return "NCBI Descriptions (CSV)";
    case "ncbi-report-text":
      return "NCBI Report (text)";
    case "json-seqalign":
      return "JSON Seq-align";
    case "xml":
      return "XML";
    case "text":
      return "Text";
    default:
      return format.toUpperCase();
  }
}