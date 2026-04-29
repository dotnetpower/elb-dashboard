import { useMsal } from "@azure/msal-react";
import { LogIn } from "lucide-react";

import { apiLoginRequest } from "@/auth/msal";

export function SignIn() {
  const { instance } = useMsal();

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
        style={{ width: "min(440px, 100%)", textAlign: "center" }}
      >
        <h1 style={{ marginTop: 0 }}>ElasticBLAST Control Plane</h1>
        <p className="muted" style={{ lineHeight: 1.6 }}>
          Browser-only operations for ElasticBLAST on Azure. Sign in with your
          Microsoft Entra account to provision the Remote Terminal and monitor
          AKS, Storage, and ACR.
        </p>
        <button
          className="glass-button glass-button--primary"
          style={{ marginTop: "var(--space-4)" }}
          onClick={() =>
            instance.loginRedirect({
              ...apiLoginRequest,
              prompt: "select_account",
            })
          }
        >
          <LogIn size={16} strokeWidth={1.5} /> Sign in with Microsoft
        </button>
      </div>
    </div>
  );
}
