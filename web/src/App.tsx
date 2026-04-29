import { Route, Routes, Navigate } from "react-router-dom";
import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";

import { Layout } from "@/components/Layout";
import { SignIn } from "@/pages/SignIn";
import { Dashboard } from "@/pages/Dashboard";
import { RemoteTerminal } from "@/pages/RemoteTerminal";

export function App() {
  return (
    <>
      <UnauthenticatedTemplate>
        <SignIn />
      </UnauthenticatedTemplate>
      <AuthenticatedTemplate>
        <Layout>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/terminal" element={<RemoteTerminal />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </AuthenticatedTemplate>
    </>
  );
}
