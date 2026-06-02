import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { EventType, type AuthenticationResult } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, HashRouter } from "react-router-dom";

import { msalInstance } from "@/auth/msal";
import { clearAuthSessionIssue } from "@/auth/sessionEvents";
import { installClientErrorHandlers, reportUnknownClientError } from "@/api/clientLog";
import { App } from "@/App";
import { ToastProvider } from "@/components/Toast";
import { AutoRefreshProvider } from "@/hooks/useAutoRefresh";
import { PreferencesProvider } from "@/hooks/usePreferences";
import { AppInsightsProvider } from "@/hooks/useAppInsights";
import { SettingsPanelProvider } from "@/hooks/useSettingsPanel";
import { isDevBypassEnabled } from "@/config/runtime";
import { initDocsMockPreview } from "@/mocks/docsPreview";
import "@/theme/glass.css";
import "@/theme/blast-submit-layout.css";
import "@/theme/dashboard-layout.css";
// Inter — the default UI typeface used across the SPA.
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/inter/400-italic.css";
import "@fontsource/inter/700-italic.css";
// JetBrains Mono — used only by the browser terminal (xterm.js fontFamily).
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";

const DOCS_MOCK_PREVIEW = import.meta.env.VITE_DOCS_MOCK_PREVIEW === "true";
const Router = DOCS_MOCK_PREVIEW ? HashRouter : BrowserRouter;

initDocsMockPreview();
installClientErrorHandlers();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
      refetchIntervalInBackground: false,
      retry: 1,
    },
  },
});

async function bootstrap() {
  const devBypass = isDevBypassEnabled();

  try {
    // Must be awaited before any other MSAL call (msal-browser v3 requirement).
    await msalInstance.initialize();
  } catch (err) {
    console.warn("MSAL initialize failed:", err);
    reportUnknownClientError("msal.initialize", err);
  }

  if (!devBypass) {
    // Restore an active account between page loads.
    const existingAccounts = msalInstance.getAllAccounts();
    if (existingAccounts.length > 0) {
      msalInstance.setActiveAccount(existingAccounts[0]);
    }

    // Set the active account whenever a login completes.
    msalInstance.addEventCallback((event) => {
      if (
        (event.eventType === EventType.LOGIN_SUCCESS ||
          event.eventType === EventType.ACQUIRE_TOKEN_SUCCESS) &&
        event.payload
      ) {
        const payload = event.payload as AuthenticationResult;
        if (payload.account) {
          msalInstance.setActiveAccount(payload.account);
        }
        // A fresh token means the session is healthy again — re-open the
        // dashboard if the session-expiry gate had routed the user to sign-in.
        clearAuthSessionIssue();
      }
    });

    // Finish the redirect leg of the auth flow before rendering.
    try {
      const redirectResult = await msalInstance.handleRedirectPromise();
      if (redirectResult?.account) {
        msalInstance.setActiveAccount(redirectResult.account);
      }
    } catch (err) {
      console.warn("MSAL redirect handling failed:", err);
      reportUnknownClientError("msal.redirect", err);
    }
  }

  const root = createRoot(document.getElementById("root")!);
  root.render(
    <StrictMode>
      <MsalProvider instance={msalInstance}>
        <QueryClientProvider client={queryClient}>
          <PreferencesProvider>
            <AppInsightsProvider>
              <AutoRefreshProvider>
                <Router
                  future={{
                    v7_startTransition: true,
                    v7_relativeSplatPath: true,
                  }}
                >
                  <SettingsPanelProvider>
                    <ToastProvider>
                      <App />
                    </ToastProvider>
                  </SettingsPanelProvider>
                </Router>
              </AutoRefreshProvider>
            </AppInsightsProvider>
          </PreferencesProvider>
        </QueryClientProvider>
      </MsalProvider>
    </StrictMode>,
  );
}

void bootstrap();
