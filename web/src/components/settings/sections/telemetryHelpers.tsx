/**
 * Pure presentational helpers for {@link TelemetrySection} — extracted from the
 * section component (issue #24).
 *
 * Owns the Application Insights connection-string shape check, the masked
 * instrumentation-key tail, browser/server status descriptors, provisioning
 * visibility, and the Azure portal deep-link builder. No state, no effects —
 * the only JSX is the small status icon returned by the descriptor helpers,
 * which is why this is a `.tsx` module.
 */

import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2 } from "lucide-react";

export function isWellFormedConnectionString(value: string): boolean {
  return value.includes("InstrumentationKey=") && value.includes("IngestionEndpoint=");
}

export function extractInstrumentationKeyTail(value: string): string {
  const match = value.match(/InstrumentationKey=([^;]+)/);
  if (!match) return "";
  const key = match[1].trim();
  return key.length > 8 ? key.slice(-8) : key;
}

export function describeBrowserConnectionSource(
  source: "user" | "deployment" | "none",
  userConnectionStringValid: boolean,
): {
  label: string;
  hint: string;
  tone: "success" | "muted" | "warning";
  icon: ReactNode;
} {
  if (source === "user") {
    if (!userConnectionStringValid) {
      return {
        label: "Invalid override",
        hint: "Complete or clear the browser connection string entered below.",
        tone: "warning",
        icon: <AlertCircle size={11} strokeWidth={2} />,
      };
    }
    return {
      label: "Browser override",
      hint: "This browser will use the connection string entered below when browser telemetry is on.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  if (source === "deployment") {
    return {
      label: "Deployment connection",
      hint: "This browser can use the connection string supplied by the deployment.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  return {
    label: "Unavailable",
    hint: "No connection string is available to this browser.",
    tone: "muted",
    icon: null,
  };
}

export function describeServerTelemetry(
  deploymentConfigured: boolean,
  deploymentStatusResolved: boolean,
): {
  label: string;
  hint: string;
  tone: "success" | "muted" | "warning";
  icon: ReactNode;
} {
  if (!deploymentStatusResolved) {
    return {
      label: "Checking",
      hint: "Checking telemetry configuration for api, worker, and beat.",
      tone: "muted",
      icon: null,
    };
  }
  if (deploymentConfigured) {
    return {
      label: "Configured",
      hint: "api, worker, and beat have an effective App Insights connection string.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  return {
    label: "Not configured",
    hint: "api, worker, and beat do not have an App Insights connection string.",
    tone: "warning",
    icon: <AlertCircle size={11} strokeWidth={2} />,
  };
}

export function shouldShowProvisionResource({
  deploymentConfigured,
  deploymentStatusResolved,
  userConnectionStringValid,
}: {
  deploymentConfigured: boolean;
  deploymentStatusResolved: boolean;
  userConnectionStringValid: boolean;
}): boolean {
  return deploymentStatusResolved && !deploymentConfigured && !userConnectionStringValid;
}

export function appInsightsPortalUrl(
  subscriptionId: string | undefined | null,
  componentName: string,
  resourceGroup: string,
): string | null {
  if (subscriptionId && resourceGroup && componentName) {
    const path =
      `/subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}` +
      `/providers/Microsoft.Insights/components/${componentName}`;
    return `https://portal.azure.com/#@/resource${path}/overview`;
  }
  return "https://portal.azure.com/#blade/HubsExtension/BrowseResource/resourceType/microsoft.insights%2Fcomponents";
}
