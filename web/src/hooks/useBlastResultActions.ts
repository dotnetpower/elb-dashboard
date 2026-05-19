import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { blastApi, type BlastExportFormat, type BlastResultFile } from "@/api/endpoints";
import { useToast } from "@/components/Toast";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";

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
  const [exportingFormat, setExportingFormat] = useState<BlastExportFormat | null>(null);

  const handleDownload = async (file: BlastResultFile) => {
    if (!jobId) return;
    setDownloadingFile(file.name);
    let url: string | null = null;
    try {
      if (file.file_id) {
        const response = await blastApi.downloadResultFile(
          jobId,
          file.file_id,
          subscriptionId,
          storageAccount,
        );
        url = URL.createObjectURL(response.blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = response.filename ?? file.name.split("/").pop() ?? `${jobId}-result`;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
      } else {
        const resp = await blastApi.downloadResult(
          jobId,
          subscriptionId,
          storageAccount,
          file.name,
        );
        window.open(resp.download_url, "_blank");
      }
    } catch (e) {
      toast(`Download failed: ${(e as Error).message}`, "error");
    } finally {
      if (url) URL.revokeObjectURL(url);
      setDownloadingFile(null);
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
      );
      const blob = new Blob([response.text], { type: response.contentType });
      url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = response.filename ?? `${jobId}_results.${format}`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      toast(`Exported ${format.toUpperCase()} report.`, "success");
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
    exportingFormat,
    handleDownload,
    handleExport,
    cancelMutation,
  };
}