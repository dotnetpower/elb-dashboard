import { useState } from "react";
import { useMsal } from "@azure/msal-react";
import { LogIn, ChevronDown, ChevronUp, Dna, Zap, Shield, ShieldAlert } from "lucide-react";

import { apiLoginRequest } from "@/auth/msal";

interface SignInProps {
  /** True when reached because the active session expired (vs. a first visit). */
  expired?: boolean;
  /** Optional message describing why the session needs a refresh. */
  expiredMessage?: string;
}

export function SignIn({ expired = false, expiredMessage }: SignInProps) {
  const { instance } = useMsal();
  const [showDetails, setShowDetails] = useState(false);

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: "var(--space-5)",
      }}
    >
      <div
        className="glass-card glass-card--strong"
        style={{ width: "min(480px, 100%)", textAlign: "center" }}
      >
        {expired && (
          <div
            role="alert"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              justifyContent: "center",
              marginBottom: "var(--space-4)",
              padding: "10px 14px",
              borderRadius: 8,
              background: "var(--bg-tertiary)",
              border: "1px solid var(--warning)",
              color: "var(--text)",
              fontSize: 13,
              lineHeight: 1.5,
              textAlign: "left",
            }}
          >
            <ShieldAlert size={16} strokeWidth={1.6} style={{ color: "var(--warning)", flexShrink: 0 }} />
            <span>
              {expiredMessage ??
                "Your sign-in session expired. Sign in again to continue."}
            </span>
          </div>
        )}
        <div style={{ marginBottom: "var(--space-4)" }}>
          <Dna size={40} strokeWidth={1.2} style={{ color: "var(--accent)", marginBottom: 8 }} />
        </div>
        <h1 style={{ marginTop: 0, fontSize: 22 }}>ElasticBLAST on Azure</h1>
        <p className="muted" style={{ lineHeight: 1.6, fontSize: 14 }}>
          {expired
            ? "Sign in again with your organization account to resume where you left off."
            : "Run BLAST searches in the cloud — no local setup needed. Sign in with your organization account to get started."}
        </p>

        <div style={{ display: "flex", gap: 12, justifyContent: "center", margin: "var(--space-4) 0" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-muted)" }}>
            <Zap size={14} /> Scalable
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-muted)" }}>
            <Shield size={14} /> Secure
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--text-muted)" }}>
            <Dna size={14} /> NCBI BLAST+
          </div>
        </div>

        <button
          className="glass-button glass-button--primary"
          style={{ marginTop: "var(--space-2)", padding: "8px 24px", fontSize: 14 }}
          onClick={() =>
            instance.loginRedirect({
              ...apiLoginRequest,
              prompt: expired ? "login" : "select_account",
            })
          }
        >
          <LogIn size={16} strokeWidth={1.5} /> Sign in with Microsoft
        </button>

        <div style={{ marginTop: "var(--space-4)" }}>
          <button
            className="glass-button"
            style={{ fontSize: 11, border: "none", background: "none", color: "var(--text-muted)" }}
            onClick={() => setShowDetails(!showDetails)}
          >
            What is this? {showDetails ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>
          {showDetails && (
            <div style={{
              marginTop: "var(--space-3)", padding: 12, borderRadius: 6,
              background: "var(--bg-tertiary)", fontSize: 12, color: "var(--text-muted)",
              textAlign: "left", lineHeight: 1.6,
            }}>
              <strong>ElasticBLAST Control Plane</strong> lets you run large-scale NCBI BLAST searches
              using Azure Kubernetes Service. Everything is managed from this browser interface —
              cluster provisioning, database downloads, job submission, and results retrieval.
              No command-line tools or local installation required.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
