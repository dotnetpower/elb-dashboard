import { lazy, Suspense, type ReactNode } from "react";
import { Route, Routes, Navigate } from "react-router-dom";
import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";

import { Layout } from "@/components/Layout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { SignIn } from "@/pages/SignIn";
import { Dashboard } from "@/pages/Dashboard";
import { BlastSubmit } from "@/pages/BlastSubmit";
import { BlastJobs } from "@/pages/BlastJobs";
import { BlastResults } from "@/pages/BlastResults";
import { BlastAnalytics } from "@/pages/BlastAnalytics";
import { ApiReference } from "@/pages/ApiReference";
import { AksCardMockups } from "@/pages/mockups/AksCardMockups";
import { AksCardMockupsRefined } from "@/pages/mockups/AksCardMockupsRefined";
import { AksCardMockupsPremium } from "@/pages/mockups/AksCardMockupsPremium";
import { AksCardMockupsSimple } from "@/pages/mockups/AksCardMockupsSimple";
import { SidecarInspectorMockups } from "@/pages/mockups/SidecarInspectorMockups";
import { configValue, isDevBypassEnabled, isFeatureEnabled } from "@/config/runtime";

const DEV_BYPASS = isDevBypassEnabled();
const CLIENT_ID_MISSING = !configValue("VITE_AZURE_CLIENT_ID") && !DEV_BYPASS;

const RemoteTerminal = lazy(() => import("@/pages/RemoteTerminal"));
const DatabaseBuilder = lazy(() =>
  import("@/pages/DatabaseBuilder").then((module) => ({
    default: module.DatabaseBuilder,
  })),
);
const ToolsPage = lazy(() =>
  import("@/pages/ToolsPage").then((module) => ({ default: module.ToolsPage })),
);

function OptionalFeatureRoute({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  if (!enabled) return <Navigate to="/" replace />;
  return <Suspense fallback={null}>{children}</Suspense>;
}

function AppRoutes() {
  const customDbEnabled = isFeatureEnabled("customDb");
  const labToolsEnabled = isFeatureEnabled("labTools");
  const terminalEnabled = isFeatureEnabled("terminal");

  return (
    <ErrorBoundary>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route
            path="/terminal"
            element={
              <OptionalFeatureRoute enabled={terminalEnabled}>
                <RemoteTerminal />
              </OptionalFeatureRoute>
            }
          />
          <Route path="/blast/submit" element={<BlastSubmit />} />
          <Route path="/blast/jobs" element={<BlastJobs />} />
          <Route path="/blast/jobs/:jobId" element={<BlastResults />} />
          <Route path="/blast/jobs/:jobId/analytics" element={<BlastAnalytics />} />
          <Route
            path="/blast/databases/build"
            element={
              <OptionalFeatureRoute enabled={customDbEnabled}>
                <DatabaseBuilder />
              </OptionalFeatureRoute>
            }
          />
          <Route
            path="/tools"
            element={
              <OptionalFeatureRoute enabled={labToolsEnabled}>
                <ToolsPage />
              </OptionalFeatureRoute>
            }
          />
          <Route path="/docs" element={<ApiReference />} />
          <Route path="/mockups/aks-card" element={<AksCardMockups />} />
          <Route
            path="/mockups/aks-card-refined"
            element={<AksCardMockupsRefined />}
          />
          <Route
            path="/mockups/aks-card-premium"
            element={<AksCardMockupsPremium />}
          />
          <Route
            path="/mockups/aks-card-simple"
            element={<AksCardMockupsSimple />}
          />
          <Route
            path="/mockups/sidecar-inspector"
            element={<SidecarInspectorMockups />}
          />
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
            See the <a href="https://github.com/dotnetpower/elb-dashboard#readme" target="_blank" rel="noreferrer">README</a> for setup instructions.
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
