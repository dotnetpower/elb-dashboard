import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Copy,
  ExternalLink,
  Globe,
  Loader2,
  PowerOff,
  RefreshCw,
  ShieldCheck,
  XCircle,
  Zap,
} from "lucide-react";

import { aksApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { useClipboardFeedback } from "@/hooks/useClipboardFeedback";
import type { OpenApiPublicHttpsStatus } from "@/api/aks";

/**
 * Public HTTPS panel — drives the `setup_openapi_public_https` Celery task.
 *
 * Behaviour summary:
 * - GET `/api/aks/openapi/public-https` returns the cached state (no kubectl
 *   round trip) so polling is cheap.
 * - POST enqueues the install task; we poll its status endpoint until the
 *   task settles, then refetch the cached state.
 * - DELETE enqueues the teardown task; on success the cached state flips
 *   back to `{enabled: false}` and the API Reference falls back to the
 *   internal LB path.
 */
export function PublicHttpsPanel({
  subscriptionId,
  resourceGroup,
  clusterName,
  onStateChange,
}: {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
  onStateChange?: (state: OpenApiPublicHttpsStatus) => void;
}) {
  const [state, setState] = useState<OpenApiPublicHttpsStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [taskRunning, setTaskRunning] = useState(false);
  const [taskPhase, setTaskPhase] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState<string>("");
  const { copied, copyText } = useClipboardFeedback();
  const pollTimer = useRef<number | null>(null);

  const refresh = async () => {
    try {
      const status = await aksApi.openApiPublicHttpsStatus();
      setState(status);
      onStateChange?.(status);
      setError(null);
    } catch (e) {
      setError(formatApiError(e, "aks"));
    } finally {
      setStatusLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    return () => {
      if (pollTimer.current !== null) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll the Celery task until terminal, then refetch the cached state.
  // 3 s cadence: setup is ~3-5 min on first run (Helm-equivalent install
  // + ACME challenge), so we trade a bit of poll volume for a snappier
  // SPA transition once the task settles.
  const pollTask = (taskId: string) => {
    const tick = async () => {
      try {
        const status = await aksApi.openApiPublicHttpsTaskStatus(taskId);
        const customStatus =
          status.custom_status && typeof status.custom_status === "object"
            ? (status.custom_status as { phase?: string })
            : {};
        const phase = customStatus.phase ?? "";
        if (phase) setTaskPhase(phase);
        const runtime = status.runtime_status ?? "";
        if (runtime === "Completed" || runtime === "Failed" || runtime === "Terminated") {
          setTaskRunning(false);
          if (runtime === "Completed") {
            await refresh();
          } else {
            const msg =
              status.output?.error ||
              `Task ${runtime.toLowerCase()} (phase=${phase || "n/a"})`;
            setError(String(msg).slice(0, 600));
          }
          return;
        }
      } catch (e) {
        setError(formatApiError(e, "aks"));
        setTaskRunning(false);
        return;
      }
      pollTimer.current = window.setTimeout(tick, 3_000);
    };
    pollTimer.current = window.setTimeout(tick, 1_500);
  };

  const enable = async () => {
    setError(null);
    setTaskRunning(true);
    setTaskPhase("queued");
    try {
      const res = await aksApi.enableOpenApiPublicHttps(
        subscriptionId,
        resourceGroup,
        clusterName,
        email,
      );
      pollTask(res.task_id || res.id);
    } catch (e) {
      setError(formatApiError(e, "aks"));
      setTaskRunning(false);
    }
  };

  const disable = async () => {
    setError(null);
    setTaskRunning(true);
    setTaskPhase("queued");
    try {
      const res = await aksApi.disableOpenApiPublicHttps(
        subscriptionId,
        resourceGroup,
        clusterName,
      );
      pollTask(res.task_id || res.id);
    } catch (e) {
      setError(formatApiError(e, "aks"));
      setTaskRunning(false);
    }
  };

  const enabled = Boolean(state?.enabled);
  const busy = statusLoading || taskRunning;
  const publicUrl = state?.public_base_url ?? "";

  return (
    <section
      style={{
        background: "var(--bg-primary)",
        border: "1px solid var(--border-weak)",
        borderRadius: "var(--radius)",
        padding: "12px 14px",
        display: "grid",
        gap: 12,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              background: "var(--bg-tertiary)",
              color: enabled ? "var(--success)" : "var(--accent)",
            }}
          >
            <Globe size={15} strokeWidth={1.5} />
          </span>
          <div>
            <div style={{ fontSize: 14, fontWeight: 650, color: "var(--text-primary)" }}>
              Public HTTPS Endpoint
            </div>
            <div
              style={{ fontSize: 11, color: "var(--text-faint)", fontFamily: "var(--font-mono)" }}
            >
              {enabled
                ? "ingress-nginx + cert-manager · Let's Encrypt"
                : "Not exposed — only reachable from the AKS VNet"}
            </div>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="glass-button"
            onClick={() => {
              setStatusLoading(true);
              void refresh();
            }}
            disabled={busy}
            title="Refresh status"
            aria-label="Refresh status"
            style={{ fontSize: 11 }}
          >
            <RefreshCw size={12} className={statusLoading ? "spin" : ""} /> Refresh
          </button>
          {enabled ? (
            <button
              type="button"
              className="glass-button"
              onClick={disable}
              disabled={busy || !subscriptionId || !resourceGroup || !clusterName}
              title="Disable the public HTTPS endpoint (Ingress + cert deleted; ingress-nginx + cert-manager remain)"
              aria-label="Disable public HTTPS"
              style={{ fontSize: 11 }}
            >
              {taskRunning ? <Loader2 size={12} className="spin" /> : <PowerOff size={12} />}
              Disable
            </button>
          ) : (
            <button
              type="button"
              className="glass-button glass-button--primary"
              onClick={enable}
              disabled={busy || !subscriptionId || !resourceGroup || !clusterName}
              title="Install ingress-nginx + cert-manager and request a Let's Encrypt cert"
              aria-label="Enable public HTTPS"
              style={{ fontSize: 11 }}
            >
              {taskRunning ? <Loader2 size={12} className="spin" /> : <Zap size={12} />}
              Enable
            </button>
          )}
        </div>
      </div>

      {!enabled && !taskRunning && (
        <div style={{ display: "grid", gap: 6 }}>
          <label
            htmlFor="public-https-email"
            style={{ fontSize: 11, color: "var(--text-muted)" }}
          >
            Operator email (Let's Encrypt expiry notifications — optional)
          </label>
          <input
            id="public-https-email"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="ops@example.com"
            style={{
              fontSize: 12,
              padding: "6px 10px",
              borderRadius: 6,
              border: "1px solid var(--border-weak)",
              background: "var(--bg-secondary)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-mono)",
            }}
          />
        </div>
      )}

      {taskRunning && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 12px",
            background: "var(--bg-secondary)",
            border: "1px solid var(--border-weak)",
            borderRadius: 8,
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          <Loader2 size={14} className="spin" />
          <div>
            <div style={{ fontWeight: 600, color: "var(--text-primary)" }}>
              {taskPhase || "queued"}
            </div>
            <div style={{ fontSize: 11 }}>
              First-time install is ~3-5 minutes (installer apply, webhook ready, Let's
              Encrypt HTTP-01 challenge).
            </div>
          </div>
        </div>
      )}

      {enabled && publicUrl && (
        <div style={{ display: "grid", gap: 8 }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "auto minmax(0, 1fr) auto auto",
              gap: 8,
              alignItems: "center",
              padding: "10px 12px",
              background: "var(--bg-secondary)",
              border: "1px solid var(--border-weak)",
              borderRadius: 8,
            }}
          >
            <ShieldCheck size={14} style={{ color: "var(--success)" }} />
            <code
              style={{
                color: "var(--text-primary)",
                fontSize: 12,
                fontFamily: "var(--font-mono)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {publicUrl}
            </code>
            <button
              type="button"
              className="glass-button"
              onClick={() => copyText(publicUrl, "public-https-url")}
              title="Copy URL"
              aria-label="Copy URL"
              style={{ fontSize: 11 }}
            >
              <Copy size={12} /> {copied === "public-https-url" ? "Copied" : "Copy"}
            </button>
            <a
              href={publicUrl}
              target="_blank"
              rel="noreferrer"
              className="glass-button"
              style={{ fontSize: 11, textDecoration: "none" }}
            >
              <ExternalLink size={11} /> Open
            </a>
          </div>

          <div
            style={{
              display: "flex",
              gap: 14,
              flexWrap: "wrap",
              fontSize: 11,
              color: "var(--text-faint)",
            }}
          >
            {state?.ingress_lb_ip && (
              <span>
                <CheckCircle2
                  size={11}
                  style={{ verticalAlign: "-1px", marginRight: 4, color: "var(--success)" }}
                />
                Ingress LB: <code>{state.ingress_lb_ip}</code>
              </span>
            )}
            {state?.cert_issuer && (
              <span>
                Cert issuer: <code>{state.cert_issuer}</code>
              </span>
            )}
            {state?.cert_expires_at && (
              <span>
                Expires: <code>{state.cert_expires_at}</code> · auto-renew via cert-manager
              </span>
            )}
            {state?.updated_at && (
              <span>
                Last setup: <code>{state.updated_at}</code>
              </span>
            )}
          </div>
        </div>
      )}

      {error && (
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 6,
            fontSize: 12,
            color: "var(--danger)",
            lineHeight: 1.45,
          }}
        >
          <XCircle size={13} style={{ marginTop: 1, flexShrink: 0 }} />
          <span>{error}</span>
        </div>
      )}
    </section>
  );
}
