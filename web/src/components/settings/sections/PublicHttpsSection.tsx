import { useCallback, useEffect, useRef, useState } from "react";
import { Copy, ExternalLink, Loader2 } from "lucide-react";

import { aksApi, type OpenApiPublicHttpsStatus } from "@/api/aks";
import { formatApiError } from "@/api/client";
import { meApi } from "@/api/me";
import { type AksClusterSummary, monitoringApi } from "@/api/monitoring";
import { msalInstance } from "@/auth/msal";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Badge, Field, Group, Row, Section, StatusLine } from "@/components/settings/primitives";
import {
  clearRunningPublicHttpsTask,
  loadRunningPublicHttpsTask,
  saveRunningPublicHttpsTask,
  type RunningPublicHttpsTask,
} from "@/components/settings/publicHttpsStorage";
import { INPUT_STYLE, SELECT_STYLE } from "@/components/settings/styles";
import { pickPreferredCluster } from "@/utils/clusterSelection";
import { useMonotonicPercent } from "@/hooks/useMonotonicPercent";

// Let's Encrypt rejects ACME account registration when the contact
// email's domain has no public TLD (`urn:ietf:params:acme:error:invalidContact`).
// `.local` / `.localhost` / `.internal` / `.test` / `.example` / `.invalid`
// are the IANA reserved private-use TLDs that trip it most often in our
// deployments (the old `_FALLBACK_OPERATOR_EMAIL = noreply@elb-dashboard.local`
// hit exactly this on elb-cluster-01 on 2026-05-27). The set below is the
// SPA's fallback; on mount the panel fetches the canonical list from
// `aksApi.openApiOperatorEmailRules()` so the client gate cannot drift
// when a new TLD is added to the backend-only `_PRIVATE_USE_TLDS`.
const DEFAULT_PRIVATE_USE_TLDS: readonly string[] = [
  "local",
  "localhost",
  "internal",
  "test",
  "example",
  "invalid",
  "lan",
  "home",
  "corp",
  "private",
];
let PRIVATE_USE_TLDS = new Set(DEFAULT_PRIVATE_USE_TLDS);

function _setPrivateUseTldsForTesting(values: readonly string[]): void {
  PRIVATE_USE_TLDS = new Set(values);
}

function isPublicLetsEncryptEmail(value: string): boolean {
  const text = (value ?? "").trim();
  if (!text || text.length > 254) return false;
  const at = text.indexOf("@");
  if (at <= 0 || at !== text.lastIndexOf("@")) return false;
  const local = text.slice(0, at);
  const domain = text.slice(at + 1).toLowerCase();
  if (!local || !domain || domain.includes("..") || domain.endsWith(".")) return false;
  const labels = domain.split(".");
  if (labels.length < 2 || labels.some((label) => !label)) return false;
  const tld = labels[labels.length - 1];
  if (!/^[a-z]{2,}$/.test(tld)) return false;
  if (PRIVATE_USE_TLDS.has(tld)) return false;
  return /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$/.test(text);
}

// Ordered phases emitted by `setup_openapi_public_https.record_progress(...)`
// in api/tasks/openapi/public_https.py. Keep the ids in sync with the task;
// the SPA renders the stepper from this list and falls back to the raw
// phase id when the backend ships a new phase before the SPA catches up.
interface PublicHttpsPhaseMeta {
  id: string;
  label: string;
  hint?: string;
}
const PUBLIC_HTTPS_PHASES: PublicHttpsPhaseMeta[] = [
  { id: "queued", label: "Queued", hint: "Waiting for a Celery worker slot." },
  { id: "ensure_kubeconfig", label: "Fetching AKS kubeconfig", hint: "Resolving cluster-admin credentials via ARM." },
  { id: "install_ingress_nginx", label: "Installing ingress-nginx", hint: "Applying the upstream manifest patched for the blastpool." },
  { id: "patch_dns_label", label: "Patching Azure DNS label", hint: "Annotating the LoadBalancer Service with a stable FQDN." },
  { id: "wait_external_ip", label: "Waiting for public IP", hint: "Azure assigns the LB's EXTERNAL-IP. Usually 30-90s." },
  { id: "ensure_node_subnet_nsg", label: "Opening node-subnet firewall", hint: "Allowing Internet 80/443 to the LB on the BYO node-subnet NSG so the ACME challenge can reach ingress." },
  { id: "install_cert_manager", label: "Installing cert-manager", hint: "Applying CRDs + controller + webhook." },
  { id: "wait_cert_manager_webhook", label: "Waiting for cert-manager webhook", hint: "Webhook Pod must become Available before any Issuer applies." },
  { id: "apply_cluster_issuer", label: "Applying ClusterIssuer", hint: "Registering the Let's Encrypt prod ACME account." },
  { id: "wait_ingress_nginx_controller", label: "Waiting for ingress-nginx controller", hint: "Controller Pod must be Ready before the admission webhook can validate Ingresses." },
  { id: "wait_admission_jobs_complete", label: "Waiting for admission bootstrap Jobs", hint: "Generates the admission webhook's TLS keypair." },
  { id: "wait_admission_endpoints_ready", label: "Waiting for admission endpoints", hint: "EndpointSlice must list the controller Pod before applying the Ingress." },
  { id: "apply_ingress", label: "Applying elb-openapi Ingress", hint: "Routing the cloudapp.azure.com FQDN to the in-cluster Service." },
  { id: "wait_certificate_ready", label: "Waiting for TLS certificate", hint: "Let's Encrypt HTTP-01 challenge. First-time issuance takes 1-3 min." },
  { id: "persist_runtime_cache", label: "Saving runtime cache", hint: "Storing the public base URL so the dashboard flips to HTTPS." },
];
const PUBLIC_HTTPS_PHASE_INDEX = new Map(PUBLIC_HTTPS_PHASES.map((p, i) => [p.id, i] as const));

function lookupPublicHttpsPhase(phase: string): { meta: PublicHttpsPhaseMeta; index: number; total: number } {
  const idx = PUBLIC_HTTPS_PHASE_INDEX.get(phase) ?? 0;
  const meta = PUBLIC_HTTPS_PHASES[idx] ?? {
    id: phase || "queued",
    label: phase ? phase.replace(/_/g, " ") : "Queued",
  };
  return { meta, index: idx, total: PUBLIC_HTTPS_PHASES.length };
}

function formatElapsedSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0s";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem === 0 ? `${m}m` : `${m}m ${rem}s`;
}

/**
 * Public HTTPS endpoint settings — drives `setup_openapi_public_https`
 * / `disable_openapi_public_https`. Installs ingress-nginx + cert-manager
 * on the selected AKS cluster and exposes elb-openapi over an
 * Azure-issued FQDN with a Let's Encrypt cert. Mirrors AksSection's
 * cluster discovery so the dropdown also lists clusters outside the
 * dashboard anchor RG.
 */
export function PublicHttpsSection({ config }: { config: ResourceConfig | null }) {
  const [clusterName, setClusterName] = useState("");
  const [availableClusters, setAvailableClusters] = useState<AksClusterSummary[]>([]);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersLoaded, setClustersLoaded] = useState(false);
  const [status, setStatus] = useState<OpenApiPublicHttpsStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [emailEdited, setEmailEdited] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Running task is hydrated from localStorage so switching to another
  // Settings tab (which unmounts this component) and coming back keeps
  // the spinner + stepper showing the live Celery progress instead of
  // re-enabling the Enable button while a setup is still running in the
  // background.
  const [runningTask, setRunningTask] = useState<RunningPublicHttpsTask | null>(() =>
    typeof window === "undefined" ? null : loadRunningPublicHttpsTask(),
  );
  const [taskPhase, setTaskPhase] = useState<string>(() =>
    runningTask ? "queued" : "",
  );
  const [elapsedSec, setElapsedSec] = useState<number>(() =>
    runningTask ? Math.max(0, Math.floor((Date.now() - runningTask.startedAt) / 1000)) : 0,
  );
  const pollTimer = useRef<number | null>(null);
  const taskRunning = runningTask !== null;

  // Monotonic progress for the setup stepper. The phase index drives the bar;
  // the backend phases are mostly forward-only, but a wait/retry phase could
  // re-emit an earlier id, so clamp the bar so it never rewinds within one run.
  // The reset key is this run's start so a new Enable/Disable begins from the
  // bottom.
  const rawPublicHttpsPct = (() => {
    const { index, total } = lookupPublicHttpsPhase(taskPhase || "queued");
    return Math.max(2, Math.min(100, Math.round(((index + 1) / total) * 100)));
  })();
  const publicHttpsProgressPct = useMonotonicPercent(rawPublicHttpsPct, {
    resetKey: `${runningTask?.startedAt ?? ""}`,
    active: taskRunning,
  });

  // Keep the latest "which cluster is this task for" identifier in a ref
  // so `pollTask` can read it without depending on `clusterName` /
  // `runningTask` — pollTask is intentionally stable (only depends on
  // `refresh`) because the resume-on-mount effect below assumes it does
  // not change per render.
  const clusterRef = useRef("");
  clusterRef.current = runningTask?.cluster || clusterName;

  // Tick the elapsed counter while a task is running.
  useEffect(() => {
    if (!runningTask) return;
    const interval = window.setInterval(() => {
      setElapsedSec(Math.max(0, Math.floor((Date.now() - runningTask.startedAt) / 1000)));
    }, 1_000);
    return () => window.clearInterval(interval);
  }, [runningTask]);

  // Auto-fill the operator email from the validated caller identity
  // (`/api/me`'s `upn`) with MSAL `account.username` as a fallback.
  // Let's Encrypt rejects `.local` / `.localhost` / `.internal` TLDs at
  // ACME account registration time with `urn:ietf:params:acme:error:invalidContact`,
  // so the Enable button is gated on a public TLD even when the field
  // is auto-populated.
  useEffect(() => {
    // Sync the SPA's private-TLD set with the backend so the client gate
    // does not drift when a new TLD is added server-side. Best-effort;
    // the hard-coded fallback set above is the safety net.
    let cancelled = false;
    void (async () => {
      try {
        const rules = await aksApi.openApiOperatorEmailRules();
        if (cancelled) return;
        const union = new Set<string>(DEFAULT_PRIVATE_USE_TLDS);
        for (const tld of rules.private_use_tlds ?? []) {
          if (typeof tld === "string" && tld) union.add(tld.toLowerCase());
        }
        _setPrivateUseTldsForTesting(Array.from(union));
      } catch {
        // Backend route not yet deployed / 401 / network — fall back to
        // the hard-coded default set, which is the same one the backend
        // ships with today.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (emailEdited) return;
    let cancelled = false;
    void (async () => {
      let candidate = "";
      try {
        const me = await meApi.get();
        candidate = (me.upn ?? "").trim();
      } catch {
        candidate = "";
      }
      if (!candidate) {
        const account = msalInstance.getActiveAccount();
        candidate = (account?.username ?? "").trim();
      }
      if (cancelled || !candidate || !isPublicLetsEncryptEmail(candidate)) {
        return;
      }
      setEmail((current) => (current ? current : candidate));
    })();
    return () => {
      cancelled = true;
    };
  }, [emailEdited]);

  useEffect(
    () => () => {
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
    },
    [],
  );

  const selectedClusterRg =
    availableClusters.find((c) => c.name === clusterName)?.resource_group ??
    config?.workloadResourceGroup ??
    "";

  const subscriptionId = config?.subscriptionId ?? "";
  const emailValid = isPublicLetsEncryptEmail(email);
  const canAct = Boolean(subscriptionId && selectedClusterRg && clusterName);
  const canEnable = canAct && emailValid;

  const refresh = useCallback(async () => {
    setError(null);
    setStatusLoading(true);
    try {
      // Scope the status read to the selected cluster so a different
      // cluster's public-HTTPS state never leaks into this panel.
      const data = await aksApi.openApiPublicHttpsStatus(
        subscriptionId,
        selectedClusterRg,
        clusterName,
      );
      setStatus(data);
    } catch (err) {
      setError(formatApiError(err, "aks"));
    } finally {
      setStatusLoading(false);
    }
  }, [subscriptionId, selectedClusterRg, clusterName]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    // Mirror AksSection cluster discovery so the picker lists every
    // AKS cluster in the subscription regardless of RG.
    if (!subscriptionId) return;
    let cancelled = false;
    setClustersLoading(true);
    void (async () => {
      try {
        const response = await monitoringApi.aks(subscriptionId);
        if (cancelled) return;
        const clusters = (response.clusters ?? []).filter((c) => c.name);
        setAvailableClusters(clusters);
        setClustersLoaded(true);
        setClusterName((current) => {
          if (current && clusters.some((c) => c.name === current)) return current;
          const preferred = pickPreferredCluster(clusters, {
            resourceGroup: config?.workloadResourceGroup,
          });
          return preferred?.name ?? current;
        });
      } catch {
        if (cancelled) return;
        setAvailableClusters([]);
        setClustersLoaded(true);
      } finally {
        if (!cancelled) setClustersLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [subscriptionId, config?.workloadResourceGroup]);

  // Poll the Celery task until terminal. 3 s cadence matches the original
  // PublicHttpsPanel — install + ACME challenge takes ~3-5 minutes on
  // first run, so we trade a bit of poll volume for a snappier UI flip.
  const pollTask = useCallback(
    (taskId: string) => {
      const tick = async () => {
        try {
          const result = await aksApi.openApiPublicHttpsTaskStatus(taskId);
          const customStatus =
            result.custom_status && typeof result.custom_status === "object"
              ? (result.custom_status as { phase?: string })
              : {};
          const phase = customStatus.phase ?? "";
          if (phase) setTaskPhase(phase);
          const runtime = result.runtime_status ?? "";
          if (runtime === "Completed" || runtime === "Failed" || runtime === "Terminated") {
            // setup_openapi_public_https swallows pipeline errors and
            // returns `{status: 'failed', error: '...'}` as a normal task
            // result, so Celery reports `runtime_status: 'Completed'`
            // even when the actual install failed (e.g. cert-manager
            // webhook never reached Ready). Treat dict-level `status:
            // 'failed'` the same as a runtime-level Failed so the SPA
            // surfaces the error banner instead of silently flipping
            // to the success state.
            const dictFailed = result.output?.status === "failed";
            const taskFailed = runtime !== "Completed" || dictFailed;
            const completedCluster = clusterRef.current;
            clearRunningPublicHttpsTask(completedCluster);
            setRunningTask(null);
            if (!taskFailed) {
              await refresh();
            } else {
              const msg =
                result.output?.error ||
                `Task ${runtime.toLowerCase()} (phase=${phase || "n/a"})`;
              // Backend's pipeline-level except attaches a `diagnostics`
              // string (Certificate / Order / Challenge / solver-pod
              // state) when cert issuance fails — surface it so the
              // operator can tell "wrong status code '503'" /
              // "untolerated taint" / "invalidContact" apart without
              // having to `kubectl describe`.
              const diagnostics =
                typeof (result.output as Record<string, unknown> | undefined)?.diagnostics === "string"
                  ? ((result.output as Record<string, unknown>).diagnostics as string)
                  : "";
              const combined = diagnostics
                ? `${String(msg).slice(0, 600)}\n\n${diagnostics.slice(0, 1500)}`
                : String(msg).slice(0, 600);
              setError(combined);
            }
            return;
          }
        } catch (err) {
          // 404 here usually means "the Celery result expired" — stop
          // polling and let the operator click Refresh status. We do
          // NOT clear the running task on transient network errors so
          // a flaky connection does not re-enable the Enable button
          // mid-install.
          const message = formatApiError(err, "aks");
          if (/404|not[_ -]?found/i.test(message)) {
            clearRunningPublicHttpsTask(clusterRef.current);
            setRunningTask(null);
          }
          setError(message);
          return;
        }
        pollTimer.current = window.setTimeout(tick, 3_000);
      };
      pollTimer.current = window.setTimeout(tick, 1_500);
    },
    [refresh],
  );

  // Resume polling on mount if a previous Enable/Disable click is still
  // in flight (component was unmounted by switching Settings tabs but
  // the Celery task is still running on the worker sidecar).
  useEffect(() => {
    if (!runningTask) return;
    pollTask(runningTask.taskId);
    return () => {
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
    };
    // Only resume once per mounted runningTask; pollTask is stable
    // because `refresh` is wrapped in useCallback with an empty dep list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningTask?.taskId]);

  const enable = async () => {
    if (!canEnable) return;
    setError(null);
    setTaskPhase("queued");
    try {
      const res = await aksApi.enableOpenApiPublicHttps(
        subscriptionId,
        selectedClusterRg,
        clusterName,
        email,
      );
      const taskId = res.task_id || res.id;
      const next: RunningPublicHttpsTask = {
        taskId,
        startedAt: Date.now(),
        cluster: clusterName,
        kind: "enable",
      };
      saveRunningPublicHttpsTask(next);
      setRunningTask(next);
      setElapsedSec(0);
      pollTask(taskId);
    } catch (err) {
      setError(formatApiError(err, "aks"));
      clearRunningPublicHttpsTask(clusterName);
      setRunningTask(null);
    }
  };

  const disable = async () => {
    if (!canAct) return;
    setError(null);
    setTaskPhase("queued");
    try {
      const res = await aksApi.disableOpenApiPublicHttps(
        subscriptionId,
        selectedClusterRg,
        clusterName,
      );
      const taskId = res.task_id || res.id;
      const next: RunningPublicHttpsTask = {
        taskId,
        startedAt: Date.now(),
        cluster: clusterName,
        kind: "disable",
      };
      saveRunningPublicHttpsTask(next);
      setRunningTask(next);
      setElapsedSec(0);
      pollTask(taskId);
    } catch (err) {
      setError(formatApiError(err, "aks"));
      clearRunningPublicHttpsTask(clusterName);
      setRunningTask(null);
    }
  };

  const enabled = Boolean(status?.enabled);
  const publicUrl = status?.public_base_url ?? "";

  return (
    <Section heading="Public HTTPS Endpoint">
      <Group>
        <StatusLine kind="info">
          Installs ingress-nginx + cert-manager on the selected AKS cluster and exposes the
          elb-openapi service over an Azure-issued FQDN with a Let&apos;s Encrypt cert.
          First-time install is ~3-5 minutes.
        </StatusLine>
        <Field
          label="AKS cluster"
          hint={
            clustersLoading
              ? "Discovering AKS clusters in this subscription..."
              : availableClusters.length === 0 && clustersLoaded
                ? "No ELB-managed AKS clusters were found in this subscription."
                : "Pick the cluster running elb-openapi."
          }
        >
          {availableClusters.length > 1 ? (
            <select
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              style={SELECT_STYLE}
            >
              {availableClusters.map((c) => (
                <option key={`${c.resource_group}/${c.name}`} value={c.name}>
                  {c.name} ({c.power_state ?? "?"})
                </option>
              ))}
            </select>
          ) : (
            <input
              value={clusterName}
              onChange={(event) => setClusterName(event.target.value)}
              placeholder={clustersLoaded && availableClusters.length === 0 ? "No AKS cluster detected" : "aks-..."}
              style={INPUT_STYLE}
            />
          )}
        </Field>
        {!enabled && (
          <Field
            label="Operator email"
            hint={
              email && !emailValid
                ? "Let's Encrypt rejects .local / .localhost / .internal — enter a public TLD email."
                : "Auto-filled from your signed-in identity. Used by Let's Encrypt to send certificate-expiry notifications."
            }
          >
            <input
              type="email"
              value={email}
              onChange={(event) => {
                setEmailEdited(true);
                setEmail(event.target.value);
              }}
              placeholder="ops@example.com"
              style={INPUT_STYLE}
              required
            />
          </Field>
        )}
        <Row
          label="Status"
          control={
            <Badge tone={enabled ? "success" : "muted"}>
              {statusLoading && !status ? "Checking..." : enabled ? "Exposed" : "Not exposed"}
            </Badge>
          }
        />
        {enabled && publicUrl && (
          <Row
            label="Public endpoint"
            control={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <code style={{ fontSize: 11, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "inline-block" }}>
                  {publicUrl}
                </code>
                <button
                  type="button"
                  className="glass-button"
                  onClick={() => {
                    if (typeof navigator !== "undefined" && navigator.clipboard) {
                      navigator.clipboard.writeText(publicUrl).catch(() => undefined);
                    }
                  }}
                  title="Copy URL"
                  aria-label="Copy URL"
                  style={{ fontSize: 11 }}
                >
                  <Copy size={12} />
                </button>
                <a
                  href={publicUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="glass-button"
                  style={{ fontSize: 11, textDecoration: "none" }}
                >
                  <ExternalLink size={11} />
                </a>
              </span>
            }
          />
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", paddingBottom: 14 }}>
          <button
            className="glass-button"
            onClick={() => void refresh()}
            disabled={statusLoading || taskRunning}
            style={{ fontSize: 12 }}
          >
            Refresh status
          </button>
          {enabled ? (
            <button
              className="glass-button"
              onClick={disable}
              disabled={!canAct || taskRunning}
              style={{ fontSize: 12 }}
            >
              Disable
            </button>
          ) : (
            <button
              className="glass-button glass-button--primary"
              onClick={enable}
              disabled={!canEnable || taskRunning}
              title={
                !canAct
                  ? "Select an AKS cluster first"
                  : !emailValid
                    ? "Enter a valid operator email with a public TLD (Let's Encrypt rejects .local / .internal)."
                    : undefined
              }
              style={{ fontSize: 12 }}
            >
              Enable
            </button>
          )}
          {taskRunning && (
            <span style={{ fontSize: 11, color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Loader2 size={12} className="spin" />
              {(() => {
                const { meta, index, total } = lookupPublicHttpsPhase(taskPhase || "queued");
                const stepLabel = taskPhase && taskPhase !== "queued"
                  ? `Step ${index + 1}/${total}: ${meta.label}`
                  : meta.label;
                return `${stepLabel} \u00b7 ${formatElapsedSeconds(elapsedSec)} elapsed`;
              })()}
            </span>
          )}
        </div>
        {taskRunning && (() => {
          const { meta } = lookupPublicHttpsPhase(taskPhase || "queued");
          const progressPct = publicHttpsProgressPct;
          return (
            <div style={{ marginTop: -4, marginBottom: 12, display: "flex", flexDirection: "column", gap: 6 }}>
              <div
                role="progressbar"
                aria-valuenow={progressPct}
                aria-valuemin={0}
                aria-valuemax={100}
                style={{
                  position: "relative",
                  height: 4,
                  borderRadius: 2,
                  background: "var(--surface-2, rgba(255,255,255,0.06))",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${progressPct}%`,
                    height: "100%",
                    background: "var(--accent, #5b8def)",
                    transition: "width 200ms ease-out",
                  }}
                />
              </div>
              {meta.hint && (
                <span style={{ fontSize: 10.5, color: "var(--text-muted)", lineHeight: 1.45 }}>
                  {meta.hint}
                  {runningTask?.cluster ? ` \u00b7 cluster ${runningTask.cluster}` : ""}
                  {runningTask?.kind === "disable" ? " \u00b7 disable flow" : ""}
                </span>
              )}
            </div>
          );
        })()}
        {enabled && status && (
          <StatusLine kind="info">
            {[
              status.ingress_lb_ip ? `LB ${status.ingress_lb_ip}` : null,
              status.cert_issuer || null,
              status.cert_expires_at ? `expires ${status.cert_expires_at} (auto-renew)` : null,
              status.updated_at ? `updated ${status.updated_at}` : null,
            ]
              .filter(Boolean)
              .join(" · ")}
          </StatusLine>
        )}
        {error && <StatusLine kind="error">{error}</StatusLine>}
      </Group>
    </Section>
  );
}
