import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, FileText } from "lucide-react";

import { blastApi } from "@/api/endpoints";

export function ElapsedTimer({ startTime }: { startTime: string }) {
  const [elapsed, setElapsed] = useState("");
  useEffect(() => {
    const start = new Date(startTime).getTime();
    const tick = () => {
      if (document.hidden) return;
      const diff = Math.max(0, Date.now() - start);
      const s = Math.floor(diff / 1000);
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) setElapsed(`${h}h ${m % 60}m ${s % 60}s`);
      else if (m > 0) setElapsed(`${m}m ${s % 60}s`);
      else setElapsed(`${s}s`);
    };
    tick();
    const id = setInterval(tick, 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [startTime]);
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{elapsed}</span>;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// --- Syntax-highlighted file previews ---
// GA-style: no inner scroll. Backend already caps the byte payload via
// `maxBytes` (1000 chars for input.fa, 10000 for elastic-blast.ini) so the
// rendered height stays bounded; the page scroll carries the rest.
export function HighlightedINI({ text }: { text: string }) {
  return (
    <pre
      style={{
        margin: 0,
        padding: "8px 10px",
        borderRadius: 4,
        background: "rgba(0,0,0,0.25)",
        fontSize: 11,
        lineHeight: 1.6,
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
      }}
    >
      {text.split("\n").map((line, i) => {
        if (line.startsWith("["))
          return (
            <div key={i} style={{ color: "var(--accent)", fontWeight: 600 }}>
              {line}
            </div>
          );
        const eq = line.indexOf("=");
        if (eq > 0 && !line.startsWith("#")) {
          return (
            <div key={i}>
              <span style={{ color: "#9aa3b8" }}>{line.slice(0, eq)}</span>
              <span style={{ color: "var(--text-faint)" }}>=</span>
              <span style={{ color: "var(--text-primary)" }}>{line.slice(eq + 1)}</span>
            </div>
          );
        }
        return (
          <div key={i} style={{ color: "var(--text-faint)" }}>
            {line}
          </div>
        );
      })}
    </pre>
  );
}

export function HighlightedFASTA({ text }: { text: string }) {
  const colorMap: Record<string, string> = {
    A: "#6ad6a3",
    T: "#e07b8a",
    G: "#f0c674",
    C: "#7aa7ff",
    U: "#e07b8a",
  };
  return (
    <pre
      style={{
        margin: 0,
        padding: "8px 10px",
        borderRadius: 4,
        background: "rgba(0,0,0,0.25)",
        fontSize: 11,
        lineHeight: 1.6,
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
      }}
    >
      {text.split("\n").map((line, i) => {
        if (line.startsWith(">"))
          return (
            <div key={i} style={{ color: "var(--accent)", fontWeight: 600 }}>
              {line}
            </div>
          );
        return (
          <div key={i}>
            {[...line].map((ch, j) => (
              <span
                key={j}
                style={{ color: colorMap[ch.toUpperCase()] || "var(--text-faint)" }}
              >
                {ch}
              </span>
            ))}
          </div>
        );
      })}
    </pre>
  );
}

export function FilePreview({
  jobId,
  filename,
  blobName,
  subscriptionId,
  storageAccount,
  resourceGroup,
  maxBytes,
}: {
  jobId: string;
  filename: string;
  blobName?: string;
  subscriptionId: string;
  storageAccount: string;
  resourceGroup?: string;
  maxBytes?: number;
}) {
  const q = useQuery({
    queryKey: [
      "blast-file",
      jobId,
      filename,
      blobName,
      subscriptionId,
      storageAccount,
      resourceGroup,
      maxBytes,
    ],
    queryFn: () =>
      blastApi.readJobFile(
        jobId,
        filename,
        subscriptionId,
        storageAccount,
        maxBytes ?? 4096,
        blobName,
        resourceGroup,
      ),
    staleTime: Infinity,
  });
  if (q.isLoading)
    return (
      <span className="muted">
        <Loader2 size={12} className="spin" style={{ verticalAlign: "middle" }} /> Loading{" "}
        {filename}...
      </span>
    );
  if (q.isError)
    return (
      <span className="muted" style={{ fontSize: 11 }}>
        Could not load {filename}
      </span>
    );
  const content = q.data?.content ?? "";
  const truncated = q.data?.truncated;
  return (
    <div>
      <div
        style={{
          fontSize: 11,
          color: "var(--text-faint)",
          marginBottom: 4,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <FileText size={11} /> {filename}
        {truncated && (
          <span
            style={{
              fontSize: 10,
              padding: "1px 6px",
              borderRadius: 3,
              background: "rgba(240,198,116,0.12)",
              color: "var(--warning)",
            }}
          >
            Showing first {(maxBytes ?? 4096).toLocaleString()} chars — file may be longer
          </span>
        )}
      </div>
      {filename.endsWith(".ini") ? (
        <HighlightedINI text={content} />
      ) : filename.endsWith(".fa") || filename.endsWith(".fasta") ? (
        <HighlightedFASTA text={content} />
      ) : (
        <pre
          style={{
            margin: 0,
            padding: "8px 10px",
            borderRadius: 4,
            background: "rgba(0,0,0,0.25)",
            fontSize: 11,
            lineHeight: 1.5,
            whiteSpace: "pre-wrap",
            color: "var(--text-muted)",
          }}
        >
          {content}
        </pre>
      )}
    </div>
  );
}
