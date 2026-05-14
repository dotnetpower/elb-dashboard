import { Route, Routes, Navigate } from "react-router-dom";
import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";

import { Layout } from "@/components/Layout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { SignIn } from "@/pages/SignIn";
import { Dashboard } from "@/pages/Dashboard";
import RemoteTerminal from "@/pages/RemoteTerminal";
import { BlastSubmit } from "@/pages/BlastSubmit";
import { BlastJobs } from "@/pages/BlastJobs";
import { BlastResults } from "@/pages/BlastResults";
import { BlastAnalytics } from "@/pages/BlastAnalytics";
import { DatabaseBuilder } from "@/pages/DatabaseBuilder";
import { ToolsPage } from "@/pages/ToolsPage";
import { ApiReference } from "@/pages/ApiReference";

const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";
const CLIENT_ID_MISSING = !import.meta.env.VITE_AZURE_CLIENT_ID && !DEV_BYPASS;

function AppRoutes() {
  return (
    <ErrorBoundary>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/terminal" element={<RemoteTerminal />} />
          <Route path="/blast/submit" element={<BlastSubmit />} />
          <Route path="/blast/jobs" element={<BlastJobs />} />
          <Route path="/blast/jobs/:jobId" element={<BlastResults />} />
          <Route path="/blast/jobs/:jobId/analytics" element={<BlastAnalytics />} />
          <Route path="/blast/databases/build" element={<DatabaseBuilder />} />
          <Route path="/tools" element={<ToolsPage />} />
          <Route path="/docs" element={<ApiReference />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </ErrorBoundary>
  );
}

export function App() {
  // #67: Show visible error when client ID is missing
  if (CLIENT_ID_MISSING) {
    return (
      <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
        <div className="glass-card glass-card--strong" style={{ width: "min(480px, 100%)", textAlign: "center" }}>
          <h2 style={{ marginTop: 0, color: "var(--warning)" }}>Setup Required</h2>
          <p className="muted" style={{ lineHeight: 1.6 }}>
            This app is not configured yet. An administrator needs to create an Azure App Registration
            and set <code className="code-val">VITE_AZURE_CLIENT_ID</code> in the environment.
          </p>
          <p className="muted" style={{ fontSize: 12 }}>
            See the <a href="https://github.com/dotnetpower/elastic-blast-azure-functionapp#readme" target="_blank" rel="noreferrer">README</a> for setup instructions.
          </p>
        </div>
      </div>
    );
  }

  if (DEV_BYPASS) {
    return <AppRoutes />;
  }

  return (
    <>
      <UnauthenticatedTemplate>
        <SignIn />
      </UnauthenticatedTemplate>
      <AuthenticatedTemplate>
        <AppRoutes />
      </AuthenticatedTemplate>
    </>
  );
}
