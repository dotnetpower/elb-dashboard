/**
 * Pure presentational helpers for {@link TelemetrySection} — extracted from the
 * section component (issue #24).
 *
 * Owns the Application Insights connection-string shape check, the masked
 * instrumentation-key tail, the effective-source badge descriptor (label / hint
 * / tone / icon), and the Azure portal deep-link builder. No state, no effects —
 * the only JSX is the small status icon returned by `describeEffectiveSource`,
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

export function describeEffectiveSource(
  source: "user" | "deployment" | "none",
  active: boolean,
): { label: string; hint: string; tone: "success" | "muted" | "warning"; icon: ReactNode } {
  if (active && source === "user") {
    return {
      label: "Browser override · active",
      hint: "The SPA is using the connection string entered below.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  if (active && source === "deployment") {
    return {
      label: "Deployment · active",
      hint: "Using APPLICATIONINSIGHTS_CONNECTION_STRING injected by the Container App template.",
      tone: "success",
      icon: <CheckCircle2 size={11} strokeWidth={2} />,
    };
  }
  if (source === "user") {
    return {
      label: "Browser override · idle",
      hint: "A connection string is entered but telemetry is disabled.",
      tone: "warning",
      icon: <AlertCircle size={11} strokeWidth={2} />,
    };
  }
  if (source === "deployment") {
    return {
      label: "Deployment · idle",
      hint: "Connection string is available but telemetry is disabled.",
      tone: "warning",
      icon: <AlertCircle size={11} strokeWidth={2} />,
    };
  }
  return {
    label: "Not configured",
    hint: "Enter a connection string below or provision an Application Insights resource.",
    tone: "muted",
    icon: null,
  };
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
