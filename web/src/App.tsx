import { Route, Routes, Navigate } from "react-router-dom";
import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";

import { Layout } from "@/components/Layout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { SignIn } from "@/pages/SignIn";
import { Dashboard } from "@/pages/Dashboard";
import { RemoteTerminal } from "@/pages/RemoteTerminal";
import { BlastSubmit } from "@/pages/BlastSubmit";
import { BlastJobs } from "@/pages/BlastJobs";
import { BlastResults } from "@/pages/BlastResults";

const DEV_BYPASS = import.meta.env.VITE_AUTH_DEV_BYPASS === "true";

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
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </ErrorBoundary>
  );
}

export function App() {
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
