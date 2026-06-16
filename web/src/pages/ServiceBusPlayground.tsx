/**
 * ServiceBusPlayground — preview page to exercise the Service Bus BLAST path.
 *
 * Three panes (producer / sample code / consumer) modelled on an AI Foundry
 * playground:
 *  1. PRODUCER — compose a BLAST request and enqueue it onto the request queue
 *     via the backend (`POST /settings/service-bus/send`). The enqueue runs
 *     under the shared managed identity; no SAS token ever reaches the browser.
 *     Reader-accessible by design.
 *  2. SAMPLE CODE — read-only Python / dashboard-API snippets that stay in sync
 *     with the form, for an external service to copy (send onto the queue and
 *     subscribe to the completion topic).
 *  3. CONSUMER — observe the real path: force a drain pass, and list completion
 *     events the optional demo external consumer received from the topic.
 *
 * Gated behind the Settings → Preview "Service Bus Playground" toggle and the
 * deployment Service Bus integration being active (a disabled integration shows
 * a clear banner and the send is rejected server-side).
 */
import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Copy,
  Eye,
  Loader2,
  Play,
  Radio,
  RefreshCw,
  Send,
} from "lucide-react";

import { formatApiError } from "@/api/client";
import {
  settingsApi,
  type ServiceBusPeekMessage,
  type ServiceBusPeekResponse,
  type ServiceBusSendRequest,
} from "@/api/settings";
import { useToast } from "@/components/Toast";

type CodeTab = "python-send" | "python-consume" | "curl";
type SubmitMode = "xml" | "tabular";

const PROGRAMS = [
  "blastn",
  "blastp",
  "blastx",
  "tblastn",
  "tblastx",
  "psiblast",
  "rpsblast",
  "rpstblastn",
] as const;

// Curated FASTA examples (kept in sync with the API Reference spec presets).
const MONKEYPOX_FASTA =
  [
    ">NC_003310.1:c48509-48048 Monkeypox virus, complete genome",
    "ATGGAGAAGCGAGAAGTTAATAAAGCTCTGTATGATCTTCAACGTAGTACTATGGTGTACAGTTCCGACG",
    "ATACTCCTCCTCGTTGGTCTACGACAATGGATGCTGATACACGGCCTACAGATTCTGATGCTGATGCTAT",
    "AATAGATGATGTATCCCGCGAAAAATCAATGAGAGAGGATAATAAGTCTTTTGATGATGTTATTCCGGTT",
    "AAAAAAATTATTTATTGGAAAGGTGTTAACCCTGTCACCGTTATTAATGAGTACTGCCAAATAACTAGGA",
    "GAGATTGGTCTTTTCGTATTGAATCAGTGGGGCCTAGTAACTCTCCTACATTTTATGCCTGTGTAGACAT",
    "TGACGGAAGAGTATTCGATAAGGCAGATGGAAAATCTAAACGAGATGCTAAAAATAATGCAGCTAAATTG",
    "GCTGTAGATAAACTTCTTAGTTATGTCATCATTAGATTCTGA",
  ].join("\n") + "\n";

const SMALL_16S_FASTA =
  [
    ">NR_024570.1 Escherichia coli str. K-12 substr. MG1655 16S ribosomal RNA, partial sequence",
    "AAATTGAAGAGTTTGATCATGGCTCAGATTGAACGCTGGCGGCAGGCCTAACACATGCAA",
    "GTCGAACGGTAACAGGAAGAAGCTTGCTTCTTTGCTGACGAGTGGCGGACGGGTGAGTAA",
    "TGTCTGGGAAACTGCCTGATGGAGGGGGATAACTACTGGAAACGGTAGCTAATACCGCAT",
    "AACGTCGCAAGACCAAAGAGGGGGACCTTCGGGCCTCTTGCCATCGGATGTGCCCAGATG",
    "GGATTAGCTAGTAGGTGGGGTAACGGCTCACCTAGGCGACGATCCCTAGCTGGTCTGAGA",
    "GGATGACCAGCCACACTGGAACTGAGACACGGTCCAGACTCCTACGGGAGGCAGCAGTGG",
    "GGAATATTGCACAATGGGCGCAAGCCTGATGCAGCCATGCCGCGTGTATGAAGAAGGCCT",
    "TCGGGTTGTAAAGTACTTTCAGCGGGGAGGAAGGGAGTAAAGTTAATACCTTTGCTCATT",
    "GACGTTACCCGCAGAAGAAGCACCGGCTAACTCCGTGCCAGCAGCCGCGGTAATACGGAG",
  ].join("\n") + "\n";

// Web BLAST-equivalent CLI flags for a core_nt run (search-space correction
// makes the e-values match NCBI Web BLAST). No `-outfmt` here — the format is
// carried by the `outfmt` field so the standard columns are never duplicated.
const CORE_NT_EXTRA = "-word_size 28 -dust yes -soft_masking false -searchsp 32156241807668";

interface PlaygroundPreset {
  key: string;
  label: string;
  hint: string;
  fasta: string;
  db: string;
  program: (typeof PROGRAMS)[number];
  taxid: string;
  isInclusive: boolean;
  mode: SubmitMode;
  // XML mode
  wordSize: string;
  evalue: string;
  maxTargetSeqs: string;
  // Tabular mode
  outfmt: string;
  extra: string;
  resourceProfile: string;
}

const PRESETS: PlaygroundPreset[] = [
  {
    key: "16s-xml",
    label: "16S rRNA · XML (fast)",
    hint: "Lightweight: E. coli 16S rRNA against the ~50 MB 16S_ribosomal_RNA DB. Best for a quick end-to-end smoke before core_nt is staged.",
    fasta: SMALL_16S_FASTA,
    db: "16S_ribosomal_RNA",
    program: "blastn",
    taxid: "",
    isInclusive: true,
    mode: "xml",
    wordSize: "28",
    evalue: "0.01",
    maxTargetSeqs: "50",
    outfmt: "7 std staxids sstrand qseq sseq",
    extra: "",
    resourceProfile: "standard",
  },
  {
    key: "core-nt-xml",
    label: "Monkeypox → core_nt · XML",
    hint: "Web BLAST-equivalent core_nt search returning BLAST XML (outfmt 5). core_nt is sharded; the backend auto-promotes the resource profile to core_nt_safe.",
    fasta: MONKEYPOX_FASTA,
    db: "core_nt",
    program: "blastn",
    taxid: "",
    isInclusive: true,
    mode: "xml",
    wordSize: "28",
    evalue: "0.05",
    maxTargetSeqs: "100",
    outfmt: "7 std staxids sstrand qseq sseq",
    extra: "",
    resourceProfile: "core_nt_safe",
  },
  {
    key: "core-nt-tab7",
    label: "Monkeypox → core_nt · Tabular (outfmt 7)",
    hint: "Same core_nt search but tabular output with comment lines (outfmt 7). Routes to the sibling /v1/jobs path; the shard merge re-ranks the standard 12 columns by evalue/bitscore.",
    fasta: MONKEYPOX_FASTA,
    db: "core_nt",
    program: "blastn",
    taxid: "",
    isInclusive: true,
    mode: "tabular",
    wordSize: "28",
    evalue: "0.05",
    maxTargetSeqs: "100",
    outfmt: "7",
    extra: CORE_NT_EXTRA,
    resourceProfile: "core_nt_safe",
  },
  {
    key: "core-nt-multitoken",
    label: "Monkeypox → core_nt · Multi-token (7 std staxids …)",
    hint: "Adds taxonomy + strand + sequence columns via an extended outfmt specifier. `std` MUST lead so the shard merge can re-rank by the fixed std positions; the trailing columns (staxids/sstrand/qseq/sseq) are preserved.",
    fasta: MONKEYPOX_FASTA,
    db: "core_nt",
    program: "blastn",
    taxid: "",
    isInclusive: true,
    mode: "tabular",
    wordSize: "28",
    evalue: "0.05",
    maxTargetSeqs: "100",
    outfmt: "7 std staxids sstrand qseq sseq",
    extra: CORE_NT_EXTRA,
    resourceProfile: "core_nt_safe",
  },
];

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "7px 10px",
  borderRadius: 6,
  border: "1px solid var(--border-medium)",
  background: "var(--bg-tertiary)",
  color: "var(--text-primary)",
  fontSize: 13,
  boxSizing: "border-box",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: 11,
  color: "var(--text-secondary)",
  marginBottom: 4,
};

const DEFAULT_PRESET = PRESETS[0];

export function ServiceBusPlayground() {
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const [presetKey, setPresetKey] = useState(DEFAULT_PRESET.key);
  const [queryFasta, setQueryFasta] = useState(DEFAULT_PRESET.fasta);
  const [db, setDb] = useState(DEFAULT_PRESET.db);
  const [program, setProgram] = useState<(typeof PROGRAMS)[number]>(DEFAULT_PRESET.program);
  const [taxid, setTaxid] = useState(DEFAULT_PRESET.taxid);
  const [isInclusive, setIsInclusive] = useState(DEFAULT_PRESET.isInclusive);
  const [mode, setMode] = useState<SubmitMode>(DEFAULT_PRESET.mode);
  const [wordSize, setWordSize] = useState(DEFAULT_PRESET.wordSize);
  const [evalue, setEvalue] = useState(DEFAULT_PRESET.evalue);
  const [maxTargetSeqs, setMaxTargetSeqs] = useState(DEFAULT_PRESET.maxTargetSeqs);
  const [outfmt, setOutfmt] = useState(DEFAULT_PRESET.outfmt);
  const [extra, setExtra] = useState(DEFAULT_PRESET.extra);
  const [resourceProfile, setResourceProfile] = useState(DEFAULT_PRESET.resourceProfile);
  const [requestId, setRequestId] = useState("");
  const [codeTab, setCodeTab] = useState<CodeTab>("python-send");
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [recentSends, setRecentSends] = useState<
    Array<{ correlationId: string; messageId: string; at: string }>
  >([]);

  const applyPreset = useCallback((key: string) => {
    const preset = PRESETS.find((p) => p.key === key);
    if (!preset) return;
    setPresetKey(preset.key);
    setQueryFasta(preset.fasta);
    setDb(preset.db);
    setProgram(preset.program);
    setTaxid(preset.taxid);
    setIsInclusive(preset.isInclusive);
    setMode(preset.mode);
    setWordSize(preset.wordSize);
    setEvalue(preset.evalue);
    setMaxTargetSeqs(preset.maxTargetSeqs);
    setOutfmt(preset.outfmt);
    setExtra(preset.extra);
    setResourceProfile(preset.resourceProfile);
  }, []);

  const status = useQuery({
    queryKey: ["service-bus", "status"],
    queryFn: () => settingsApi.getServiceBus(),
    refetchInterval: 15000,
  });

  const observed = useQuery({
    queryKey: ["service-bus", "observed-completions"],
    queryFn: () => settingsApi.getObservedCompletions(50),
    refetchInterval: 10000,
  });

  // On-demand peek of the actual messages currently in the request queue.
  // Disabled by default (no auto-poll) so it only runs when the operator clicks
  // "Peek"; uses the data-plane Receiver claim so it works even when the
  // Manage-claim runtime counts above are unavailable.
  const peek = useQuery({
    queryKey: ["service-bus", "peek"],
    queryFn: () => settingsApi.peekServiceBus(10),
    enabled: false,
    refetchOnWindowFocus: false,
  });

  const effectiveEnabled = status.data?.effective_enabled ?? false;
  const namespaceFqdn = status.data?.config.namespace_fqdn ?? "<namespace>.servicebus.windows.net";
  const requestQueue = status.data?.config.request_queue ?? "elastic-blast-requests";
  const completionQueue = status.data?.config.completion_queue ?? "elastic-blast-results";
  const completionTopic = status.data?.config.completion_topic ?? "elastic-blast-completions";
  const observerSubscription = observed.data?.subscription ?? "playground-observer";

  const buildBody = useCallback(
    (dryRun: boolean): ServiceBusSendRequest => {
      const body: ServiceBusSendRequest = {
        query_fasta: queryFasta,
        db: db.trim(),
        program,
      };
      if (mode === "tabular") {
        // Free-form /v1/jobs path — multi-token outfmt + raw extra flags.
        body.blast_options = {
          evalue: Number(evalue) || 0.05,
          max_target_seqs: Number(maxTargetSeqs) || 500,
        };
        const of = outfmt.trim();
        if (of) body.blast_options.outfmt = of;
        const ex = extra.trim();
        if (ex) body.blast_options.extra = ex;
        const rp = resourceProfile.trim();
        if (rp) body.resource_profile = rp;
      } else {
        // XML-locked /api/v1/elastic-blast/submit path.
        body.options = {
          outfmt: 5,
          word_size: Number(wordSize) || 28,
          evalue: Number(evalue) || 0.05,
          max_target_seqs: Number(maxTargetSeqs) || 500,
        };
        const rp = resourceProfile.trim();
        if (rp && rp !== "standard") body.resource_profile = rp;
      }
      const taxidNum = Number(taxid.trim());
      if (taxid.trim() && Number.isFinite(taxidNum) && taxidNum >= 1) {
        body.taxid = taxidNum;
        body.is_inclusive = isInclusive;
      }
      const rid = requestId.trim();
      if (rid) body.request_id = rid;
      if (dryRun) body.dry_run = true;
      return body;
    },
    [
      queryFasta,
      db,
      program,
      mode,
      wordSize,
      evalue,
      maxTargetSeqs,
      outfmt,
      extra,
      resourceProfile,
      taxid,
      isInclusive,
      requestId,
    ],
  );

  const sendMutation = useMutation({
    mutationFn: (dryRun: boolean) => settingsApi.sendServiceBus(buildBody(dryRun)),
    onSuccess: (res) => {
      if (res.status === "valid") {
        setLastResult(`Validated (no message sent) · corr ${res.external_correlation_id}`);
        toast("Request is valid — not enqueued (dry run).", "success");
        return;
      }
      setLastResult(
        `Queued · message_id ${res.message_id ?? "?"} · corr ${res.external_correlation_id}`,
      );
      setRecentSends((prev) =>
        [
          {
            correlationId: res.external_correlation_id,
            messageId: res.message_id ?? "",
            at: new Date().toISOString(),
          },
          ...prev,
        ].slice(0, 20),
      );
      toast("Message enqueued onto the request queue.", "success");
      void queryClient.invalidateQueries({ queryKey: ["service-bus", "status"] });
      // If the operator has already peeked, refresh it so the just-sent message
      // shows up in the content list right away.
      if (peek.data) void peek.refetch();
    },
    onError: (err) => {
      const msg = formatApiError(err, "Send failed");
      setLastResult(msg);
      toast(msg, "error");
    },
  });

  const drainMutation = useMutation({
    mutationFn: () => settingsApi.drainServiceBus(),
    onSuccess: (res) => {
      toast(
        `Drain pass complete — received ${res.received ?? 0}, completed ${res.completed ?? 0}.`,
        "success",
      );
      void queryClient.invalidateQueries({ queryKey: ["service-bus", "status"] });
      void observed.refetch();
      if (peek.data) void peek.refetch();
    },
    onError: (err) => toast(formatApiError(err, "Drain failed"), "error"),
  });

  const sampleCode = useMemo(
    () =>
      buildSampleCode(codeTab, {
        namespaceFqdn,
        requestQueue,
        completionQueue,
        completionTopic,
        observerSubscription,
        body: buildBody(false),
      }),
    [codeTab, namespaceFqdn, requestQueue, completionQueue, completionTopic, observerSubscription, buildBody],
  );

  const copyCode = useCallback(() => {
    void navigator.clipboard.writeText(sampleCode).then(
      () => toast("Sample code copied.", "success"),
      () => toast("Copy failed.", "error"),
    );
  }, [sampleCode, toast]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <header className="glass-card" style={{ display: "grid", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Radio size={18} strokeWidth={1.5} />
          <h1 style={{ margin: 0, fontSize: 18 }}>Service Bus Playground</h1>
          <span className="badge" style={{ fontSize: 10 }}>
            preview
          </span>
          <span style={{ flex: 1 }} />
          <StatusPill enabled={effectiveEnabled} loading={status.isLoading} />
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 13, lineHeight: 1.5 }}>
          Enqueue a BLAST request onto <code>{requestQueue}</code> and watch the real
          consumer pick it up and execute it. Completion events are published to the{" "}
          <code>{completionQueue}</code> result queue for an external service to drain.
        </p>
      </header>

      {!effectiveEnabled && !status.isLoading && (
        <div
          className="glass-card"
          role="status"
          style={{ display: "flex", gap: 10, alignItems: "flex-start" }}
        >
          <AlertTriangle size={16} style={{ color: "var(--warning)", marginTop: 2 }} />
          <div style={{ fontSize: 13, lineHeight: 1.5 }}>
            The Service Bus integration is not active. Sends are rejected until both the
            deployment switch <code>SERVICEBUS_ENABLED</code> and the saved Settings →
            Service Bus config are on. You can still preview the sample code below.
          </div>
        </div>
      )}

      <div
        style={{
          display: "grid",
          gap: 16,
          gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
          alignItems: "start",
        }}
      >
        {/* Pane 1 — Producer */}
        <section className="glass-card" style={{ display: "grid", gap: 12 }}>
          <h2 style={{ margin: 0, fontSize: 14 }}>① Request</h2>
          <div>
            <label style={labelStyle} htmlFor="pg-preset">
              Example
            </label>
            <select
              id="pg-preset"
              value={presetKey}
              onChange={(e) => applyPreset(e.target.value)}
              style={inputStyle}
            >
              {PRESETS.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
            </select>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 11, lineHeight: 1.5 }}>
              {PRESETS.find((p) => p.key === presetKey)?.hint}
            </p>
          </div>
          <div>
            <label style={labelStyle}>Output format</label>
            <div style={{ display: "flex", gap: 6 }}>
              <ModeButton active={mode === "xml"} onClick={() => setMode("xml")}>
                XML · outfmt 5
              </ModeButton>
              <ModeButton active={mode === "tabular"} onClick={() => setMode("tabular")}>
                Tabular · multi-token
              </ModeButton>
            </div>
          </div>
          <div>
            <label style={labelStyle} htmlFor="pg-fasta">
              Query FASTA
            </label>
            <textarea
              id="pg-fasta"
              value={queryFasta}
              onChange={(e) => setQueryFasta(e.target.value)}
              rows={5}
              style={{ ...inputStyle, fontFamily: "var(--font-mono, monospace)", resize: "vertical" }}
            />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div>
              <label style={labelStyle} htmlFor="pg-db">
                Database
              </label>
              <input
                id="pg-db"
                value={db}
                onChange={(e) => setDb(e.target.value)}
                style={inputStyle}
              />
            </div>
            <div>
              <label style={labelStyle} htmlFor="pg-program">
                Program
              </label>
              <select
                id="pg-program"
                value={program}
                onChange={(e) => setProgram(e.target.value as (typeof PROGRAMS)[number])}
                style={inputStyle}
              >
                {PROGRAMS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div>
              <label style={labelStyle} htmlFor="pg-taxid">
                Taxid (optional)
              </label>
              <input
                id="pg-taxid"
                value={taxid}
                onChange={(e) => setTaxid(e.target.value)}
                placeholder="e.g. 3431483"
                inputMode="numeric"
                style={inputStyle}
              />
            </div>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                fontSize: 12,
                alignSelf: "end",
                paddingBottom: 7,
                color: taxid.trim() ? "var(--text-primary)" : "var(--text-tertiary)",
              }}
            >
              <input
                type="checkbox"
                checked={isInclusive}
                disabled={!taxid.trim()}
                onChange={(e) => setIsInclusive(e.target.checked)}
              />
              Inclusive
            </label>
          </div>
          {mode === "xml" ? (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
                <div>
                  <label style={labelStyle} htmlFor="pg-word">
                    word_size
                  </label>
                  <input
                    id="pg-word"
                    value={wordSize}
                    onChange={(e) => setWordSize(e.target.value)}
                    inputMode="numeric"
                    style={inputStyle}
                  />
                </div>
                <div>
                  <label style={labelStyle} htmlFor="pg-evalue">
                    evalue
                  </label>
                  <input
                    id="pg-evalue"
                    value={evalue}
                    onChange={(e) => setEvalue(e.target.value)}
                    style={inputStyle}
                  />
                </div>
                <div>
                  <label style={labelStyle} htmlFor="pg-mts">
                    max_target
                  </label>
                  <input
                    id="pg-mts"
                    value={maxTargetSeqs}
                    onChange={(e) => setMaxTargetSeqs(e.target.value)}
                    inputMode="numeric"
                    style={inputStyle}
                  />
                </div>
              </div>
              <p className="muted" style={{ margin: 0, fontSize: 11, lineHeight: 1.5 }}>
                <code>outfmt</code> is fixed to <code>5</code> (BLAST XML) — the XML→FASTA
                result pipeline needs it. The message is bridged to{" "}
                <code>/api/v1/elastic-blast/submit</code>.
              </p>
            </>
          ) : (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                <div>
                  <label style={labelStyle} htmlFor="pg-evalue-t">
                    evalue
                  </label>
                  <input
                    id="pg-evalue-t"
                    value={evalue}
                    onChange={(e) => setEvalue(e.target.value)}
                    style={inputStyle}
                  />
                </div>
                <div>
                  <label style={labelStyle} htmlFor="pg-mts-t">
                    max_target
                  </label>
                  <input
                    id="pg-mts-t"
                    value={maxTargetSeqs}
                    onChange={(e) => setMaxTargetSeqs(e.target.value)}
                    inputMode="numeric"
                    style={inputStyle}
                  />
                </div>
              </div>
              <div>
                <label style={labelStyle} htmlFor="pg-outfmt">
                  outfmt <span className="muted">(multi-token — keep std leading)</span>
                </label>
                <input
                  id="pg-outfmt"
                  value={outfmt}
                  onChange={(e) => setOutfmt(e.target.value)}
                  placeholder="7 std staxids sstrand qseq sseq"
                  style={{ ...inputStyle, fontFamily: "var(--font-mono, monospace)" }}
                />
              </div>
              <div>
                <label style={labelStyle} htmlFor="pg-extra">
                  extra <span className="muted">(raw CLI flags — no -outfmt here)</span>
                </label>
                <input
                  id="pg-extra"
                  value={extra}
                  onChange={(e) => setExtra(e.target.value)}
                  placeholder="-word_size 28 -dust yes -soft_masking false"
                  style={{ ...inputStyle, fontFamily: "var(--font-mono, monospace)" }}
                />
              </div>
              <div>
                <label style={labelStyle} htmlFor="pg-rp">
                  resource_profile
                </label>
                <input
                  id="pg-rp"
                  value={resourceProfile}
                  onChange={(e) => setResourceProfile(e.target.value)}
                  placeholder="standard"
                  style={inputStyle}
                />
              </div>
              <p className="muted" style={{ margin: 0, fontSize: 11, lineHeight: 1.5 }}>
                Tabular messages route to the sibling <code>/v1/jobs</code>. A sharded DB
                (e.g. <code>core_nt</code>) re-ranks the standard 12 columns by
                evalue/bitscore, so a tabular layout must keep <code>std</code> leading.
              </p>
            </>
          )}
          <div>
            <label style={labelStyle} htmlFor="pg-request-id">
              request_id <span className="muted">(optional pass-through)</span>
            </label>
            <input
              id="pg-request-id"
              value={requestId}
              onChange={(e) => setRequestId(e.target.value)}
              placeholder="e.g. caller tracking id — echoed to the completion topic"
              maxLength={256}
              style={inputStyle}
            />
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button
              type="button"
              className="glass-button"
              onClick={() => sendMutation.mutate(true)}
              disabled={sendMutation.isPending}
            >
              Validate
            </button>
            <button
              type="button"
              className="glass-button glass-button--primary"
              onClick={() => sendMutation.mutate(false)}
              disabled={sendMutation.isPending || !effectiveEnabled}
              title={!effectiveEnabled ? "Service Bus integration is not active" : undefined}
            >
              {sendMutation.isPending ? (
                <Loader2 size={14} className="spin" />
              ) : (
                <Send size={14} />
              )}{" "}
              Send
            </button>
          </div>
          {lastResult && (
            <div
              style={{
                fontSize: 12,
                padding: "8px 10px",
                borderRadius: 6,
                background: "var(--bg-tertiary)",
                border: "1px solid var(--border-subtle)",
                wordBreak: "break-all",
              }}
            >
              {lastResult}
            </div>
          )}
        </section>

        {/* Pane 2 — Sample code */}
        <section className="glass-card" style={{ display: "grid", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: 14 }}>② Sample code</h2>
            <span style={{ flex: 1 }} />
            <button type="button" className="glass-button" onClick={copyCode}>
              <Copy size={13} /> Copy
            </button>
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <CodeTabButton active={codeTab === "python-send"} onClick={() => setCodeTab("python-send")}>
              Python · send
            </CodeTabButton>
            <CodeTabButton
              active={codeTab === "python-consume"}
              onClick={() => setCodeTab("python-consume")}
            >
              Python · consume
            </CodeTabButton>
            <CodeTabButton active={codeTab === "curl"} onClick={() => setCodeTab("curl")}>
              Dashboard API
            </CodeTabButton>
          </div>
          <pre
            style={{
              margin: 0,
              padding: 12,
              borderRadius: 8,
              background: "var(--bg-code, #0d1117)",
              color: "var(--text-code, #c9d1d9)",
              fontSize: 12,
              lineHeight: 1.5,
              overflowX: "auto",
              maxHeight: 420,
            }}
          >
            <code>{sampleCode}</code>
          </pre>
          {codeTab === "python-consume" && (
            <p className="muted" style={{ margin: 0, fontSize: 11, lineHeight: 1.5 }}>
              An external service runs this against its own subscription on the{" "}
              <code>{completionTopic}</code> topic (needs{" "}
              <code>Azure Service Bus Data Receiver</code>). Topic fan-out gives every
              subscription its own copy, so a subscriber never competes with the
              dashboard for messages.
            </p>
          )}
        </section>

        {/* Pane 3 — Consumer */}
        <section className="glass-card" style={{ display: "grid", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: 14 }}>③ Consumer</h2>
            <span style={{ flex: 1 }} />
            <button
              type="button"
              className="glass-button"
              onClick={() => void observed.refetch()}
              title="Refresh observed completions"
            >
              <RefreshCw size={13} />
            </button>
          </div>

          <div style={{ display: "grid", gap: 4, fontSize: 12 }}>
            <CountRow
              label="Queue depth (active)"
              value={status.data?.counts.queue?.active_message_count}
            />
            <CountRow
              label="Dead-letter"
              value={status.data?.counts.queue?.dead_letter_message_count}
            />
          </div>

          {/* Non-destructive content view: peek the actual messages in the
              request queue. Works even when the Manage-claim counts above are
              unavailable (peek needs only the data-plane Receiver claim). */}
          <div style={{ display: "grid", gap: 6 }}>
            <button
              type="button"
              className="glass-button"
              onClick={() => void peek.refetch()}
              disabled={peek.isFetching || !effectiveEnabled}
              title={
                !effectiveEnabled ? "Service Bus integration is not active" : "Peek queue messages"
              }
            >
              {peek.isFetching ? <Loader2 size={14} className="spin" /> : <Eye size={14} />} Peek
              messages
            </button>
            <PeekResults peek={peek} />
          </div>

          <button
            type="button"
            className="glass-button glass-button--primary"
            onClick={() => drainMutation.mutate()}
            disabled={drainMutation.isPending || !effectiveEnabled}
            title={!effectiveEnabled ? "Service Bus integration is not active" : undefined}
          >
            {drainMutation.isPending ? <Loader2 size={14} className="spin" /> : <Play size={14} />}{" "}
            Run consumer now
          </button>
          <p className="muted" style={{ margin: 0, fontSize: 11, lineHeight: 1.5 }}>
            Triggers one real <code>drain_and_resubmit</code> pass — the same the 30 s beat
            runs — so a just-sent message is bridged to BLAST execution immediately.
          </p>

          {recentSends.length > 0 && (
            <div style={{ display: "grid", gap: 4 }}>
              <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>Recent sends</div>
              {recentSends.slice(0, 5).map((s) => (
                <div
                  key={s.correlationId}
                  style={{ fontSize: 11, fontFamily: "var(--font-mono, monospace)", opacity: 0.85 }}
                >
                  {s.correlationId.slice(0, 12)}… → {s.messageId.slice(0, 10) || "queued"}
                </div>
              ))}
            </div>
          )}

          <div style={{ display: "grid", gap: 6 }}>
            <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
              Completions observed on <code>{observerSubscription}</code>
              {observed.data && !observed.data.consumer_enabled && " (demo consumer off)"}
            </div>
            {observed.data && observed.data.events.length === 0 && (
              <div className="muted" style={{ fontSize: 12 }}>
                No completion events observed yet.
              </div>
            )}
            {observed.data?.events.slice(0, 10).map((e) => (
              <div
                key={e.event_id || `${e.external_correlation_id}-${e.status}`}
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  fontSize: 11,
                  padding: "5px 8px",
                  borderRadius: 6,
                  background: "var(--bg-tertiary)",
                }}
              >
                <StatusDot status={e.status} />
                <span style={{ fontFamily: "var(--font-mono, monospace)" }}>
                  {e.external_correlation_id.slice(0, 12)}…
                </span>
                {e.request_id && (
                  <span
                    title={`request_id: ${e.request_id}`}
                    style={{
                      fontFamily: "var(--font-mono, monospace)",
                      opacity: 0.7,
                      padding: "1px 5px",
                      borderRadius: 4,
                      background: "var(--bg-secondary)",
                    }}
                  >
                    req {e.request_id.slice(0, 12)}
                    {e.request_id.length > 12 ? "…" : ""}
                  </span>
                )}
                <span style={{ flex: 1 }} />
                <span style={{ opacity: 0.8 }}>{e.status}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

function CodeTabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="glass-button"
      style={{
        fontSize: 11,
        padding: "4px 10px",
        background: active ? "var(--accent-subtle, var(--bg-tertiary))" : "transparent",
        borderColor: active ? "var(--accent)" : "var(--border-subtle)",
      }}
    >
      {children}
    </button>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="glass-button"
      style={{
        flex: 1,
        fontSize: 12,
        padding: "6px 10px",
        background: active ? "var(--accent-subtle, var(--bg-tertiary))" : "transparent",
        borderColor: active ? "var(--accent)" : "var(--border-subtle)",
        color: active ? "var(--text-primary)" : "var(--text-secondary)",
      }}
    >
      {children}
    </button>
  );
}

function StatusPill({ enabled, loading }: { enabled: boolean; loading: boolean }) {
  if (loading) {
    return (
      <span className="muted" style={{ fontSize: 12 }}>
        <Loader2 size={12} className="spin" /> checking…
      </span>
    );
  }
  return (
    <span
      style={{
        fontSize: 12,
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        color: enabled ? "var(--success)" : "var(--text-tertiary)",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: 999,
          background: enabled ? "var(--success)" : "var(--text-tertiary)",
        }}
      />
      {enabled ? "integration active" : "integration off"}
    </span>
  );
}

function CountRow({ label, value }: { label: string; value: number | undefined }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span className="muted">{label}</span>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{value ?? "—"}</span>
    </div>
  );
}

const PEEK_REASON_LABELS: Record<string, string> = {
  not_configured: "namespace not configured",
  disabled: "integration not active",
  auth_failed: "no Service Bus Data Receiver claim",
  unavailable: "namespace unreachable",
  error: "peek failed",
};

/** Renders the result of a non-destructive queue peek: a status line plus, when
 *  available, the sanitised content of each message currently in the queue. */
function PeekResults({
  peek,
}: {
  peek: {
    data?: ServiceBusPeekResponse;
    isFetching: boolean;
    isError: boolean;
    isFetched: boolean;
  };
}) {
  if (peek.isError) {
    return (
      <div className="muted" style={{ fontSize: 12 }}>
        Peek failed. Try again.
      </div>
    );
  }
  if (!peek.data) {
    if (peek.isFetching) {
      return (
        <div className="muted" style={{ fontSize: 12 }}>
          Peeking…
        </div>
      );
    }
    return (
      <p className="muted" style={{ margin: 0, fontSize: 11, lineHeight: 1.5 }}>
        Non-destructively shows the messages currently sitting in the request queue, including
        their content — without removing or locking them.
      </p>
    );
  }
  const data = peek.data;
  if (!data.available) {
    return (
      <div className="muted" style={{ fontSize: 12 }}>
        Content unavailable
        {data.reason ? ` (${PEEK_REASON_LABELS[data.reason] ?? data.reason})` : ""}.
      </div>
    );
  }
  if (data.messages.length === 0) {
    return (
      <div className="muted" style={{ fontSize: 12 }}>
        Queue is empty — no messages to peek.
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
        {data.count} message{data.count === 1 ? "" : "s"} in <code>{data.queue}</code>
      </div>
      {data.messages.map((m, i) => (
        <PeekMessageItem key={`${m.sequence_number ?? m.message_id ?? "msg"}-${i}`} message={m} />
      ))}
    </div>
  );
}

function PeekMessageItem({ message }: { message: ServiceBusPeekMessage }) {
  return (
    <div
      style={{
        display: "grid",
        gap: 4,
        padding: "8px 10px",
        borderRadius: 6,
        background: "var(--bg-tertiary)",
        border: "1px solid var(--border-subtle)",
      }}
    >
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", fontSize: 11 }}>
        {message.program ? (
          <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{message.program}</span>
        ) : null}
        {message.db ? <span className="muted">db {message.db}</span> : null}
        {message.correlation_id ? (
          <span
            title={`correlation_id: ${message.correlation_id}`}
            style={{ fontFamily: "var(--font-mono, monospace)", opacity: 0.8 }}
          >
            {message.correlation_id.slice(0, 12)}
            {message.correlation_id.length > 12 ? "…" : ""}
          </span>
        ) : null}
        {message.request_id ? (
          <span
            title={`request_id: ${message.request_id}`}
            style={{
              fontFamily: "var(--font-mono, monospace)",
              opacity: 0.7,
              padding: "1px 5px",
              borderRadius: 4,
              background: "var(--bg-secondary)",
            }}
          >
            req {message.request_id.slice(0, 12)}
            {message.request_id.length > 12 ? "…" : ""}
          </span>
        ) : null}
      </div>
      <pre
        style={{
          margin: 0,
          padding: 8,
          borderRadius: 6,
          background: "var(--bg-code, #0d1117)",
          color: "var(--text-code, #c9d1d9)",
          fontSize: 11,
          lineHeight: 1.5,
          overflowX: "auto",
          maxHeight: 200,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        <code>{message.body_preview}</code>
      </pre>
      {message.body_truncated ? (
        <span className="muted" style={{ fontSize: 10 }}>
          content truncated
        </span>
      ) : null}
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === "succeeded"
      ? "var(--success)"
      : status === "failed"
        ? "var(--danger, #e06c75)"
        : "var(--accent, #6b9bd1)";
  return (
    <span style={{ width: 8, height: 8, borderRadius: 999, background: color, flex: "0 0 auto" }} />
  );
}

interface CodeContext {
  namespaceFqdn: string;
  requestQueue: string;
  completionQueue: string;
  completionTopic: string;
  observerSubscription: string;
  body: ServiceBusSendRequest;
}

function buildSampleCode(tab: CodeTab, ctx: CodeContext): string {
  const bodyJson = JSON.stringify(stripDryRun(ctx.body), null, 4);
  if (tab === "python-send") {
    return [
      "import json, uuid",
      "from azure.identity import DefaultAzureCredential",
      "from azure.servicebus import ServiceBusClient, ServiceBusMessage",
      "",
      `NAMESPACE = "${ctx.namespaceFqdn}"`,
      `QUEUE = "${ctx.requestQueue}"`,
      "",
      `body = ${bodyJson}`,
      'body["external_correlation_id"] = uuid.uuid4().hex',
      "",
      "with ServiceBusClient(NAMESPACE, DefaultAzureCredential()) as client:",
      "    with client.get_queue_sender(QUEUE) as sender:",
      "        sender.send_messages(",
      "            ServiceBusMessage(",
      "                json.dumps(body),",
      '                content_type="application/json",',
      '                subject="blast.request",',
      '                correlation_id=body["external_correlation_id"],',
      "            )",
      "        )",
      'print("queued", body["external_correlation_id"])',
    ].join("\n");
  }
  if (tab === "python-consume") {
    return [
      "import json",
      "from azure.identity import DefaultAzureCredential",
      "from azure.servicebus import ServiceBusClient",
      "",
      `NAMESPACE = "${ctx.namespaceFqdn}"`,
      `RESULT_QUEUE = "${ctx.completionQueue}"`,
      "",
      "# Messaging is unified on queues: drain the result queue with a queue",
      "# receiver (competing consumer — one external service drains it).",
      "with ServiceBusClient(NAMESPACE, DefaultAzureCredential()) as client:",
      "    receiver = client.get_queue_receiver(RESULT_QUEUE, max_wait_time=10)",
      "    with receiver:",
      "        for msg in receiver:",
      "            event = json.loads(str(msg))",
      "            # event: {event_id, external_correlation_id, openapi_job_id,",
      "            #         status, ts, result_ref}",
      "            print(event['external_correlation_id'], event['status'])",
      "            receiver.complete_message(msg)",
    ].join("\n");
  }
  return [
    "# Enqueue through the dashboard (runs under the managed identity):",
    "TOKEN=$(az account get-access-token --resource <api-client-id> --query accessToken -o tsv)",
    "",
    "curl -X POST \\",
    "  https://<dashboard-host>/api/settings/service-bus/send \\",
    '  -H "Authorization: Bearer $TOKEN" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d '${JSON.stringify(stripDryRun(ctx.body))}'`,
  ].join("\n");
}

function stripDryRun(body: ServiceBusSendRequest): ServiceBusSendRequest {
  const clone = { ...body };
  delete clone.dry_run;
  return clone;
}
