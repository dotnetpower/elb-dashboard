/**
 * ProvisionErrorCard — structured failure surface for AKS provisioning.
 *
 * Replaces the raw red-text dump that the modal used to render under the
 * Create button. The new card carries (in order):
 *
 *   - title row with status icon + headline
 *   - one-line summary (the message the user reads first), produced by
 *     `armErrorClassifier` from the raw ARM response
 *   - category-specific secondary details
 *   - action buttons (portal deep-link, docs, retry)
 *   - optional raw response in a `<details>` accordion for debugging
 *
 * Hides the raw Azure message behind a `<details>` accordion so it
 * stays available for debugging but does not visually dominate.
 */
import { AlertCircle, BookOpen, Check, Copy, ExternalLink, RotateCcw, X } from "lucide-react";
import { useState } from "react";

import { classifyArmError, type ArmErrorAction } from "./armErrorClassifier";

export interface ProvisionErrorCardProps {
  /** Raw Azure response (kept verbatim for debugging and feeding to
   *  the classifier). */
  raw: string;
  /** Context the classifier uses to deep-link the portal action and
   *  fill in `{region}` placeholders in the headline. */
  context?: {
    subscriptionId?: string;
    region?: string;
    resourceGroup?: string;
  };
  /** Optional Azure portal deep-link the parent wants surfaced in
   *  addition to whatever the classifier produces. Used after a
   *  cancellation when the cluster ARM resource may have been
   *  partially created — clicking opens the cluster overview blade
   *  so the user can verify and delete if needed. */
  extraPortalUrl?: string | null;
  /** Label for the `extraPortalUrl` action. Defaults to a generic
   *  "Open in Azure portal" so the parent doesn't have to think
   *  about wording for the common case. */
  extraPortalLabel?: string;
  /** Called when the user clicks the dismiss (×) action. */
  onDismiss: () => void;
  /** Called when the user clicks "Edit & retry" — wired by the parent
   *  to clear the error state and let the user adjust the form. */
  onRetry?: () => void;
}

function actionIcon(kind: ArmErrorAction["kind"]) {
  if (kind === "portal") return <ExternalLink size={11} strokeWidth={1.5} />;
  if (kind === "docs") return <BookOpen size={11} strokeWidth={1.5} />;
  if (kind === "command") return <Copy size={11} strokeWidth={1.5} />;
  return <ExternalLink size={11} strokeWidth={1.5} />;
}

/** Render a command-kind action as a clipboard-copy button. Falls back to
 *  document.execCommand when navigator.clipboard is unavailable (some
 *  cross-origin frames / older browsers); shows a transient "Copied!"
 *  label so the operator gets immediate feedback. */
function CommandActionButton({ action }: { action: ArmErrorAction }) {
  const [copied, setCopied] = useState(false);
  const onClick = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(action.href);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = action.href;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "absolute";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard refused (e.g. permissions policy). Leave the command in
      // the details/raw block where the operator can still hand-copy it.
    }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className="glass-button"
      title={action.href}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: 11,
        padding: "4px 10px",
        color: "var(--accent)",
      }}
    >
      {copied ? (
        <Check size={11} strokeWidth={1.5} />
      ) : (
        actionIcon(action.kind)
      )}
      {copied ? "Copied!" : action.label}
    </button>
  );
}

export function ProvisionErrorCard({
  raw,
  context,
  extraPortalUrl,
  extraPortalLabel = "Open cluster in Azure portal",
  onDismiss,
  onRetry,
}: ProvisionErrorCardProps) {
  const classified = classifyArmError(raw, context ?? {});
  return (
    <div
      role="alert"
      style={{
        position: "relative",
        padding: "12px 14px",
        borderRadius: 10,
        border: "1px solid rgba(255,107,107,0.35)",
        background: "rgba(255,107,107,0.06)",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss provisioning error"
        style={{
          position: "absolute",
          top: 6,
          right: 6,
          background: "transparent",
          border: "none",
          padding: 4,
          cursor: "pointer",
          color: "var(--text-muted)",
        }}
      >
        <X size={14} strokeWidth={1.5} />
      </button>

      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 8,
          paddingRight: 20,
        }}
      >
        <AlertCircle
          size={16}
          strokeWidth={1.5}
          style={{ color: "var(--danger)", marginTop: 1, flexShrink: 0 }}
        />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: "var(--text-primary)",
              marginBottom: 2,
            }}
          >
            Provisioning failed
          </div>
          <div
            style={{
              fontSize: 12,
              color: "var(--text-primary)",
              lineHeight: 1.5,
              wordBreak: "break-word",
            }}
          >
            {classified.summary}
          </div>
          {classified.details && (
            <div
              style={{
                marginTop: 4,
                fontSize: 11,
                color: "var(--text-muted)",
                lineHeight: 1.5,
              }}
            >
              {classified.details}
            </div>
          )}
        </div>
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          marginTop: 2,
        }}
      >
        {classified.actions.map((action) => {
          if (action.kind === "command") {
            return (
              <CommandActionButton
                key={`${action.kind}:${action.label}`}
                action={action}
              />
            );
          }
          return (
            <a
              key={`${action.kind}:${action.href}`}
              href={action.href}
              target="_blank"
              rel="noopener noreferrer"
              className="glass-button"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                fontSize: 11,
                padding: "4px 10px",
                color: "var(--accent)",
                textDecoration: "none",
              }}
            >
              {actionIcon(action.kind)}
              {action.label}
            </a>
          );
        })}
        {extraPortalUrl && (
          <a
            key="extra-portal"
            href={extraPortalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="glass-button"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontSize: 11,
              padding: "4px 10px",
              color: "var(--warning)",
              textDecoration: "none",
            }}
            title="The cluster create may have started on Azure even though the task was cancelled — verify in the portal and delete if needed."
          >
            <ExternalLink size={11} strokeWidth={1.5} />
            {extraPortalLabel}
          </a>
        )}
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="glass-button"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontSize: 11,
              padding: "4px 10px",
            }}
          >
            <RotateCcw size={11} strokeWidth={1.5} />
            Edit &amp; retry
          </button>
        )}
      </div>

      {raw && raw !== classified.summary && (
        <details style={{ fontSize: 11, color: "var(--text-muted)" }}>
          <summary
            style={{
              cursor: "pointer",
              userSelect: "none",
              padding: "2px 0",
            }}
          >
            Show raw Azure response
          </summary>
          <pre
            style={{
              marginTop: 6,
              padding: "8px 10px",
              borderRadius: 6,
              background: "rgba(0,0,0,0.18)",
              border: "1px solid var(--glass-border)",
              fontFamily: "var(--font-mono, ui-monospace, monospace)",
              fontSize: 10,
              lineHeight: 1.4,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 200,
              overflowY: "auto",
            }}
          >
            {raw}
          </pre>
        </details>
      )}
    </div>
  );
}
