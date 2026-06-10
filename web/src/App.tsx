import { lazy, Suspense, type ReactNode } from "react";
import { Route, Routes, Navigate, Link } from "react-router-dom";
import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";

import { Layout } from "@/components/Layout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { SignIn } from "@/pages/SignIn";
import { AccessDenied } from "@/pages/AccessDenied";
import { useAuthSessionIssue } from "@/auth/sessionEvents";
import { useDashboardAccessGate } from "@/hooks/useDashboardAccessGate";
import { Dashboard } from "@/pages/Dashboard";
import { BlastSubmit } from "@/pages/BlastSubmit";
import { BlastJobs } from "@/pages/BlastJobs";
import { BlastResults } from "@/pages/BlastResults";
import { BlastAnalytics } from "@/pages/BlastAnalytics";
import { ApiReference } from "@/pages/ApiReference";
import { UpgradePage } from "@/pages/UpgradePage";
import { configValue, isDevBypassEnabled, isUsableClientId } from "@/config/runtime";
import { usePreviewFeatureEnabled } from "@/hooks/usePreferences";

const DEV_BYPASS = isDevBypassEnabled();
const CLIENT_ID_MISSING =
  !isUsableClientId(configValue("VITE_AZURE_CLIENT_ID")) && !DEV_BYPASS;

const RemoteTerminal = lazy(() =>
  import("@/pages/RemoteTerminal").catch((error: unknown) => ({
    default: () => <TerminalLoadUnavailable error={error} />,
  })),
);
const DatabaseBuilder = lazy(() =>
  import("@/pages/DatabaseBuilder").then((module) => ({
    default: module.DatabaseBuilder,
  })),
);
const ToolsPage = lazy(() =>
  import("@/pages/ToolsPage").then((module) => ({ default: module.ToolsPage })),
);
const LiveWall = lazy(() =>
  import("@/pages/Monitor/LiveWall").then((module) => ({ default: module.LiveWall })),
);
const SequenceDetail = lazy(() =>
  import("@/pages/sequence/SequenceDetail").then((module) => ({
    default: module.SequenceDetail,
  })),
);
const DiagnosticsPage = lazy(() => import("@/pages/diagnostics/DiagnosticsPage"));

function OptionalFeatureRoute({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  if (!enabled) return <Navigate to="/" replace />;
  return <Suspense fallback={<RouteLoadingSkeleton />}>{children}</Suspense>;
}

function RouteLoadingSkeleton() {
  return (
    <div
      className="glass-card"
      aria-label="Loading page"
      style={{ display: "grid", gap: 12 }}
    >
      <span
        className="skeleton"
        style={{ width: "38%", height: 16, borderRadius: 999 }}
      />
      <span
        className="skeleton"
        style={{ width: "72%", height: 12, borderRadius: 999 }}
      />
      <span
        className="skeleton"
        style={{ width: "54%", height: 12, borderRadius: 999 }}
      />
    </div>
  );
}

function TerminalLoadUnavailable({ error }: { error: unknown }) {
  const detail =
    error instanceof Error ? error.message : "Terminal bundle could not be loaded.";
  return (
    <div
      className="glass-card glass-card--strong"
      role="status"
      style={{ maxWidth: 720 }}
    >
      <h2 style={{ marginTop: 0 }}>Terminal unavailable</h2>
      <p className="muted" style={{ lineHeight: 1.6 }}>
        The browser terminal bundle did not load. Existing BLAST jobs and dashboard
        monitoring continue to run.
      </p>
      <p className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
        {detail}
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 14 }}>
        <button
          type="button"
          className="glass-button glass-button--primary"
          onClick={() => location.reload()}
        >
          Reload Terminal
        </button>
        <Link className="glass-button" to="/">
          Dashboard
        </Link>
      </div>
    </div>
  );
}

function AppRoutes() {
  const customDbEnabled = usePreviewFeatureEnabled("customDb");
  const labToolsEnabled = usePreviewFeatureEnabled("labTools");
  const liveWallEnabled = usePreviewFeatureEnabled("liveWall");
  const terminalEnabled = usePreviewFeatureEnabled("terminal");

  return (
    <ErrorBoundary>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route
            path="/monitor/live-wall"
            element={
              <OptionalFeatureRoute enabled={liveWallEnabled}>
                <LiveWall />
              </OptionalFeatureRoute>
            }
          />
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
            path="/sequence/:accession"
            element={
              <Suspense fallback={<RouteLoadingSkeleton />}>
                <SequenceDetail />
              </Suspense>
            }
          />
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
          <Route path="/upgrade" element={<UpgradePage />} />
          <Route
            path="/diagnostics"
            element={
              <Suspense fallback={<RouteLoadingSkeleton />}>
                <DiagnosticsPage />
              </Suspense>
            }
          />
          <Route
            path="/diagnostics/:category"
            element={
              <Suspense fallback={<RouteLoadingSkeleton />}>
                <DiagnosticsPage />
              </Suspense>
            }
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
      <div
        style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}
      >
        <div
          className="glass-card glass-card--strong"
          style={{ width: "min(480px, 100%)", textAlign: "center" }}
        >
          <h2 style={{ marginTop: 0, color: "var(--warning)" }}>Setup Required</h2>
          <p className="muted" style={{ lineHeight: 1.6 }}>
            This app is not configured yet. An administrator needs to create an Azure App
            Registration and set <code className="code-val">VITE_AZURE_CLIENT_ID</code> in
            the environment.
          </p>
          <p className="muted" style={{ fontSize: 12 }}>
            See the{" "}
            <a
              href="https://github.com/dotnetpower/elb-dashboard#readme"
              target="_blank"
              rel="noreferrer"
            >
              README
            </a>{" "}
            for setup instructions.
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
        <AuthenticatedApp />
      </AuthenticatedTemplate>
    </>
  );
}

// Gate the authenticated app behind the session-issue store. MSAL's
// AuthenticatedTemplate only checks that an account is still cached, not that
// its tokens are still valid — so a silently-expired session would otherwise
// keep the dashboard mounted behind a stale banner. When a session issue is
// raised (no active account, interaction required, refresh failed, or a 401
// from the API/ARM) we route the user to the in-app sign-in page instead.
//
// On top of that, the optional backend entry gate (`ENFORCE_DASHBOARD_RBAC`)
// can deny a signed-in tenant member who holds no Azure read role on the
// dashboard scope. `useDashboardAccessGate` resolves the `/api/me` bootstrap
// once and, only on an explicit `dashboard_access_denied` 403, swaps the app
// for the access-denied screen. When the gate is OFF (default) `/api/me`
// returns 200 and this is a sub-100ms pass-through.
function AuthenticatedApp() {
  const sessionIssue = useAuthSessionIssue();
  const access = useDashboardAccessGate();
  if (sessionIssue) {
    return <SignIn expired expiredMessage={sessionIssue.message} />;
  }
  if (access.status === "loading") {
    return <AccessCheckLoading />;
  }
  if (access.status === "denied") {
    return <AccessDenied message={access.message} />;
  }
  return <AppRoutes />;
}

function AccessCheckLoading() {
  return (
    <div
      style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}
      aria-busy="true"
      aria-label="Checking access"
    >
      <div
        className="glass-card"
        style={{ width: "min(360px, 100%)", display: "grid", gap: 12 }}
      >
        <span
          className="skeleton"
          style={{ width: "42%", height: 14, borderRadius: 999 }}
        />
        <span
          className="skeleton"
          style={{ width: "78%", height: 12, borderRadius: 999 }}
        />
      </div>
    </div>
  );
}
